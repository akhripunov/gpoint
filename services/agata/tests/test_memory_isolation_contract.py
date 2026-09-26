from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from app.db.models import Scope
from app.domain.principal import PrincipalContext
from app.memory.retrieval import HybridRetrievalService, RetrievalRequest


def _sql(user_id, character_id):
    principal = PrincipalContext(uuid4(), user_id, user_id, "internal")
    request = RetrievalRequest(
        character_id=character_id,
        scope=Scope.real_world,
        scenario_id=None,
        query="Как зовут мою собаку?",
        as_of=datetime.now(UTC),
    )
    statement = HybridRetrievalService.__new__(HybridRetrievalService)._hard_filter(
        principal, request
    )
    return statement.compile(dialect=postgresql.dialect())


def test_retrieval_hard_filter_binds_both_user_and_character_identity():
    user_one, user_two = uuid4(), uuid4()
    character_one, character_two = uuid4(), uuid4()
    for compiled, user_id, character_id in (
        (_sql(user_one, character_one), user_one, character_one),
        (_sql(user_one, character_two), user_one, character_two),
        (_sql(user_two, character_one), user_two, character_one),
    ):
        bound = list(compiled.params.values())
        assert user_id in bound
        assert character_id in bound
        sql = str(compiled)
        assert "memories.user_id" in sql
        assert "memories.character_id" in sql
