#!/usr/bin/env python3
"""Local-only, resumable dataset collection and normalization for AGATA."""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import html
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.error
import zipfile

HERE = Path(__file__).resolve().parents[1]
DATA = HERE / "data"
CONFIG = HERE / "configs" / "datasets.json"
USER_AGENT = "AGATA-Conversation-Lab/1.0 (dataset provenance and local normalization)"
MIN_FREE = 15 * 1024**3
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
PHONE_CANDIDATE_RE = re.compile(r"(?<!\w)\+?\d[\d ().-]{7,}\d(?!\w)")
TAG_RE = re.compile(r"<[^>]{1,300}>")


def sources():
    return json.loads(CONFIG.read_text("utf-8"))["datasets"]


def get_source(name):
    for s in sources():
        if s["id"] == name:
            return s
    raise ValueError(f"Unknown dataset: {name}")


def root_dir():
    return HERE


def space(path=DATA):
    return shutil.disk_usage(path)


def request(url, *, headers=None, timeout=60):
    h = {"User-Agent": USER_AGENT, **(headers or {})}
    req = urllib.request.Request(url, headers=h)
    return urllib.request.urlopen(req, timeout=timeout)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_rel(name):
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or "\\" in name or not p.parts:
        raise ValueError(f"Unsafe archive path: {name}")
    return p


def unpack_zip(archive, dest):
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            rel = safe_rel(info.filename)
            target = dest.joinpath(*rel.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)


def unpack_tar(archive, dest):
    with tarfile.open(archive, "r:gz") as t:
        for member in t.getmembers():
            rel = safe_rel(member.name)
            if not (member.isfile() or member.isdir()):
                raise ValueError(f"Refusing non-file archive entry: {member.name}")
            target = dest.joinpath(*rel.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                src = t.extractfile(member)
                if src is None:
                    raise ValueError(f"Cannot read archive entry: {member.name}")
                with src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)


def download_file(url, target, expected_size=None, expected_sha=None):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and (expected_size is None or target.stat().st_size == expected_size):
        digest = sha256_file(target)
        if not expected_sha or digest.lower() == expected_sha.lower():
            return {"path": str(target), "size_bytes": target.stat().st_size, "sha256": digest, "resumed": False, "cached": True}
    part = target.with_suffix(target.suffix + ".part")
    for attempt in range(4):
        offset = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        try:
            with request(url, headers=headers, timeout=120) as resp:
                if offset and getattr(resp, "status", 200) != 206:
                    offset = 0
                    part.unlink(missing_ok=True)
                mode = "ab" if offset and getattr(resp, "status", 200) == 206 else "wb"
                with open(part, mode) as out:
                    shutil.copyfileobj(resp, out, 1024 * 1024)
            actual = part.stat().st_size
            if expected_size is not None and actual != expected_size:
                raise IOError(f"size mismatch: expected {expected_size}, got {actual}")
            digest = sha256_file(part)
            if expected_sha and digest.lower() != expected_sha.lower():
                raise IOError(f"SHA256 mismatch: expected {expected_sha}, got {digest}")
            os.replace(part, target)
            return {"path": str(target), "size_bytes": actual, "sha256": digest, "resumed": bool(offset), "cached": False}
        except Exception:
            if attempt == 3:
                raise
            time.sleep(1.5 * (attempt + 1))


def hf_tree(repo, revision="main"):
    url = f"https://huggingface.co/api/datasets/{repo}/tree/{revision}?recursive=true&expand=false"
    with request(url) as r:
        rows = json.load(r)
    with request(f"https://huggingface.co/api/datasets/{repo}/revision/{revision}") as r:
        rev = json.load(r).get("sha", revision)
    items = []
    for item in rows:
        name = item.get("path", "")
        if item.get("type") != "file" or not name.lower().endswith((".parquet", ".csv", ".json", ".jsonl", ".jsonl.gz", ".tsv")):
            continue
        lfs = item.get("lfs") or {}
        items.append({"path": name, "size": lfs.get("size", item.get("size")), "sha256": lfs.get("oid"), "revision": rev})
    if not items:
        raise RuntimeError(f"No supported data files discovered for {repo} at {rev}")
    return rev, items


def manifest_file():
    return DATA / "runs" / "manifest.json"


def load_manifest():
    p = manifest_file()
    return json.loads(p.read_text("utf-8")) if p.exists() else {"schema_version": 1, "datasets": {}}


def save_manifest(m):
    p = manifest_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n", "utf-8")
    os.replace(tmp, p)


