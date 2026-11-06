FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    READING_COACH_HOME=/data

WORKDIR /app

COPY pyproject.toml README.md ./
COPY reading_coach ./reading_coach

RUN pip install --no-cache-dir ".[pdf]" \
    && useradd --create-home --uid 10001 coach \
    && mkdir -p /data \
    && chown -R coach:coach /data

USER coach
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os,sys,urllib.request; p=os.environ.get('READING_COACH_PORT','8765'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+p+'/api/health',timeout=2).status==200 else 1)"

CMD ["reading-coach-web", "--no-open"]
