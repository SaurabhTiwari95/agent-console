# --- build stage: install dependencies (git is needed only here, to fetch cost-capped-agent) ----------
FROM python:3.15-slim AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY requirements.txt requirements-agent.txt ./
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt -r requirements-agent.txt

# --- runtime stage: small image, non-root user, no git -------------------------------------------------
FROM python:3.15-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONSOLE_DB_PATH=/data/console.db \
    AGENT_DB_PATH=/data/agent.db \
    INDEX_ON_START=/srv/sample_corpus
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
 && mkdir /data && chown app /data
WORKDIR /srv
COPY --from=build /install /usr/local
COPY agentconsole ./agentconsole
COPY sample_corpus ./sample_corpus
USER app
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
  CMD python -c "import os, sys, urllib.request; sys.exit(urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/healthz', timeout=2).status != 200)"
# One process: runs, rate limits and SQLite live in it (see README, "How it scales"). PORT is set by the host.
CMD ["python", "-m", "agentconsole", "--host", "0.0.0.0"]
