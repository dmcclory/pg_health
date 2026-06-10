"""Query PostgreSQL catalog views and assemble a PgHealthSnapshot."""

from datetime import datetime, timezone
from typing import Optional

import psycopg

from .models import (
    AutovacuumSettings,
    LongRunningQuery,
    PgHealthSnapshot,
    RunningVacuum,
    SchemaHealth,
    TableAttention,
    WorkerSaturation,
    XidHealth,
)


# ---------------------------------------------------------------------------
# SQL fragments
# ---------------------------------------------------------------------------

_QUERY_SETTINGS = """
SELECT
    (SELECT setting::int  FROM pg_settings WHERE name = 'autovacuum_max_workers')         AS max_workers,
    (SELECT setting::int  FROM pg_settings WHERE name = 'autovacuum_naptime')             AS naptime,
    (SELECT setting::int  FROM pg_settings WHERE name = 'autovacuum_vacuum_threshold')    AS vacuum_threshold,
    (SELECT setting::float8 FROM pg_settings WHERE name = 'autovacuum_vacuum_scale_factor') AS vacuum_scale_factor,
    (SELECT setting::int  FROM pg_settings WHERE name = 'autovacuum_analyze_threshold')   AS analyze_threshold,
    (SELECT setting::float8 FROM pg_settings WHERE name = 'autovacuum_analyze_scale_factor') AS analyze_scale_factor,
    (SELECT setting::float8 FROM pg_settings WHERE name = 'autovacuum_vacuum_cost_delay') AS cost_delay,
    (SELECT setting::int  FROM pg_settings WHERE name = 'autovacuum_vacuum_cost_limit')   AS cost_limit;
"""

_QUERY_TABLES = """
SELECT
    schemaname,
    relname,
    n_live_tup,
    n_dead_tup,
    n_tup_ins,
    n_tup_upd,
    n_tup_del,
    n_tup_hot_upd,
    last_vacuum,
    last_autovacuum,
    last_autoanalyze,
    vacuum_count,
    autovacuum_count,
    analyze_count,
    n_mod_since_analyze
FROM pg_stat_user_tables
WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
ORDER BY n_dead_tup DESC;
"""

_QUERY_RELOPTIONS = """
SELECT
    n.nspname AS schemaname,
    c.relname AS relname,
    c.reloptions
FROM pg_class c
JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE c.relkind = 'r'
  AND c.reloptions IS NOT NULL
  AND n.nspname NOT IN ('pg_catalog', 'information_schema');
"""

# Only works on PG 14+. Wrapped in try/except in the collector.
_QUERY_RUNNING_VACUUMS = """
SELECT
    p.pid,
    s.schemaname,
    s.relname,
    p.phase,
    p.heap_blks_total,
    p.heap_blks_scanned,
    p.heap_blks_vacuumed,
    p.index_vacuum_count,
    p.dead_tuples_removed,
    a.query_start,
    a.leader_pid
FROM pg_stat_progress_vacuum p
JOIN pg_stat_activity a ON p.pid = a.pid
LEFT JOIN pg_stat_all_tables s ON p.relid = s.relid;
"""

_QUERY_LONG_RUNNING = """
SELECT
    pid,
    datname,
    usename,
    left(query, 500) AS query,
    state,
    wait_event_type,
    wait_event,
    backend_type,
    EXTRACT(EPOCH FROM (now() - query_start))::int AS duration_seconds,
    CASE
        WHEN state = 'idle in transaction' THEN true
        WHEN state = 'active' AND EXTRACT(EPOCH FROM (now() - query_start)) > 300 THEN true
        ELSE false
    END AS blocks_vacuum
FROM pg_stat_activity
WHERE pid != pg_backend_pid()
  AND backend_type = 'client backend'
  AND query NOT ILIKE 'autovacuum%%'
  AND query NOT ILIKE '%%pg_stat_%%'
  AND EXTRACT(EPOCH FROM (now() - query_start)) > 10
ORDER BY duration_seconds DESC;
"""

