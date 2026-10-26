"""Where RepoPilot keeps its files on disk."""

import os
from pathlib import Path


def cache_dir() -> Path:
    """~/.cache/repopilot (honours XDG_CACHE_HOME). Not created here."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "repopilot"


def default_db_path() -> Path:
    return cache_dir() / "repopilot.db"
