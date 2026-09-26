from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class PrincipalContext:
    tenant_id: UUID
    user_id: UUID
    actor_id: UUID
    channel: str

    def __post_init__(self) -> None:
        if (
            not all(
                isinstance(value, UUID) for value in (self.tenant_id, self.user_id, self.actor_id)
            )
            or not isinstance(self.channel, str)
            or self.channel not in {"api", "internal"}
        ):
            raise ValueError("invalid_principal")