_QUERY_WORKERS = """
SELECT count(*) FROM pg_stat_activity
WHERE query LIKE 'autovacuum:%';
"""

_QUERY_XID = """
SELECT
    datname,
    age(datfrozenxid) AS xid_age,
    age(datminmxid)   AS mxid_age
FROM pg_database
WHERE datname = current_database();
"""

_QUERY_VERSION = """
SELECT version();
"""


# ---------------------------------------------------------------------------
# Public: all queries for export
# ---------------------------------------------------------------------------

QUERIES = {
    "settings": _QUERY_SETTINGS.strip(),
    "tables": _QUERY_TABLES.strip(),
    "reloptions": _QUERY_RELOPTIONS.strip(),
    "running_vacuums": _QUERY_RUNNING_VACUUMS.strip(),
    "long_running": _QUERY_LONG_RUNNING.strip(),
    "workers": _QUERY_WORKERS.strip(),
    "xid": _QUERY_XID.strip(),
    "version": _QUERY_VERSION.strip(),
}


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class Collector:
    """Queries a live PostgreSQL database and builds a PgHealthSnapshot."""

    def __init__(self, dsn: str):
        self.dsn = dsn

    def collect(self) -> PgHealthSnapshot:
        """Run all queries and return a complete snapshot."""
        now = datetime.now(timezone.utc)
        now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        with psycopg.connect(self.dsn) as conn:
            settings = self._fetch_settings(conn)
            tables = self._fetch_tables(conn)
            reloptions = self._fetch_reloptions(conn)
            running_vacuums = self._fetch_running_vacuums(conn)
            long_running = self._fetch_long_running(conn)
            active_workers = self._fetch_active_workers(conn)
            xid = self._fetch_xid(conn)
            version_str = self._fetch_version(conn)
            db_name = conn.info.dbname or ""

        schemas = _rollup_schemas(tables, reloptions)
        tables_attention = _flag_attention_tables(tables, reloptions)

        # Worker saturation — which schemas are starved?
        starved = [
            s.name for s in schemas
            if s.tables_needing_vacuum > 0
        ]
        worker_sat = WorkerSaturation(
            max_workers=settings.max_workers,
            active_workers=active_workers,
            starved_schemas=starved,
        )

        total_live = sum(s.total_live_tuples for s in schemas)
        total_dead = sum(s.total_dead_tuples for s in schemas)
        schemas_needing = sum(1 for s in schemas if s.tables_needing_vacuum > 0)

        pg_version = _parse_version(version_str)

        snapshot = PgHealthSnapshot(
            title=f"{db_name} · {now.strftime('%Y-%m-%d %H:%M')} UTC",
            subtitle=f"{len(schemas)} schemas · {len(tables)} tables · {pg_version}",
            captured_at=now_iso,
            db_name=db_name,
            postgres_version=pg_version,
            settings=settings,
            total_schemas=len(schemas),
            total_tables=len(tables),
            total_live_tuples=total_live,
            total_dead_tuples=total_dead,
            schemas_needing_vacuum=schemas_needing,
            worker_saturation=worker_sat,
            xid_health=xid,
            schemas=schemas,
            tables_needing_attention=tables_attention,
            running_vacuums=running_vacuums,
            long_running_queries=long_running,
        )

        return snapshot

    # --- individual fetchers ---

    def _fetch_settings(self, conn) -> AutovacuumSettings:
        row = conn.execute(_QUERY_SETTINGS).fetchone()
        return AutovacuumSettings(
            max_workers=row[0],
            naptime=row[1],
            vacuum_threshold=row[2],
            vacuum_scale_factor=row[3],
            analyze_threshold=row[4],
            analyze_scale_factor=row[5],
            cost_delay=row[6],
            cost_limit=row[7],
        )

    def _fetch_tables(self, conn) -> list[dict]:
        cur = conn.execute(_QUERY_TABLES)
        cols = [desc[0] for desc in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def _fetch_reloptions(self, conn) -> dict[str, dict]:
        """Return {(schema.table): {option_key: option_value}}."""
        rows = conn.execute(_QUERY_RELOPTIONS).fetchall()
        result = {}
        for schemaname, relname, opts in rows:
            key = f"{schemaname}.{relname}"
            parsed = {}
            for opt in (opts or []):
                if "=" in opt:
                    k, v = opt.split("=", 1)
                    parsed[k] = v
            result[key] = parsed
        return result

    def _fetch_running_vacuums(self, conn) -> list[RunningVacuum]:
        try:
            rows = conn.execute(_QUERY_RUNNING_VACUUMS).fetchall()
        except Exception:
            # pg_stat_progress_vacuum may not exist on PG < 14
            return []
        vacuums = []
        now = datetime.now(timezone.utc)
        for row in rows:
            started = row[9]  # query_start timestamp
            if started:
                started = started.replace(tzinfo=timezone.utc)
                duration = int((now - started).total_seconds())
                started_iso = started.strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                duration = 0
                started_iso = None
            vacuums.append(RunningVacuum(
                pid=row[0],
                schema=row[1] or "",
                table=row[2] or "",
                phase=row[3] or "",
                started_at=started_iso,
                duration_seconds=duration,
                heap_blks_total=row[4],
                heap_blks_scanned=row[5],
                heap_blks_vacuumed=row[6],
                index_vacuum_count=row[7],
                dead_tuples_removed=row[8],
                leader_pid=row[10],
            ))
        return vacuums

    def _fetch_long_running(self, conn) -> list[LongRunningQuery]:
        rows = conn.execute(_QUERY_LONG_RUNNING).fetchall()
        return [
            LongRunningQuery(
                pid=r[0],
                schema=r[1],
                user=r[2],
                query=r[3],
                duration_seconds=r[7],
                state=r[4],
                wait_event_type=r[5],
                wait_event=r[6],
                blocks_vacuum=r[8],
            )
            for r in rows
        ]

    def _fetch_active_workers(self, conn) -> int:
        row = conn.execute(_QUERY_WORKERS).fetchone()
        return row[0]

    def _fetch_xid(self, conn) -> XidHealth:
        row = conn.execute(_QUERY_XID).fetchone()
        return XidHealth(
            datname=row[0],
            xid_age=row[1],
            mxid_age=row[2],
        )

    def _fetch_version(self, conn) -> str:
        row = conn.execute(_QUERY_VERSION).fetchone()
        return row[0]

    # --- helpers ---

    @staticmethod
    def _parse_version(version_str: str) -> str:
        """Extract 'PostgreSQL X.Y.Z' from the full version string."""
        parts = version_str.split()
        if len(parts) >= 2:
            return f"{parts[0]} {parts[1]}"
        return version_str


# ---------------------------------------------------------------------------
# Shared helpers — used by both Collector and FileCollector
# ---------------------------------------------------------------------------

def _vacuum_threshold(table: dict, opts: dict) -> int:
    """Calculate the effective autovacuum trigger threshold for a table."""
    threshold = int(opts.get("autovacuum_vacuum_threshold", 50))
    scale = float(opts.get("autovacuum_vacuum_scale_factor", 0.2))
    live = table["n_live_tup"] or 0
    return threshold + int(scale * live)


def _parse_version(version_str: str) -> str:
    """Extract 'PostgreSQL X.Y.Z' from the full version string."""
    parts = version_str.split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1]}"
    return version_str


