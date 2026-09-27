"""Load per-town configuration from config/<town>.toml."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
# The town every command uses unless given --town. The workflow sets TOWN once.
DEFAULT_TOWN = os.environ.get("TOWN") or "gloucester"


def load_config(town: str) -> dict:
    with (CONFIG_DIR / f"{town}.toml").open("rb") as f:
        config = tomllib.load(f)
    config["slug"] = town
    return config


def configured(config: dict, table: str) -> bool:
    """Whether the town has a source. A town without one leaves its table out of
    the config file, and the command that fetches it does nothing."""
    if table in config:
        return True
    print(f"::notice::No [{table}] in config/{config['slug']}.toml; skipping.")
    return False
