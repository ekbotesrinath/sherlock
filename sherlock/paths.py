"""Central path resolution for Sherlock's on-disk state.

All persistent state (cache, dependency graphs, profiles, logs, account
memory) lives under a single ``.sherlock/`` directory. Its location is
resolved once, here:

1. ``SHERLOCK_HOME`` environment variable, if set.
2. Otherwise the repository root (the parent of the ``sherlock`` package),
   which preserves the historical repo-local ``.sherlock/`` layout when
   running from a source checkout.
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(
    os.environ.get("SHERLOCK_HOME", Path(__file__).resolve().parent.parent)
)

CONFIG_DIR = PROJECT_ROOT / ".sherlock"
LOG_DIR = CONFIG_DIR / "logs"
CACHE_DIR = CONFIG_DIR / "cache"
DEPS_DIR = CONFIG_DIR / "deps"
ACCOUNT_MEMORY_FILE = CONFIG_DIR / "account_memory.json"
DOTENV_FILE = PROJECT_ROOT / ".env"