def _rollup_schemas(
    tables: list[dict], reloptions: dict
) -> list[SchemaHealth]:
    """Aggregate per-table stats into per-schema rollups."""
    schemas: dict[str, dict] = {}
    for t in tables:
        name = t["schemaname"]
        if name not in schemas:
            schemas[name] = {
                "name": name,
                "table_count": 0,
                "total_live_tuples": 0,
                "total_dead_tuples": 0,
                "autovacuum_times": [],
                "total_ins": 0,
                "total_upd": 0,
                "total_del": 0,
                "total_hot_upd": 0,
                "tables_needing_vacuum": 0,
                "has_overridden_settings": False,
            }
        s = schemas[name]
        s["table_count"] += 1
        s["total_live_tuples"] += t["n_live_tup"] or 0
        s["total_dead_tuples"] += t["n_dead_tup"] or 0
        s["total_ins"] += t["n_tup_ins"] or 0
        s["total_upd"] += t["n_tup_upd"] or 0
        s["total_del"] += t["n_tup_del"] or 0
        s["total_hot_upd"] += t["n_tup_hot_upd"] or 0
        if t["last_autovacuum"]:
            s["autovacuum_times"].append(str(t["last_autovacuum"]))
        table_key = f"{name}.{t['relname']}"
        if table_key in reloptions:
            s["has_overridden_settings"] = True
        threshold = _vacuum_threshold(t, reloptions.get(table_key, {}))
        if (t["n_dead_tup"] or 0) > threshold:
            s["tables_needing_vacuum"] += 1

    result = []
    for s in schemas.values():
        autovacuum_times = s.pop("autovacuum_times")
        total_upd = s["total_upd"]
        total_writes = s.pop("total_ins") + total_upd + s.pop("total_del")
        total_hot = s.pop("total_hot_upd")

        oldest = min(autovacuum_times) if autovacuum_times else None
        newest = max(autovacuum_times) if autovacuum_times else None

        result.append(SchemaHealth(
            name=s["name"],
            table_count=s["table_count"],
            total_live_tuples=s["total_live_tuples"],
            total_dead_tuples=s["total_dead_tuples"],
            oldest_table_last_autovacuum=oldest,
            newest_table_last_autovacuum=newest,
            writes_per_minute=round(total_writes / 60.0, 1) if total_writes else 0.0,
            hot_update_ratio=round(total_hot / total_upd, 3) if total_upd else 0.0,
            tables_needing_vacuum=s["tables_needing_vacuum"],
            has_overridden_settings=s["has_overridden_settings"],
        ))

    result.sort(key=lambda s: s.dead_tuple_ratio, reverse=True)
    return result


