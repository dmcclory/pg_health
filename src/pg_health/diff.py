"""Compare two PgHealthSnapshot instances and produce a delta report."""

from dataclasses import dataclass, field
from typing import Optional

from .models import PgHealthSnapshot, SchemaHealth, TableAttention


# ---------------------------------------------------------------------------
# Delta data structures
# ---------------------------------------------------------------------------

@dataclass
class SchemaDelta:
    """Change in a single schema between two snapshots."""
    name: str
    dead_ratio_before: float
    dead_ratio_after: float
    dead_tuples_before: int
    dead_tuples_after: int
    live_tuples_before: int
    live_tuples_after: int
    tables_needing_vacuum_before: int
    tables_needing_vacuum_after: int
    last_autovacuum_before: Optional[str]
    last_autovacuum_after: Optional[str]

    @property
    def dead_ratio_delta(self) -> float:
        return self.dead_ratio_after - self.dead_ratio_before

    @property
    def dead_tuples_delta(self) -> int:
        return self.dead_tuples_after - self.dead_tuples_before


@dataclass
class TableDelta:
    """How a single table changed between snapshots."""
    schema: str
    table: str
    dead_tuples_before: int
    dead_tuples_after: int
    last_autovacuum_before: Optional[str]
    last_autovacuum_after: Optional[str]
    status: str  # "new", "worse", "better", "resolved"


@dataclass
class SnapshotDiff:
    """The full delta between two snapshots."""
    before_title: str
    after_title: str
    before_captured: str
    after_captured: str

    overall_dead_ratio_before: float
    overall_dead_ratio_after: float
    overall_dead_tuples_before: int
    overall_dead_tuples_after: int

    workers_before: int
    workers_after: int
    max_workers: int

    xid_status_before: str
    xid_status_after: str

    schema_deltas: list[SchemaDelta] = field(default_factory=list)
    table_deltas: list[TableDelta] = field(default_factory=list)

    # Summary counts
    new_problem_tables: int = 0
    resolved_problem_tables: int = 0
    worsened_schemas: int = 0
    improved_schemas: int = 0


# ---------------------------------------------------------------------------
# Diff logic
# ---------------------------------------------------------------------------

