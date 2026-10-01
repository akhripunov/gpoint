FROM python:3.12-slim AS runtime
ARG GPOINT_BUILD_SHA
LABEL org.opencontainers.image.revision=${GPOINT_BUILD_SHA} org.opencontainers.image.source="https://github.com/akhripunov/gpoint"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv/agata
RUN addgroup --system agata && adduser --system --ingroup agata agata
COPY pyproject.toml ./
RUN pip install --no-cache-dir .
COPY app ./app
COPY migrations ./migrations
COPY --chmod=644 alembic.ini ./alembic.ini
# Normalize nested directory permissions with the container's chmod rather
# than relying on symbolic COPY permission handling across BuildKit versions.
RUN chmod -R u=rwX,go=rX /srv/agata/app /srv/agata/migrations
USER agata
RUN python -c "from app.db.models import Base; assert len(Base.metadata.tables) > 0"
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
