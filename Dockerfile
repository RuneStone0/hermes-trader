# Self-contained trading service: deterministic strategies + YOLO + self-improvement.
# Talks to Alpaca (paper) and DeepSeek directly — no Hermes dependency.

# Stage 1: extract git metadata (commit hash + date) baked into the image so the
# dashboard footer can show version/commit/freshness. Works under both deploy.sh
# and Portainer's git-stack auto-update (both provide the .git dir in the context).
FROM alpine/git AS meta
WORKDIR /src
COPY . /src
RUN { git rev-parse --short HEAD 2>/dev/null || echo unknown; } > /commit.txt \
    && { git log -1 --format=%cI 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ; } > /date.txt

# Stage 2: runtime.
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TRADER_HOME=/data \
    PYTHONUNBUFFERED=1 \
    PORT=43210

WORKDIR /app
COPY *.py /app/
COPY --from=meta /commit.txt /app/commit.txt
COPY --from=meta /date.txt /app/date.txt

VOLUME /data
EXPOSE 43210

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:43210/health', timeout=3)" || exit 1

CMD ["python3", "app.py"]
