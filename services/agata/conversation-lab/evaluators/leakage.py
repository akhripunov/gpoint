"""Streaming local overlap checks. No source passages appear in results."""
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import unicodedata


def tokens(text):
    return re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold())


def key(text):
    return " ".join(tokens(text))


def text_fields(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from text_fields(item)
    elif isinstance(value, list):
        for item in value:
            yield from text_fields(item)


def check_leakage(scenarios, corpus_paths, training_paths=None):
    exact = defaultdict(set); spans = defaultdict(set); gram_index = defaultdict(set); queries = {}
    for s in scenarios:
        sid = s["scenario_id"]
        user_texts = [t["text"] for t in s["dialogue_turns"] if t.get("role") == "user"]
        texts = user_texts + list(text_fields({k: v for k, v in s.items() if k != "dialogue_turns"}))
        for text in texts:
            normalized = key(text); words = normalized.split()
            if len(words) >= 8:
                exact[normalized].add(sid); queries[(sid, normalized)] = set(words)
                for start in {0, max(0, len(words)//2-2), len(words)-5}:
                    gram_index[tuple(words[start:start+5])].add((sid, normalized))
        for a, b in zip(user_texts, user_texts[1:]):
            joined = key(a + " " + b)
            if len(joined.split()) >= 12:
                spans[joined].add(sid)
    exact_hits, near_hits, seen = [], [], set(); records = 0
    training_hits = False
    for is_training, paths in [(False, corpus_paths), (True, training_paths or [])]:
        for path in paths:
            with Path(path).open(encoding="utf-8") as f:
                for line_number, line in enumerate(f, 1):
                    row = json.loads(line); records += 1
                    texts = [key(t["text"]) for t in row["turns"]]
                    if any(not isinstance(t, dict) for t in row["turns"]):
                        raise ValueError("invalid source turn")
                    source = str(row.get("source", Path(path).parent.name))
                    def record_hit(sid, normalized, kind, near=False):
                        nonlocal training_hits
                        signature = (sid, source, hashlib.sha256(normalized.encode()).hexdigest(), kind, is_training)
                        if signature in seen:
                            return
                        seen.add(signature)
                        item = {"scenario_id": sid, "source": source, "source_line": line_number,
                                "source_text_hash": signature[2], "kind": kind, "training_source": is_training}
                        (near_hits if near else exact_hits).append(item)
                        if is_training and not near:
                            training_hits = True
                    for normalized in texts:
                        for sid in exact.get(normalized, ()):
                            record_hit(sid, normalized, "turn")
                        words = normalized.split()
                        possible = set()
                        for i in range(len(words)-4):
                            possible.update(gram_index.get(tuple(words[i:i+5]), ()))
                        word_set = set(words)
                        for sid, q in possible:
                            if normalized == q:
                                continue
                            qwords = queries[(sid, q)]
                            overlap = len(qwords & word_set) / len(qwords | word_set)
                            if overlap >= .8:
                                record_hit(sid, normalized, "lexical_overlap", near=True)
                    for step in (1, 2):
                        for i in range(len(texts)-step):
                            joined = texts[i] + " " + texts[i+step]
                            for sid in spans.get(joined, ()):
                                record_hit(sid, joined, "span")
    return {"status": "FAIL" if exact_hits else "REVIEW_REQUIRED" if near_hits else "PASS",
            "exact_matches": exact_hits, "near_copy_alerts": near_hits, "records_scanned": records,
            "future_training_check": "FAIL" if training_hits else "PASS" if training_paths else "NOT_YET_APPLICABLE",
            "limits": "Exact substantive turns and 2-turn spans plus lexical overlap; semantic paraphrases and very short common phrases are not proven disjoint."}
