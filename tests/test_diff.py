"""Tests for snapshot diffing."""

from pg_health.models import (
    PgHealthSnapshot,
    from_dict,
    XidHealth,
    WorkerSaturation,
    AutovacuumSettings,
    SchemaHealth,
    TableAttention,
)
from pg_health.diff import diff_snapshots


def _settings():
    return AutovacuumSettings(
        max_workers=3, naptime=60, vacuum_threshold=50,
        vacuum_scale_factor=0.2, analyze_threshold=50,
        analyze_scale_factor=0.1, cost_delay=2, cost_limit=200,
        raw_av_cost_limit=200, vacuum_cost_limit=200,
    )


def _snapshot(dead_tuples=5000, live_tuples=100_000, workers=1, schemas=None, tables=None):
    if schemas is None:
        schemas = [
            SchemaHealth(
                name="tenant_acme", table_count=5,
                total_live_tuples=live_tuples // 2, total_dead_tuples=dead_tuples // 2,
                oldest_table_last_autovacuum="2026-05-23T12:00:00Z",
                newest_table_last_autovacuum="2026-05-23T14:00:00Z",
                writes_per_minute=100.0, hot_update_ratio=0.7,
                tables_needing_vacuum=1 if dead_tuples > 3000 else 0,
                has_overridden_settings=False,
            ),
            SchemaHealth(
                name="tenant_globex", table_count=5,
                total_live_tuples=live_tuples // 2, total_dead_tuples=dead_tuples // 2,
                oldest_table_last_autovacuum="2026-05-23T13:00:00Z",
                newest_table_last_autovacuum="2026-05-23T14:30:00Z",
                writes_per_minute=50.0, hot_update_ratio=0.8,
                tables_needing_vacuum=0,
                has_overridden_settings=False,
            ),
        ]
    if tables is None:
        tables = []
        if dead_tuples > 3000:
            tables.append(TableAttention(
                schema="tenant_acme", table="orders",
                dead_tuples=dead_tuples // 2, live_tuples=live_tuples // 2,
                last_autovacuum="2026-05-23T12:00:00Z",
                last_vacuum=None, last_autoanalyze="2026-05-23T12:00:00Z",
                n_tup_ins=1000, n_tup_upd=500, n_tup_del=200, n_tup_hot_upd=350,
                autovacuum_count=5, vacuum_count=0, analyze_count=5,
                custom_vacuum_threshold=None, custom_vacuum_scale_factor=None,
                reason="High dead tuple count",
            ))
    return PgHealthSnapshot(
        title="test · 2026-05-23 14:30 UTC",
        subtitle="2 schemas · 10 tables · PostgreSQL 15.4",
        captured_at="2026-05-23T14:30:00Z",
        db_name="test_db",
        postgres_version="PostgreSQL 15.4",
        settings=_settings(),
        total_schemas=2,
        total_tables=10,
        total_live_tuples=live_tuples,
        total_dead_tuples=dead_tuples,
        schemas_needing_vacuum=sum(1 for s in schemas if s.tables_needing_vacuum > 0),
        worker_saturation=WorkerSaturation(max_workers=3, active_workers=workers),
        xid_health=XidHealth(datname="test_db", xid_age=100_000, mxid_age=50_000),
        schemas=schemas,
        tables_needing_attention=tables,
    )


def test_diff_detects_increasing_dead_tuples():
    before = _snapshot(dead_tuples=5000)
    after = _snapshot(dead_tuples=20000)

    d = diff_snapshots(before, after)
    assert d.overall_dead_tuples_after > d.overall_dead_tuples_before
    assert d.overall_dead_ratio_after > d.overall_dead_ratio_before


def test_diff_detects_resolved_tables():
    before = _snapshot(dead_tuples=5000)
    after = _snapshot(dead_tuples=100)  # much healthier

    d = diff_snapshots(before, after)
    assert d.resolved_problem_tables >= 1


def test_diff_detects_new_problems():
    before = _snapshot(dead_tuples=100, tables=[])
    after = _snapshot(dead_tuples=50000)

    d = diff_snapshots(before, after)
    assert d.new_problem_tables >= 1


def test_diff_shows_schema_deltas():
    before = _snapshot(dead_tuples=5000)
    after = _snapshot(dead_tuples=50000)

    d = diff_snapshots(before, after)
    assert len(d.schema_deltas) == 2  # both schemas present
    # At least one schema should show a worsening
    assert any(sd.dead_ratio_delta > 0 for sd in d.schema_deltas)


def test_diff_text_render():
    from pg_health.diff import diff_to_text
    before = _snapshot(dead_tuples=5000)
    after = _snapshot(dead_tuples=50000)
    d = diff_snapshots(before, after)
    text = diff_to_text(d)
    assert "Diff:" in text
    assert "Dead tuple ratio" in text
    assert "Schema changes" in text


def test_diff_html_render():
    from pg_health.diff import diff_to_html
    before = _snapshot(dead_tuples=5000)
    after = _snapshot(dead_tuples=50000)
    d = diff_snapshots(before, after)
    html = diff_to_html(d)
    assert "<!DOCTYPE html>" in html
    assert "Snapshot Diff" in html
