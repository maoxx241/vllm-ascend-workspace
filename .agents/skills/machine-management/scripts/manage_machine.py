#!/usr/bin/env python3
"""Low-level deterministic helpers for machine-management.

These helpers keep remote host/container workflows compact and predictable so
higher-level task wrappers can rely on scripts instead of constructing long SSH
heredocs in the conversation. Prefer `machine_add.py`, `machine_verify.py`,
`machine_repair.py`, and `machine_remove.py` for normal agent-facing work. All
subcommands print JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Sequence


IMAGE_REGISTRY_NJU = "quay.nju.edu.cn/ascend/vllm-ascend"
IMAGE_REGISTRY_OFFICIAL = "quay.io/ascend/vllm-ascend"
IMAGE_REGISTRY_MIRRORS = (IMAGE_REGISTRY_NJU, IMAGE_REGISTRY_OFFICIAL)
IMAGE_SELECTOR_MAIN = "main"
IMAGE_SELECTOR_STABLE = "stable"
IMAGE_SELECTOR_ALIASES = {
    IMAGE_SELECTOR_MAIN: IMAGE_SELECTOR_MAIN,
    "main-branch": IMAGE_SELECTOR_MAIN,
    IMAGE_SELECTOR_STABLE: IMAGE_SELECTOR_STABLE,
    "release": IMAGE_SELECTOR_STABLE,
    "latest-release": IMAGE_SELECTOR_STABLE,
    "latest-official": IMAGE_SELECTOR_STABLE,
}
LEGACY_IMAGE_SELECTORS = {"auto"}
FORBIDDEN_IMAGE_TAGS = {"latest"}
DEFAULT_IMAGE = IMAGE_SELECTOR_MAIN
DEFAULT_IMAGE_CANDIDATES = tuple(f"{repo}:main" for repo in IMAGE_REGISTRY_MIRRORS)
LATEST_RELEASE_API = "https://api.github.com/repos/vllm-project/vllm-ascend/releases/latest"
LATEST_RELEASE_PAGE = "https://github.com/vllm-project/vllm-ascend/releases/latest"
LATEST_RELEASE_TIMEOUT_SECONDS = 10
IMAGE_RESOLVER_USER_AGENT = "vaws-machine-management/1.0"
DEFAULT_WORKDIR = "/vllm-workspace"
DEFAULT_HOST_USER = "root"
DEFAULT_HOST_PORT = 22
DEFAULT_PORT_RANGE = "46000:46999"
DEFAULT_KNOWN_HOSTS = pathlib.Path.home() / ".ssh" / "known_hosts"
SENTINEL = "__VAWS_JSON__="
PROGRESS_SENTINEL = "__VAWS_PROGRESS__="
DEFAULT_PASSWORD_ENV = "VAWS_SSH_PASSWORD"
DEFAULT_PROBE_TIMEOUT_SECONDS = 60
DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS = 1800
DEFAULT_SMOKE_TIMEOUT_SECONDS = 240
DEFAULT_REMOTE_TIMEOUT_GRACE_SECONDS = 8
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class MachineManagementError(RuntimeError):
    """Raised for deterministic, user-facing failures."""


@dataclass(frozen=True)
class SshTarget:
    host: str
    user: str = DEFAULT_HOST_USER
    port: int = DEFAULT_HOST_PORT


@dataclass(frozen=True)
class ImageResolution:
    selector: str
    requested: str
    policy: str
    candidates: tuple[str, ...]
    resolved_tag: str | None = None
    mirror_order: tuple[str, ...] = IMAGE_REGISTRY_MIRRORS


def print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def image_candidates_for_tag(tag: str) -> tuple[str, ...]:
    return tuple(f"{repo}:{tag}" for repo in IMAGE_REGISTRY_MIRRORS)


def docker_ref_tag(ref: str) -> str | None:
    if "@" in ref:
        return None
    last_slash = ref.rfind("/")
    last_colon = ref.rfind(":")
    if last_colon > last_slash:
        return ref[last_colon + 1 :]
    return None


def require_explicit_image_ref(ref: str) -> str:
    candidate = ref.strip()
    if not candidate:
        raise MachineManagementError(
            "image reference is empty; choose `main`, `stable`, or an explicit non-latest image reference"
        )
    normalized = candidate.lower()
    if normalized in LEGACY_IMAGE_SELECTORS:
        raise MachineManagementError(
            "legacy image selector `auto` is no longer allowed; ask the user to choose `main`, `stable`, or a concrete image reference"
        )
    if normalized in IMAGE_SELECTOR_ALIASES:
        raise MachineManagementError(
            "semantic image selectors must be handled directly, not mixed into a custom candidate list"
        )
    if "@" in candidate:
        return candidate
    tag = docker_ref_tag(candidate)
    if tag is None:
        raise MachineManagementError(
            "explicit image references must include a concrete tag or digest; bare repositories implicitly resolve to `latest` and are not allowed"
        )
    if tag.lower() in FORBIDDEN_IMAGE_TAGS:
        raise MachineManagementError(
            "the moving `latest` tag is not allowed for managed machine bootstrap; choose `main`, `stable`, or a concrete version tag"
        )
    return candidate


def fetch_latest_release_tag(timeout_seconds: int = LATEST_RELEASE_TIMEOUT_SECONDS) -> str:
    errors: list[str] = []
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": IMAGE_RESOLVER_USER_AGENT,
    }
    api_request = urllib.request.Request(LATEST_RELEASE_API, headers=headers)
    try:
        with urllib.request.urlopen(api_request, timeout=timeout_seconds) as response:
            payload = json.load(response)
        tag = str(payload.get("tag_name") or "").strip()
        if tag and not payload.get("draft") and not payload.get("prerelease"):
            return tag
        errors.append("GitHub API did not return a final release tag")
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        errors.append(f"GitHub API: {exc}")

    page_request = urllib.request.Request(LATEST_RELEASE_PAGE, headers={"User-Agent": IMAGE_RESOLVER_USER_AGENT})
    try:
        with urllib.request.urlopen(page_request, timeout=timeout_seconds) as response:
            final_url = response.geturl()
        match = re.search(r"/tag/([^/?#]+)$", final_url)
        if match:
            return urllib.parse.unquote(match.group(1))
        errors.append("release redirect did not expose a tag name")
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        errors.append(f"GitHub releases page: {exc}")

    detail = "; ".join(errors)
    raise MachineManagementError(
        "could not resolve the latest official vllm-ascend release tag; retry later, choose `main`, or pass an explicit image reference"
        + (f" ({detail})" if detail else "")
    )


def resolve_image_request(spec: str) -> ImageResolution:
    requested = spec.strip()
    if not requested:
        raise MachineManagementError(
            "image selector is missing; choose `main`, `stable`, or an explicit non-latest image reference"
        )
    normalized = requested.lower()
    selector = IMAGE_SELECTOR_ALIASES.get(normalized)
    if selector == IMAGE_SELECTOR_MAIN:
        return ImageResolution(
            selector=IMAGE_SELECTOR_MAIN,
            requested=requested,
            policy="main-branch",
            candidates=image_candidates_for_tag(IMAGE_SELECTOR_MAIN),
            resolved_tag=IMAGE_SELECTOR_MAIN,
        )
    if selector == IMAGE_SELECTOR_STABLE:
        release_tag = fetch_latest_release_tag()
        return ImageResolution(
            selector=IMAGE_SELECTOR_STABLE,
            requested=requested,
            policy="latest-official-release",
            candidates=image_candidates_for_tag(release_tag),
            resolved_tag=release_tag,
        )

    candidates = tuple(require_explicit_image_ref(item) for item in requested.split(",") if item.strip())
    if not candidates:
        raise MachineManagementError(
            "image selector is empty; choose `main`, `stable`, or an explicit non-latest image reference"
        )
    return ImageResolution(
        selector="explicit",
        requested=requested,
        policy="explicit",
        candidates=candidates,
    )


def image_request_payload(spec: str) -> dict[str, Any]:
    resolution = resolve_image_request(spec)
    return {
        "requested": resolution.requested,
        "selector": resolution.selector,
        "policy": resolution.policy,
        "candidates": list(resolution.candidates),
        "resolved_tag": resolution.resolved_tag,
        "mirror_order": list(resolution.mirror_order),
    }


def run_local(
    cmd: Sequence[str],
    *,
    input_text: str | None = None,
    check: bool = False,
    env: dict[str, str] | None = None,
    stdin_source: int | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            list(cmd),
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            stdin=stdin_source,
        )
    except FileNotFoundError as exc:
        raise MachineManagementError(f"required local command not found: {cmd[0]}") from exc
    if check and proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "command failed"
        raise MachineManagementError(detail)
    return proc


def run_local_interactive(cmd: Sequence[str]) -> int:
    try:
        proc = subprocess.run(list(cmd))
    except FileNotFoundError as exc:
        raise MachineManagementError(f"required local command not found: {cmd[0]}") from exc
    return proc.returncode


def shell_join(cmd: Sequence[str]) -> str:
    items = list(cmd)
    if os.name == "nt":
        return subprocess.list2cmdline(items)
    return shlex.join(items)


def remote_shell_command(argv: Sequence[str]) -> str:
    items = [str(item) for item in argv]
    return " ".join(shlex.quote(item) for item in items)


def ssh_command(
    target: SshTarget,
    *,
    batch_mode: bool = True,
    extra_options: Sequence[str] = (),
    identity_file: pathlib.Path | None = None,
) -> list[str]:
    command = [
        "ssh",
        "-o",
        f"BatchMode={'yes' if batch_mode else 'no'}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "LogLevel=ERROR",
        "-o",
        "ConnectTimeout=10",
    ]
    if identity_file is not None:
        command.extend(["-i", str(identity_file), "-o", "IdentitiesOnly=yes"])
    for option in extra_options:
        command.extend(["-o", option])
    command.extend(["-p", str(target.port), f"{target.user}@{target.host}"])
    return command


def validate_env_name(name: str) -> str:
    if not ENV_NAME_PATTERN.fullmatch(name):
        raise MachineManagementError(f"invalid environment variable name: {name!r}")
    return name


def private_key_for_public_key(path: pathlib.Path | None) -> pathlib.Path | None:
    if path is None:
        return None
    candidate = path.with_suffix("") if path.suffix == ".pub" else path
    if candidate.exists():
        return candidate.resolve()
    return None


def read_password_value(args: argparse.Namespace) -> tuple[str | None, str | None, str]:
    if getattr(args, "password", None) is not None:
        return args.password, "literal", DEFAULT_PASSWORD_ENV
    if getattr(args, "password_env", None):
        env_name = validate_env_name(args.password_env)
        value = os.environ.get(env_name)
        if value is None:
            raise MachineManagementError(f"environment variable {env_name!r} is not set")
        return value, f"env:{env_name}", env_name
    if getattr(args, "password_stdin", False):
        value = sys.stdin.read()
        if value is None:
            value = ""
        value = value.rstrip("\r\n")
        if not value:
            raise MachineManagementError("no password was received on stdin")
        return value, "stdin", DEFAULT_PASSWORD_ENV
    return None, None, DEFAULT_PASSWORD_ENV


def build_authorized_keys_remote_command(public_key: str) -> str:
    quoted_key = shlex.quote(public_key)
    return (
        "umask 077; "
        "mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys; "
        "chmod 700 ~/.ssh; "
        "chmod 600 ~/.ssh/authorized_keys; "
        f"grep -qxF {quoted_key} ~/.ssh/authorized_keys 2>/dev/null || "
        f"printf '%s\n' {quoted_key} >> ~/.ssh/authorized_keys"
    )


def write_askpass_helper(temp_dir: pathlib.Path, env_name: str) -> pathlib.Path:
    env_name = validate_env_name(env_name)
    if os.name == "nt":
        helper_py = temp_dir / "askpass.py"
        helper_py.write_text(
            "import os, sys\n"
            f"sys.stdout.write(os.environ.get({env_name!r}, '') + '\n')\n",
            encoding="utf-8",
        )
        helper = temp_dir / "askpass.cmd"
        helper.write_text(
            "@echo off\r\n"
            "setlocal\r\n"
            f"\"{sys.executable}\" \"{helper_py}\"\r\n",
            encoding="utf-8",
        )
    else:
        helper = temp_dir / "askpass.sh"
        helper.write_text(
            f"#!/bin/sh\nprintf '%s\\n' \"${{{env_name}}}\"\n",
            encoding="utf-8",
        )
        helper.chmod(0o700)
    return helper


def run_with_askpass(
    cmd: Sequence[str],
    *,
    password: str,
    env_name: str = DEFAULT_PASSWORD_ENV,
) -> subprocess.CompletedProcess[str]:
    env_name = validate_env_name(env_name)
    temp_dir = pathlib.Path(tempfile.mkdtemp(prefix="vaws-askpass-"))
    try:
        helper = write_askpass_helper(temp_dir, env_name)
        env = os.environ.copy()
        env[env_name] = password
        env["SSH_ASKPASS"] = str(helper)
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env.setdefault("DISPLAY", "vaws:0")
        return run_local(cmd, env=env, stdin_source=subprocess.DEVNULL)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@dataclass
class RemoteResult:
    target: SshTarget
    returncode: int
    stdout: str
    stderr: str
    payload: dict[str, Any] | None
    timed_out: bool = False
    timeout_seconds: int | None = None
    progress_events: list[dict[str, Any]] | None = None


def parse_sentinel(stdout: str) -> dict[str, Any] | None:
    payload: dict[str, Any] | None = None
    for line in stdout.splitlines():
        if line.startswith(SENTINEL):
            try:
                payload = json.loads(line[len(SENTINEL) :])
            except json.JSONDecodeError as exc:
                payload = {"success": False, "error": f"invalid remote JSON: {exc}"}
    return payload


def parse_progress_event(line: str) -> dict[str, Any] | None:
    if not line.startswith(PROGRESS_SENTINEL):
        return None
    try:
        event = json.loads(line[len(PROGRESS_SENTINEL) :])
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None
    return event


def format_progress_event(event: dict[str, Any], *, target: SshTarget | None = None) -> str:
    phase = str(event.get("phase") or "remote")
    message = str(event.get("message") or event.get("status") or "running")
    expected = event.get("expected_seconds")
    if isinstance(expected, int) and expected > 0:
        message = f"{message} (~{expected}s)"
    host = f" {target.host}" if target is not None else ""
    return f"[vaws{host}][{phase}] {message}"


def emit_progress_event(event: dict[str, Any], *, target: SshTarget | None = None) -> None:
    sys.stderr.write(format_progress_event(event, target=target) + "\n")
    sys.stderr.flush()


def run_remote_script(
    target: SshTarget,
    script: str,
    *,
    args: Sequence[str] = (),
    batch_mode: bool = True,
    timeout_seconds: int | None = None,
    stream_progress: bool = True,
) -> RemoteResult:
    remote_cmd = remote_shell_command(["bash", "-s", "--", *args])
    cmd = ssh_command(target, batch_mode=batch_mode) + [remote_cmd]
    try:
        proc = subprocess.Popen(
            list(cmd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise MachineManagementError(f"required local command not found: {cmd[0]}") from exc

    assert proc.stdin is not None
    proc.stdin.write(script)
    proc.stdin.close()

    q: queue.Queue[tuple[str, str | None]] = queue.Queue()
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    progress_events: list[dict[str, Any]] = []

    def reader(stream_name: str, pipe: Any) -> None:
        try:
            for line in pipe:
                q.put((stream_name, line))
        finally:
            q.put((stream_name, None))

    threads = [
        threading.Thread(target=reader, args=("stdout", proc.stdout), daemon=True),
        threading.Thread(target=reader, args=("stderr", proc.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()

    done_streams: set[str] = set()
    deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
    timed_out = False

    while len(done_streams) < 2 or proc.poll() is None:
        if deadline is not None and time.monotonic() >= deadline and proc.poll() is None:
            timed_out = True
            proc.kill()
            break
        wait_timeout = 0.2
        if deadline is not None:
            wait_timeout = max(0.01, min(wait_timeout, deadline - time.monotonic()))
        try:
            stream_name, line = q.get(timeout=wait_timeout)
        except queue.Empty:
            continue
        if line is None:
            done_streams.add(stream_name)
            continue
        if stream_name == "stdout":
            stdout_parts.append(line)
        else:
            stderr_parts.append(line)
        event = parse_progress_event(line)
        if event is not None:
            progress_events.append(event)
            if stream_progress:
                emit_progress_event(event, target=target)

    try:
        proc.wait(timeout=DEFAULT_REMOTE_TIMEOUT_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=DEFAULT_REMOTE_TIMEOUT_GRACE_SECONDS)

    for thread in threads:
        thread.join(timeout=1)

    while True:
        try:
            stream_name, line = q.get_nowait()
        except queue.Empty:
            break
        if line is None:
            continue
        if stream_name == "stdout":
            stdout_parts.append(line)
        else:
            stderr_parts.append(line)
        event = parse_progress_event(line)
        if event is not None and event not in progress_events:
            progress_events.append(event)
            if stream_progress:
                emit_progress_event(event, target=target)

    stdout = "".join(stdout_parts)
    stderr = "".join(stderr_parts)
    return RemoteResult(
        target=target,
        returncode=proc.returncode or 0,
        stdout=stdout,
        stderr=stderr,
        payload=parse_sentinel(stdout),
        timed_out=timed_out,
        timeout_seconds=timeout_seconds,
        progress_events=progress_events,
    )


def compact_failure_tail(text: str, *, max_lines: int = 20, max_chars: int = 1600) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    lines = stripped.splitlines()
    tail = "\n".join(lines[-max_lines:])
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    return tail


def assert_remote_success(result: RemoteResult, *, require_payload: bool = True) -> dict[str, Any]:
    payload = dict(result.payload or {})
    if result.progress_events:
        payload.setdefault("progress_events", result.progress_events)
    if result.timeout_seconds is not None:
        transport = payload.setdefault("transport", {})
        transport.setdefault("timeout_seconds", result.timeout_seconds)
        transport.setdefault("timed_out", result.timed_out)

    if result.timed_out:
        if result.payload is not None and result.payload.get("success") is True:
            transport = payload.setdefault("transport", {})
            transport.update({
                "timeout_seconds": result.timeout_seconds,
                "timed_out": True,
                "timeout_recovered_after_payload": True,
                "returncode": result.returncode,
            })
            return payload
        detail = f"remote command timed out after {result.timeout_seconds}s"
        if result.progress_events:
            detail += f"; last progress: {format_progress_event(result.progress_events[-1], target=result.target)}"
        stderr_tail = compact_failure_tail(result.stderr)
        if stderr_tail:
            detail += f"\n--- stderr tail ---\n{stderr_tail}"
        raise MachineManagementError(detail)

    if require_payload and result.payload is None:
        detail = result.stderr.strip() or result.stdout.strip() or "remote command produced no JSON payload"
        raise MachineManagementError(detail)
    if result.returncode != 0:
        stderr_tail = compact_failure_tail(result.stderr)
        if result.payload and result.payload.get("error"):
            base = str(result.payload["error"])
            if stderr_tail:
                raise MachineManagementError(f"{base}\n--- stderr tail ---\n{stderr_tail}")
            raise MachineManagementError(base)
        detail = result.stderr.strip() or result.stdout.strip() or "remote command failed"
        raise MachineManagementError(detail)
    return payload


def find_public_key(explicit: str | None = None) -> pathlib.Path:
    if explicit:
        path = pathlib.Path(explicit).expanduser().resolve()
        if not path.exists():
            raise MachineManagementError(f"public key file not found: {path}")
        return path

    candidates = [
        pathlib.Path.home() / ".ssh" / "id_ed25519.pub",
        pathlib.Path.home() / ".ssh" / "id_rsa.pub",
        pathlib.Path.home() / ".ssh" / "id_ecdsa.pub",
    ]
    for path in candidates:
        if path.exists():
            return path.resolve()
    raise MachineManagementError(
        "no local public key found; pass --public-key-file or create ~/.ssh/id_ed25519.pub"
    )


def load_public_key(path: pathlib.Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text.startswith("ssh-"):
        raise MachineManagementError(f"not a valid SSH public key: {path}")
    return text


def render_host_probe_script() -> str:
    template = r'''#!/usr/bin/env bash
set -euo pipefail
image_request_json="$1"
port_range="$2"
prefix="$3"
py=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    py="$cand"
    break
  fi
done
if [ -z "$py" ]; then
  echo "__SENTINEL__{\"success\": false, \"error\": \"python not found on remote host\"}"
  exit 3
fi
"$py" - "$image_request_json" "$port_range" "$prefix" <<'PY'
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys

image_request, port_range, prefix = sys.argv[1:4]
image = json.loads(image_request)
start_s, end_s = port_range.split(":", 1)
start, end = int(start_s), int(end_s)


def run(cmd: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


candidates = list(image.get("candidates") or [])
result: dict[str, object] = {
    "success": True,
    "hostname": socket.gethostname(),
    "python": shutil.which("python3") or shutil.which("python"),
    "docker": {
        "present": shutil.which("docker") is not None,
        "version": None,
        "info_ok": False,
    },
    "required_paths": {},
    "optional_mounts": [],
    "npu_smi_present": os.path.exists("/usr/local/bin/npu-smi"),
    "image": {
        "name": image.get("requested"),
        "requested": image.get("requested"),
        "selector": image.get("selector"),
        "policy": image.get("policy"),
        "resolved_tag": image.get("resolved_tag"),
        "candidates": candidates,
        "mirror_order": list(image.get("mirror_order") or []),
        "present_local": False,
        "present_local_candidates": [],
    },
    "managed_containers": [],
    "free_port": None,
    "free_port_range": [start, end],
    "firewall": {
        "ufw": shutil.which("ufw") is not None,
        "firewalld": shutil.which("firewall-cmd") is not None,
    },
}

required = [
    "/dev/davinci_manager",
    "/dev/hisi_hdc",
    "/dev/devmm_svm",
    "/usr/local/Ascend/driver",
    "/usr/local/dcmi",
    "/usr/local/bin/npu-smi",
    "/usr/local/sbin",
    "/usr/share/zoneinfo/Asia/Shanghai",
]
for item in required:
    result["required_paths"][item] = pathlib.Path(item).exists()

for item in ["/home", "/tmp", "/weight", "/data", "/mnt"]:
    if pathlib.Path(item).exists():
        result["optional_mounts"].append(item)

if result["docker"]["present"]:
    rc, out, _ = run(["docker", "--version"])
    if rc == 0:
        result["docker"]["version"] = out
    rc, _, _ = run(["docker", "info"])
    result["docker"]["info_ok"] = rc == 0
    present_local_candidates: list[str] = []
    for candidate in candidates:
        rc, _, _ = run(["docker", "image", "inspect", candidate])
        if rc == 0:
            present_local_candidates.append(candidate)
    result["image"]["present_local_candidates"] = present_local_candidates
    result["image"]["present_local"] = bool(present_local_candidates)
    rc, out, _ = run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"])
    if rc == 0:
        rows = []
        for line in out.splitlines():
            if not line:
                continue
            parts = line.split("\t", 2)
            if len(parts) < 3:
                continue
            name, status, actual_image = parts
            if name.startswith(prefix):
                rows.append({"name": name, "status": status, "image": actual_image})
        result["managed_containers"] = rows

rc, out, _ = run(["ss", "-ltnH"])
used: set[int] = set()
if rc == 0:
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[3]
        if ":" not in local:
            continue
        try:
            used.add(int(local.rsplit(":", 1)[1]))
        except ValueError:
            continue
for port in range(start, end + 1):
    if port not in used:
        result["free_port"] = port
        break

print("__SENTINEL__" + json.dumps(result, ensure_ascii=False))
PY
'''
    return template.replace("__SENTINEL__", SENTINEL)


def render_bootstrap_host_script() -> str:
    template = r'''#!/usr/bin/env bash
set -euo pipefail
container="$1"
port="$2"
image_request_json="$3"
workdir="$4"
pubkey="$5"
namespace="${6:-}"
replace_on_image_change="${7:-false}"

emit_json() {
  python3 - "$1" <<'PY'
import json
import sys
print("__SENTINEL__" + json.dumps(json.loads(sys.argv[1]), ensure_ascii=False))
PY
}

emit_progress() {
  python3 - "$1" "$2" "$3" "${4:-}" <<'PY' >&2
import json
import sys
payload = {
    "phase": sys.argv[1],
    "status": sys.argv[2],
    "message": sys.argv[3],
}
if len(sys.argv) > 4 and sys.argv[4]:
    try:
        payload["expected_seconds"] = int(sys.argv[4])
    except ValueError:
        pass
print("__PROGRESS__" + json.dumps(payload, ensure_ascii=False))
PY
}

image_field() {
  python3 - "$image_request_json" "$1" <<'PY'
import json
import sys
payload = json.loads(sys.argv[1])
field = sys.argv[2]
value = payload.get(field)
if value is None:
    raise SystemExit(1)
if isinstance(value, str):
    print(value)
else:
    print(json.dumps(value, ensure_ascii=False))
PY
}

resolve_image_candidates() {
  python3 - "$image_request_json" <<'PY'
import json
import sys
payload = json.loads(sys.argv[1])
for item in payload.get("candidates") or []:
    print(item)
PY
}

run_with_progress() {
  phase="$1"
  message="$2"
  expected_seconds="$3"
  shift 3
  log_file="$(mktemp)"
  "$@" >"$log_file" 2>&1 &
  pid=$!
  start_ts=$(date +%s)
  while kill -0 "$pid" 2>/dev/null; do
    sleep 8
    if ! kill -0 "$pid" 2>/dev/null; then
      break
    fi
    elapsed=$(( $(date +%s) - start_ts ))
    if [ -s "$log_file" ]; then
      last_line="$(tail -n 1 "$log_file" 2>/dev/null | tr -d '\r' | sed 's/[^[:print:]\t]//g' | cut -c1-180)"
      if [ -n "$last_line" ]; then
        emit_progress "$phase" "running" "$message - $last_line" "$expected_seconds"
      else
        emit_progress "$phase" "running" "$message - still working (elapsed ${elapsed}s)" "$expected_seconds"
      fi
    else
      emit_progress "$phase" "running" "$message - still working (elapsed ${elapsed}s)" "$expected_seconds"
    fi
  done
  wait "$pid"
  status=$?
  if [ "$status" -ne 0 ]; then
    tail -n 120 "$log_file" >&2 || true
  fi
  rm -f "$log_file"
  return "$status"
}

emit_progress "preflight" "running" "validating host docker and Ascend prerequisites" 15
if ! command -v docker >/dev/null 2>&1; then
  emit_json '{"success": false, "error": "docker not found on host", "phase": "probe"}'
  exit 10
fi
if ! docker info >/dev/null 2>&1; then
  emit_json '{"success": false, "error": "docker info failed on host", "phase": "probe"}'
  exit 11
fi

missing=()
for p in \
  /dev/davinci_manager \
  /dev/hisi_hdc \
  /dev/devmm_svm \
  /usr/local/Ascend/driver \
  /usr/local/dcmi \
  /usr/local/bin/npu-smi \
  /usr/local/sbin \
  /usr/share/zoneinfo/Asia/Shanghai
  do
  [ -e "$p" ] || missing+=("$p")
done
if [ "${#missing[@]}" -gt 0 ]; then
  missing_json=$(python3 - <<'PY' "${missing[@]}"
import json
import sys
print(json.dumps(list(sys.argv[1:]), ensure_ascii=False))
PY
)
  emit_json "{\"success\": false, \"error\": \"required host paths missing\", \"phase\": \"probe\", \"missing\": ${missing_json}}"
  exit 12
fi

actions=()
created=false
started=false
pulled=false
installed_ssh=false
firewall="none"
selected_image=""
image_resolution="unknown"
requested_image="$(image_field requested || true)"
requested_selector="$(image_field selector || true)"
image_policy="$(image_field policy || true)"
resolved_tag="$(image_field resolved_tag || true)"
previous_image=""
mapfile -t image_candidates < <(resolve_image_candidates)
if [ "${#image_candidates[@]}" -eq 0 ]; then
  emit_json '{"success": false, "error": "image selector resolved to zero candidates", "phase": "image"}'
  exit 19
fi

container_exists=false
if docker inspect "$container" >/dev/null 2>&1; then
  container_exists=true
  current_image=$(docker inspect -f '{{.Config.Image}}' "$container")
  previous_image="$current_image"
  image_matches=false
  for candidate in "${image_candidates[@]}"; do
    if [ "$candidate" = "$current_image" ]; then
      image_matches=true
      break
    fi
  done
  if [ "$image_matches" != "true" ]; then
    if [ "$replace_on_image_change" = "true" ]; then
      emit_progress "image" "running" "existing container image $current_image does not match requested selector; recreating container" 60
      docker rm -f "$container" >/dev/null
      actions+=("removed-container-for-image-change")
      container_exists=false
    else
      mismatch_payload=$(python3 - <<'PY' "$current_image" "$image_request_json"
import json
import sys
current_image, image_request_json = sys.argv[1:]
image_request = json.loads(image_request_json)
print(json.dumps({
    "success": False,
    "error": "existing container image does not match the requested selector",
    "phase": "image",
    "existing_container_image": current_image,
    "requested_image": image_request.get("requested"),
    "requested_selector": image_request.get("selector"),
    "image_policy": image_request.get("policy"),
    "resolved_tag": image_request.get("resolved_tag"),
    "image_candidates": list(image_request.get("candidates") or []),
    "suggestion": "rerun with explicit container replacement consent or remove the managed container first",
}, ensure_ascii=False))
PY
)
      emit_json "$mismatch_payload"
      exit 21
    fi
  fi
fi

if [ "$container_exists" = "true" ]; then
  emit_progress "container" "running" "reusing an existing managed container" 15
  state=$(docker inspect -f '{{.State.Status}}' "$container")
  selected_image=$(docker inspect -f '{{.Config.Image}}' "$container")
  image_resolution="existing-container"
  if [ "$state" != "running" ]; then
    docker start "$container" >/dev/null
    started=true
    actions+=("started-existing-container")
  fi
else
  emit_progress "image" "running" "resolved image selector ${requested_image:-unknown} (${image_policy:-unknown})" 30
  for candidate in "${image_candidates[@]}"; do
    emit_progress "image" "running" "trying image candidate $candidate" 600
    if run_with_progress "image-pull" "docker pull $candidate" 600 docker pull "$candidate"; then
      selected_image="$candidate"
      image_resolution="pulled"
      pulled=true
      actions+=("pulled-image")
      break
    fi
    emit_progress "image" "running" "pull failed for $candidate; checking local cache" 30
    if docker image inspect "$candidate" >/dev/null 2>&1; then
      selected_image="$candidate"
      image_resolution="local-cache"
      actions+=("reused-local-image-cache")
      break
    fi
  done
  if [ -z "$selected_image" ]; then
    emit_json '{"success": false, "error": "no usable image candidate was found", "phase": "image"}'
    exit 20
  fi

  mount_args=()
  for optional in /home /tmp /weight /data /mnt; do
    if [ -e "$optional" ]; then
      mount_args+=("-v" "$optional:$optional")
    fi
  done

  emit_progress "container" "running" "creating managed container" 45
  docker run --name "$container" -it -d --network host --shm-size=500g \
    --privileged=true \
    --label com.vaws.managed=true \
    --label com.vaws.container_ssh_port="$port" \
    --label com.vaws.workdir="$workdir" \
    --label com.vaws.namespace="$namespace" \
    -w "$workdir" \
    --device=/dev/davinci_manager \
    --device=/dev/hisi_hdc \
    --device=/dev/devmm_svm \
    --entrypoint=bash \
    -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
    -v /usr/local/dcmi:/usr/local/dcmi \
    -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
    -v /usr/local/sbin:/usr/local/sbin \
    -v /usr/share/zoneinfo/Asia/Shanghai:/etc/localtime:ro \
    "${mount_args[@]}" \
    "$selected_image" >/dev/null

  created=true
  actions+=("created-container")
fi

if ! docker exec "$container" bash -lc 'command -v sshd >/dev/null 2>&1 && command -v ssh >/dev/null 2>&1'; then
  emit_progress "container-ssh" "running" "updating apt metadata inside the container" 300
  if ! run_with_progress "container-ssh-apt-update" "apt-get update inside the container" 300 \
    docker exec "$container" bash -lc 'set -euo pipefail; export DEBIAN_FRONTEND=noninteractive; apt-get -o Acquire::Check-Date=false -o Acquire::Check-Valid-Until=false update'; then
    emit_json '{"success": false, "error": "apt-get update inside container failed", "phase": "container-ssh-apt-update"}'
    exit 30
  fi
  emit_progress "container-ssh" "running" "installing openssh inside the container" 600
  if ! run_with_progress "container-ssh-apt-install" "apt-get install openssh-server openssh-client" 600 \
    docker exec "$container" bash -lc 'set -euo pipefail; export DEBIAN_FRONTEND=noninteractive; apt-get install -y openssh-server openssh-client --fix-missing'; then
    emit_json '{"success": false, "error": "installing openssh inside container failed", "phase": "container-ssh-apt-install"}'
    exit 31
  fi
  installed_ssh=true
  actions+=("installed-openssh")
fi

emit_progress "container-ssh" "running" "configuring dedicated container sshd" 60
if ! docker exec -i "$container" bash -s -- "$port" "$pubkey" <<'INNER'
set -euo pipefail
port="$1"
pubkey="$2"
install -d -m 700 /root/.ssh
[ -f /root/.ssh/authorized_keys ] || touch /root/.ssh/authorized_keys
if ! grep -qxF "$pubkey" /root/.ssh/authorized_keys 2>/dev/null; then
  printf '%s\n' "$pubkey" >> /root/.ssh/authorized_keys
fi
chmod 600 /root/.ssh/authorized_keys
install -d -m 0755 /run/sshd
ssh-keygen -A >/dev/null 2>&1 || true
cat > /etc/ssh/sshd_vaws_config <<EOF2
Port ${port}
ListenAddress 0.0.0.0
Protocol 2
HostKey /etc/ssh/ssh_host_rsa_key
HostKey /etc/ssh/ssh_host_ecdsa_key
HostKey /etc/ssh/ssh_host_ed25519_key
PermitRootLogin prohibit-password
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
UsePAM no
PrintMotd no
AuthorizedKeysFile .ssh/authorized_keys
PidFile /run/sshd_vaws.pid
EOF2
/usr/sbin/sshd -t -f /etc/ssh/sshd_vaws_config
if [ -f /run/sshd_vaws.pid ]; then
  kill "$(cat /run/sshd_vaws.pid)" 2>/dev/null || true
  rm -f /run/sshd_vaws.pid
fi
pkill -f '/etc/ssh/sshd_vaws_config' 2>/dev/null || true
/usr/sbin/sshd -f /etc/ssh/sshd_vaws_config
if command -v ss >/dev/null 2>&1; then
  ss -ltnH | awk '{print $4}' | grep -Eq "[:.]${port}$"
fi
INNER
then
  emit_json '{"success": false, "error": "configuring dedicated container sshd failed", "phase": "container-ssh"}'
  exit 32
fi
actions+=("configured-dedicated-sshd")

emit_progress "firewall" "running" "updating host firewall for the container ssh port" 30
if command -v ufw >/dev/null 2>&1; then
  ufw allow "${port}/tcp" >/dev/null 2>&1 || true
  firewall="ufw"
elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
  firewall-cmd --quiet --add-port="${port}/tcp" --permanent >/dev/null 2>&1 || true
  firewall-cmd --quiet --reload >/dev/null 2>&1 || true
  firewall="firewalld"
fi

payload=$(python3 - <<'PY' "$container" "$port" "$image_request_json" "$selected_image" "$image_resolution" "$workdir" "$namespace" "$created" "$started" "$pulled" "$installed_ssh" "$firewall" "${actions[*]}" "$previous_image" "$replace_on_image_change"
import json
import sys

container, port, image_request_json, selected_image, image_resolution, workdir, namespace, created, started, pulled, installed_ssh, firewall, actions, previous_image, replace_on_image_change = sys.argv[1:]
image_request = json.loads(image_request_json)
print(json.dumps({
    "success": True,
    "container": container,
    "container_ssh_port": int(port),
    "image": selected_image,
    "requested_image": image_request.get("requested"),
    "requested_selector": image_request.get("selector"),
    "image_policy": image_request.get("policy"),
    "resolved_tag": image_request.get("resolved_tag"),
    "selected_image": selected_image,
    "image_resolution": image_resolution,
    "image_candidates": list(image_request.get("candidates") or []),
    "image_mirror_order": list(image_request.get("mirror_order") or []),
    "workdir": workdir,
    "namespace": namespace or None,
    "previous_image": previous_image or None,
    "created": created == "true",
    "started_existing": started == "true",
    "pulled_image": pulled == "true",
    "installed_openssh": installed_ssh == "true",
    "replace_container_on_image_change": replace_on_image_change == "true",
    "firewall": firewall,
    "actions": [item for item in actions.split() if item],
}, ensure_ascii=False))
PY
)
emit_progress "complete" "done" "managed container bootstrap completed" 0
emit_json "$payload"
'''
    return template.replace("__SENTINEL__", SENTINEL).replace("__PROGRESS__", PROGRESS_SENTINEL)


def render_smoke_script() -> str:
    template = r'''#!/usr/bin/env bash
set -euo pipefail
requested_python="${1:-}"

emit_progress() {
  python3 - "$1" "$2" "$3" <<'PY' >&2
import json
import sys
print("__PROGRESS__" + json.dumps({
    "phase": sys.argv[1],
    "status": sys.argv[2],
    "message": sys.argv[3],
}, ensure_ascii=False))
PY
}

detect_python() {
  if [ -n "$requested_python" ] && [ -x "$requested_python" ]; then
    printf '%s\n' "$requested_python"
    return 0
  fi
  while IFS= read -r candidate; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done < <(ls -1d /usr/local/python*/bin/python3 2>/dev/null | sort -V -r)
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return 0
  fi
  return 1
}

emit_progress "python-discovery" "running" "discovering a usable python inside the container"
PYTHON_BIN="$(detect_python || true)"
if [ -z "$PYTHON_BIN" ]; then
  echo "__SENTINEL__{\"success\": false, \"error\": \"python not found in container\", \"phase\": \"python-discovery\"}"
  exit 40
fi

emit_progress "env" "running" "preparing Ascend and python environment"
export PATH="${PATH:-}"
export LD_LIBRARY_PATH="/usr/local/Ascend/driver/lib64/driver:/usr/local/Ascend/driver/lib64:${LD_LIBRARY_PATH:-}"
export VLLM_WORKER_MULTIPROC_METHOD=spawn
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1

sourced_file_log=""
safe_source() {
  file="$1"
  if [ -f "$file" ]; then
    set +u
    . "$file" >/dev/null 2>&1 || true
    set -u
    sourced_file_log+="$file\n"
  fi
}

safe_source /usr/local/Ascend/ascend-toolkit/set_env.sh
safe_source /usr/local/Ascend/ascend-toolkit/latest/set_env.sh
safe_source /usr/local/Ascend/nnal/atb/set_env.sh
safe_source /vllm-workspace/vllm-ascend/vllm_ascend/_cann_ops_custom/vendors/vllm-ascend/bin/set_env.bash

"$PYTHON_BIN" - <<'PY' "$PYTHON_BIN" "$sourced_file_log"
import json
import os
import sys
import traceback

PROGRESS = "__PROGRESS__"
python_path = sys.argv[1]
sourced_text = sys.argv[2]

def progress(phase: str, message: str) -> None:
    sys.stderr.write(PROGRESS + json.dumps({"phase": phase, "status": "running", "message": message}, ensure_ascii=False) + "\n")
    sys.stderr.flush()

result = {
    "success": False,
    "python_path": python_path,
    "python_version": sys.version.split()[0],
    "driver_ld_library_path_prefix": [
        "/usr/local/Ascend/driver/lib64/driver",
        "/usr/local/Ascend/driver/lib64",
    ],
    "ld_library_path": os.environ.get("LD_LIBRARY_PATH", ""),
    "sourced_scripts": [line for line in sourced_text.splitlines() if line],
}
try:
    progress("smoke", "importing torch and torch_npu")
    import torch
    import torch_npu  # noqa: F401

    progress("smoke", "allocating a small tensor on NPU")
    x = torch.zeros(1, 2).npu()
    progress("smoke", "container smoke test passed")
    result.update(
        {
            "success": True,
            "torch_version": getattr(torch, "__version__", None),
            "shape": list(x.shape),
            "device": str(x.device),
        }
    )
    if not str(x.device).startswith("npu"):
        raise RuntimeError(f"unexpected device: {x.device}")
except Exception as exc:  # pragma: no cover - runtime dependent
    tb = traceback.format_exc().splitlines()
    result.update(
        {
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback_tail": tb[-25:],
        }
    )
print("__SENTINEL__" + json.dumps(result, ensure_ascii=False))
raise SystemExit(0 if result["success"] else 3)
PY
'''
    return template.replace("__SENTINEL__", SENTINEL).replace("__PROGRESS__", PROGRESS_SENTINEL)


def render_mesh_export_key_script() -> str:
    template = r'''#!/usr/bin/env bash
set -euo pipefail
comment="$1"
install -d -m 700 /root/.ssh
if [ ! -f /root/.ssh/id_ed25519 ]; then
  ssh-keygen -t ed25519 -C "$comment" -f /root/.ssh/id_ed25519 -N "" >/dev/null
fi
pubkey="$(cat /root/.ssh/id_ed25519.pub)"
fingerprint="$(ssh-keygen -lf /root/.ssh/id_ed25519.pub | awk '{print $2}')"
python3 - <<'PY' "$comment" "$pubkey" "$fingerprint"
import json
import sys
print("__SENTINEL__" + json.dumps({
    "success": True,
    "comment": sys.argv[1],
    "public_key": sys.argv[2],
    "fingerprint": sys.argv[3],
}, ensure_ascii=False))
PY
'''
    return template.replace("__SENTINEL__", SENTINEL)


def render_mesh_add_peer_script() -> str:
    template = r'''#!/usr/bin/env bash
set -euo pipefail
peer_key="$1"
peer_host="$2"
peer_port="$3"
install -d -m 700 /root/.ssh
[ -f /root/.ssh/authorized_keys ] || touch /root/.ssh/authorized_keys
[ -f /root/.ssh/known_hosts ] || touch /root/.ssh/known_hosts
if ! grep -qxF "$peer_key" /root/.ssh/authorized_keys 2>/dev/null; then
  printf '%s\n' "$peer_key" >> /root/.ssh/authorized_keys
fi
chmod 600 /root/.ssh/authorized_keys
ssh-keyscan -p "$peer_port" "$peer_host" >> /root/.ssh/known_hosts 2>/dev/null || true
python3 - <<'PY' "$peer_host" "$peer_port" "$peer_key"
import json
import sys
parts = sys.argv[3].split()
print("__SENTINEL__" + json.dumps({
    "success": True,
    "peer_host": sys.argv[1],
    "peer_port": int(sys.argv[2]),
    "peer_key_added": True,
    "peer_key_comment": parts[-1] if parts else None,
}, ensure_ascii=False))
PY
'''
    return template.replace("__SENTINEL__", SENTINEL)


def render_mesh_remove_peer_script() -> str:
    template = r'''#!/usr/bin/env bash
set -euo pipefail
peer_comment="$1"
peer_host="$2"
peer_port="$3"
removed=0
if [ -f /root/.ssh/authorized_keys ]; then
  tmp="$(mktemp)"
  before="$(wc -l < /root/.ssh/authorized_keys 2>/dev/null || echo 0)"
  grep -vF "$peer_comment" /root/.ssh/authorized_keys > "$tmp" || true
  cat "$tmp" > /root/.ssh/authorized_keys
  rm -f "$tmp"
  chmod 600 /root/.ssh/authorized_keys
  after="$(wc -l < /root/.ssh/authorized_keys 2>/dev/null || echo 0)"
  removed=$(( before - after ))
fi
if [ -f /root/.ssh/known_hosts ]; then
  ssh-keygen -R "[${peer_host}]:${peer_port}" -f /root/.ssh/known_hosts >/dev/null 2>&1 || true
fi
python3 - <<'PY' "$peer_comment" "$peer_host" "$peer_port" "$removed"
import json
import sys
print("__SENTINEL__" + json.dumps({
    "success": True,
    "peer_comment": sys.argv[1],
    "peer_host": sys.argv[2],
    "peer_port": int(sys.argv[3]),
    "removed_authorized_key_lines": int(sys.argv[4]),
}, ensure_ascii=False))
PY
'''
    return template.replace("__SENTINEL__", SENTINEL)


def render_remove_container_host_script() -> str:
    template = r'''#!/usr/bin/env bash
set -euo pipefail
container="$1"
if ! command -v docker >/dev/null 2>&1; then
  echo "__SENTINEL__{\"success\": false, \"error\": \"docker not found on host\"}"
  exit 10
fi
if docker inspect "$container" >/dev/null 2>&1; then
  state="$(docker inspect -f '{{.State.Status}}' "$container")"
  docker rm -f "$container" >/dev/null
  python3 - <<'PY' "$container" "$state"
import json
import sys
print("__SENTINEL__" + json.dumps({
    "success": True,
    "container": sys.argv[1],
    "removed": True,
    "previous_state": sys.argv[2],
}, ensure_ascii=False))
PY
else
  python3 - <<'PY' "$container"
import json
import sys
print("__SENTINEL__" + json.dumps({
    "success": True,
    "container": sys.argv[1],
    "removed": False,
    "previous_state": None,
}, ensure_ascii=False))
PY
fi
'''
    return template.replace("__SENTINEL__", SENTINEL)


def container_target(args: argparse.Namespace) -> SshTarget:
    return SshTarget(host=args.host, user="root", port=args.container_ssh_port)


def host_target(args: argparse.Namespace) -> SshTarget:
    return SshTarget(host=args.host, user=args.user, port=args.host_port)


def check_direct_ssh(
    target: SshTarget,
    *,
    identity_file: pathlib.Path | None = None,
) -> dict[str, Any]:
    try:
        result = run_local(
            ssh_command(target, batch_mode=True, identity_file=identity_file) + ["printf", "ok"]
        )
    except MachineManagementError as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": str(exc),
        }
    return {
        "ok": result.returncode == 0 and result.stdout.endswith("ok"),
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "identity_file": str(identity_file) if identity_file is not None else None,
    }


def build_bootstrap_host_key_command(
    target: SshTarget,
    *,
    key_path: pathlib.Path,
    public_key: str,
) -> tuple[str, list[str]]:
    del key_path
    remote_cmd = build_authorized_keys_remote_command(public_key)
    command = ssh_command(
        target,
        batch_mode=False,
        extra_options=(
            "PreferredAuthentications=password,keyboard-interactive",
            "PubkeyAuthentication=no",
            "NumberOfPasswordPrompts=1",
        ),
    ) + ["sh", "-c", remote_cmd]
    return "ssh", command


def cmd_bootstrap_host_key(args: argparse.Namespace) -> int:
    target = host_target(args)
    key_path = find_public_key(args.public_key_file)
    private_key = private_key_for_public_key(key_path)
    public_key = load_public_key(key_path)
    password, password_source, password_env_name = read_password_value(args)
    before = check_direct_ssh(target, identity_file=private_key)
    tool_name, command = build_bootstrap_host_key_command(
        target,
        key_path=key_path,
        public_key=public_key,
    )
    payload: dict[str, Any] = {
        "target": {"host": target.host, "user": target.user, "host_port": target.port},
        "public_key_file": str(key_path),
        "private_key_file": str(private_key) if private_key is not None else None,
        "precheck": before,
        "bootstrap_tool": tool_name,
        "bootstrap_command": shell_join(command),
        "password_mode": password_source,
        "needs_interactive_terminal": password is None,
        "password_automation": "ssh-askpass" if password is not None else None,
    }
    if before["ok"]:
        payload.update({"success": True, "executed": False, "result": "already-configured"})
        print_json(payload)
        return 0

    if args.print_command:
        payload.update({
            "success": True,
            "executed": False,
            "result": "command-preview",
            "message": "run the bootstrap command once if you want to handle the password manually",
        })
        print_json(payload)
        return 0

    if password is not None:
        proc = run_with_askpass(command, password=password, env_name=password_env_name)
        after = check_direct_ssh(target, identity_file=private_key)
        payload.update(
            {
                "executed": True,
                "command_returncode": proc.returncode,
                "postcheck": after,
                "success": after["ok"],
                "result": "bootstrapped" if after["ok"] else "failed",
                "mode": "noninteractive-password",
                "stdout_tail": compact_failure_tail(proc.stdout),
                "stderr_tail": compact_failure_tail(proc.stderr),
            }
        )
        if not after["ok"]:
            payload["error"] = (
                "password-based host bootstrap did not establish key-based SSH; verify the supplied password, username, and host password-login policy"
            )
            print_json(payload)
            return 2
        print_json(payload)
        return 0

    returncode = run_local_interactive(command)
    after = check_direct_ssh(target, identity_file=private_key)
    payload.update(
        {
            "executed": True,
            "command_returncode": returncode,
            "postcheck": after,
            "success": after["ok"],
            "result": "bootstrapped" if after["ok"] else "failed",
            "mode": "interactive-terminal",
        }
    )
    if not after["ok"]:
        payload["error"] = (
            "interactive host bootstrap did not establish key-based SSH; verify the host password, username, and password-login policy"
        )
        print_json(payload)
        return 2

    print_json(payload)
    return 0


def cmd_probe_host(args: argparse.Namespace) -> int:
    target = host_target(args)
    image_request = image_request_payload(args.image)
    result = run_remote_script(
        target,
        render_host_probe_script(),
        args=[json.dumps(image_request, ensure_ascii=False), args.port_range, args.managed_prefix],
        batch_mode=True,
        timeout_seconds=DEFAULT_PROBE_TIMEOUT_SECONDS,
    )
    payload = assert_remote_success(result)
    payload["target"] = {
        "host": target.host,
        "user": target.user,
        "port": target.port,
    }
    print_json(payload)
    return 0


def cmd_bootstrap_container(args: argparse.Namespace) -> int:
    target = host_target(args)
    key_path = find_public_key(args.public_key_file)
    private_key = private_key_for_public_key(key_path)
    public_key = load_public_key(key_path)
    image_request = image_request_payload(args.image)
    result = run_remote_script(
        target,
        render_bootstrap_host_script(),
        args=[
            args.container_name,
            str(args.container_ssh_port),
            json.dumps(image_request, ensure_ascii=False),
            args.workdir,
            public_key,
            args.namespace or "",
            "true" if args.replace_container_on_image_change else "false",
        ],
        batch_mode=True,
        timeout_seconds=DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS,
    )
    payload = assert_remote_success(result)

    ssh_check = check_direct_ssh(
        SshTarget(host=args.host, user="root", port=args.container_ssh_port),
        identity_file=private_key,
    )
    payload.update(
        {
            "public_key_file": str(key_path),
            "private_key_file": str(private_key) if private_key is not None else None,
            "namespace": args.namespace or None,
            "direct_container_ssh": ssh_check,
            "target": {
                "host": target.host,
                "user": target.user,
                "host_port": target.port,
                "container_ssh_port": args.container_ssh_port,
            },
        }
    )
    if not ssh_check["ok"]:
        payload["success"] = False
        payload["error"] = "container SSH did not come up after bootstrap"
        print_json(payload)
        return 2

    print_json(payload)
    return 0


def smoke_payload(args: argparse.Namespace) -> dict[str, Any]:
    target = container_target(args)
    python_arg = args.python if args.python else ""
    result = run_remote_script(
        target,
        render_smoke_script(),
        args=[python_arg],
        batch_mode=True,
        timeout_seconds=DEFAULT_SMOKE_TIMEOUT_SECONDS,
    )
    payload = assert_remote_success(result, require_payload=True)
    payload["target"] = {
        "host": target.host,
        "user": target.user,
        "container_ssh_port": target.port,
    }
    return payload


def cmd_smoke(args: argparse.Namespace) -> int:
    payload = smoke_payload(args)
    print_json(payload)
    return 0


def cmd_verify_machine(args: argparse.Namespace) -> int:
    host = host_target(args)
    container = container_target(args)
    identity_file = None
    try:
        identity_file = private_key_for_public_key(find_public_key(None))
    except MachineManagementError:
        identity_file = None
    host_check = check_direct_ssh(host, identity_file=identity_file)
    container_check = check_direct_ssh(container, identity_file=identity_file)
    result: dict[str, Any] = {
        "host": {"host": host.host, "user": host.user, "port": host.port},
        "container": {"host": container.host, "user": container.user, "port": container.port},
        "identity_file": str(identity_file) if identity_file is not None else None,
        "host_ssh": host_check,
        "container_ssh": container_check,
    }
    if container_check["ok"]:
        smoke_args = argparse.Namespace(
            host=args.host,
            user=args.user,
            container_ssh_port=args.container_ssh_port,
            python=args.python,
        )
        try:
            result["smoke"] = smoke_payload(smoke_args)
        except MachineManagementError as exc:
            result["smoke"] = {"success": False, "error": str(exc)}
    else:
        result["smoke"] = {"success": False, "skipped": "container SSH failed"}

    result["ready"] = bool(
        host_check["ok"]
        and container_check["ok"]
        and result["smoke"].get("success") is True
    )
    print_json(result)
    return 0


def cmd_mesh_export_key(args: argparse.Namespace) -> int:
    target = container_target(args)
    result = run_remote_script(
        target,
        render_mesh_export_key_script(),
        args=[args.comment],
        batch_mode=True,
    )
    payload = assert_remote_success(result)
    payload["target"] = {
        "host": target.host,
        "user": target.user,
        "container_ssh_port": target.port,
    }
    print_json(payload)
    return 0


def cmd_mesh_add_peer(args: argparse.Namespace) -> int:
    target = container_target(args)
    result = run_remote_script(
        target,
        render_mesh_add_peer_script(),
        args=[args.peer_public_key, args.peer_host, str(args.peer_port)],
        batch_mode=True,
    )
    payload = assert_remote_success(result)
    payload["target"] = {
        "host": target.host,
        "user": target.user,
        "container_ssh_port": target.port,
    }
    print_json(payload)
    return 0


def cmd_mesh_remove_peer(args: argparse.Namespace) -> int:
    target = container_target(args)
    result = run_remote_script(
        target,
        render_mesh_remove_peer_script(),
        args=[args.peer_comment, args.peer_host, str(args.peer_port)],
        batch_mode=True,
    )
    payload = assert_remote_success(result)
    payload["target"] = {
        "host": target.host,
        "user": target.user,
        "container_ssh_port": target.port,
    }
    print_json(payload)
    return 0


def remove_known_host_entry(host: str, port: int, known_hosts: pathlib.Path) -> dict[str, Any]:
    if not known_hosts.exists():
        return {
            "success": True,
            "known_hosts": str(known_hosts),
            "removed": False,
            "reason": "known_hosts file does not exist",
        }
    proc = run_local(
        ["ssh-keygen", "-R", f"[{host}]:{port}", "-f", str(known_hosts)]
    )
    combined = (proc.stdout + "\n" + proc.stderr).lower()
    return {
        "success": proc.returncode == 0 or "not found" in combined,
        "known_hosts": str(known_hosts),
        "removed": proc.returncode == 0,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def cmd_clean_local_known_hosts(args: argparse.Namespace) -> int:
    known_hosts = pathlib.Path(args.known_hosts).expanduser().resolve()
    payload = remove_known_host_entry(args.host, args.container_ssh_port, known_hosts)
    payload.update({"host": args.host, "container_ssh_port": args.container_ssh_port})
    print_json(payload)
    return 0


def cmd_remove_container(args: argparse.Namespace) -> int:
    target = host_target(args)
    result = run_remote_script(
        target,
        render_remove_container_host_script(),
        args=[args.container_name],
        batch_mode=True,
    )
    payload = assert_remote_success(result)
    payload["target"] = {
        "host": target.host,
        "user": target.user,
        "host_port": target.port,
    }
    if args.clean_local_known_hosts and args.container_ssh_port:
        payload["local_known_hosts_cleanup"] = remove_known_host_entry(
            args.host,
            args.container_ssh_port,
            pathlib.Path(args.known_hosts).expanduser().resolve(),
        )
    print_json(payload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=lambda *args, **kwargs: argparse.ArgumentParser(*args, allow_abbrev=False, **kwargs),
    )

    def add_host_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--host", required=True, help="host IP or DNS name")
        p.add_argument("--user", dest="user", default=DEFAULT_HOST_USER, help=f"SSH user (default: {DEFAULT_HOST_USER})")
        p.add_argument(
            "--host-port",
            "--host-ssh-port",
            dest="host_port",
            type=int,
            default=DEFAULT_HOST_PORT,
            help=f"host SSH port (default: {DEFAULT_HOST_PORT})",
        )

    def add_container_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--host", required=True, help="host IP or DNS name")
        p.add_argument("--user", dest="user", default=DEFAULT_HOST_USER, help=f"SSH user (default: {DEFAULT_HOST_USER})")
        p.add_argument(
            "--container-ssh-port",
            "--container-port",
            "--port",
            dest="container_ssh_port",
            type=int,
            required=True,
            help="direct SSH port exposed by the managed container",
        )

    probe = subparsers.add_parser("probe-host", help="probe host prerequisites and choose a free high SSH port")
    add_host_args(probe)
    probe.add_argument(
        "--image",
        required=True,
        help=(
            "explicit image selector: `main`, `stable`, or a full non-latest image reference; "
            f"`main` tries {DEFAULT_IMAGE_CANDIDATES[0]} then {DEFAULT_IMAGE_CANDIDATES[1]}"
        ),
    )
    probe.add_argument("--port-range", default=DEFAULT_PORT_RANGE, help=f"inclusive range START:END (default: {DEFAULT_PORT_RANGE})")
    probe.add_argument("--managed-prefix", default="vaws-", help="managed container name prefix (default: vaws-)")
    probe.set_defaults(func=cmd_probe_host)

    host_bootstrap = subparsers.add_parser(
        "bootstrap-host-key",
        help="bootstrap host key auth; use a supplied password non-interactively when available, otherwise fall back to an interactive prompt",
    )
    add_host_args(host_bootstrap)
    host_bootstrap.add_argument(
        "--public-key-file",
        help="local SSH public key to install on the host; defaults to ~/.ssh/id_ed25519.pub if present",
    )
    password_group = host_bootstrap.add_mutually_exclusive_group()
    password_group.add_argument(
        "--password",
        help="host password already supplied by the user in the current chat; convenient but exposes the value to process args and command logs",
    )
    password_group.add_argument(
        "--password-env",
        help="read the host password from one environment variable and keep it out of command args",
    )
    password_group.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the host password from standard input",
    )
    host_bootstrap.add_argument(
        "--print-command",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="print the exact bootstrap command instead of running it",
    )
    host_bootstrap.set_defaults(func=cmd_bootstrap_host_key)

    bootstrap = subparsers.add_parser("bootstrap-container", help="create or repair the managed container and dedicated sshd")
    add_host_args(bootstrap)
    bootstrap.add_argument("--container-name", "--name", dest="container_name", required=True, help="container name to create or reuse")
    bootstrap.add_argument(
        "--container-ssh-port",
        "--container-port",
        "--port",
        dest="container_ssh_port",
        type=int,
        required=True,
        help="high non-default SSH port for the managed container",
    )
    bootstrap.add_argument(
        "--image",
        required=True,
        help=(
            "explicit image selector: `main`, `stable`, or a full non-latest image reference; "
            f"`main` tries {DEFAULT_IMAGE_CANDIDATES[0]} then {DEFAULT_IMAGE_CANDIDATES[1]}"
        ),
    )
    bootstrap.add_argument(
        "--replace-container-on-image-change",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="recreate the existing managed container when its current image does not match the requested selector",
    )
    bootstrap.add_argument("--workdir", default=DEFAULT_WORKDIR, help=f"container workdir (default: {DEFAULT_WORKDIR})")
    bootstrap.add_argument("--namespace", help="stable workspace machine username used for collision-safe container naming")
    bootstrap.add_argument("--public-key-file", help="local SSH public key to add to the container; defaults to ~/.ssh/id_ed25519.pub if present")
    bootstrap.set_defaults(func=cmd_bootstrap_container)

    smoke = subparsers.add_parser("smoke", help="run the container-side torch/torch_npu smoke test with dynamic env discovery")
    add_container_args(smoke)
    smoke.add_argument("--python", help="optional explicit python path inside the container")
    smoke.set_defaults(func=cmd_smoke)

    verify = subparsers.add_parser("verify-machine", help="check host SSH, container SSH, and smoke readiness together")
    add_container_args(verify)
    verify.add_argument(
        "--host-port",
        "--host-ssh-port",
        dest="host_port",
        type=int,
        default=DEFAULT_HOST_PORT,
        help=f"host SSH port (default: {DEFAULT_HOST_PORT})",
    )
    verify.add_argument("--python", help="optional explicit python path inside the container")
    verify.set_defaults(func=cmd_verify_machine)

    mesh_export = subparsers.add_parser("mesh-export-key", help="ensure a mesh key exists in the container and print its public key")
    add_container_args(mesh_export)
    mesh_export.add_argument("--comment", required=True, help="stable mesh key comment, for example vaws-mesh:173.125.1.2")
    mesh_export.set_defaults(func=cmd_mesh_export_key)

    mesh_add = subparsers.add_parser("mesh-add-peer", help="add a peer mesh key and known_hosts entry inside a managed container")
    add_container_args(mesh_add)
    mesh_add.add_argument("--peer-public-key", required=True, help="peer public key line, including its comment")
    mesh_add.add_argument("--peer-host", required=True, help="peer host IP or DNS name")
    mesh_add.add_argument("--peer-port", type=int, required=True, help="peer container SSH port")
    mesh_add.set_defaults(func=cmd_mesh_add_peer)

    mesh_remove = subparsers.add_parser("mesh-remove-peer", help="remove a peer mesh key and known_hosts entry inside a managed container")
    add_container_args(mesh_remove)
    mesh_remove.add_argument("--peer-comment", required=True, help="stable mesh comment, for example vaws-mesh:173.125.1.2")
    mesh_remove.add_argument("--peer-host", required=True, help="peer host IP or DNS name")
    mesh_remove.add_argument("--peer-port", type=int, required=True, help="peer container SSH port")
    mesh_remove.set_defaults(func=cmd_mesh_remove_peer)

    clean_known_hosts = subparsers.add_parser("clean-local-known-hosts", help="remove one managed container endpoint from local known_hosts")
    clean_known_hosts.add_argument("--host", required=True, help="host IP or DNS name")
    clean_known_hosts.add_argument(
        "--container-ssh-port",
        "--container-port",
        "--port",
        dest="container_ssh_port",
        type=int,
        required=True,
        help="managed container SSH port",
    )
    clean_known_hosts.add_argument("--known-hosts", default=str(DEFAULT_KNOWN_HOSTS), help=f"known_hosts path (default: {DEFAULT_KNOWN_HOSTS})")
    clean_known_hosts.set_defaults(func=cmd_clean_local_known_hosts)

    remove = subparsers.add_parser("remove-container", help="remove a managed container from the host")
    add_host_args(remove)
    remove.add_argument("--container-name", "--name", dest="container_name", required=True, help="container name to remove")
    remove.add_argument(
        "--container-ssh-port",
        "--container-port",
        "--port",
        dest="container_ssh_port",
        type=int,
        help="container SSH port for optional local known_hosts cleanup",
    )
    remove.add_argument("--known-hosts", default=str(DEFAULT_KNOWN_HOSTS), help=f"known_hosts path (default: {DEFAULT_KNOWN_HOSTS})")
    remove.add_argument(
        "--clean-local-known-hosts",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="also remove the endpoint from local known_hosts when --container-ssh-port is provided",
    )
    remove.set_defaults(func=cmd_remove_container)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except MachineManagementError as exc:
        print_json({"success": False, "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
