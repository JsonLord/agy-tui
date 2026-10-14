#!/usr/bin/env python3
"""Absolute-path, stdio-only factory MCP launcher."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.factory_local.mcp import mcp

if __name__ == "__main__":
    mcp.run()
