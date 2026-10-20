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
    messages = request.get('messages', []) if isinstance(request, dict) else []
    content = '\n'.join(
        item.get('content', '') for item in messages
        if isinstance(item, dict) and isinstance(item.get('content'), str)
    )
    if 'SYNTH_RECALL_PROBE' in content:
        print(json.dumps({
            'event': 'LOCAL_TEST_PROVIDER_CONTEXT',
            'message_count': len(messages),
            'city_fact_present': 'SYNTH_CITY_ZEPHYRBAY' in content,
            'old_meeting_present': 'SYNTH_OLD_2026-10-22_18:40' in content,
            'current_meeting_present': 'SYNTH_CURRENT_2026-10-23_19:15' in content,
        }, separators=(',', ':')), flush=True)
    return 'DETERMINISTIC_LOCAL_E2E_REPLY'


worker.run_inference = deterministic_inference

stopped = False


def stop(_signal, _frame):
    global stopped
    stopped = True


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)
idle_seconds = int(os.environ.get('EROS_TEST_WORKER_IDLE_SECONDS', '5'))
if not 1 <= idle_seconds <= 30:
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
else:
    raise SystemExit('local_test_worker_job_timeout')

if completed == 0:
    raise SystemExit('local_test_worker_no_jobs')