def download_one(s):
    if s["group"] != "starter":
        raise PermissionError("Extended datasets require a separate OWNER approval and are disabled in this run")
    dest = DATA / "raw" / s["raw_dir"]
    dest.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(DATA).free
    if free < MIN_FREE:
        raise OSError(f"Disk guard: {free} bytes free; require at least {MIN_FREE}")
    entry = {"id":s["id"],"name":s["name"],"source":s["source"],"download_url":s.get("download_url",s["source"]),"language":s["language"],"format":s["format"],"description":s["description"],"license":s["license"],"expected_records":s.get("expected_records"),"downloaded_at_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"status":"DOWNLOADING","files":[]}
    if s["source_kind"] == "huggingface":
        repo = s["source"].split("/datasets/", 1)[1]
        revision, files = hf_tree(repo)
        expected = sum(x["size"] or 0 for x in files)
        expansion = sum((x["size"] or 0) * (12 if x["path"].lower().endswith((".zip", ".tar.gz", ".gz")) else 1) for x in files)
        if shutil.disk_usage(DATA).free - expansion < MIN_FREE:
            raise OSError(f"Disk guard: source plus conservative extraction/processing allowance needs {expansion} bytes, free-space reserve is {MIN_FREE}")
        entry["revision"] = revision
        entry["files"] = [download_file(f"https://huggingface.co/datasets/{repo}/resolve/{revision}/{urllib.parse.quote(x['path'], safe='/')}?download=true", dest / x["path"], x["size"], x["sha256"]) | {"source_path": x["path"], "source_sha256": x["sha256"]} for x in files]
    else:
        url = s["download_url"]
        name = Path(urllib.parse.urlparse(url).path).name or "source.archive"
        entry["files"] = [download_file(url, dest / name)]
        entry["revision"] = None
        if name.endswith(".zip"):
            unpack_zip(dest / name, dest / "extracted")
        elif name.endswith(".tar.gz"):
            unpack_tar(dest / name, dest / "extracted")
    entry["raw_size_bytes"] = sum(x["size_bytes"] for x in entry["files"])
    entry["status"] = "DOWNLOADED_VERIFIED"
    m = load_manifest(); m["datasets"][s["id"]] = entry; save_manifest(m)
    return entry


def verify_one(s, entry=None):
    entry = entry or load_manifest()["datasets"].get(s["id"], {})
    if entry.get("status") != "DOWNLOADED_VERIFIED" or not entry.get("files"):
        return False
    for f in entry["files"]:
        path = Path(f["path"])
        if not path.exists() or path.stat().st_size != f["size_bytes"]:
            return False
        actual = sha256_file(path)
        if actual != f["sha256"] or (f.get("source_sha256") and actual != f["source_sha256"]):
            return False
    return True


def clean_text(v):
    if v is None:
        return ""
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    v = html.unescape(str(v)).replace("\x00", " ").replace("\ufeff", " ")
    for marker, punct in (("_comma_", ","), ("_period_", "."), ("_question_mark_", "?"), ("_exclamation_point_", "!"), ("_apostrophe_", "'")):
        v = v.replace(marker, punct)
    v = TAG_RE.sub(" ", v)
    v = re.sub(r"\s+", " ", v).strip()
    return re.sub(r"\s+([,.;!?])", r"\1", v)


def dialogue_from_row(row, s, split):
    if not isinstance(row, dict):
        row = {"value": row}
    low = {str(k).lower(): k for k in row}
    def pick(*names):
        for n in names:
            if n in low:
                return row[low[n]]
        return None
    raw = pick("dialog", "dialogue", "utterances", "messages", "conversation", "free_messages", "sample", "text")
    if isinstance(raw, dict):
        raw = raw.get("messages") or raw.get("utterances") or list(raw.values())
    turns = []
    if isinstance(raw, list):
        for i, item in enumerate(raw):
            speaker = f"speaker_{i % 2 + 1}"
            text = item
            if isinstance(item, dict):
                text = item.get("text", item.get("utterance", item.get("content", "")))
                speaker = str(item.get("speaker", item.get("role", speaker)))
            text = clean_text(text)
            marker = re.match(r"#Person(\d+)#\s*:?[ ]*(.*)", text, re.S)
            if marker:
                speaker, text = f"speaker_{marker.group(1)}", clean_text(marker.group(2))
            if text:
                turns.append({"speaker": speaker, "text": text})
    elif isinstance(raw, str):
        for i, line in enumerate(raw.splitlines()):
            text = clean_text(line)
            if text:
                turns.append({"speaker": f"speaker_{i % 2 + 1}", "text": text})
    # HF DialogSum data commonly exposes dialogue as a string with Person markers.
    if len(turns) < 2 and isinstance(raw, str):
        parts = re.split(r"(?=#Person\d+#\s*:?)", raw)
        found = []
        for part in parts:
            m = re.match(r"#Person(\d+)#\s*:?[ ]*(.*)", part.strip(), re.S)
            if m and clean_text(m.group(2)):
                found.append({"speaker": f"speaker_{m.group(1)}", "text": clean_text(m.group(2))})
        if len(found) >= 2:
            turns = found
    metadata = {k: v for k, v in row.items() if k not in (low.get("dialog"), low.get("dialogue"), low.get("utterances"), low.get("messages"), low.get("conversation"), low.get("free_messages"), low.get("sample"), low.get("text"))}
    metadata = {str(k): v for k, v in metadata.items() if isinstance(v, (str, int, float, bool, type(None), list, dict))}
    # Blended Skill Talk's previous_utterance is the context immediately before
    # free_messages. Include both in natural conversational order; never treat
    # guided candidate responses or suggestions as spoken dialogue.
    previous = pick("previous_utterance")
    free = pick("free_messages")
    if isinstance(previous, list) and isinstance(free, list):
        combined = []
        for item in previous + free:
            text = clean_text(item.get("text") if isinstance(item, dict) else item)
            if text: combined.append(text)
        turns = [{"speaker": f"speaker_{i%2+1}", "text": text} for i, text in enumerate(combined)]
    ident = pick("id", "dialogue_id", "conv_id", "conversation_id", "dialog_id")
    return {"dialogue_id": str(ident if ident is not None else ""), "source": s["id"], "language": s["language"], "split": split, "turns": turns, "metadata": metadata}


def iter_hf_rows(repo, config, split, offset=0, page=100, expected_revision=None):
    while True:
        query = urllib.parse.urlencode({"dataset": repo, "config": config, "split": split, "offset": offset, "length": page})
        for attempt in range(7):
            try:
                with request("https://datasets-server.huggingface.co/rows?" + query, timeout=90) as r:
                    served_revision = r.headers.get("x-revision")
                    payload = json.load(r)
                if expected_revision and served_revision != expected_revision:
                    raise RuntimeError(f"HF viewer revision mismatch: expected {expected_revision}, received {served_revision}")
                break
            except urllib.error.HTTPError as e:
                if e.code != 429 and e.code < 500 or attempt == 6: raise
                retry = e.headers.get("Retry-After")
                try: delay = min(120, max(2, float(retry))) if retry else min(60, 2 ** (attempt + 1))
                except ValueError: delay = min(60, 2 ** (attempt + 1))
                time.sleep(delay)
        rows = payload.get("rows", [])
        if not rows:
            break
        for j, row in enumerate(rows):
            yield offset + j, row.get("row", row)
        offset += len(rows)
        if offset >= payload.get("num_rows_total", 0):
            break
        # Stay below public viewer request-rate limits; retry-after remains authoritative.
        time.sleep(2.0)


def hf_splits(repo):
    query = urllib.parse.urlencode({"dataset": repo})
    with request("https://datasets-server.huggingface.co/splits?" + query) as r:
        payload = json.load(r)
    return [(x["config"], x["split"]) for x in payload.get("splits", [])]


def split_resume_plan(splits, checkpoint):
    if not checkpoint:
        return [(config, split, 0) for config, split in splits]
    marker = (checkpoint.get("config"), checkpoint.get("split"))
    indexes = [(c, s) for c, s in splits]
    if marker not in indexes:
        raise RuntimeError(f"Saved processing split {marker} no longer exists in the pinned dataset")
    resume_at = indexes.index(marker)
    return [(config, split, checkpoint.get("offset", 0) if i == resume_at else 0)
            for i, (config, split) in enumerate(splits) if i >= resume_at]


def iter_cornell(path):
    base = path / "extracted"
    index = {}
    lines_files = list(base.rglob("movie_lines.txt"))
    conv_files = list(base.rglob("movie_conversations.txt"))
    if not lines_files or not conv_files:
        raise RuntimeError("ConvoKit ZIP lacks expected movie_lines.txt or movie_conversations.txt files")
    lines_path, conv_path = lines_files[0], conv_files[0]
    for raw in lines_path.read_text("utf-8", errors="replace").splitlines():
        cols = raw.split(" +++$+++ ", 4)
        if len(cols) == 5:
            index[cols[0]] = (cols[1], cols[2], cols[3], clean_text(cols[4]))
    for n, raw in enumerate(conv_path.read_text("utf-8", errors="replace").splitlines()):
        cols = raw.split(" +++$+++ ", 3)
        if len(cols) < 4: continue
        ids = re.findall(r"L\d+", cols[3])
        turns = []
        for lid in ids:
            if lid in index:
                char_id, movie_id, char_name, text = index[lid]
                if text: turns.append({"speaker": char_id or char_name, "text": text})
        if turns:
            yield {"dialogue_id": ids[0] if ids else str(n), "source": "cornell-dialogs", "language": "en", "split": "unspecified", "turns": turns, "metadata": {"movie_id": cols[2], "character_ids": [cols[0], cols[1]]}}


def iter_convokit(path):
    base = path / "extracted" / "movie-corpus"
    utter_file = base / "utterances.jsonl"
    convo_file = base / "conversations.json"
    if not utter_file.exists() or not convo_file.exists():
        raise RuntimeError("ConvoKit ZIP lacks utterances.jsonl or conversations.json")
    groups = {}
    with open(utter_file, encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            cid = str(row.get("conversation_id") or row.get("id"))
            groups.setdefault(cid, []).append(row)
    conv_meta = json.loads(convo_file.read_text("utf-8"))
    for cid, rows in groups.items():
        by_id = {str(r.get("id")): r for r in rows}
        children = {}
        for r in rows:
            parent = r.get("reply-to")
            if parent and str(parent) in by_id:
                children.setdefault(str(parent), []).append(r)
        ordered, visited = [], set()
        def walk(uid):
            if uid in visited: return
            visited.add(uid); row = by_id.get(uid)
            if row: ordered.append(row)
            for child in children.get(uid, []): walk(str(child.get("id")))
        walk(cid)
        for row in rows:
            walk(str(row.get("id")))
        turns = [{"speaker": str(r.get("speaker") or "speaker_1"), "text": clean_text(r.get("text"))} for r in ordered if clean_text(r.get("text"))]
        meta = (conv_meta.get(cid) or {}).get("meta", {})
        yield {"dialogue_id": cid, "source": "cornell-dialogs", "language": "en", "split": "unspecified", "turns": turns, "metadata": meta}


def iter_empathetic(path):
    found = list((path / "extracted").rglob("*.csv"))
    if not found: raise RuntimeError("EmpatheticDialogues archive contains no CSV splits")
    by_conv = {}
    for file in found:
        split = file.stem.lower()
        with open(file, encoding="utf-8-sig", newline="") as f:
            with open(file, encoding="utf-8-sig", newline="") as probe:
                sample = probe.read(4096)
            delimiter = "\t" if sample.count("\t") > sample.count(",") else ","
            for row in csv.DictReader(f, delimiter=delimiter):
                key = row.get("conv_id") or row.get("conversation_id")
                text = clean_text(row.get("utterance"))
                if not key or not text: continue
                by_conv.setdefault((split, key), []).append(row)
    for (split, key), rows in by_conv.items():
        turns = [{"speaker": f"speaker_{r.get('speaker_idx','1')}", "text": clean_text(r.get("utterance"))} for r in rows if clean_text(r.get("utterance"))]
        if len(turns) >= 2:
            yield {"dialogue_id": key, "source": "empathetic-dialogues", "language": "en", "split": split, "turns": turns, "metadata": {k: rows[0].get(k) for k in ("context", "prompt", "selfeval", "react") if rows[0].get(k)}}


def iter_blended(path, s):
    for file in sorted(path.glob("*.json")):
        split = file.stem.lower()
        with open(file, encoding="utf-8") as f:
            for index, line in enumerate(f):
                line = line.strip()
                if not line: continue
                row = json.loads(line)
                yield dialogue_from_row(row, s, split)


def iter_jsonl_gz(path, s):
    files = sorted(path.rglob("*.jsonl.gz"))
    if not files: raise RuntimeError("Pinned source contains no JSONL.GZ data files")
    for file in files:
        with gzip.open(file, "rt", encoding="utf-8", errors="replace") as f:
            for index, line in enumerate(f):
                if line.strip():
                    yield dialogue_from_row(json.loads(line), s, "unspecified")


def skip_records(iterator, n):
    for index, row in enumerate(iterator):
        if index >= n: yield row


def is_pii(text):
    if EMAIL_RE.search(text): return True
    for match in PHONE_CANDIDATE_RE.finditer(text):
        value=match.group(); digits=re.sub(r"\D","",value)
        if not 10 <= len(digits) <= 15: continue
        groups=re.findall(r"\d+",value)
        if value.lstrip().startswith("+") or "(" in value or ")" in value: return True
        if len(groups)==1 and len(digits)==11 and digits[0] in "78": return True
        if "-" in value or "." in value:
            if len(groups)>=3 and ((len(groups[-1])==2 and len(groups[-2])==2) or (len(groups[-1])==4 and len(groups[-2])==3)):
                return True
        if len(groups)>=4 and len(groups[-1])==2 and len(groups[-2])==2:
            return True
    return False


def normalize_record(record):
    cleaned = []
    for i, turn in enumerate(record["turns"]):
        text = clean_text(turn.get("text"))
        speaker = clean_text(turn.get("speaker")) or f"speaker_{i%2+1}"
        marker = re.match(r"#Person(\d+)#\s*:?[ ]*(.*)", text, re.S)
        if marker:
            speaker, text = f"speaker_{marker.group(1)}", clean_text(marker.group(2))
        if text: cleaned.append({"speaker":speaker,"text":text})
    record["turns"] = cleaned
    record["turns"] = record["turns"][:]
    record["quality_flags"] = []
    if len(record["turns"]) < 2: record["quality_flags"].append("too_few_turns")
    if len(record["turns"]) > 80: record["quality_flags"].append("very_long_dialogue")
    if any(is_pii(t["text"]) for t in record["turns"]): record["quality_flags"].append("pii_suspected")
    joined = " ".join(t["text"] for t in record["turns"])
    if len(joined) < 12: record["quality_flags"].append("very_short")
    if len(joined) > 20000: record["quality_flags"].append("very_long_text")
    if not record["dialogue_id"]:
        record["dialogue_id"] = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:20]
    return record


def quality_categories(record):
    text = " ".join(t["text"].lower() for t in record["turns"])
    tags = []
    if len(record["turns"]) >= 6: tags.extend(["topic_maintenance", "long_dialogue"])
    if re.search(r"\b(laugh|funny|joke|haha|lol|хаха|смешн|шутк)\w*", text): tags.append("humor_candidate")
    if re.search(r"\b(feel|sorry|understand|glad|worry|sad|эмоци|чувств|жаль|понимаю|рад)\w*", text): tags.append("emotional_candidate")
    if len(record["turns"]) > 1 and record["turns"][0]["speaker"] != record["turns"][-1]["speaker"]: tags.append("multi_speaker")
    if not tags: tags.append("everyday_candidate")
    if record["quality_flags"]: tags.append("low_quality_flagged")
    return tags


def summarize_example(rec):
    turns = rec["turns"]
    joined = " ".join(t["text"] for t in turns)
    words = re.findall(r"[^\W_]+", joined, flags=re.UNICODE)
    return {"dialogue_id": rec["dialogue_id"], "split": rec.get("split"), "turns": len(turns), "approx_words": len(words), "speakers": len({x["speaker"] for x in turns}), "categories": quality_categories(rec), "quality_flags": rec["quality_flags"], "review_prompt": "Open this record by processed_jsonl_line in dialogs.jsonl to assess coherence, naturalness, role attribution and text damage."}


def quality_review_one(s):
    manifest=load_manifest(); d=manifest["datasets"].get(s["id"],{})
    if d.get("processing_status") != "PROCESSED": raise RuntimeError(f"{s['id']} is not fully processed")
    outdir=DATA/"processed"/s["id"]; output=outdir/"dialogs.jsonl"; sample_path=outdir/"quality-review.jsonl"
    if not output.exists(): raise FileNotFoundError(output)
    review_reserve=output.stat().st_size+MIN_FREE
    if shutil.disk_usage(DATA).free < review_reserve:
        raise OSError(f"Quality-review disk guard: need {review_reserve} bytes free; found {shutil.disk_usage(DATA).free}")
    original_rows=sum(1 for _ in open(output,encoding="utf-8"))
    temp=output.with_suffix(".jsonl.tmp"); seen_db=sqlite3.connect(outdir/".seen.sqlite")
    seen_db.execute("CREATE TABLE IF NOT EXISTS audit_seen (digest TEXT PRIMARY KEY)"); seen_db.execute("DELETE FROM audit_seen")
    flag_counts={}; category_counts={}; count=0; removed=0; flagged_records=0
    with open(output,encoding="utf-8") as src, open(temp,"w",encoding="utf-8") as dst:
        for index,line in enumerate(src):
            rec=normalize_record(json.loads(line)); rec["categories"]=quality_categories(rec)
            if rec["quality_flags"]: flagged_records+=1
            for flag in rec["quality_flags"]: flag_counts[flag]=flag_counts.get(flag,0)+1
            for cat in rec["categories"]: category_counts[cat]=category_counts.get(cat,0)+1
            if len(rec["turns"]) < 2:
                removed+=1; continue
            sig=hashlib.sha256((str(rec.get("split"))+"\0"+json.dumps(rec["turns"],ensure_ascii=False,sort_keys=True)).encode()).hexdigest()
            try: seen_db.execute("INSERT INTO audit_seen VALUES (?)",(sig,))
            except sqlite3.IntegrityError:
                removed+=1; continue
            dst.write(json.dumps(rec,ensure_ascii=False)+"\n"); count+=1
        dst.flush(); os.fsync(dst.fileno())
    seen_db.commit()
    raw_unique=seen_db.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
    seen_db.close()
    os.replace(temp,output)
    total=count; targets=set(round(i*(total-1)/29) for i in range(30)) if total>=30 else set(range(total))
    sample_temp=sample_path.with_suffix(".jsonl.tmp"); examples=0
    with open(output,encoding="utf-8") as src, open(sample_temp,"w",encoding="utf-8") as samples:
        for index,line in enumerate(src):
            if index in targets:
                rec=json.loads(line); summary=summarize_example(rec); summary["processed_jsonl_line"]=index+1
                samples.write(json.dumps(summary,ensure_ascii=False)+"\n"); examples+=1
        samples.flush(); os.fsync(samples.fileno())
    os.replace(sample_temp,sample_path)
    input_count=int(d.get("processed_records_seen",original_rows))
    source_duplicates=max(0,input_count-raw_unique)
    filtered=max(0,raw_unique-count)
    d.update({"processed_accepted":count,"process_checkpoint":input_count,"duplicates":source_duplicates,"filtered":filtered,"quality_review_removed_rows":max(int(d.get("quality_review_removed_rows",0)),original_rows-count),"processed_bytes":output.stat().st_size,"processed_sha256":sha256_file(output),"quality_flagged":flagged_records,"quality_flag_counts":flag_counts,"quality_category_counts":category_counts,"quality_examples":examples})
    d.update({"name":s["name"],"source":s["source"],"download_url":s.get("download_url",s["source"]),"language":s["language"],"format":s["format"],"description":s["description"],"license":s["license"],"expected_records":s.get("expected_records")})
    d.pop("processing_error",None); save_manifest(manifest)
    return d


def quality_review(chosen):
    rows=[]; failed=[]
    for s in chosen:
        try:
            d=quality_review_one(s)
            rows.append({"id":s["id"],"language":s["language"],"accepted":d["processed_accepted"],"duplicate_records":d["duplicates"],"filtered_records":d["filtered"],"flagged_records":d["quality_flagged"],"example_count":d["quality_examples"],"flag_counts":d["quality_flag_counts"],"category_counts":d["quality_category_counts"],"examples_file":str(DATA/"processed"/s["id"]/"quality-review.jsonl"),"dialogues_file":str(DATA/"processed"/s["id"]/"dialogs.jsonl")})
            print(f"REVIEW_READY {s['id']} examples={d['quality_examples']} records={d['processed_accepted']} flags={d['quality_flag_counts']}")
        except Exception as e:
            failed.append({"id":s["id"],"error":str(e)})
            print(f"REVIEW_BLOCKED {s['id']}: {e}",file=sys.stderr)
    p=DATA/"runs"/"quality-findings.json"; p.write_text(json.dumps({"generated_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"automated_assessment":"structural heuristics only; manually inspect each linked example; no suitability-for-training verdict","failed":failed,"datasets":rows},ensure_ascii=False,indent=2)+"\n","utf-8")
    return 1 if failed else 0


def records(s):
    raw = DATA / "raw" / s["raw_dir"]
    checkpoint = int(load_manifest()["datasets"].get(s["id"], {}).get("process_checkpoint", 0))
    if s["processor"] == "cornell_text": return skip_records(iter_cornell(raw), checkpoint)
    if s["processor"] == "convokit_zip":
        return skip_records(iter_convokit(raw), checkpoint)
    if s["processor"] == "empathetic_csv": return skip_records(iter_empathetic(raw), checkpoint)
    if s["processor"] == "blended_jsonl": return skip_records(iter_blended(raw, s), checkpoint)
    if s["processor"] == "jsonl_gz": return skip_records(iter_jsonl_gz(raw, s), checkpoint)
    repo = s["source"].split("/datasets/",1)[1]
    def gen():
        pinned = load_manifest()["datasets"].get(s["id"], {}).get("revision")
        current, _ = hf_tree(repo)
        if pinned and current != pinned:
            raise RuntimeError(f"Dataset main advanced since local raw download: raw revision={pinned}, current main={current}; refusing rows API that cannot pin revision")
        checkpoint = load_manifest()["datasets"].get(s["id"], {}).get("process_cursor") or {}
        available_splits = hf_splits(repo)
        if not checkpoint and load_manifest()["datasets"].get(s["id"], {}).get("process_checkpoint", 0):
            # Legacy checkpoint before per-split cursors were added.
            checkpoint = {"config":available_splits[0][0],"split":available_splits[0][1],"offset":int(load_manifest()["datasets"][s["id"]]["process_checkpoint"])}
        for config, split, start in split_resume_plan(available_splits, checkpoint):
            for source_offset, row in iter_hf_rows(repo, config, split, offset=start, expected_revision=pinned):
                rec = dialogue_from_row(row, s, split)
                rec["_lab_cursor"] = {"config":config,"split":split,"offset":source_offset+1}
                yield rec
    return gen()


def process_one(s, max_records=None):
    m = load_manifest(); prior = m["datasets"].get(s["id"], {})
    if not verify_one(s, prior): raise RuntimeError(f"{s['id']} source file size/SHA256 verification failed; refusing processing")
    if prior.get("processing_status") == "PROCESSED" and max_records is None:
        if prior.get("processing_error"):
            prior.pop("processing_error", None); m["datasets"][s["id"]]=prior; save_manifest(m)
        return prior
    needed = max(MIN_FREE, int(prior.get("raw_size_bytes", 0) * 20))
    if shutil.disk_usage(DATA).free < needed:
        raise OSError(f"Processing disk guard: require {needed} bytes free before processing; found {shutil.disk_usage(DATA).free}")
    outdir = DATA / "processed" / s["id"]; outdir.mkdir(parents=True, exist_ok=True)
    output = outdir / "dialogs.jsonl"
    examples = outdir / "quality-review.jsonl"
    temp = output.with_suffix(".jsonl.part")
    seen_path = outdir / ".seen.sqlite"
    db = sqlite3.connect(seen_path)
    db.execute("CREATE TABLE IF NOT EXISTS seen (digest TEXT PRIMARY KEY)")
    count = int(prior.get("process_checkpoint", 0)); accepted = int(prior.get("processed_accepted", 0))
    duplicates = int(prior.get("duplicates", 0)); flagged = int(prior.get("filtered", 0)); quality_flagged = int(prior.get("quality_flagged", 0)); examples_count = sum(1 for _ in open(examples, encoding="utf-8")) if examples.exists() else 0
    mode = "a" if temp.exists() else "w"
    # A partial output is checkpointed record-by-record; SQLite prevents duplicate appends on resume.
    sample_targets = set(round(i * max(s.get("expected_records") or 30, 1) / 30) for i in range(30))
    with open(temp, mode, encoding="utf-8") as out, open(examples, "a", encoding="utf-8") as ex:
        for raw in records(s):
            count += 1
            cursor = raw.pop("_lab_cursor", None)
            rec = normalize_record(raw)
            if rec["quality_flags"]: quality_flagged += 1
            sig = hashlib.sha256((str(rec.get("split")) + "\0" + json.dumps(rec["turns"], ensure_ascii=False, sort_keys=True)).encode()).hexdigest()
            try: db.execute("INSERT INTO seen VALUES (?)", (sig,))
            except sqlite3.IntegrityError:
                duplicates += 1; continue
            if len(rec["turns"]) < 2: flagged += 1; continue
            accepted += 1
            rec["categories"] = quality_categories(rec)
            out.write(json.dumps(rec, ensure_ascii=False) + "\n"); out.flush()
            if count - 1 in sample_targets and examples_count < 50:
                ex.write(json.dumps(summarize_example(rec), ensure_ascii=False) + "\n"); ex.flush(); examples_count += 1
            if max_records is not None and count >= max_records: break
            if count % 100 == 0:
                db.commit()
                m = load_manifest(); d = m["datasets"].setdefault(s["id"], prior)
                d["process_checkpoint"] = count; d["processed_accepted"] = accepted
                d["duplicates"] = duplicates; d["filtered"] = flagged; d["quality_flagged"] = quality_flagged
                if cursor: d["process_cursor"] = cursor
                save_manifest(m)
    db.commit(); db.close()
    complete = max_records is None
    if complete: os.replace(temp, output)
    m = load_manifest(); d = m["datasets"][s["id"]]
    if complete:
        # The JSONL itself is the authoritative accepted-record count after a resume.
        accepted = sum(1 for _ in open(output, encoding="utf-8"))
        d["process_checkpoint"] = count
        d.pop("processing_error", None)
    d.update({"processing_status": "PROCESSED" if complete else "PARTIAL", "processed_records_seen": count, "processed_accepted": accepted, "duplicates": duplicates, "filtered": flagged, "quality_flagged": quality_flagged, "processed_bytes": temp.stat().st_size if temp.exists() else output.stat().st_size, "quality_examples": examples_count})
    save_manifest(m)
    return d


def report():
    m = load_manifest(); rows = []
    for s in sources():
        d = m["datasets"].get(s["id"], {})
        raw_path=DATA / "raw" / s["raw_dir"]
        processed_path=DATA / "processed" / s["id"]
        raw_disk=sum(p.stat().st_size for p in raw_path.rglob("*") if p.is_file()) if raw_path.exists() else 0
        processed_disk=sum(p.stat().st_size for p in processed_path.rglob("*") if p.is_file()) if processed_path.exists() else 0
        rows.append({"id":s["id"],"group":s["group"],"language":s["language"],"source":s["source"],"status":d.get("status","NOT_DOWNLOADED"),"download_bytes":d.get("raw_size_bytes",0),"raw_disk_bytes":raw_disk,"processed_bytes":processed_disk,"records":d.get("processed_records_seen",0),"accepted":d.get("processed_accepted",0),"duplicates":d.get("duplicates",0),"filtered":d.get("filtered",0),"quality_flagged":d.get("quality_flagged",0),"sha256":[f.get("sha256") for f in d.get("files",[])],"license":s["license"],"processing_status":d.get("processing_status","NOT_STARTED")})
    report_path = DATA / "runs" / "report.json"; report_path.parent.mkdir(parents=True,exist_ok=True)
    totals={"download_bytes":sum(x["download_bytes"] for x in rows),"raw_disk_bytes":sum(x["raw_disk_bytes"] for x in rows),"processed_records":sum(x["records"] for x in rows),"accepted":sum(x["accepted"] for x in rows),"russian_records":sum(x["records"] for x in rows if x["language"]=="ru"),"english_records":sum(x["records"] for x in rows if x["language"]=="en")}
    report_path.write_text(json.dumps({"generated_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"datasets":rows,"totals":totals,"disk_free_bytes":space().free,"rldd_downloaded":bool(m["datasets"].get("russian-conversations-rldd",{}).get("files")),"toloka_downloaded":bool(m["datasets"].get("toloka-persona-chat-ru",{}).get("files"))},ensure_ascii=False,indent=2)+"\n","utf-8")
    for x in rows: print(f"{x['id']:28} {x['status']:20} {x['download_bytes']:>12} B  disk={x['raw_disk_bytes']}  records={x['records']} accepted={x['accepted']} processing={x['processing_status']}")
    print(f"total_download_bytes={totals['download_bytes']} raw_disk_bytes={totals['raw_disk_bytes']} records={totals['processed_records']} accepted={totals['accepted']}")
    print(f"disk_free_bytes={space().free} report={report_path}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["list-sources","download","verify","process","quality-review","status","report"])
    p.add_argument("dataset", nargs="?", help="source id, or 'starter'/'all' for starter datasets")
    p.add_argument("--extended", action="store_true", help="requires a separate explicit owner decision; always rejected in this version")
    p.add_argument("--confirm-extended", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--max-records", type=int)
    args = p.parse_args(argv)
    ss = sources()
    if args.command == "list-sources":
        for s in ss: print(f"{s['group']:8} {s['id']:28} {s['language']}  {s['license']}")
        return 0
    if args.command in ("status","report"):
        report(); return 0
    if args.dataset in (None,"starter","all"):
        chosen = [s for s in ss if s["group"] == "starter"]
    else:
        chosen = [get_source(args.dataset)]
    if any(s["group"] != "starter" for s in chosen) or args.extended:
        raise PermissionError("DEFERRED: RLDD and Toloka are not enabled. A separate OWNER approval must explicitly authorize extended download.")
    if args.command == "download":
        print(f"available_bytes={space().free} required_reserve_bytes={MIN_FREE}")
        failed = False
        for s in chosen:
            try:
                d = download_one(s); print(f"DOWNLOADED_VERIFIED {s['id']} bytes={d['raw_size_bytes']}")
            except Exception as e:
                m=load_manifest(); m["datasets"].setdefault(s["id"],{}).update({"status":"DOWNLOAD_BLOCKED","error":str(e)}); save_manifest(m)
                print(f"DOWNLOAD_BLOCKED {s['id']}: {e}",file=sys.stderr)
                failed = True
        return 1 if failed else 0
    if args.command == "verify":
        m=load_manifest()
        for s in chosen:
            d=m["datasets"].get(s["id"],{}); ok=verify_one(s,d)
            print(("PASS" if ok else "FAIL"),s["id"])
            if not ok: return 2
        return 0
    if args.command == "process":
        failed = False
        for s in chosen:
            try: d=process_one(s,args.max_records); print(f"{d['processing_status']} {s['id']} count={d['processed_records_seen']} accepted={d['processed_accepted']}")
            except Exception as e:
                m=load_manifest(); m["datasets"].setdefault(s["id"],{}).update({"processing_status":"PROCESS_BLOCKED","processing_error":str(e)}); save_manifest(m)
                print(f"PROCESS_BLOCKED {s['id']}: {e}",file=sys.stderr)
                failed = True
        return 1 if failed else 0
    if args.command == "quality-review":
        return quality_review(chosen)
    return 2


if __name__ == "__main__":
    try: raise SystemExit(main())
    except (PermissionError, ValueError, OSError, RuntimeError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr); raise SystemExit(2)
