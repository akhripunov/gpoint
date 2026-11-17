#!/usr/bin/env python3
"""Emit SQL fixtures for the disposable stock-Eros affinity recall matrix."""

import hashlib
import json
import sys


def sql_text(value):
    return "'" + str(value).replace("'", "''") + "'"


def query_vector(text):
    values = []
    for index in range(512):
        digest = hashlib.sha256(f"{text}\0{index}".encode()).digest()
        value = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF * 2 - 1
        values.append(f"{value:.17g}")
    return "[" + ",".join(values) + "]"


cases = json.load(open(sys.argv[1], encoding="utf-8"))
print("BEGIN;")
for case in cases:
    user = sql_text(case["user_id"])
    instance = sql_text(case["instance_id"])
    session = sql_text(case["session_id"])
    vector = sql_text(query_vector(case["query"])) + "::vector"
    profile = [
        ("fact", "SYNTH_PROFILE_NEUTRAL_1", None),
        ("fact", "SYNTH_PROFILE_NEUTRAL_2", None),
        ("preference", "SYNTH_PROFILE_ROMANCE", "romance"),
        ("event", "SYNTH_PROFILE_FAMILY", "family"),
        ("emotion", "SYNTH_PROFILE_FINANCE", "finance"),
        ("relation", "SYNTH_PROFILE_KINK", "kink"),
    ]
    for category, marker, domain in profile:
        metadata = "{}" if domain is None else json.dumps({"domain": domain})
        row_vector = vector
        if marker == "SYNTH_PROFILE_NEUTRAL_2":
            row_vector = sql_text(query_vector(case["query"] + " decoy")) + "::vector"
        print(
            "INSERT INTO engine.companion_memories "
            "(session_id,user_id,instance_id,content,embedding,category,metadata) VALUES "
            f"({session},{user},NULL,{sql_text(marker)},{row_vector},{sql_text(category)},{sql_text(metadata)}::jsonb);"
        )
    for n in range(1, 4):
        marker = f"SYNTH_REL_RAW_{n}"
        print(
            "INSERT INTO engine.companion_memories "
            "(session_id,user_id,instance_id,content,embedding,category,metadata) VALUES "
            f"({session},{user},{instance},{sql_text(marker)},{vector},NULL,NULL);"
        )
    for n in range(1, 4):
        marker = sql_text(f"SYNTH_EPISODE_{n}")
        print(
            "INSERT INTO engine.relationship_episodes (instance_id,summary,started_at,ended_at) "
            f"VALUES ({instance},{marker},now()-interval '{7-n} days',now()-interval '{6-n} days');"
        )
    print(
        "INSERT INTO engine.relationship_arcs (instance_id,opening,arc,folded_through) "
        f"VALUES ({instance},'SYNTH_ARC_OPENING','SYNTH_ARC_CURRENT',now());"
    )
    for n in range(1, 3):
        print(
            "INSERT INTO engine.relationship_journal "
            "(instance_id,occurred_on,kind,realm,summary,source,recurrence,embedding) VALUES "
            f"({instance},current_date-{n},'shared_plan','real',"
            f"{sql_text(f'SYNTH_JOURNAL_{n}')},'engine','none',{vector});"
        )
    # The production affinity schema derives Bond and Chemistry from these
    # four line axes. Values are interior points in each official tier band.
    value = {1: 0.05, 2: 0.20, 3: 0.45, 4: 0.75, 5: 0.95}
    bond = value[int(case["bond_tier"])]
    chemistry = value[int(case["chemistry_tier"])]
    print(
        "INSERT INTO engine.companion_affinity "
        "(session_id,user_id,instance_id,trust,intrigue,intimacy,tension,updated_at) VALUES "
        f"({session},{user},{instance},{bond},{bond},{chemistry},{chemistry},now());"
    )
    print(
        "SELECT "
        f"{sql_text('LOCAL_E2E_AFFINITY_FIXTURE=' + case['name'])} || "
        "'|bond=' || bond::text || '|chemistry=' || chemistry::text "
        "FROM engine.companion_affinity "
        f"WHERE session_id={session};"
    )
print("COMMIT;")
