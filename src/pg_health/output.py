"""Render a PgHealthSnapshot to JSON, HTML, or plain text."""

import json
from datetime import datetime, timezone

from .models import PgHealthSnapshot


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

def to_json(snapshot: PgHealthSnapshot, indent: int = 2) -> str:
    """Serialize a snapshot to JSON."""
    return json.dumps(snapshot.to_dict(), indent=indent, default=str)


def to_json_file(snapshot: PgHealthSnapshot, path: str, indent: int = 2) -> None:
    """Write a snapshot to a JSON file."""
    with open(path, "w") as f:
        f.write(to_json(snapshot, indent))


# ---------------------------------------------------------------------------
# Plain text (terminal-friendly)
# ---------------------------------------------------------------------------

def to_text(snapshot: PgHealthSnapshot) -> str:
    """Render a snapshot as plain text for the terminal."""
    lines: list[str] = []

    # Header
    lines.append(f"  {snapshot.title}")
    lines.append(f"  {snapshot.subtitle}")
    lines.append("")

    # Headline
    dead_ratio = snapshot.overall_dead_ratio
    lines.append(
        f"  Dead tuples: {_fmt(snapshot.total_dead_tuples)} "
        f"({dead_ratio:.1f}% of {_fmt(snapshot.total_live_tuples)} live)"
    )
    lines.append(
        f"  Schemas needing vacuum: {snapshot.schemas_needing_vacuum} / {snapshot.total_schemas}"
    )

    # Worker saturation
    ws = snapshot.worker_saturation
    sat_status = "SATURATED" if ws.saturated else "OK"
    lines.append(
        f"  Autovacuum workers: {ws.active_workers}/{ws.max_workers} [{sat_status}]"
    )
    if ws.starved_schemas:
        lines.append(
            f"  Starved schemas: {', '.join(ws.starved_schemas[:5])}"
            + (f" (+{len(ws.starved_schemas) - 5} more)" if len(ws.starved_schemas) > 5 else "")
        )

    # XID health
    xid = snapshot.xid_health
    xid_icon = {"ok": "✓", "warning": "⚠", "critical": "✗"}[xid.status]
    lines.append(f"  XID age: {_fmt(xid.xid_age)} [{xid_icon} {xid.status}]")

    # Autovacuum settings
    s = snapshot.settings
    lines.append("")
    lines.append("  Autovacuum settings:")
    lines.append(
        f"  Workers: {s.max_workers}  |  Naptime: {s.naptime}s"
        f"  |  Threshold: {s.vacuum_threshold} + {s.vacuum_scale_factor} × rows"
    )
    lines.append(
        f"  Cost delay: {s.cost_delay}ms  |  Cost limit: {s.cost_limit}"
    )

    lines.append("")

    # Schema summary
    if snapshot.schemas:
        lines.append("  Schema summary (by dead tuple %):")
        lines.append(f"  {'Schema':<30} {'Dead%':>7} {'Dead':>10} {'Live':>10} {'Needs VAC':>10} {'Last AV':>20}")
        lines.append("  " + "-" * 95)
        for s in snapshot.schemas:
            last_av = _fmt_time(s.oldest_table_last_autovacuum) if s.oldest_table_last_autovacuum else "never"
            lines.append(
                f"  {s.name:<30} {s.dead_tuple_ratio:>6.1f}% "
                f"{_fmt(s.total_dead_tuples):>10} {_fmt(s.total_live_tuples):>10} "
                f"{s.tables_needing_vacuum:>10} {last_av:>20}"
            )
        lines.append("")

    # Tables needing attention
    if snapshot.tables_needing_attention:
        lines.append(f"  Tables needing attention ({len(snapshot.tables_needing_attention)}):")
        lines.append(f"  {'Table':<50} {'Dead':>8} {'Dead%':>7} {'Avg AV interval':>16} {'Reason'}")
        lines.append("  " + "-" * 130)
        for t in snapshot.tables_needing_attention[:20]:
            label = f"{t.schema}.{t.table}"
            avg_interval = _avg_interval(t.autovacuum_count, snapshot.stats_reset, snapshot.captured_at)
            lines.append(
                f"  {label:<50} {_fmt(t.dead_tuples):>8} {t.dead_tuple_ratio:>6.1f}% {avg_interval:>16} {t.reason}"
            )
        if len(snapshot.tables_needing_attention) > 20:
            lines.append(f"  ... and {len(snapshot.tables_needing_attention) - 20} more")
        lines.append("")

    # Running vacuums
    if snapshot.running_vacuums:
        lines.append(f"  Running vacuums ({len(snapshot.running_vacuums)}):")
        for v in snapshot.running_vacuums:
            lines.append(
                f"  {v.schema}.{v.table} — {v.phase} "
                f"({v.progress_pct:.0f}%, {v.duration_seconds}s elapsed)"
            )
        lines.append("")

    # Long-running queries
    if snapshot.long_running_queries:
        lines.append(f"  Long-running queries ({len(snapshot.long_running_queries)}):")
        for q in snapshot.long_running_queries[:10]:
            blocker = " [BLOCKS VACUUM]" if q.blocks_vacuum else ""
            query_preview = q.query[:80].replace("\n", " ")
            lines.append(
                f"  PID {q.pid} · {q.user}@{q.schema or 'system'} · "
                f"{q.duration_seconds}s · {q.state}{blocker} · {query_preview}"
            )
        lines.append("")

    # Footer
    lines.append(f"  Captured at {snapshot.captured_at}")
    if snapshot.stats_reset:
        lines.append(f"  Stats since {_fmt_time(snapshot.stats_reset)}")
    if snapshot.previous_snapshot_ref:
        lines.append(f"  Reference: {snapshot.previous_snapshot_ref}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def to_html(snapshot: PgHealthSnapshot) -> str:
    """Render a snapshot as a self-contained HTML report."""
    dead_ratio = snapshot.overall_dead_ratio
    dead_severity = _severity(dead_ratio)
    ws = snapshot.worker_saturation

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{snapshot.title}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         margin: 0; padding: 2rem; background: #f8f9fa; color: #212529; }}
  .container {{ max-width: 1100px; margin: 0 auto; }}
  h1 {{ font-size: 1.5rem; margin-bottom: 0.25rem; }}
  h2 {{ font-size: 1.1rem; margin-top: 2rem; border-bottom: 1px solid #dee2e6; padding-bottom: 0.5rem; }}
  .subtitle {{ color: #6c757d; margin-bottom: 1.5rem; }}
  .card {{ background: #fff; border-radius: 8px; padding: 1.5rem; margin-bottom: 1rem;
          box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; }}
  .metric {{ text-align: center; }}
  .metric .value {{ font-size: 2rem; font-weight: 700; }}
  .metric .label {{ color: #6c757d; font-size: 0.85rem; }}
  .ok {{ color: #198754; }}
  .warning {{ color: #fd7e14; }}
  .critical {{ color: #dc3545; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
  th {{ text-align: left; padding: 0.5rem; border-bottom: 2px solid #dee2e6; color: #6c757d; font-size: 0.8rem; text-transform: uppercase; }}
  td {{ padding: 0.5rem; border-bottom: 1px solid #f1f3f5; }}
  tr:hover td {{ background: #f8f9fa; }}
  .badge {{ display: inline-block; padding: 0.15rem 0.5rem; border-radius: 4px; font-size: 0.75rem; font-weight: 600; }}
  .badge-ok {{ background: #d1e7dd; color: #0f5132; }}
  .badge-warning {{ background: #fff3cd; color: #664d03; }}
  .badge-critical {{ background: #f8d7da; color: #842029; }}
  .reason {{ color: #dc3545; }}
  footer {{ margin-top: 2rem; color: #6c757d; font-size: 0.8rem; text-align: center; }}
</style>
</head>
<body>
<div class="container">
  <h1>{_esc(snapshot.title)}</h1>
  <p class="subtitle">{_esc(snapshot.subtitle)}</p>

  <div class="card">
    <div class="grid">
      <div class="metric">
        <div class="value {_severity_class(dead_severity)}">{dead_ratio:.1f}%</div>
        <div class="label">Dead Tuple Ratio</div>
      </div>
      <div class="metric">
        <div class="value">{_fmt(snapshot.total_dead_tuples)}</div>
        <div class="label">Dead Tuples</div>
      </div>
      <div class="metric">
        <div class="value {_severity_class('critical' if ws.saturated else 'ok')}">{ws.active_workers}/{ws.max_workers}</div>
        <div class="label">AV Workers{' — SATURATED' if ws.saturated else ''}</div>
      </div>
      <div class="metric">
        <div class="value"><span class="badge badge-{snapshot.xid_health.status}">{snapshot.xid_health.status}</span></div>
        <div class="label">XID Age: {_fmt(snapshot.xid_health.xid_age)}</div>
      </div>
    </div>
  </div>

  <h2>Autovacuum Settings</h2>
  <div class="card">
    <div class="grid">
      <div class="metric">
        <div class="value">{snapshot.settings.max_workers}</div>
        <div class="label">Max Workers</div>
      </div>
      <div class="metric">
        <div class="value">{snapshot.settings.naptime}s</div>
        <div class="label">Naptime</div>
      </div>
      <div class="metric">
        <div class="value">{snapshot.settings.vacuum_threshold} + {snapshot.settings.vacuum_scale_factor}×rows</div>
        <div class="label">Vacuum Trigger</div>
      </div>
      <div class="metric">
        <div class="value">{snapshot.settings.cost_delay}ms / {snapshot.settings.cost_limit}</div>
        <div class="label">Cost Delay / Limit</div>
      </div>
    </div>
  </div>
"""

    # Schema table
    if snapshot.schemas:
        html += """  <h2>Schemas</h2>
  <div class="card">
    <table>
      <thead><tr><th>Schema</th><th>Tables</th><th>Live</th><th>Dead</th><th>Dead %</th><th>Needs VAC</th><th>Last AV (oldest)</th></tr></thead>
      <tbody>
"""
        for s in snapshot.schemas:
            row_class = f' class="{_severity_class(_severity(s.dead_tuple_ratio))}"' if s.dead_tuple_ratio > 10 else ""
            last_av = _fmt_time(s.oldest_table_last_autovacuum) if s.oldest_table_last_autovacuum else "never"
            html += (
                f"        <tr{row_class}>"
                f"<td>{_esc(s.name)}</td>"
                f"<td>{s.table_count}</td>"
                f"<td>{_fmt(s.total_live_tuples)}</td>"
                f"<td>{_fmt(s.total_dead_tuples)}</td>"
                f"<td>{s.dead_tuple_ratio:.1f}%</td>"
                f"<td>{s.tables_needing_vacuum}</td>"
                f"<td>{_esc(last_av)}</td>"
                f"</tr>\n"
            )
        html += """      </tbody>
    </table>
  </div>
"""

    # Tables needing attention
    if snapshot.tables_needing_attention:
        html += f"""  <h2>Tables Needing Attention ({len(snapshot.tables_needing_attention)})</h2>
  <div class="card">
    <table>
      <thead><tr><th>Table</th><th>Live</th><th>Dead</th><th>Dead %</th><th>HOT Ratio</th><th>Last AV</th><th>Avg AV interval</th><th>Reason</th></tr></thead>
      <tbody>
"""
        for t in snapshot.tables_needing_attention[:50]:
            hot = f"{t.hot_update_ratio:.0%}" if t.hot_update_ratio > 0 else "n/a"
            last_av = _fmt_time(t.last_autovacuum) if t.last_autovacuum else "never"
            avg = _avg_interval(t.autovacuum_count, snapshot.stats_reset, snapshot.captured_at)
            html += (
                f"        <tr>"
                f"<td>{_esc(t.schema)}.{_esc(t.table)}</td>"
                f"<td>{_fmt(t.live_tuples)}</td>"
                f"<td>{_fmt(t.dead_tuples)}</td>"
                f"<td>{t.dead_tuple_ratio:.1f}%</td>"
                f"<td>{hot}</td>"
                f"<td>{_esc(last_av)}</td>"
                f"<td>{_esc(avg)}</td>"
                f"<td class=\"reason\">{_esc(t.reason)}</td>"
                f"</tr>\n"
            )
        html += """      </tbody>
    </table>
  </div>
"""

    # Running vacuums
    if snapshot.running_vacuums:
        html += f"""  <h2>Running Vacuums ({len(snapshot.running_vacuums)})</h2>
  <div class="card">
    <table>
      <thead><tr><th>Table</th><th>Phase</th><th>Progress</th><th>Elapsed</th><th>Dead Removed</th></tr></thead>
      <tbody>
"""
        for v in snapshot.running_vacuums:
            html += (
                f"        <tr>"
                f"<td>{_esc(v.schema)}.{_esc(v.table)}</td>"
                f"<td>{_esc(v.phase)}</td>"
                f"<td>{v.progress_pct:.0f}%</td>"
                f"<td>{v.duration_seconds}s</td>"
                f"<td>{_fmt(v.dead_tuples_removed)}</td>"
                f"</tr>\n"
            )
        html += """      </tbody>
    </table>
  </div>
"""

    # Long-running queries
    if snapshot.long_running_queries:
        html += f"""  <h2>Long-Running Queries ({len(snapshot.long_running_queries)})</h2>
  <div class="card">
    <table>
      <thead><tr><th>PID</th><th>User</th><th>Duration</th><th>State</th><th>Blocks VAC</th><th>Query</th></tr></thead>
      <tbody>
"""
        for q in snapshot.long_running_queries[:20]:
            blocker = "Yes" if q.blocks_vacuum else "No"
            query_preview = _esc(q.query[:120].replace("\n", " "))
            html += (
                f"        <tr>"
                f"<td>{q.pid}</td>"
                f"<td>{_esc(q.user)}</td>"
                f"<td>{q.duration_seconds}s</td>"
                f"<td>{_esc(q.state)}</td>"
                f"<td>{blocker}</td>"
                f"<td style=\"font-family:monospace;font-size:0.8rem;\">{query_preview}</td>"
                f"</tr>\n"
            )
        html += """      </tbody>
    </table>
  </div>
"""

    html += f"""  <footer>
    Captured at {_esc(snapshot.captured_at)}
    {f"· Stats since {_esc(_fmt_time(snapshot.stats_reset))}" if snapshot.stats_reset else ""}
  </footer>
</div>
</body>
</html>"""

    return html


def to_html_file(snapshot: PgHealthSnapshot, path: str) -> None:
    """Write a snapshot to an HTML file."""
    with open(path, "w") as f:
        f.write(to_html(snapshot))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _severity(ratio: float) -> str:
    if ratio >= 20:
        return "critical"
    if ratio >= 5:
        return "warning"
    return "ok"


def _severity_class(sev: str) -> str:
    return sev


def _fmt(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _fmt_time(ts: str) -> str:
    """Make a timestamp human-friendly."""
    try:
        # Handle ISO-8601 with timezone
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - dt
        if delta.days > 0:
            return f"{delta.days}d ago"
        hours = delta.seconds // 3600
        if hours > 0:
            return f"{hours}h ago"
        minutes = delta.seconds // 60
        return f"{minutes}m ago"
    except (ValueError, TypeError):
        return ts


def _avg_interval(count: int, stats_reset: str | None, captured_at: str) -> str:
    """Calculate average autovacuum interval since stats reset."""
    if not stats_reset or count == 0:
        return "—"
    try:
        reset_dt = datetime.fromisoformat(stats_reset.replace("Z", "+00:00"))
        cap_dt = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        total_seconds = (cap_dt - reset_dt).total_seconds()
        avg_seconds = total_seconds / count
        if avg_seconds >= 86400:
            return f"{avg_seconds / 86400:.1f}d"
        if avg_seconds >= 3600:
            return f"{avg_seconds / 3600:.1f}h"
        if avg_seconds >= 60:
            return f"{avg_seconds / 60:.0f}m"
        return f"{avg_seconds:.0f}s"
    except (ValueError, TypeError):
        return "—"


def _esc(s: str) -> str:
    """Minimal HTML escaping."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
