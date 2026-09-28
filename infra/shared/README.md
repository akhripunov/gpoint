# Shared infrastructure Compose

`docker-compose.yml` is the versioned source of truth for the shared runtime
Compose project deployed at `/opt/shared/compose/docker-compose.yml`. It owns
the shared PostgreSQL and Redis services; it does not contain MaxiBot
application code or configuration.

The PostgreSQL instance contains the `gpoint` and `maxibot` databases. GPoint
and AGATA use the single `gpoint` database, with GPoint/Prisma in schema
`public` and AGATA/Alembic in schema `agata`. MaxiBot's database is `maxibot`.

Changes to this shared PostgreSQL service must verify both databases and
preserve both. MaxiBot application code and application configuration remain
in their separate project. Runtime credentials are supplied by the external
`/opt/shared/env/postgres.env` file and must never be committed here.

Compose-relative paths are intentional: when deployed in `/opt/shared/compose`,
the env file resolves to `/opt/shared/env/postgres.env`, and the data binds
resolve beneath `/opt/shared/postgres` and `/opt/shared/redis`.
