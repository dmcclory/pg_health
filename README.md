# pg-health

Point-in-time PostgreSQL autovacuum health snapshots. No agents, no metrics database, no Grafana.
Just point it at a database and get a report.

## Install

```bash
uv tool install pg-health
# or run without installing:
uvx pg-health check --dsn postgres://...
```

## Quick start

### Take a snapshot

```bash
pg-health check --url postgres://user:pass@host:5432/mydb --output-dir ./snapshots

# Or set $DATABASE_URL and skip --url entirely:
export DATABASE_URL=postgres://user:pass@host:5432/mydb
pg-health check
```

Produces three files per run:

- `pg-health-2026-05-23T143000Z.json` — machine-readable snapshot
- `pg-health-2026-05-23T143000Z.html` — self-contained HTML report
- `pg-health-2026-05-23T143000Z.txt` — plain text for the terminal

### Compare two snapshots

```bash
pg-health diff ./snapshots/pg-health-2026-05-23T143000Z.json \
               ./snapshots/pg-health-2026-05-23T153000Z.json
```

Shows what changed: dead tuple ratios, new problem tables, resolved issues,
worker saturation shifts, XID age movement.

Add `--format html` for an HTML diff report, or `--output report.html` to save it.

## What it checks

| Area | What it looks at |
|---|---|
| **Dead tuples** | Per-schema and per-table dead tuple ratios |
| **Vacuum health** | Last autovacuum timestamps, vacuum frequency, tables falling behind |
| **Worker saturation** | Are all autovacuum workers busy? Which schemas are starved? |
| **HOT updates** | Update efficiency — low HOT ratio = index bloat |
| **Vacuum blockers** | Long-running queries, idle-in-transaction sessions |
| **Running vacuums** | Live progress from `pg_stat_progress_vacuum` (PG 14+) |
| **XID wraparound** | Transaction ID age with warning/critical thresholds |

## Scheduled use

Run it on a cron schedule:

```bash
0 * * * * pg-health check --dsn $DATABASE_URL --output-dir /var/pg-health/ --format json
```

Then diff snapshots on demand to see trends.

## Requirements

- Python 3.11+
- PostgreSQL (any version; `pg_stat_progress_vacuum` requires PG 14+)
- Read-only access to catalog views (any database user can query `pg_stat_user_tables`)

## License

MIT
