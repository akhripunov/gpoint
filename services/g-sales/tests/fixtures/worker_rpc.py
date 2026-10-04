"""A disposable worker process, with no Codex/LLM invocation."""
import json
import os
import sys

import httpx

with httpx.Client(base_url=os.environ['GSALES_E2E_CORE_URL'],headers={'Authorization':'Bearer disposable-e2e-core'},timeout=10) as client:
    if sys.argv[1]=='claim':
        response=client.post('/internal/v1/worker/claim')
    else:
        response=client.post('/internal/v1/worker/complete',json=json.loads(sys.stdin.read()))
    response.raise_for_status()
    print(response.text)
