"""Offline, read-only corpus access; report writes are confined and exclusive."""
from pathlib import Path
import hashlib
import json
import re

SOURCE_IDS = {"russian-dialogues-2", "blended-skill-talk-ru", "dialogsum-ru", "empathetic-dialogues", "daily-dialog", "cornell-dialogs"}
CANONICAL_DATA = Path("/Users/akhripuniov/Downloads/Codex/GPoint/services/agata/conversation-lab/data")


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def confined(path, parent):
    path, parent = Path(path).resolve(), Path(parent).resolve()
    if path == parent or not path.is_relative_to(parent):
        raise ValueError("path escapes allowed input/output directory")
    return path


def verify_sources(data_root):
    root = Path(data_root).resolve()
    manifest_path = confined(root / "runs/manifest.json", root / "runs")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for area in ("raw", "processed", "runs"):
        confined(root / area, root)
    results = {}
    for sid, entry in manifest["datasets"].items():
        if sid not in SOURCE_IDS:
            raise ValueError("unknown/deferred source in processed manifest")
        if entry.get("processing_status") != "PROCESSED" or not entry.get("files"):
            raise ValueError("source not fully processed")
        confined(root / "raw" / sid, root / "raw")
        confined(root / "processed" / sid, root / "processed")
        raw = []
        for item in entry["files"]:
            path = confined(Path(item["path"]), root / "raw" / sid)
            actual = digest(path)
            if actual != item["sha256"] or (item.get("source_sha256") and actual != item["source_sha256"]):
                raise ValueError("raw checksum mismatch: " + sid)
            raw.append({"path": str(path), "sha256": actual, "bytes": path.stat().st_size})
        path = confined(root / "processed" / sid / "dialogs.jsonl", root / "processed" / sid)
        actual = digest(path)
        if actual != entry.get("processed_sha256"):
            raise ValueError("processed checksum mismatch: " + sid)
        results[sid] = {"processed_path": str(path), "processed_sha256": actual, "raw": raw}
    if not results:
        raise ValueError("empty source manifest")
    return results


def read_pointer_dialogues(data_root, source_id):
    if source_id not in SOURCE_IDS:
        raise ValueError("unknown/deferred source")
    root = Path(data_root).resolve()
    verified = verify_sources(root)
    if source_id not in verified:
        raise ValueError("source absent from manifest")
    pointers_path = confined(root / "processed" / source_id / "quality-review.jsonl", root / "processed" / source_id)
    pointers = [json.loads(line) for line in pointers_path.read_text(encoding="utf-8").splitlines()]
    if len(pointers) != 30:
        raise ValueError("expected thirty review pointers")
    wanted = {}
    for p in pointers:
        line = p["processed_jsonl_line"]
        if type(line) is not int or line < 1 or line in wanted:
            raise ValueError("invalid/duplicate review pointer")
        wanted[line] = p
    if len({str(p["dialogue_id"]) for p in pointers}) != 30:
        raise ValueError("duplicate review identity")
    found = {}
    with Path(verified[source_id]["processed_path"]).open(encoding="utf-8") as f:
        for line, text in enumerate(f, 1):
            if line not in wanted:
                continue
            record = json.loads(text)
            if str(record.get("dialogue_id")) != str(wanted[line]["dialogue_id"]) or record.get("source") != source_id:
                raise ValueError("pointer record identity mismatch")
            turns = record.get("turns")
            if not isinstance(turns, list) or len(turns) < 2 or any(not isinstance(t, dict) or not t.get("speaker") or not isinstance(t.get("text"), str) or not t["text"].strip() for t in turns):
                raise ValueError("invalid full dialogue")
            found[line] = {"dialogue_id": record["dialogue_id"], "source": source_id,
                           "processed_jsonl_line": line, "turns": turns,
                           "processed_sha256": verified[source_id]["processed_sha256"]}
    if len(found) != 30:
        raise ValueError("review pointer outside corpus")
    return [found[p["processed_jsonl_line"]] for p in pointers]


def redact(text):
    text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[CONTACT REDACTED]", text)
    text = re.sub(r"https?://\S+", "[URL REDACTED]", text)
    pattern = re.compile(r"(?<!\w)\+?\d[\d ().-]{7,}\d(?!\w)")
    def phone(match):
        value = match.group(); digits = re.sub(r"\D", "", value)
        before, after = text[max(0, match.start()-40):match.start()], text[match.end():match.end()+30]
        # Explicit non-contact context applies only to unprefixed numeric tokens.
        if not value.startswith("+") and "(" not in value:
            if re.search(r"(?:заказ|номер заказа|reference|order|id)\s*[:#]?\s*$", before, re.I) or re.match(r"\s*(?:руб|доллар|usd|eur|₽|€|\$)", after, re.I):
                return value
            if re.fullmatch(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}", value):
                return value
        return "[NUMBER REDACTED]" if 10 <= len(digits) <= 15 else value
    return pattern.sub(phone, text)


def write_report(data_root, run_id, filename, payload):
    if not re.fullmatch(r"phase2-[a-zA-Z0-9_-]+", run_id) or not re.fullmatch(r"[a-zA-Z0-9_-]+\.json", filename):
        raise ValueError("invalid exclusive report destination")
    root = Path(data_root).resolve(); runs = root / "runs"
    if runs.is_symlink():
        raise ValueError("symlink output root")
    folder = confined(runs / run_id, runs)
    if (runs / run_id).is_symlink():
        raise ValueError("symlink report directory")
    folder.mkdir(parents=True, exist_ok=True)
    path = confined(folder / filename, folder)
    with path.open("x", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2); f.write("\n")
    return path
