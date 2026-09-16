"""Where a deployment keeps its own things.

The package is code. A *workspace* is everything that is yours: the config,
the watchlist, the database, the logs — and, if you want to override the
shipped ones, fingerprints. Naming that directory once, rather than deriving
it from where the code happens to be installed, is what lets one machine
run several deployments and lets a deployment move by moving one folder.

Resolution, first match wins:
  1. `--home` on the command line (handled in cli)
  2. `$RADAR_HOME`
  3. the current working directory
"""

from __future__ import annotations

import os
from pathlib import Path

ENV = "RADAR_HOME"

_override: Path | None = None


def set_home(path: str | os.PathLike | None) -> None:
    """Pin the workspace for this process — used by `--home`."""
    global _override
    _override = Path(path).expanduser().resolve() if path else None


def home() -> Path:
    if _override is not None:
        return _override
    env = os.environ.get(ENV)
    if env:
        return Path(env).expanduser().resolve()
    return Path.cwd()


def path(*parts: str) -> Path:
    """A path inside the workspace."""
    return home().joinpath(*parts)


def package_root() -> Path:
    return Path(__file__).resolve().parent


def describe() -> str:
    src = "--home" if _override is not None else (ENV if os.environ.get(ENV) else "cwd")
    return f"{home()}  ({src})"
