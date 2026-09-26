"""Load per-town configuration from config/<town>.toml."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"


def load_config(town: str) -> dict:
    with (CONFIG_DIR / f"{town}.toml").open("rb") as f:
        config = tomllib.load(f)
    config["slug"] = town
    return config
