"""Snapshot data shape for PostgreSQL health monitoring."""

from dataclasses import dataclass, field, asdict
from typing import List, Optional


# ---------------------------------------------------------------------------
# Settings — the autovacuum knobs
# ---------------------------------------------------------------------------

@dataclass
class AutovacuumSettings:
    """GUCs that control autovacuum behaviour."""
    max_workers: int
    naptime: int
    vacuum_threshold: int
    vacuum_scale_factor: float
    analyze_threshold: int
    analyze_scale_factor: float
    cost_delay: float        # ms; can be fractional in newer PG
    cost_limit: int


# ---------------------------------------------------------------------------
# Per-schema rollup
# ---------------------------------------------------------------------------

@dataclass
class SchemaHealth:
    """All tables in one schema, rolled up."""
    name: str
    table_count: int
    total_live_tuples: int
    total_dead_tuples: int
    oldest_table_last_autovacuum: Optional[str]  # ISO-8601 or null
    newest_table_last_autovacuum: Optional[str]  # ISO-8601 or null
    writes_per_minute: float
    hot_update_ratio: float
    tables_needing_vacuum: int
    has_overridden_settings: bool

    @property
    def dead_tuple_ratio(self) -> float:
        if self.total_live_tuples == 0:
            return 0.0
        return self.total_dead_tuples / self.total_live_tuples * 100


# ---------------------------------------------------------------------------
# Individual tables that need attention
# ---------------------------------------------------------------------------

@dataclass
class TableAttention:
    """A single table that's showing signs of vacuum stress."""
    schema: str
    table: str
    dead_tuples: int
    live_tuples: int
    last_autovacuum: Optional[str]
    last_vacuum: Optional[str]
    last_autoanalyze: Optional[str]
    n_tup_ins: int
    n_tup_upd: int
    n_tup_del: int
    n_tup_hot_upd: int
    autovacuum_count: int
    vacuum_count: int
    analyze_count: int
    custom_vacuum_threshold: Optional[int]
    custom_vacuum_scale_factor: Optional[float]
    reason: str

    @property
    def dead_tuple_ratio(self) -> float:
        if self.live_tuples == 0:
            return 0.0
        return self.dead_tuples / self.live_tuples * 100

    @property
    def hot_update_ratio(self) -> float:
        if self.n_tup_upd == 0:
            return 0.0
        return self.n_tup_hot_upd / self.n_tup_upd


# ---------------------------------------------------------------------------
# Running vacuums (from pg_stat_progress_vacuum, PG 14+)
# ---------------------------------------------------------------------------

@dataclass
class RunningVacuum:
    """A vacuum currently in progress."""
    pid: int
    schema: str
    table: str
    phase: str
    started_at: str
    duration_seconds: int
    heap_blks_total: int
    heap_blks_scanned: int
    heap_blks_vacuumed: int
    index_vacuum_count: int
    dead_tuples_removed: int
    leader_pid: Optional[int]

    @property
    def progress_pct(self) -> float:
        if self.heap_blks_total == 0:
            return 0.0
        return self.heap_blks_scanned / self.heap_blks_total * 100


# ---------------------------------------------------------------------------
# Long-running queries (the vacuum blockers)
# ---------------------------------------------------------------------------

@dataclass
class LongRunningQuery:
    """A query that's been running long enough to matter."""
    pid: int
    schema: Optional[str]
    user: str
    query: str
    duration_seconds: int
    state: str
    wait_event_type: Optional[str]
    wait_event: Optional[str]
    blocks_vacuum: bool


# ---------------------------------------------------------------------------
# Transaction ID health
# ---------------------------------------------------------------------------

@dataclass
class XidHealth:
    """Transaction ID wraparound risk."""
    datname: str
    xid_age: int
    mxid_age: int

    @property
    def status(self) -> str:
        if self.xid_age >= 1_800_000_000:
            return "critical"
        if self.xid_age >= 1_500_000_000:
            return "warning"
        return "ok"


