#!/usr/bin/env python3
from __future__ import annotations

import os
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def frontend_command(env: dict[str, str], *, platform: str | None = None) -> list[str]:
    platform = platform or os.name
    candidates = ("npm.cmd", "npm.exe", "npm") if platform == "nt" else ("npm",)
    npm = next((path for name in candidates if (path := shutil.which(name))), None)
    if not npm:
        raise RuntimeError("npm executable not found on PATH")
    return [npm, "run", "start", "--", "--hostname", env["NFM_BIND"], "--port", env["NFM_WEB_PORT"]]


def main() -> int:
    env = os.environ.copy()
    env.setdefault("NFM_BIND", "127.0.0.1")
    env.setdefault("NFM_PORT", "8789")
    env.setdefault("NFM_WEB_PORT", "8788")
    env.setdefault("NFM_API_URL", f"http://127.0.0.1:{env['NFM_PORT']}")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(name, None)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["PYTHONPATH"] = str(ROOT / "backend") + os.pathsep + env.get("PYTHONPATH", "")
    children = [
        subprocess.Popen([sys.executable, "-m", "npu_fleet_monitor"], cwd=ROOT, env=env),
        subprocess.Popen(
            frontend_command(env),
            cwd=ROOT,
            env=env,
        ),
    ]
    stopping = False

    def stop(*_: object) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        for child in children:
            if child.poll() is None:
                child.terminate()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        while not stopping:
            for child in children:
                code = child.poll()
                if code is not None:
                    stop()
                    return code
            time.sleep(0.5)
    finally:
        stop()
        deadline = time.time() + 8
        for child in children:
            if child.poll() is None:
                try:
                    child.wait(timeout=max(0.1, deadline - time.time()))
                except subprocess.TimeoutExpired:
                    child.kill()
        for child in children:
            child.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
