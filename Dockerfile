# Self-contained trading service: deterministic strategies + YOLO + self-improvement.
# Talks to Alpaca (paper) and DeepSeek directly — no Hermes dependency.

# Stage 1: extract git metadata (commit hash + date) baked into the image so the
# dashboard footer can show version/commit/freshness.
#
# Two deploy paths:
#   - Raw `docker compose` (deploy.sh): the build context includes .git, so read
#     the local HEAD directly.
#   - Portainer git-repository stack: Portainer clones the working tree WITHOUT
#     .git into a hash-named dir, so fall back to `git ls-remote` against origin.
FROM alpine/git AS meta
ARG REPO_URL=https://github.com/RuneStone0/hermes-trader
ARG REPO_REF=refs/heads/main
WORKDIR /src
COPY . /src
RUN if git rev-parse --short HEAD >/dev/null 2>&1; then \
        git rev-parse --short HEAD; \
    else \
        git ls-remote "$REPO_URL" "$REPO_REF" 2>/dev/null | cut -f1 | cut -c1-7; \
    fi > /commit.txt \
    && { git log -1 --format=%cI 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ; } > /date.txt
# Never ship an empty commit marker (e.g. ls-remote unreachable at build time).
RUN [ -s /commit.txt ] || printf 'unknown' > /commit.txt

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
