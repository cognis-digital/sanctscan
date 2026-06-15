"""Command-line interface for SANCTSCAN.

Examples:

  # Screen a single name against an OFAC-style CSV watchlist
  python -m sanctscan screen --watchlist demos/01-basic/watchlist.csv \\
      --name "Vladmir Putin"

  # Screen a column of names from a CSV, emit JSON for CI / piping
  python -m sanctscan screen -w watchlist.csv --input customers.csv \\
      --column full_name --format json --threshold 0.85

  # Exit code is non-zero when any hit at/above the threshold is found,
  # so it can gate a pipeline:
  python -m sanctscan screen -w wl.csv -n "Some Name" || echo "FLAGGED"
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from typing import Dict, List, Optional

from . import TOOL_NAME, TOOL_VERSION
from .core import Hit, WatchlistEntry, load_watchlist, screen_records


def _read_names_from_file(path: str, column: Optional[str]) -> List[str]:
    names: List[str] = []
    if path == "-":
        fh = sys.stdin
        close = False
    else:
        fh = open(path, "r", encoding="utf-8-sig", newline="")
        close = True
    try:
        if path.lower().endswith(".csv") or column:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                return names
            col = column
            if col is None:
                # Heuristic: first column whose header contains "name".
                for c in reader.fieldnames:
                    if "name" in c.lower():
                        col = c
                        break
                if col is None:
                    col = reader.fieldnames[0]
            cols = {c.lower(): c for c in reader.fieldnames}
            actual = cols.get(col.lower(), col)
            for row in reader:
                val = (row.get(actual) or "").strip()
                if val:
                    names.append(val)
        else:
            for line in fh:
                line = line.strip()
                if line:
                    names.append(line)
    finally:
        if close:
            fh.close()
    return names


def _format_table(results: Dict[str, List[Hit]]) -> str:
    lines: List[str] = []
    total_hits = 0
    for query, hits in results.items():
        if not hits:
            lines.append("CLEAR  %s" % query)
            continue
        for h in hits:
            total_hits += 1
            lines.append(
                "HIT    %-28s -> %-28s score=%.3f [%s/%s uid=%s]"
                % (
                    _truncate(query, 28),
                    _truncate(h.matched_name, 28),
                    h.score,
                    h.program or "-",
                    h.entity_type or "-",
                    h.uid,
                )
            )
            lines.append("           why: %s" % h.explanation)
    screened = len(results)
    flagged = sum(1 for hs in results.values() if hs)
    lines.append("")
    lines.append(
        "Summary: screened=%d flagged=%d hits=%d"
        % (screened, flagged, total_hits)
    )
    return "\n".join(lines)


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _format_json(results: Dict[str, List[Hit]]) -> str:
    payload = {
        "tool": TOOL_NAME,
        "version": TOOL_VERSION,
        "screened": len(results),
        "flagged": sum(1 for hs in results.values() if hs),
        "hits": sum(len(hs) for hs in results.values()),
        "results": [
            {
                "query": query,
                "flagged": bool(hits),
                "matches": [h.to_dict() for h in hits],
            }
            for query, hits in results.items()
        ],
    }
    return json.dumps(payload, indent=2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description=(
            "SANCTSCAN -- deterministic, auditable sanctions name-screening "
            "with explainable fuzzy matching."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--version", action="version",
        version="%s %s" % (TOOL_NAME, TOOL_VERSION),
    )
    sub = parser.add_subparsers(dest="command")

    sc = sub.add_parser(
        "screen",
        help="Screen one or more names against a sanctions watchlist.",
        description="Screen names against a watchlist (CSV or JSON).",
    )
    sc.add_argument(
        "-w", "--watchlist", required=True,
        help="Path to the watchlist file (.csv or .json).",
    )
    src = sc.add_mutually_exclusive_group(required=True)
    src.add_argument("-n", "--name", help="A single name to screen.")
    src.add_argument(
        "-i", "--input",
        help="File of names to screen (CSV, or newline-delimited text, '-' for stdin).",
    )
    sc.add_argument(
        "-c", "--column",
        help="Column name to read from a CSV input (default: auto-detect).",
    )
    sc.add_argument(
        "-t", "--threshold", type=float, default=0.80,
        help="Minimum match score in [0,1] to report a hit (default: 0.80).",
    )
    sc.add_argument(
        "--max-hits", type=int, default=None,
        help="Max hits to report per screened name (default: unlimited).",
    )
    sc.add_argument(
        "-f", "--format", choices=("table", "json"), default="table",
        help="Output format (default: table).",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command != "screen":
        parser.print_help()
        return 2

    if not 0.0 <= args.threshold <= 1.0:
        print("error: --threshold must be between 0 and 1", file=sys.stderr)
        return 2

    if args.max_hits is not None and args.max_hits < 0:
        print("error: --max-hits must be >= 0", file=sys.stderr)
        return 2

    try:
        watchlist: List[WatchlistEntry] = load_watchlist(args.watchlist)
    except (OSError, ValueError, json.JSONDecodeError, csv.Error) as exc:
        print("error: could not load watchlist: %s" % exc, file=sys.stderr)
        return 2

    if not watchlist:
        print("error: watchlist is empty or unreadable", file=sys.stderr)
        return 2

    if args.name is not None:
        names = [args.name]
    else:
        try:
            names = _read_names_from_file(args.input, args.column)
        except (OSError, csv.Error) as exc:
            print("error: could not read input: %s" % exc, file=sys.stderr)
            return 2

    if not names:
        print("error: no names to screen", file=sys.stderr)
        return 2

    results = screen_records(
        names,
        watchlist,
        threshold=args.threshold,
        max_hits_per_name=args.max_hits,
    )

    if args.format == "json":
        print(_format_json(results))
    else:
        print(_format_table(results))

    # Non-zero exit when anything is flagged, so this can gate a CI pipeline.
    flagged = any(results.values())
    return 1 if flagged else 0


if __name__ == "__main__":
    sys.exit(main())
