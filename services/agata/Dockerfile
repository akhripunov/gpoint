FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv/agata
RUN addgroup --system agata && adduser --system --ingroup agata agata
COPY pyproject.toml ./
RUN pip install --no-cache-dir .
COPY --chmod=u=rwX,go=rX app ./app
COPY --chmod=u=rwX,go=rX migrations ./migrations
COPY --chmod=644 alembic.ini ./alembic.ini
USER agata
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
