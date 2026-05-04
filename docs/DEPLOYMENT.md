# Deployment Options

## Recommended Path

For this project, the best deployment sequence is:

1. local dry-run and paper mode on your development machine
2. single-node always-on worker on a small VPS
3. tiny-size live mode after review of logs, paper performance, and kill-switch behavior

Best default choice:

- deploy the first real worker on a small VPS from Hetzner or DigitalOcean

Why this is the best fit for v1:

- the service is a long-running loop, not a bursty request app
- SQLite and JSONL logging are simplest on a single machine
- easier debugging, lower platform complexity, and predictable cost
- process supervision is straightforward with `systemd`, Docker Compose, or a process manager

## Option 1: VPS

Recommended for v1 and first live deployment.

Suggested stack:

- Ubuntu 24.04
- Python virtualenv or Docker
- `systemd` service or Docker Compose
- reverse proxy only if exposing dashboards or admin endpoints
- encrypted `.env` handling through provider secrets, `sops`, or manual host management

Pros:

- best control over network, logs, and local state
- stable for long-running workers
- easiest fit for SQLite journaling
- straightforward restarts and backups

Cons:

- more host management than managed platforms

## Option 2: Fly.io

Good if you want simpler managed deployment while still running a long-lived process.

Recommended use:

- paper-trading worker
- dashboard or operator API

Pros:

- simpler than raw VPS
- easy deployment flow
- good for small persistent services

Cons:

- persistent disk sizing and region planning matter
- still more opinionated than a simple VPS

## Option 3: Railway or Render

Useful for operator APIs, dashboards, or non-critical research services.

Good fit:

- read-only market scanners
- report generation
- operator UI

Not ideal as the first live trading runtime because:

- background worker behavior and persistent local state are less predictable than on VPS
- SQLite usage is usually a weaker fit

## Option 4: Kubernetes

Not recommended for v1.

Use only if:

- you later split the system into independent services
- you need separate research, execution, and reporting workers
- you move from SQLite to PostgreSQL and object storage

For the first version this adds too much operational surface area.

## Option 5: Serverless

Not recommended for the main trading loop.

Avoid for:

- order management loops
- timed exits
- continuous market monitoring

Possible use later:

- report generation
- periodic reconciliation
- webhook processing

## Deployment Split Recommendation

If the project grows, split deployment like this:

- trading worker on VPS
- optional operator API on Fly.io or Railway
- optional dashboard on Vercel or static hosting

Do not separate them for v1 unless there is a clear operational reason.

## systemd + Retention + Backup (Phase 5)

The daemon is designed to run under a process supervisor. The recommended
stack on a VPS is `systemd` for the daemon, `systemd` timers (or `cron`) for
backups, `logrotate` for the event journal, and the operator API behind
`uvicorn` or a local reverse proxy.

### polymarket-trading-engine.service

```ini
[Unit]
Description=Polymarket Trading Engine daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=polymarket
Group=polymarket
WorkingDirectory=/opt/polymarket-trading-engine
EnvironmentFile=/opt/polymarket-trading-engine/.env
ExecStart=/opt/polymarket-trading-engine/.venv/bin/polymarket-trading-engine daemon
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
# Tighten the sandbox if systemd version supports it:
ProtectSystem=strict
ReadWritePaths=/opt/polymarket-trading-engine/data /opt/polymarket-trading-engine/logs
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

Drop that into `/etc/systemd/system/polymarket-trading-engine.service`, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-trading-engine.service
sudo systemctl status polymarket-trading-engine.service
journalctl -u polymarket-trading-engine -f   # live log tail
```

### polymarket-trading-engine-api.service (optional)

Mirror the daemon unit but with `ExecStart=.../polymarket-trading-engine-api` and
expose it over a local loopback (or via an HTTPS reverse proxy) for the
dashboard to poll `/api/metrics` and `/api/healthz`.

### Backups via VACUUM INTO

`make backup DEST=/var/backups/polymarket` (or the CLI directly) produces a
consistent standalone `agent.db.<utc>` snapshot while the daemon is still
writing, thanks to SQLite's `VACUUM INTO` + WAL mode.

A `systemd` timer that backs up nightly and uploads off-host:

```ini
# /etc/systemd/system/polymarket-agent-backup.service
[Unit]
Description=Polymarket agent SQLite backup

[Service]
Type=oneshot
User=polymarket
EnvironmentFile=/opt/polymarket-trading-engine/.env
ExecStart=/opt/polymarket-trading-engine/.venv/bin/polymarket-trading-engine backup /var/backups/polymarket/
ExecStartPost=/usr/bin/rsync -a /var/backups/polymarket/ backup@off-host:/srv/polymarket-agent-backups/
```

```ini
# /etc/systemd/system/polymarket-agent-backup.timer
[Unit]
Description=Nightly Polymarket agent SQLite backup

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now polymarket-agent-backup.timer
```

### Maintenance (retention + VACUUM)

The daemon already runs retention + WAL checkpoint on
`DAEMON_MAINTENANCE_INTERVAL_SECONDS` (default 1 hour) and auto-prunes
`events.jsonl` every `~200` writes once it crosses
`EVENTS_JSONL_MAX_BYTES` (default 200 MB, keeping the tail 50 MB). Manual
knobs from the operator CLI:

```bash
make maintenance          # prune history + WAL checkpoint
make maintenance-vacuum   # same + full VACUUM (takes exclusive lock)
```

Schedule a weekly `VACUUM` with another `systemd` timer if you want a
compaction pass that reclaims pages after large deletions.

### logrotate for events.jsonl

The daemon's auto-prune is the primary defence, but `logrotate` also works
as a belt-and-suspenders rotation:

```text
# /etc/logrotate.d/polymarket-trading-engine
/opt/polymarket-trading-engine/logs/events.jsonl {
    daily
    rotate 7
    size 200M
    copytruncate
    missingok
    notifempty
    compress
}
```

### Kill-switch observability

`/api/healthz` returns non-200-ready when any check fails, including:

- heartbeat stale beyond `DAEMON_HEARTBEAT_STALE_SECONDS` (default 30s)
- authenticated readonly probe fails while in live mode
- `safety_stop_reason` fires (`daily_loss_limit`, `rejected_order_limit`,
  `auth_not_ready`, `daemon_heartbeat_stale`)

Point your uptime monitor (UptimeRobot, Pingdom, `healthchecks.io`) at
`/api/healthz` and alert on failure. Prometheus users should scrape
`/api/metrics?format=prometheus` — the `polymarket_agent_safety_stop_triggered`
gauge is exactly what an alert rule needs.

## Operational Requirements

Wherever the worker runs, require:

- process supervision and automatic restart
- system clock synchronization
- structured logs
- SQLite backup strategy
- JSONL log rotation
- alerting on kill-switch activation
- alerting on repeated auth or execution failures
- environment-specific config for paper vs live mode

## Best Initial Deployment

Best overall recommendation for this project:

- local development
- paper-trade on a small VPS
- tiny-size live on the same VPS after review

This gives the lowest operational complexity while the strategy and execution controls are still being validated.
