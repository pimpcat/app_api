#!/usr/bin/env python3
"""Redirige al script en ruteo/scripts/."""
import runpy
import sys

sys.exit(runpy.run_path("ruteo/scripts/diagnose_corridor_subgraph.py", run_name="__main__") or 0)
