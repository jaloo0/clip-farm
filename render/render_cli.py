#!/usr/bin/env python3
"""python render_cli.py <folder with *.job.json + *.mp4> <output folder> [--layout auto|stack|fullcam|gameplay] [--only c01,c02]"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render.render import render_dir  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("inp"); ap.add_argument("out")
ap.add_argument("--layout", default="auto", choices=["auto", "stack", "fullcam", "gameplay"])
ap.add_argument("--only", default=None)
a = ap.parse_args()
rep = render_dir(Path(a.inp), Path(a.out), a.layout, a.only)
sys.exit(1 if rep and all(not r.get("file") for r in rep) else 0)
