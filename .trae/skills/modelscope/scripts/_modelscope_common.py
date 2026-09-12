"""Console protocol shared by the ModelScope command-line helpers."""
import os
import sys
from pathlib import Path


def configure_stdio() -> None:
    if os.name == "nt":
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")


def file_signature(path: Path) -> list[int] | None:
    """Cheap freshness evidence; full SHA256 remains the explicit verifier's job."""
    try:
        if not path.is_file():
            return None
        info = path.stat()
    except OSError:
        return None
    return [info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino, info.st_dev]
