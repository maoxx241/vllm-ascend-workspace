#!/usr/bin/env python3
"""Plan or configure personal development forks independently of agent skills."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from vaws_github import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
