#!/usr/bin/env python3
"""Absolute-path stdio launcher independent of client cwd."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.hybrid_local.mcp import mcp

if __name__ == "__main__":
    mcp.run()
