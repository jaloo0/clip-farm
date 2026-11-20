"""Small helpers for the render half (no downloads, no whisper here)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = Path(os.environ.get("CF_MODELS", ROOT / "models"))
DEFAULTS = {"fps_cap": 60, "credit": True, "hashtags": ["#GTA6", "#gaming"]}


def settings() -> dict:
    return dict(DEFAULTS)


def say(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"::warning::{msg}" if os.environ.get("GITHUB_ACTIONS") else f"! {msg}", file=sys.stderr, flush=True)


def run(cmd: list[str], check: bool = True, timeout: float | None = None) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
    if check and p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip().splitlines()[-8:]
        raise RuntimeError(f"{Path(cmd[0]).name} failed ({p.returncode}):\n  " + "\n  ".join(tail))
    return p


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", str(text).lower()).strip("-")
    return (s[:n].rstrip("-")) or "clip"
