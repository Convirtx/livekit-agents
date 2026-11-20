FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
# tini reaps the room processes a stopped worker leaves behind (the supervisor is PID 1).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/* \
    && pip install -r requirements.txt \
    && useradd --create-home --uid 10001 agent

COPY --chown=agent:agent main.py captioner.py sink.py supervisor.py ./

USER agent

# Model files the plugins need at runtime (none for Deepgram today; keeps the image ready).
RUN python main.py download-files

# Health check on 8081: the supervisor when CAPTIONER_CONFIG_URL is set, else the worker.
EXPOSE 8081
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8081/', timeout=4).status == 200 else 1)"

# SIGTERM drains: running rooms keep captions until they end or CAPTIONER_DRAIN_TIMEOUT.
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--"]
# With CAPTIONER_CONFIG_URL the supervisor runs the worker with the tenant site's settings;
# without it, it starts `main.py start` with the env.
CMD ["python", "supervisor.py"]
