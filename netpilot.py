#!/usr/bin/env python3
"""Compatibility executable for NetPilot's three feature areas."""
from netpilot_lib.cli import entrypoint, main, parser

if __name__ == "__main__":
    raise SystemExit(entrypoint())
