# Deploying the Hermes Trader service

The trading system is a **self-contained Docker service**: it talks to Alpaca
(paper) and DeepSeek directly and needs no Hermes at runtime. Hermes is used only
as an external monitor (optional).

## One-time deploy on the Umbrel host

SSH into the Umbrel host (the `umbrel` user has `docker` + `sudo` group access):

```bash
mkdir -p ~/trader && cd ~/trader
git clone https://github.com/RuneStone0/hermes-trader.git .
cp .env.example .env
chmod 600 .env
# edit .env -> fill in DEEPSEEK_API_KEY + the 6 ALPACA_* keys
docker compose up -d --build
docker compose ps              # expect hermes-trader ... Up (healthy)
curl -s localhost:8080/health
```

## Reviewing the dashboard

The dashboard is read-only and unauthenticated, so it binds to **host loopback
only**. View it through an SSH tunnel from your workstation:

```bash
ssh -L 8080:localhost:8080 umbrel@umbrel.local
# then open http://localhost:8080 in your browser
```

## Updating (push new code, redeploy)

```bash
cd ~/trader && git pull && docker compose up -d --build
```

## Monitoring (Hermes as the external monitor)

The Hermes `trader` profile has a `service-monitor` cron job that SSHes to the
host and reports: `docker inspect --format '{{.State.Health.Status}}' hermes-trader`,
`curl -s localhost:8080/health`, and the tail of each job log under
`/data/logs` in the `trader-data` volume.

## Rollback to the Hermes-cron system

1. `cd ~/trader && docker compose down`
2. Resume the trader-profile cron jobs (`hermes cron resume <id>`).

## Schedules (UTC, Mon–Fri)

| job              | schedule                  |
|------------------|---------------------------|
| daily            | every 5 min, 13:00–21:00  |
| weekly           | 14:45                     |
| yolo             | 14:00, 17:00, 19:00       |
| reconcile        | every 10 min              |
| self-improvement | 21:30                     |

## Safety floor (applies to YOLO too)

- retail US equities/ETFs only (v1 auto-trade universe = `config.YOLO.watchlist`)
- no crypto except BTC/USD (crypto not yet in v1 auto-trade)
- stop-loss required on every trade
- max 25% equity per position, max 2% equity risked per trade, max 5 concurrent positions
