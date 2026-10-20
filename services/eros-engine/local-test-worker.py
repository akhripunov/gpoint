#!/usr/bin/env python3
"""Disposable deterministic GPOINT worker for local Eros acceptance only."""

import json
import os
import signal
import sys
import time
from pathlib import Path

repo = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo / 'services/agata/worker'))

if os.environ.get('GPOINT_AGATA_WORKER_LOCAL_TEST') != 'true':
    raise SystemExit('local_test_mode_required')
if os.environ.get('GPOINT_AGATA_WORKER_URL') != 'http://127.0.0.1:3000/agata-worker':
    raise SystemExit('isolated_test_gateway_mismatch')

import gpoint_mac_worker as worker

def deterministic_inference(job, **_kwargs):
    request = job.get('request', {})
    purpose = job.get('purpose')
    messages = request.get('messages', []) if isinstance(request, dict) else []
    content = '\n'.join(
        item.get('content', '') for item in messages
        if isinstance(item, dict) and isinstance(item.get('content'), str)
    )
    if 'SYNTHETIC_MEMORY_EXTRACTION_RESULT' in content:
        memories = []
        if 'SYNTH_CITY_ZEPHYRBAY' in content:
            memories.append({
                'content': 'SYNTH_CITY_ZEPHYRBAY',
                'category': 'fact',
                'confidence': 1.0,
            })
        old_meeting = 'SYNTH_OLD_2026-10-22_18:40'
        current_meeting = 'SYNTH_CURRENT_2026-10-23_19:15'
        if old_meeting in content and current_meeting in content:
            memories.append({
                'content': f'{old_meeting} (historical); {current_meeting} (current)',
                'category': 'event',
                'confidence': 1.0,
            })
        print(json.dumps({
            'event': 'LOCAL_TEST_MEMORY_EXTRACTION',
            'candidate_count': len(memories),
        }, separators=(',', ':')), flush=True)
        return json.dumps({'memories': memories}, separators=(',', ':'))
    if purpose == 'eros_chat_completion' and 'SYNTH_RECALL_PROBE' in content:
        memory_context = '\n'.join(
            item.get('content', '') for item in messages
            if isinstance(item, dict)
            and item.get('role') == 'system'
            and isinstance(item.get('content'), str)
            and ('[user_profile]' in item['content'] or '[shared_memories]' in item['content'])
        )
        recent_history = [
            item for item in messages
            if isinstance(item, dict) and item.get('role') in {'user', 'assistant'}
        ][-12:]
        recent_content = '\n'.join(
            item.get('content', '') for item in recent_history
            if isinstance(item.get('content'), str)
        )
        facts = {
            'city': 'SYNTH_CITY_ZEPHYRBAY',
            'old_meeting': 'SYNTH_OLD_2026-10-22_18:40',
            'current_meeting': 'SYNTH_CURRENT_2026-10-23_19:15',
        }
        evidence = {
            f'{name}_durable_memory_section_present': marker in memory_context
            for name, marker in facts.items()
        }
        evidence.update({
            f'{name}_absent_from_recent_history': marker not in recent_content
            for name, marker in facts.items()
        })
        user_messages = [
            item for item in messages
            if isinstance(item, dict) and item.get('role') == 'user'
        ]
        last_user_content = user_messages[-1].get('content') if user_messages else None
        last_user_is_recall_probe = (
            isinstance(last_user_content, str) and 'SYNTH_RECALL_PROBE' in last_user_content
        )
        print(json.dumps({
            'event': 'LOCAL_TEST_PROVIDER_CONTEXT',
            'message_count': len(messages),
            'recent_history_count': len(recent_history),
            'user_message_count': len(user_messages),
            'last_user_is_recall_probe': last_user_is_recall_probe,
            **evidence,
        }, separators=(',', ':')), flush=True)
        if all(evidence.values()):
            return 'SYNTH_RECALL_CONFIRMED city=ZephyrBay meeting=2026-10-23_19:15 history=2026-10-22_18:40'
        return 'SYNTH_RECALL_MISSING'
    return 'DETERMINISTIC_LOCAL_E2E_REPLY'


worker.run_inference = deterministic_inference

stopped = False


def stop(_signal, _frame):
    global stopped
    stopped = True


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)
idle_seconds = int(os.environ.get('EROS_TEST_WORKER_IDLE_SECONDS', '30'))
if not 1 <= idle_seconds <= 60:
    raise SystemExit('local_test_idle_timeout_invalid')
last_success = None
completed = 0
deadline = time.monotonic() + 300

while not stopped and time.monotonic() < deadline:
    try:
        if worker.process_one('GPOINT'):
            completed += 1
            last_success = time.monotonic()
            print(f'LOCAL_TEST_WORKER_COMPLETED=1 index={completed}', flush=True)
    except worker.TransientNetworkError:
        pass
    if last_success is not None and time.monotonic() - last_success >= idle_seconds:
        break
    time.sleep(0.5)
if not stopped and time.monotonic() >= deadline:
    raise SystemExit('local_test_worker_job_timeout')

if completed == 0:
    raise SystemExit('local_test_worker_no_jobs')
