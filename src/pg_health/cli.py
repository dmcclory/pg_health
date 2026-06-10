"""CLI entry point for pg-health.

Usage:
    pg-health check --dsn postgres://user:pass@host:5432/db [--output-dir ./snapshots]
    pg-health diff snapshot1.json snapshot2.json [--format text|html]
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .collector import Collector, FileCollector, QUERIES
from .diff import diff_snapshots, diff_to_html, diff_to_text
from .models import from_dict
from .output import to_html, to_json, to_text, to_json_file, to_html_file


def main():
    parser = argparse.ArgumentParser(
        prog="pg-health",
        description="PostgreSQL autovacuum health snapshots",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- check subcommand ---
    check_p = sub.add_parser("check", help="Query a database (live or from files) and write a snapshot")
    check_p.add_argument("--url", help="PostgreSQL connection string (or set $DATABASE_URL)")
    check_p.add_argument(
        "--from-files", metavar="DIR",
        help="Read pre-collected query results from a directory (file mode)",
    )
    check_p.add_argument(
        "--output-dir", default=".", help="Directory to write snapshots (default: .)",
    )
    check_p.add_argument(
        "--format",
        choices=["json", "html", "text", "all"],
        default="all",
        help="Output format (default: all)",
    )

    # --- queries subcommand ---
    queries_p = sub.add_parser(
        "queries", help="Print the SQL queries pg-health uses",
    )
    queries_p.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )

    # --- diff subcommand ---
    diff_p = sub.add_parser("diff", help="Compare two snapshot files")
    diff_p.add_argument("before", help="Path to earlier snapshot (JSON)")
    diff_p.add_argument("after", help="Path to later snapshot (JSON)")
    diff_p.add_argument(
        "--format",
        choices=["text", "html"],
        default="text",
        help="Output format (default: text)",
    )
    diff_p.add_argument("--output", "-o", help="Write output to this file")

    args = parser.parse_args()

    if args.command == "check":
        _run_check(args)
    elif args.command == "diff":
        _run_diff(args)
    elif args.command == "queries":
        _run_queries(args)


def _run_check(args):
    """Run a health check — either live DB or from files."""
    if args.from_files:
        print(f"Reading query results from {args.from_files}...", file=sys.stderr)
        collector = FileCollector(args.from_files)
    else:
        url = args.url or os.environ.get("DATABASE_URL")
        if not url:
            print("Error: provide --url or set $DATABASE_URL.", file=sys.stderr)
            sys.exit(1)
        print(f"Connecting to {url}...", file=sys.stderr)
        collector = Collector(url)

    snapshot = collector.collect()

    outdir = Path(args.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    base = outdir / f"pg-health-{timestamp}"

    formats = [args.format] if args.format != "all" else ["json", "html", "text"]

    for fmt in formats:
        if fmt == "json":
            path = base.with_suffix(".json")
            to_json_file(snapshot, str(path))
            print(f"  → {path}", file=sys.stderr)
        elif fmt == "html":
            path = base.with_suffix(".html")
            to_html_file(snapshot, str(path))
            print(f"  → {path}", file=sys.stderr)
        elif fmt == "text":
            path = base.with_suffix(".txt")
            path.write_text(to_text(snapshot))
            print(f"  → {path}", file=sys.stderr)

    # Also print text to stdout for quick viewing
    print(to_text(snapshot))


def _run_diff(args):
    """Compare two snapshot files."""
    with open(args.before) as f:
        before = from_dict(json.load(f))
    with open(args.after) as f:
        after = from_dict(json.load(f))

    d = diff_snapshots(before, after)

    if args.format == "html":
        output = diff_to_html(d)
    else:
        output = diff_to_text(d)

    if args.output:
        Path(args.output).write_text(output)
        print(f"  → {args.output}", file=sys.stderr)
    else:
        print(output)


def _run_queries(args):
    """Print the SQL queries pg-health uses."""
    if args.format == "json":
        import json
        print(json.dumps(QUERIES, indent=2))
    else:
        for name, sql in QUERIES.items():
            print(f"-- {name}")
            print(sql)
            print()