def diff_snapshots(before: PgHealthSnapshot, after: PgHealthSnapshot) -> SnapshotDiff:
    """Compare two snapshots and return a structured diff."""
    diff = SnapshotDiff(
        before_title=before.title,
        after_title=after.title,
        before_captured=before.captured_at,
        after_captured=after.captured_at,
        overall_dead_ratio_before=before.overall_dead_ratio,
        overall_dead_ratio_after=after.overall_dead_ratio,
        overall_dead_tuples_before=before.total_dead_tuples,
        overall_dead_tuples_after=after.total_dead_tuples,
        workers_before=before.worker_saturation.active_workers,
        workers_after=after.worker_saturation.active_workers,
        max_workers=after.worker_saturation.max_workers,
        xid_status_before=before.xid_health.status,
        xid_status_after=after.xid_health.status,
    )

    # Schema deltas
    before_by_name = {s.name: s for s in before.schemas}
    after_by_name = {s.name: s for s in after.schemas}
    all_names = set(before_by_name.keys()) | set(after_by_name.keys())

    for name in sorted(all_names):
        b = before_by_name.get(name)
        a = after_by_name.get(name)
        if not b or not a:
            continue  # skip schemas that appeared/disappeared for now

        delta = SchemaDelta(
            name=name,
            dead_ratio_before=b.dead_tuple_ratio,
            dead_ratio_after=a.dead_tuple_ratio,
            dead_tuples_before=b.total_dead_tuples,
            dead_tuples_after=a.total_dead_tuples,
            live_tuples_before=b.total_live_tuples,
            live_tuples_after=a.total_live_tuples,
            tables_needing_vacuum_before=b.tables_needing_vacuum,
            tables_needing_vacuum_after=a.tables_needing_vacuum,
            last_autovacuum_before=b.oldest_table_last_autovacuum,
            last_autovacuum_after=a.oldest_table_last_autovacuum,
        )
        diff.schema_deltas.append(delta)

        if delta.dead_ratio_delta > 1.0:
            diff.worsened_schemas += 1
        elif delta.dead_ratio_delta < -1.0:
            diff.improved_schemas += 1

    # Sort: worst changes first
    diff.schema_deltas.sort(key=lambda d: abs(d.dead_ratio_delta), reverse=True)

    # Table deltas
    before_tables = {f"{t.schema}.{t.table}": t for t in before.tables_needing_attention}
    after_tables = {f"{t.schema}.{t.table}": t for t in after.tables_needing_attention}

    before_keys = set(before_tables.keys())
    after_keys = set(after_tables.keys())

    # New problem tables
    for key in after_keys - before_keys:
        t = after_tables[key]
        diff.table_deltas.append(TableDelta(
            schema=t.schema,
            table=t.table,
            dead_tuples_before=0,
            dead_tuples_after=t.dead_tuples,
            last_autovacuum_before=None,
            last_autovacuum_after=t.last_autovacuum,
            status="new",
        ))
        diff.new_problem_tables += 1

    # Resolved tables
    for key in before_keys - after_keys:
        t = before_tables[key]
        diff.table_deltas.append(TableDelta(
            schema=t.schema,
            table=t.table,
            dead_tuples_before=t.dead_tuples,
            dead_tuples_after=0,
            last_autovacuum_before=t.last_autovacuum,
            last_autovacuum_after=None,
            status="resolved",
        ))
        diff.resolved_problem_tables += 1

    # Changed tables (present in both)
    for key in before_keys & after_keys:
        b = before_tables[key]
        a = after_tables[key]
        delta = a.dead_tuples - b.dead_tuples
        if delta == 0:
            continue
        status = "worse" if delta > 0 else "better"
        diff.table_deltas.append(TableDelta(
            schema=a.schema,
            table=a.table,
            dead_tuples_before=b.dead_tuples,
            dead_tuples_after=a.dead_tuples,
            last_autovacuum_before=b.last_autovacuum,
            last_autovacuum_after=a.last_autovacuum,
            status=status,
        ))

    # Sort: new problems first, then worst changes
    status_order = {"new": 0, "worse": 1, "better": 2, "resolved": 3}
    diff.table_deltas.sort(key=lambda d: (status_order[d.status], -abs(d.dead_tuples_after - d.dead_tuples_before)))

    return diff


# ---------------------------------------------------------------------------
# Render diff to text
# ---------------------------------------------------------------------------

