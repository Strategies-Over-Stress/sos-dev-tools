"""Shared .env loader — walks up from CWD to find the nearest .env file."""

import os
import sys
from pathlib import Path


def find_env():
    """Walk up from CWD to find the nearest .env file."""
    current = Path.cwd()
    for parent in [current, *current.parents]:
        env_path = parent / ".env"
        if env_path.exists():
            return env_path
    return None


def load_env():
    """Load the nearest .env file into os.environ."""
    env_path = find_env()
    if not env_path:
        return  # No .env found — rely on actual environment variables

    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)
