#!/usr/bin/env python3
"""Archive one Prefill run and collect reproducibility metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--container")
    parser.add_argument("--service-log", type=Path)
    parser.add_argument("--console-log", type=Path)
    parser.add_argument("--aisbench-output-dir", type=Path)
    parser.add_argument("--aisbench-log", type=Path)
    parser.add_argument("--aisbench-result", type=Path)
    parser.add_argument("--service-script", type=Path)
    parser.add_argument("--prefix-config", type=Path)
    parser.add_argument("--aisbench-command", type=Path)
    parser.add_argument("--metrics-before", type=Path)
    parser.add_argument("--metrics-timeseries", type=Path)
    parser.add_argument("--metrics-after", type=Path)
    parser.add_argument("--npu-before", type=Path)
    parser.add_argument("--npu-after", type=Path)
    parser.add_argument("--vllm-repo")
    parser.add_argument("--vllm-ascend-repo")
    parser.add_argument("--aisbench-repo", type=Path)
    parser.add_argument("--prefix-tool-repo", type=Path)
    parser.add_argument("--conda-env", default="ais_bench")
    return parser.parse_args()


def command_output(command: list[str]) -> dict[str, Any]:
    try:
        proc = subprocess.run(command, text=True, capture_output=True, check=False)
        return {
            "command": command,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except FileNotFoundError as exc:
        return {"command": command, "returncode": 127, "stdout": "", "stderr": str(exc)}


def save_command(path: Path, command: list[str]) -> dict[str, Any]:
    result = command_output(command)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result["stdout"] + result["stderr"], encoding="utf-8")
    return result


SENSITIVE_KEY_RE = re.compile(
    r"PASSWORD|PASSWD|TOKEN|SECRET|API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY|CREDENTIAL|COOKIE|AUTH",
    re.IGNORECASE,
)


def redact_inspect(value: Any) -> Any:
    """Redact credentials while retaining useful docker-inspect metadata."""
    if isinstance(value, dict):
        return {
            key: "<redacted>" if SENSITIVE_KEY_RE.search(str(key)) else redact_inspect(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        redacted = []
        redact_next = False
        for item in value:
            if redact_next:
                redacted.append("<redacted>")
                redact_next = False
                continue
            if isinstance(item, str) and "=" in item:
                key, item_value = item.split("=", 1)
                redacted.append(f"{key}=<redacted>" if SENSITIVE_KEY_RE.search(key) else item)
            else:
                redacted.append(redact_inspect(item))
                if isinstance(item, str) and item.startswith("-") and SENSITIVE_KEY_RE.search(item):
                    redact_next = True
        return redacted
    return value


def copy_item(source: Path | None, destination: Path, warnings: list[str]) -> None:
    if source is None:
        return
    if not source.exists():
        warnings.append(f"missing source: {source}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)
    else:
        shutil.copy2(source, destination)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_container(args: argparse.Namespace, env_dir: Path, logs_dir: Path, warnings: list[str]) -> None:
    if not args.container:
        warnings.append("container not provided; container/image/Ascend logs not collected")
        return
    inspect = command_output(["docker", "inspect", "--type", "container", args.container])
    if inspect["returncode"] != 0:
        (env_dir / "container_inspect_error.txt").write_text(
            inspect["stderr"], encoding="utf-8"
        )
        warnings.append(f"docker inspect failed for {args.container}: {inspect['stderr'].strip()}")
        return

    try:
        inspect_payload = json.loads(inspect["stdout"])
        container_info = inspect_payload[0]
        (env_dir / "container_inspect.json").write_text(
            json.dumps(redact_inspect(inspect_payload), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except (json.JSONDecodeError, IndexError):
        warnings.append("could not parse container inspect output")
        container_info = {}
    image_ref = container_info.get("Image") or container_info.get("Config", {}).get("Image")
    if image_ref:
        image_inspect = save_command(
            env_dir / "image_inspect.json", ["docker", "image", "inspect", image_ref]
        )
        if image_inspect["returncode"] != 0:
            warnings.append(f"docker image inspect failed for {image_ref}")
    else:
        warnings.append("container image reference was not present in docker inspect")

    ascend_dir = logs_dir / "ascend"
    ascend_dir.mkdir(parents=True, exist_ok=True)
    copied = command_output(
        ["docker", "cp", f"{args.container}:/root/ascend/log/.", str(ascend_dir)]
    )
    if copied["returncode"] != 0:
        warnings.append(f"docker cp Ascend logs failed: {copied['stderr'].strip()}")

    versions: dict[str, Any] = {}
    for key, repo in (("vllm", args.vllm_repo), ("vllm_ascend", args.vllm_ascend_repo)):
        if repo:
            versions[key] = {
                "commit": command_output(
                    ["docker", "exec", args.container, "git", "-C", repo, "rev-parse", "HEAD"]
                ),
                "status": command_output(
                    ["docker", "exec", args.container, "git", "-C", repo, "status", "--short"]
                ),
                "branch": command_output(
                    ["docker", "exec", args.container, "git", "-C", repo, "branch", "--show-current"]
                ),
            }
            for field in ("commit", "status", "branch"):
                if versions[key][field]["returncode"] != 0:
                    warnings.append(f"could not collect container {key} {field} from {repo}")
    versions["python_packages"] = command_output(
        [
            "docker",
            "exec",
            args.container,
            "python",
            "-c",
            "import importlib.metadata as m; "
            "names=('vllm','vllm-ascend','torch','torch-npu'); "
            "print('\\n'.join(n+'='+(m.version(n) if any(d.metadata.get('Name','').lower()==n "
            "for d in m.distributions()) else '<not-installed>') for n in names))",
        ]
    )
    if versions["python_packages"]["returncode"] != 0:
        warnings.append("could not collect container Python package versions")
    (env_dir / "container_versions.json").write_text(
        json.dumps(versions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def collect_local_tooling(args: argparse.Namespace, env_dir: Path, warnings: list[str]) -> None:
    repos = {
        "aisbench": args.aisbench_repo,
        "aisbench_auto_tools_prefix": args.prefix_tool_repo,
    }
    payload: dict[str, Any] = {}
    for name, repo in repos.items():
        if repo is None:
            warnings.append(f"{name} repository not provided; commit not collected")
            continue
        payload[name] = {
            "path": str(repo.resolve()),
            "commit": command_output(["git", "-C", str(repo), "rev-parse", "HEAD"]),
            "branch": command_output(["git", "-C", str(repo), "branch", "--show-current"]),
            "status": command_output(["git", "-C", str(repo), "status", "--short"]),
        }
        for field in ("commit", "branch", "status"):
            if payload[name][field]["returncode"] != 0:
                warnings.append(f"could not collect {name} {field} from {repo}")
    (env_dir / "benchmark_tool_versions.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    freeze = save_command(
        env_dir / "aisbench_python_packages.txt",
        ["conda", "run", "-n", args.conda_env, "python", "-m", "pip", "freeze"],
    )
    if freeze["returncode"] != 0:
        warnings.append(f"could not capture Python packages from conda env {args.conda_env}")


def main() -> None:
    args = parse_args()
    run_dir = args.run_dir.resolve()
    logs_dir = run_dir / "logs"
    config_dir = run_dir / "config"
    aisbench_dir = run_dir / "aisbench"
    metrics_dir = run_dir / "metrics"
    npu_dir = run_dir / "npu"
    env_dir = run_dir / "environment"
    for path in (logs_dir, config_dir, aisbench_dir, metrics_dir, npu_dir, env_dir):
        path.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []
    required_sources = {
        "service log": args.service_log,
        "AISBench console log": args.console_log,
        "AISBench outputs directory": args.aisbench_output_dir,
        "AISBench log": args.aisbench_log,
        "AISBench result CSV": args.aisbench_result,
        "service script": args.service_script,
        "prefix-tool config": args.prefix_config,
        "AISBench command": args.aisbench_command,
    }
    for label, source in required_sources.items():
        if source is None:
            warnings.append(f"required artifact argument not provided: {label}")
    copy_item(args.service_log, logs_dir / "service.log", warnings)
    copy_item(args.console_log, logs_dir / "aisbench_console.log", warnings)
    copy_item(args.aisbench_output_dir, aisbench_dir / "outputs", warnings)
    copy_item(args.aisbench_log, aisbench_dir / "aisbench.log", warnings)
    copy_item(args.aisbench_result, aisbench_dir / "aisbench_result.csv", warnings)
    copy_item(args.service_script, config_dir / "service_script.sh", warnings)
    copy_item(args.prefix_config, config_dir / "prefix_tool_config.py", warnings)
    copy_item(args.aisbench_command, config_dir / "aisbench_command.txt", warnings)
    copy_item(args.metrics_before, metrics_dir / "before.txt", warnings)
    copy_item(args.metrics_timeseries, metrics_dir / "timeseries.log", warnings)
    copy_item(args.metrics_after, metrics_dir / "after.txt", warnings)
    copy_item(args.npu_before, npu_dir / "before.txt", warnings)
    copy_item(args.npu_after, npu_dir / "after.txt", warnings)

    host_commands = {
        "date.txt": ["date", "--iso-8601=seconds"],
        "hostname.txt": ["hostname"],
        "uname.txt": ["uname", "-a"],
        "lscpu.txt": ["lscpu"],
        "memory.txt": ["free", "-h"],
        "disk.txt": ["df", "-h"],
        "npu_smi.txt": ["npu-smi", "info"],
        "docker_version.txt": ["docker", "version"],
    }
    for filename, command in host_commands.items():
        result = save_command(env_dir / filename, command)
        if result["returncode"] != 0:
            warnings.append(f"metadata command failed: {' '.join(command)}")

    collect_container(args, env_dir, logs_dir, warnings)
    collect_local_tooling(args, env_dir, warnings)
    (run_dir / "archive_warnings.json").write_text(
        json.dumps({"created_at": datetime.now().astimezone().isoformat(), "warnings": warnings}, indent=2)
        + "\n",
        encoding="utf-8",
    )

    manifest_lines = []
    for path in sorted(run_dir.rglob("*")):
        if path.is_file() and path.name != "manifest.sha256":
            manifest_lines.append(f"{sha256(path)}  {path.relative_to(run_dir)}")
    (run_dir / "manifest.sha256").write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")
    incomplete = any(
        warning.startswith("required artifact")
        or warning.startswith("container not provided")
        or "Ascend logs failed" in warning
        for warning in warnings
    )
    print(
        json.dumps(
            {
                "status": "incomplete" if incomplete else "ok",
                "run_dir": str(run_dir),
                "warnings": warnings,
                "sha256_manifest": str(run_dir / "manifest.sha256"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
