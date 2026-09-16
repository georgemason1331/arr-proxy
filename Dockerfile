FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    ARRPROXY_CONFIG=/config/config.yaml

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY arrproxy ./arrproxy

# Runs unprivileged; /config is the only path it needs to write (to persist a
# generated API key), so the operator can chown just that directory.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin arrproxy \
    && mkdir -p /config && chown -R arrproxy:arrproxy /config /app
USER arrproxy

EXPOSE 8989 7878 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,sys,urllib.request; \
url=os.environ.get('ARRPROXY_HEALTH_URL','http://127.0.0.1:8787/-/health'); \
sys.exit(0 if urllib.request.urlopen(url, timeout=4).status == 200 else 1)"

ENTRYPOINT ["python", "-m", "arrproxy"]
