"""secondbrain_mcp — governed knowledge gateway MCP server (v0)."""

from .server import (PROTOCOL_VERSION, GatewayError, GatewaySession,
                     MCPServer, TOOLS, main)

__all__ = ["PROTOCOL_VERSION", "GatewayError", "GatewaySession", "MCPServer",
           "TOOLS", "main"]
__version__ = "0.1.0"