def diff_to_text(diff: SnapshotDiff) -> str:
    """Render a diff as plain text for the terminal."""
    lines: list[str] = []

    lines.append(f"  Diff: {diff.before_title}  →  {diff.after_title}")
    lines.append(f"  {diff.before_captured}  →  {diff.after_captured}")
    lines.append("")

    # Overall
    dr_before = diff.overall_dead_ratio_before
    dr_after = diff.overall_dead_ratio_after
    dr_delta = dr_after - dr_before
    arrow = _arrow(dr_delta)
    lines.append(f"  Dead tuple ratio: {dr_before:.1f}% → {dr_after:.1f}% ({arrow}{abs(dr_delta):.1f}%)")

    dt_before = diff.overall_dead_tuples_before
    dt_after = diff.overall_dead_tuples_after
    dt_delta = dt_after - dt_before
    lines.append(f"  Dead tuples: {_fmt(dt_before)} → {_fmt(dt_after)} ({_signed(dt_delta)})")

    # Workers
    w_status = ""
    if diff.workers_after > diff.workers_before:
        w_status = "↑ more workers busy"
    elif diff.workers_after < diff.workers_before:
        w_status = "↓ fewer workers busy"
    lines.append(
        f"  Workers: {diff.workers_before}/{diff.max_workers} → "
        f"{diff.workers_after}/{diff.max_workers}{w_status}"
    )

    # XID
    if diff.xid_status_before != diff.xid_status_after:
        lines.append(f"  XID health: {diff.xid_status_before} → {diff.xid_status_after} ⚠")

    lines.append("")

    # Schema changes
    changed_schemas = [d for d in diff.schema_deltas if abs(d.dead_ratio_delta) > 0.5]
    if changed_schemas:
        lines.append(f"  Schema changes ({len(changed_schemas)} with notable shifts):")
        lines.append(f"  {'Schema':<30} {'Before':>8} {'After':>8} {'Delta':>10} {'Dead Δ':>10}")
        lines.append("  " + "-" * 75)
        for d in changed_schemas[:15]:
            arrow = _arrow(d.dead_ratio_delta)
            lines.append(
                f"  {d.name:<30} {d.dead_ratio_before:>7.1f}% {d.dead_ratio_after:>7.1f}% "
                f"{arrow}{abs(d.dead_ratio_delta):.1f}% {_signed(d.dead_tuples_delta):>10}"
            )
        lines.append("")

    # Table changes
    if diff.table_deltas:
        status_labels = {
            "new": "NEW",
            "worse": "WORSE",
            "better": "BETTER",
            "resolved": "RESOLVED",
        }
        lines.append(
            f"  Table changes: {diff.new_problem_tables} new, "
            f"{diff.resolved_problem_tables} resolved, "
            f"{len(diff.table_deltas) - diff.new_problem_tables - diff.resolved_problem_tables} changed"
        )
        lines.append(f"  {'Status':<10} {'Table':<50} {'Before':>8} {'After':>8}")
        lines.append("  " + "-" * 85)
        for d in diff.table_deltas[:20]:
            label = f"{d.schema}.{d.table}"
            before_str = _fmt(d.dead_tuples_before) if d.dead_tuples_before else "—"
            after_str = _fmt(d.dead_tuples_after) if d.dead_tuples_after else "—"
            lines.append(
                f"  {status_labels[d.status]:<10} {label:<50} {before_str:>8} {after_str:>8}"
            )
        if len(diff.table_deltas) > 20:
            lines.append(f"  ... and {len(diff.table_deltas) - 20} more")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Render diff to HTML
# ---------------------------------------------------------------------------

