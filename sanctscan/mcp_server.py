"""SANCTSCAN MCP server — exposes scan() as an MCP tool for Cognis.Studio."""
from __future__ import annotations
from sanctscan.core import scan, to_json

def serve() -> int:
    """Start an MCP stdio server. Requires the optional 'mcp' extra:
        pip install "cognis-sanctscan[mcp]"
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except Exception:
        print("Install the MCP extra: pip install 'cognis-sanctscan[mcp]'")
        return 1
    app = FastMCP("sanctscan")

    @app.tool()
    def sanctscan_scan(target: str) -> str:
        """Screens counterparties and transactions against OFAC/EU/UN sanctions lists with fuzzy name matching and explainable hit scoring.. Returns JSON findings."""
        return to_json(scan(target))

    app.run()
    return 0
