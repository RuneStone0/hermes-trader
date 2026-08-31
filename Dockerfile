# Self-contained trading service: deterministic strategies + YOLO + self-improvement.
# Talks to Alpaca (paper) and DeepSeek directly — no Hermes dependency.
FROM python:3.11-slim

# tzdata -> zoneinfo (market-time handling); ca-certificates -> HTTPS to Alpaca/DeepSeek
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TRADER_HOME=/data \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app
COPY . /app

# Persistent state (trades.db, dashboard, logs, proposals) lives here.
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)" || exit 1

CMD ["python3", "app.py"]
