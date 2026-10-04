"""Test-only Core HTTP host. Fake embeddings, manually completed worker output."""
import base64
from uuid import UUID

import uvicorn
from app.api.internal import get_embedding_provider, require_internal_auth
from app.db.base import SessionFactory
from app.memory.retrieval import DeterministicFakeEmbeddingProvider
from app.memory.worker import MemoryWorker
from app.messages.crypto import AeadCrypto
from fastapi import Depends

from app.config import get_settings
from app.main import app

app.dependency_overrides[get_embedding_provider]=DeterministicFakeEmbeddingProvider


@app.post('/test/worker-cycle',dependencies=[Depends(require_internal_auth)])
async def worker_cycle():
    s=get_settings()
    worker=MemoryWorker(SessionFactory,tenant_id=UUID(s.tenant_id),provider=DeterministicFakeEmbeddingProvider(),
        crypto=AeadCrypto(encoded_key=s.message_aead_key.get_secret_value(),key_version='v1'),
        shadow_hmac_key=base64.b64decode(s.shadow_candidate_hmac_key.get_secret_value()),batch_size=64)
    result=await worker.run_once()
    return {'claimed':result.claimed,'completed':result.completed,'failed':result.failed}


if __name__=='__main__':
    import sys
    uvicorn.run(app,host='127.0.0.1',port=int(sys.argv[1]),log_level='error')
