"""Console protocol shared by the ModelScope command-line helpers."""
import os
import sys


def configure_stdio() -> None:
    if os.name == "nt":
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
