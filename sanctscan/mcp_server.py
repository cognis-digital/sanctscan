"""SANCTSCAN MCP server — exposes screen_name() as an MCP tool."""
from __future__ import annotations

import json
import sys


def serve() -> int:
    """Start an MCP stdio server. Requires the optional 'mcp' extra:
        pip install "cognis-sanctscan[mcp]"
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        print(
            "Install the MCP extra: pip install 'cognis-sanctscan[mcp]'",
            file=sys.stderr,
        )
        return 1

    from sanctscan.core import load_watchlist, screen_name

    app = FastMCP("sanctscan")

    @app.tool()
    def sanctscan_screen(target: str, watchlist_path: str) -> str:
        """Screen a name against a watchlist file.

        Screens a name against OFAC/EU/UN-style sanctions lists using
        fuzzy name matching with explainable hit scoring.
        Returns JSON findings.
        """
        try:
            wl = load_watchlist(watchlist_path)
        except (OSError, ValueError) as exc:
            return json.dumps({"error": str(exc)})
        hits = screen_name(target, wl)
        return json.dumps([h.to_dict() for h in hits], indent=2)

    app.run()
    return 0