def _flag_attention_tables(
    tables: list[dict], reloptions: dict
) -> list[TableAttention]:
    """Find tables that stand out as problematic."""
    attention = []
    for t in tables:
        dead = t["n_dead_tup"] or 0
        live = t["n_live_tup"] or 0
        reasons = []

        # Dead tuple ratio check
        if live > 0:
            ratio = dead / live * 100
            if ratio > 10 and dead > 1000:
                reasons.append(f"{_fmt_number(dead)} dead tuples ({ratio:.1f}%)")

        # Never vacuumed
        if not t["last_autovacuum"] and live > 100:
            reasons.append("never vacuumed")

        # Old vacuum + still accumulating
        if t["last_autovacuum"] and dead > 10000:
            reasons.append(f"{_fmt_number(dead)} dead tuples since last vacuum")

        if not reasons:
            continue

        table_key = f"{t['schemaname']}.{t['relname']}"
        opts = reloptions.get(table_key, {})
        custom_threshold = int(opts["autovacuum_vacuum_threshold"]) if "autovacuum_vacuum_threshold" in opts else None
        custom_scale = float(opts["autovacuum_vacuum_scale_factor"]) if "autovacuum_vacuum_scale_factor" in opts else None

        attention.append(TableAttention(
            schema=t["schemaname"],
            table=t["relname"],
            dead_tuples=dead,
            live_tuples=live,
            last_autovacuum=str(t["last_autovacuum"]) if t["last_autovacuum"] else None,
            last_vacuum=str(t["last_vacuum"]) if t.get("last_vacuum") else None,
            last_autoanalyze=str(t["last_autoanalyze"]) if t.get("last_autoanalyze") else None,
            n_tup_ins=t["n_tup_ins"] or 0,
            n_tup_upd=t["n_tup_upd"] or 0,
            n_tup_del=t["n_tup_del"] or 0,
            n_tup_hot_upd=t["n_tup_hot_upd"] or 0,
            autovacuum_count=t["autovacuum_count"] or 0,
            vacuum_count=t["vacuum_count"] or 0,
            analyze_count=t["analyze_count"] or 0,
            custom_vacuum_threshold=custom_threshold,
            custom_vacuum_scale_factor=custom_scale,
            reason="; ".join(reasons),
        ))

    attention.sort(key=lambda t: t.dead_tuples, reverse=True)
    return attention