def diff_to_html(diff: SnapshotDiff) -> str:
    """Render a diff as a self-contained HTML report."""
    dr_delta = diff.overall_dead_ratio_after - diff.overall_dead_ratio_before
    overall_class = "critical" if dr_delta > 5 else "warning" if dr_delta > 1 else "ok"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Diff: {diff.before_title} → {diff.after_title}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         margin: 0; padding: 2rem; background: #f8f9fa; color: #212529; }}
  .container {{ max-width: 1100px; margin: 0 auto; }}
  h1 {{ font-size: 1.5rem; margin-bottom: 0.25rem; }}
  h2 {{ font-size: 1.1rem; margin-top: 2rem; border-bottom: 1px solid #dee2e6; padding-bottom: 0.5rem; }}
  .subtitle {{ color: #6c757d; margin-bottom: 1.5rem; }}
  .card {{ background: #fff; border-radius: 8px; padding: 1.5rem; margin-bottom: 1rem;
          box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1rem; }}
  .metric {{ text-align: center; }}
  .metric .value {{ font-size: 1.8rem; font-weight: 700; }}
  .metric .label {{ color: #6c757d; font-size: 0.85rem; }}
  .ok {{ color: #198754; }}
  .warning {{ color: #fd7e14; }}
  .critical {{ color: #dc3545; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
  th {{ text-align: left; padding: 0.5rem; border-bottom: 2px solid #dee2e6; color: #6c757d; font-size: 0.8rem; text-transform: uppercase; }}
  td {{ padding: 0.5rem; border-bottom: 1px solid #f1f3f5; }}
  .badge {{ display: inline-block; padding: 0.15rem 0.5rem; border-radius: 4px; font-size: 0.75rem; font-weight: 600; }}
  .badge-new {{ background: #f8d7da; color: #842029; }}
  .badge-worse {{ background: #fff3cd; color: #664d03; }}
  .badge-better {{ background: #d1e7dd; color: #0f5132; }}
  .badge-resolved {{ background: #cff4fc; color: #055160; }}
  .delta-up {{ color: #dc3545; }}
  .delta-down {{ color: #198754; }}
  footer {{ margin-top: 2rem; color: #6c757d; font-size: 0.8rem; text-align: center; }}
</style>
</head>
<body>
<div class="container">
  <h1>Snapshot Diff</h1>
  <p class="subtitle">{_esc(diff.before_title)} → {_esc(diff.after_title)}</p>

  <div class="card">
    <div class="grid">
      <div class="metric">
        <div class="value {_esc(overall_class)}">{_signed_delta(dr_delta)}%</div>
        <div class="label">Dead Ratio Change</div>
      </div>
      <div class="metric">
        <div class="value">{_signed(diff.overall_dead_tuples_after - diff.overall_dead_tuples_before)}</div>
        <div class="label">Dead Tuples Δ</div>
      </div>
      <div class="metric">
        <div class="value">{diff.new_problem_tables} new / {diff.resolved_problem_tables} resolved</div>
        <div class="label">Problem Tables</div>
      </div>
      <div class="metric">
        <div class="value">{diff.worsened_schemas} worse / {diff.improved_schemas} better</div>
        <div class="label">Schemas</div>
      </div>
    </div>
  </div>
"""

    # Schema deltas
    changed = [d for d in diff.schema_deltas if abs(d.dead_ratio_delta) > 0.5]
    if changed:
        html += """  <h2>Schema Changes</h2>
  <div class="card">
    <table>
      <thead><tr><th>Schema</th><th>Before</th><th>After</th><th>Δ %</th><th>Dead Δ</th></tr></thead>
      <tbody>
"""
        for d in changed[:30]:
            delta_class = "delta-up" if d.dead_ratio_delta > 0 else "delta-down"
            html += (
                f"        <tr>"
                f"<td>{_esc(d.name)}</td>"
                f"<td>{d.dead_ratio_before:.1f}%</td>"
                f"<td>{d.dead_ratio_after:.1f}%</td>"
                f"<td class=\"{delta_class}\">{_signed_delta(d.dead_ratio_delta)}%</td>"
                f"<td class=\"{delta_class}\">{_signed(d.dead_tuples_delta)}</td>"
                f"</tr>\n"
            )
        html += """      </tbody>
    </table>
  </div>
"""

    # Table deltas
    if diff.table_deltas:
        html += """  <h2>Table Changes</h2>
  <div class="card">
    <table>
      <thead><tr><th>Status</th><th>Table</th><th>Before</th><th>After</th></tr></thead>
      <tbody>
"""
        for d in diff.table_deltas[:50]:
            html += (
                f"        <tr>"
                f"<td><span class=\"badge badge-{d.status}\">{d.status}</span></td>"
                f"<td>{_esc(d.schema)}.{_esc(d.table)}</td>"
                f"<td>{_fmt(d.dead_tuples_before) if d.dead_tuples_before else '—'}</td>"
                f"<td>{_fmt(d.dead_tuples_after) if d.dead_tuples_after else '—'}</td>"
                f"</tr>\n"
            )
        html += """      </tbody>
    </table>
  </div>
"""

    html += f"""  <footer>{_esc(diff.before_captured)} → {_esc(diff.after_captured)}</footer>
</div>
</body>
</html>"""

    return html


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _arrow(delta: float) -> str:
    if delta > 0:
        return "↑+"
    if delta < 0:
        return "↓"
    return "→"


def _signed(n: int) -> str:
    if n > 0:
        return f"+{_fmt(n)}"
    if n < 0:
        return f"-{_fmt(abs(n))}"
    return "0"


def _signed_delta(f: float) -> str:
    if f > 0:
        return f"+{f:.1f}"
    if f < 0:
        return f"{f:.1f}"
    return "0.0"


def _fmt(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