# ---------------------------------------------------------------------------
# Vacuum worker saturation
# ---------------------------------------------------------------------------

@dataclass
class WorkerSaturation:
    """Are autovacuum workers maxed out?"""
    max_workers: int
    active_workers: int
    starved_schemas: List[str] = field(default_factory=list)

    @property
    def idle_workers(self) -> int:
        return max(0, self.max_workers - self.active_workers)

    @property
    def saturated(self) -> bool:
        return self.active_workers >= self.max_workers


# ---------------------------------------------------------------------------
# Top-level snapshot
# ---------------------------------------------------------------------------

@dataclass
class PgHealthSnapshot:
    """One point-in-time reading of PostgreSQL health."""
    title: str
    subtitle: str
    captured_at: str
    db_name: str
    postgres_version: str
    settings: AutovacuumSettings
    total_schemas: int
    total_tables: int
    total_live_tuples: int
    total_dead_tuples: int
    schemas_needing_vacuum: int
    worker_saturation: WorkerSaturation
    xid_health: XidHealth
    schemas: List[SchemaHealth] = field(default_factory=list)
    tables_needing_attention: List[TableAttention] = field(default_factory=list)
    running_vacuums: List[RunningVacuum] = field(default_factory=list)
    long_running_queries: List[LongRunningQuery] = field(default_factory=list)
    previous_snapshot_ref: Optional[str] = None

    @property
    def overall_dead_ratio(self) -> float:
        if self.total_live_tuples == 0:
            return 0.0
        return self.total_dead_tuples / self.total_live_tuples * 100

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Deserialization
# ---------------------------------------------------------------------------

def from_dict(data: dict) -> PgHealthSnapshot:
    """Build a PgHealthSnapshot from a dict (e.g. parsed JSON)."""
    settings_data = data["settings"]
    settings = AutovacuumSettings(
        max_workers=settings_data["max_workers"],
        naptime=settings_data["naptime"],
        vacuum_threshold=settings_data["vacuum_threshold"],
        vacuum_scale_factor=settings_data["vacuum_scale_factor"],
        analyze_threshold=settings_data["analyze_threshold"],
        analyze_scale_factor=settings_data["analyze_scale_factor"],
        cost_delay=settings_data["cost_delay"],
        cost_limit=settings_data["cost_limit"],
    )

    worker_data = data.get("worker_saturation", {})
    worker_sat = WorkerSaturation(
        max_workers=worker_data.get("max_workers", settings.max_workers),
        active_workers=worker_data.get("active_workers", 0),
        starved_schemas=worker_data.get("starved_schemas", []),
    )

    xid_data = data.get("xid_health", {})
    xid = XidHealth(
        datname=xid_data.get("datname", data.get("db_name", "")),
        xid_age=xid_data.get("xid_age", 0),
        mxid_age=xid_data.get("mxid_age", 0),
    )

    return PgHealthSnapshot(
        title=data["title"],
        subtitle=data.get("subtitle", ""),
        captured_at=data["captured_at"],
        db_name=data["db_name"],
        postgres_version=data.get("postgres_version", ""),
        settings=settings,
        total_schemas=data.get("total_schemas", 0),
        total_tables=data.get("total_tables", 0),
        total_live_tuples=data.get("total_live_tuples", 0),
        total_dead_tuples=data.get("total_dead_tuples", 0),
        schemas_needing_vacuum=data.get("schemas_needing_vacuum", 0),
        worker_saturation=worker_sat,
        schemas=[SchemaHealth(**s) for s in data.get("schemas", [])],
        tables_needing_attention=[
            TableAttention(**t) for t in data.get("tables_needing_attention", [])
        ],
        running_vacuums=[RunningVacuum(**v) for v in data.get("running_vacuums", [])],
        long_running_queries=[
            LongRunningQuery(**q) for q in data.get("long_running_queries", [])
        ],
        xid_health=xid,
        previous_snapshot_ref=data.get("previous_snapshot_ref"),
    )
