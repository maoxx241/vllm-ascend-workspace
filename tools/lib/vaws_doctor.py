from __future__ import annotations

from .config import RepoPaths
from .doctor import doctor


def run(paths: RepoPaths) -> int:
    return doctor(paths)
