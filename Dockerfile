FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt \
    && useradd --create-home --uid 10001 agent

COPY --chown=agent:agent main.py captioner.py sink.py ./

USER agent

# Model files the plugins need at runtime (none for Deepgram today; keeps the image ready).
RUN python main.py download-files

# The worker serves a health check on 8081 in production mode.
EXPOSE 8081
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8081/', timeout=4).status == 200 else 1)"

# SIGTERM drains: running rooms keep captions until they end or CAPTIONER_DRAIN_TIMEOUT.
STOPSIGNAL SIGTERM
CMD ["python", "main.py", "start"]
