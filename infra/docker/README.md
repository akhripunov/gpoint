# Local infrastructure

The V1 foundation reserves PostgreSQL, Redis, and BullMQ integration points. Add a pinned `docker-compose.yml` only when local integration tests are introduced; no service should be represented as healthy until its connectivity test exists.
