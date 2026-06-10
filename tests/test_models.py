"""Tests for the PgHealthSnapshot data model."""

from pg_health.models import (
    PgHealthSnapshot,
    from_dict,
    XidHealth,
    WorkerSaturation,
    AutovacuumSettings,
)


def _make_snapshot(**overrides) -> PgHealthSnapshot:
    settings = AutovacuumSettings(
        max_workers=3, naptime=60, vacuum_threshold=50,
        vacuum_scale_factor=0.2, analyze_threshold=50,
        analyze_scale_factor=0.1, cost_delay=2, cost_limit=200,
    )
    defaults = {
        "title": "test · 2026-05-23 14:30 UTC",
        "subtitle": "2 schemas · 10 tables · PostgreSQL 15.4",
        "captured_at": "2026-05-23T14:30:00Z",
        "db_name": "test_db",
        "postgres_version": "PostgreSQL 15.4",
        "settings": settings,
        "total_schemas": 2,
        "total_tables": 10,
        "total_live_tuples": 100_000,
        "total_dead_tuples": 5_000,
        "schemas_needing_vacuum": 1,
        "worker_saturation": WorkerSaturation(max_workers=3, active_workers=1),
        "xid_health": XidHealth(datname="test_db", xid_age=100_000, mxid_age=50_000),
    }
    defaults.update(overrides)
    return PgHealthSnapshot(**defaults)


def test_snapshot_roundtrip():
    """A snapshot can be serialized to dict and deserialized back."""
    s = _make_snapshot()
    d = s.to_dict()
    s2 = from_dict(d)
    assert s2.title == s.title
    assert s2.total_dead_tuples == s.total_dead_tuples
    assert s2.overall_dead_ratio == s.overall_dead_ratio


def test_dead_ratio():
    s = _make_snapshot(total_live_tuples=1000, total_dead_tuples=200)
    assert abs(s.overall_dead_ratio - 20.0) < 0.01


def test_dead_ratio_no_live():
    s = _make_snapshot(total_live_tuples=0, total_dead_tuples=0)
    assert s.overall_dead_ratio == 0.0


def test_xid_health_status():
    assert XidHealth("db", 100_000, 50_000).status == "ok"
    assert XidHealth("db", 1_600_000_000, 50_000).status == "warning"
    assert XidHealth("db", 1_900_000_000, 50_000).status == "critical"


def test_worker_saturation():
    w = WorkerSaturation(max_workers=3, active_workers=3)
    assert w.saturated
    assert w.idle_workers == 0

    w2 = WorkerSaturation(max_workers=3, active_workers=1)
    assert not w2.saturated
    assert w2.idle_workers == 2
