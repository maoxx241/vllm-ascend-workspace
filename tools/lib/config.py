from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RepoPaths:
    root: Path

    @property
    def local_overlay(self) -> Path:
        return self.root / ".workspace.local"