def _fmt_number(n: int) -> str:
    """Format a number with k/M suffixes."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


# ---------------------------------------------------------------------------
# File-based collector — reads pre-collected JSON instead of hitting a live DB
# ---------------------------------------------------------------------------

class FileCollector:
    """Read query results from JSON files and assemble a PgHealthSnapshot.

    Expected file layout inside the input directory:
        settings.json
        tables.json
        reloptions.json
        running_vacuums.json
        long_running.json
        workers.json
        xid.json
        version.json

    Each file contains the raw query results as a JSON array of objects
    (or a single object for single-row queries).
    """

    # Filenames expected on disk
    _FILES = (
        "settings", "tables", "reloptions", "running_vacuums",
        "long_running", "workers", "xid", "version",
    )

    def __init__(self, input_dir: str):
        from pathlib import Path
        self.input_dir = Path(input_dir)

    def collect(self) -> PgHealthSnapshot:
        """Read all JSON files and build a snapshot."""
        import json
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        data = {}
        for name in self._FILES:
            path = self.input_dir / f"{name}.json"
            if path.exists():
                with open(path) as f:
                    data[name] = json.load(f)
            else:
                data[name] = None

        settings = self._parse_settings(data.get("settings"))
        tables = data.get("tables") or []
        reloptions = self._parse_reloptions(data.get("reloptions"))
        running_vacuums = self._parse_running_vacuums(data.get("running_vacuums"))
        long_running = self._parse_long_running(data.get("long_running"))
        active_workers = self._parse_workers(data.get("workers"))
        xid = self._parse_xid(data.get("xid"))
        version_str = self._parse_version_str(data.get("version"))

        db_name = xid.datname if xid.datname != "unknown" else ""

        schemas = _rollup_schemas(tables, reloptions)
        tables_attention = _flag_attention_tables(tables, reloptions)

        starved = [s.name for s in schemas if s.tables_needing_vacuum > 0]
        worker_sat = WorkerSaturation(
            max_workers=settings.max_workers,
            active_workers=active_workers,
            starved_schemas=starved,
        )

        total_live = sum(s.total_live_tuples for s in schemas)
        total_dead = sum(s.total_dead_tuples for s in schemas)
        schemas_needing = sum(1 for s in schemas if s.tables_needing_vacuum > 0)
        pg_version = _parse_version(version_str)

        return PgHealthSnapshot(
            title=f"{db_name} · {now.strftime('%Y-%m-%d %H:%M')} UTC (from files)",
            subtitle=f"{len(schemas)} schemas · {len(tables)} tables · {pg_version}",
            captured_at=now_iso,
            db_name=db_name,
            postgres_version=pg_version,
            settings=settings,
            total_schemas=len(schemas),
            total_tables=len(tables),
            total_live_tuples=total_live,
            total_dead_tuples=total_dead,
            schemas_needing_vacuum=schemas_needing,
            worker_saturation=worker_sat,
            xid_health=xid,
            schemas=schemas,
            tables_needing_attention=tables_attention,
            running_vacuums=running_vacuums,
            long_running_queries=long_running,
        )

    @staticmethod
    def _parse_settings(data) -> AutovacuumSettings:
        if not data:
            return AutovacuumSettings(3, 60, 50, 0.2, 50, 0.1, 2, 200)
        if isinstance(data, list):
            data = data[0]
        return AutovacuumSettings(
            max_workers=int(data.get("max_workers", 3)),
            naptime=int(data.get("naptime", 60)),
            vacuum_threshold=int(data.get("vacuum_threshold", 50)),
            vacuum_scale_factor=float(data.get("vacuum_scale_factor", 0.2)),
            analyze_threshold=int(data.get("analyze_threshold", 50)),
            analyze_scale_factor=float(data.get("analyze_scale_factor", 0.1)),
            cost_delay=float(data.get("cost_delay", 2)),
            cost_limit=int(data.get("cost_limit", 200)),
        )

    @staticmethod
    def _parse_reloptions(data) -> dict[str, dict]:
        if not data:
            return {}
        result = {}
        for row in data:
            key = f"{row.get('schemaname', '')}.{row.get('relname', '')}"
            opts = row.get("reloptions") or []
            parsed = {}
            for opt in opts:
                if "=" in opt:
                    k, v = opt.split("=", 1)
                    parsed[k] = v
            result[key] = parsed
        return result

    def _parse_running_vacuums(self, data) -> list[RunningVacuum]:
        if not data:
            return []
        vacuums = []
        now = datetime.now(timezone.utc)
        for row in data:
            started = row.get("query_start")
            if started:
                try:
                    started_dt = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
                    duration = int((now - started_dt).total_seconds())
                    started_iso = started_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                except (ValueError, TypeError):
                    duration = 0
                    started_iso = str(started)
            else:
                duration = 0
                started_iso = None
            vacuums.append(RunningVacuum(
                pid=int(row.get("pid", 0)),
                schema=row.get("schemaname", ""),
                table=row.get("relname", ""),
                phase=row.get("phase", ""),
                started_at=started_iso,
                duration_seconds=duration,
                heap_blks_total=int(row.get("heap_blks_total", 0)),
                heap_blks_scanned=int(row.get("heap_blks_scanned", 0)),
                heap_blks_vacuumed=int(row.get("heap_blks_vacuumed", 0)),
                index_vacuum_count=int(row.get("index_vacuum_count", 0)),
                dead_tuples_removed=int(row.get("dead_tuples_removed", 0)),
                leader_pid=row.get("leader_pid"),
            ))
        return vacuums

    @staticmethod
    def _parse_long_running(data) -> list[LongRunningQuery]:
        if not data:
            return []
        return [
            LongRunningQuery(
                pid=int(r.get("pid", 0)),
                schema=r.get("datname"),
                user=r.get("usename", ""),
                query=r.get("query", ""),
                duration_seconds=int(r.get("duration_seconds", 0)),
                state=r.get("state", ""),
                wait_event_type=r.get("wait_event_type"),
                wait_event=r.get("wait_event"),
                blocks_vacuum=bool(r.get("blocks_vacuum", False)),
            )
            for r in data
        ]

    @staticmethod
    def _parse_workers(data) -> int:
        if not data:
            return 0
        if isinstance(data, list):
            data = data[0]
        for val in data.values():
            return int(val)
        return 0

    @staticmethod
    def _parse_xid(data) -> XidHealth:
        if not data:
            return XidHealth("unknown", 0, 0)
        if isinstance(data, list):
            data = data[0]
        return XidHealth(
            datname=data.get("datname", "unknown"),
            xid_age=int(data.get("xid_age", 0)),
            mxid_age=int(data.get("mxid_age", 0)),
        )

    @staticmethod
    def _parse_version_str(data) -> str:
        if not data:
            return "unknown"
        if isinstance(data, list):
            data = data[0]
        for val in data.values():
            return str(val)
        return "unknown"
