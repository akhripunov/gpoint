FROM python:3.12-slim
ARG GPOINT_BUILD_SHA
LABEL org.opencontainers.image.revision=${GPOINT_BUILD_SHA}
LABEL org.opencontainers.image.source="https://github.com/akhripunov/gpoint"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv/g-sales
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir . && addgroup --system gsales && adduser --system --ingroup gsales gsales
COPY migrations ./migrations
USER gsales
EXPOSE 8010
HEALTHCHECK --interval=15s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/health/live',timeout=3)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010"]
