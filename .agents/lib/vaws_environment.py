"""Publish immutable control-plane environments and look up their ready receipts.

Only explicit dependency sync constructs environments. Each install happens at
its permanent content address while holding an OS lock; the receipt is published
last. Launchers only read receipts. A project selection is setup configuration,
not a process lease, and an explicit receipt pins an already running client.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import platform
import re
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import time
import tomllib

PIN_ENV = "VAWS_ENV_RECEIPT"
MANAGED_PIN_ENV = "VAWS_MANAGED_ENV_RECEIPT"
READY_NAME = ".vaws-ready.json"
RECIPE_VERSION = 3


class EnvironmentError(RuntimeError):
    """An environment is unavailable or cannot be constructed safely."""


def _json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _digest(value) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


def _identity() -> dict:
    return {"platform": sys.platform, "arch": platform.machine().lower(),
            "abi": sysconfig.get_config_var("SOABI") or sys.implementation.cache_tag,
            "python_version": platform.python_version(),
            "implementation": sys.implementation.name,
            "cache_tag": sys.implementation.cache_tag,
            "sysconfig_platform": sysconfig.get_platform(), "build": sys.version}


def _python_identity(python: str | None) -> tuple[str, dict]:
    candidate = python or getattr(sys, "_base_executable", sys.executable)
    executable = shutil.which(candidate) or (str(Path(candidate).absolute()) if Path(candidate).is_file() else None)
    if executable is None:
        result = subprocess.run(["uv", "python", "find", candidate], capture_output=True, text=True, check=False)
        if result.returncode:
            raise EnvironmentError(result.stderr.strip() or f"cannot find Python {candidate!r}")
        executable = result.stdout.strip()
    # uv's minor-version Python path can be a mutable junction/symlink. A venv
    # embeds this base path, so resolve it before both inspection and install.
    executable = str(Path(executable).resolve(strict=True))
    code = ("import sys,json,platform,sysconfig;print(json.dumps({"
            "'platform':sys.platform,'arch':platform.machine().lower(),"
            "'abi':sysconfig.get_config_var('SOABI') or sys.implementation.cache_tag,"
            "'python_version':platform.python_version(),'implementation':sys.implementation.name,"
            "'cache_tag':sys.implementation.cache_tag,'sysconfig_platform':sysconfig.get_platform(),"
            "'build':sys.version}))")
    result = subprocess.run([executable, "-I", "-c", code], capture_output=True, text=True, check=False)
    if result.returncode:
        raise EnvironmentError(result.stderr.strip() or "cannot inspect the selected Python")
    identity = json.loads(result.stdout)
    if identity["platform"] != sys.platform:
        raise EnvironmentError("dependency sync must run with a Python native to this platform")
    return executable, identity


def _store() -> Path:
    if value := os.environ.get("VAWS_ENV_HOME"):
        return Path(value).expanduser().absolute()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / "vaws/environments"


def _inputs(repo_root: Path) -> tuple[bytes, bytes, dict, str, str]:
    try:
        project = (repo_root / "pyproject.toml").read_bytes()
        lock = (repo_root / "uv.lock").read_bytes()
        document = tomllib.loads(project.decode("utf-8-sig"))
        lock_document = tomllib.loads(lock.decode("utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise EnvironmentError(f"cannot read locked dependency inputs: {exc}") from exc
    uv = document.get("tool", {}).get("uv", {})
    if uv.get("package", True) is not False:
        raise EnvironmentError("immutable workspace environments require tool.uv.package = false")
    if uv.get("workspace"):
        raise EnvironmentError("mutable workspace dependencies cannot be cached as immutable environments")
    for name, source in uv.get("sources", {}).items():
        for item in source if isinstance(source, list) else [source]:
            if isinstance(item, dict) and (item.get("path") or item.get("workspace") or item.get("editable")):
                raise EnvironmentError(f"mutable source for {name!r} is unsupported; use a locked wheel or immutable Git revision")
            if isinstance(item, dict) and item.get("git") and not re.fullmatch(r"[a-fA-F0-9]{40}", item.get("rev", "")):
                raise EnvironmentError(f"Git source {name!r} must use a complete immutable commit")
    for package in lock_document.get("package", []):
        source = package.get("source", {})
        # uv represents this package=false project as virtual='.'. It is not installed.
        if source.get("editable") or source.get("directory") or source.get("path") or (source.get("virtual") and source["virtual"] != "."):
            raise EnvironmentError("uv.lock contains a mutable local dependency")
    project_fields = ("name", "version", "requires-python", "dependencies", "optional-dependencies", "dynamic")
    projection = {"project": {key: value for key, value in document.get("project", {}).items() if key in project_fields}}
    projection.update({key: document[key] for key in ("dependency-groups", "build-system") if key in document})
    projection["uv"] = uv
    lock_sha = hashlib.sha256(lock).hexdigest()
    return project, lock, document, _digest({"project": projection, "lock_sha256": lock_sha}), lock_sha


def _selection(document: dict, groups=None, extras=(), options=()) -> tuple[dict, str | None, list[str]]:
    uv = document.get("tool", {}).get("uv", {})
    available_groups = set(document.get("dependency-groups", {}))
    defaults = uv.get("default-groups", ["dev"] if "dev" in available_groups else [])
    selected = set(available_groups if defaults == "all" else defaults) if groups is None else set(groups)
    available_extras = set(document.get("project", {}).get("optional-dependencies", {}))
    extra_set = set(extras)
    python = None
    transport = []
    project = True
    options = list(options)
    index = 0
    while index < len(options):
        token = options[index]
        flag, equal, value = token.partition("=")
        if flag in ("--group", "--only-group", "--no-group", "--extra", "--no-extra", "--python", "--cache-dir", "--link-mode"):
            if not equal:
                index += 1
                if index == len(options):
                    raise EnvironmentError(f"{flag} requires a value")
                value = options[index]
            if flag == "--group": selected.add(value)
            elif flag == "--only-group":
                if project: selected.clear()
                project = False
                selected.add(value)
            elif flag == "--no-group": selected.discard(value)
            elif flag == "--extra": extra_set.add(value)
            elif flag == "--no-extra": extra_set.discard(value)
            elif flag == "--python": python = value
            else: transport.extend((flag, value))
        elif equal:
            raise EnvironmentError(f"unsupported immutable sync option: {token}")
        elif flag in ("--locked", "--no-editable", "--no-install-project"):
            pass
        elif flag in ("--no-default-groups", "--no-dev"):
            selected = set() if flag == "--no-default-groups" else selected - {"dev"}
        elif flag == "--dev": selected.add("dev")
        elif flag == "--only-dev": selected, project = {"dev"}, False
        elif flag == "--all-groups": selected = available_groups.copy()
        elif flag == "--all-extras": extra_set = available_extras.copy()
        elif flag in ("--offline", "--no-cache", "--refresh", "--quiet", "--verbose"):
            transport.append(flag)
        else:
            raise EnvironmentError(f"unsupported immutable sync option: {token}; dependency-changing options must be represented in the environment key")
        index += 1
    if selected - available_groups or extra_set - available_extras:
        raise EnvironmentError(f"unknown dependency groups or extras: {sorted((selected - available_groups) | (extra_set - available_extras))}")
    return {"groups": sorted(selected), "extras": sorted(extra_set), "project": project}, python, transport


def _key(identity: dict, input_id: str, selection: dict) -> str:
    return _digest({"recipe_version": RECIPE_VERSION, "python_identity": identity, "input_id": input_id, "selection": selection})


def _selection_path(repo_root: Path, target_platform: str) -> Path:
    return repo_root / ".vaws-local/environment-selection" / f"{target_platform}.json"


def _access_path(value: str | Path) -> Path:
    text = str(value)
    if os.name != "nt" and re.match(r"^[a-zA-Z]:[\\/]", text):
        windows = PureWindowsPath(text)
        return Path("/mnt") / windows.drive[0].lower() / Path(*windows.parts[1:])
    return Path(value)


def read_receipt(path: str | Path, *, expected_platform: str | None = None) -> dict:
    actual = _access_path(path)
    try:
        value = json.loads(actual.read_text(encoding="utf-8"))
        required = ("key", "root", "python", "base_python", "platform", "arch", "abi", "python_version", "python_identity", "input_id", "lock_sha256", "selection", "store", "receipt")
        if value.get("schema_version") != 1 or value.get("recipe_version") != RECIPE_VERSION or any(name not in value for name in required):
            raise ValueError("incomplete ready receipt")
        if expected_platform and value["platform"] != expected_platform:
            raise ValueError(f"ready receipt belongs to {value['platform']}, expected {expected_platform}")
        if value["key"] != _key(value["python_identity"], value["input_id"], value["selection"]):
            raise ValueError("ready receipt content does not match its key")
        root = _access_path(value["root"])
        if root.name != value["key"] or actual.resolve() != (root / READY_NAME).resolve():
            raise ValueError("ready receipt does not belong to its content-addressed root")
        if _access_path(value["receipt"]).resolve() != actual.resolve() or root.parent.resolve() != _access_path(value["store"]).resolve():
            raise ValueError("ready receipt paths do not agree")
        python = _access_path(value["python"])
        expected = root / ("Scripts/python.exe" if value["platform"] == "win32" else "bin/python")
        if python != expected or not python.is_file():
            raise ValueError("ready interpreter is missing or outside its environment")
        base_python = _access_path(value["base_python"])
        if not base_python.is_file() or base_python.resolve() != base_python:
            raise ValueError("ready base interpreter is missing or is a mutable alias")
        if any(value[field] != value["python_identity"][field] for field in ("platform", "arch", "abi", "python_version")):
            raise ValueError("ready Python facts are inconsistent")
        return value
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise EnvironmentError(f"environment is not ready: {actual}: {exc}") from exc


def _configured(repo_root: Path, target_platform: str) -> dict | None:
    path = _selection_path(repo_root, target_platform)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if value["platform"] != target_platform:
            raise ValueError("selection platform does not match its filename")
        return value
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise EnvironmentError(f"invalid environment selection: {path}: {exc}") from exc


def _lookup(repo_root: Path, target_platform: str, *, require_configuration=False) -> dict:
    configuration = _configured(repo_root, target_platform)
    _, _, document, input_id, _ = _inputs(repo_root)
    if configuration:
        identity, selection = configuration["python_identity"], configuration["selection"]
        store = _access_path(configuration["store"])
    else:
        if require_configuration:
            raise EnvironmentError("no Windows environment selection exists; run dependency sync in native Windows")
        identity = _identity()
        selection = _selection(document)[0]
        store = _store()
    return read_receipt(store / _key(identity, input_id, selection) / READY_NAME, expected_platform=target_platform)


def native_ready(repo_root: Path, *, pin: str | Path | None = None) -> dict:
    """Look up readiness without probes, installs, selection writes or services."""
    selected = pin or os.environ.get(PIN_ENV)
    if selected:
        return read_receipt(selected, expected_platform=sys.platform)
    return _lookup(Path(repo_root), sys.platform)


def windows_ready(repo_root: Path) -> dict:
    """Read native Windows facts from WSL; never synthesize Windows ABI facts."""
    if pin := os.environ.get(MANAGED_PIN_ENV):
        return read_receipt(pin, expected_platform="win32")
    if pin := os.environ.get(PIN_ENV):
        value = read_receipt(pin)
        if value["platform"] == "win32":
            return value
    return _lookup(Path(repo_root), "win32", require_configuration=True)


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(_json(value) + b"\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def select_environment(repo_root: Path, receipt: dict) -> None:
    value = read_receipt(receipt["receipt"])
    _atomic_json(_selection_path(Path(repo_root), value["platform"]), value)


@contextmanager
def _key_lock(store: Path, key: str):
    locks = store / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    with (locks / (key + ".lock")).open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield handle.fileno()
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _install(command: list[str], environment: dict, lock_fd: int) -> None:
    if os.name == "nt":
        from vaws_windows import owned_process
        with owned_process(command, env=environment, stdout=sys.stderr, stderr=sys.stderr) as process:
            code = process.wait()
    else:
        # The child retains the lock if this process is killed. A second builder
        # cannot clean its unfinished root until that installer has exited.
        process = subprocess.Popen(command, env=environment, stdout=sys.stderr, stderr=sys.stderr,
                                   start_new_session=True, pass_fds=(lock_fd,))
        try:
            code = process.wait()
        except BaseException:
            os.killpg(process.pid, signal.SIGTERM)
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise
    if code:
        raise EnvironmentError(f"locked dependency installation failed with exit code {code}")


def prepare_environment(repo_root: Path, *, groups=None, extras=(), python=None, install_options=()) -> dict:
    """Construct once at the final address, publish ready last, and select it."""
    repo_root = Path(repo_root)
    project, lock, document, input_id, lock_sha = _inputs(repo_root)
    selection, selected_python, transport = _selection(document, groups, extras, install_options)
    executable, identity = _python_identity(python or selected_python)
    store = _store()
    # Packaged desktop apps can virtualize LocalAppData writes. Resolve only
    # after explicit creation, so every receipt records the physical directory
    # seen by external Windows processes and WSL, not this app's virtual view.
    store.mkdir(parents=True, exist_ok=True)
    store = store.resolve(strict=True)
    key = _key(identity, input_id, selection)
    root = store / key
    receipt_path = root / READY_NAME
    if receipt_path.exists():
        receipt = read_receipt(receipt_path, expected_platform=sys.platform)
        select_environment(repo_root, receipt)
        return receipt
    with _key_lock(store, key) as lock_fd:
        if receipt_path.exists():
            receipt = read_receipt(receipt_path, expected_platform=sys.platform)
        else:
            # An unpublished failed attempt may be retried. Published roots are
            # never synced, moved, replaced or garbage-collected here.
            if root.exists():
                if root.is_symlink() or root.resolve().parent != store.resolve() or root.name != key:
                    raise EnvironmentError("unfinished environment path escapes its store")
                shutil.rmtree(root)
            with tempfile.TemporaryDirectory(prefix="vaws-locked-inputs-") as temporary:
                frozen = Path(temporary)
                (frozen / "pyproject.toml").write_bytes(project)
                (frozen / "uv.lock").write_bytes(lock)
                command = ["uv", "sync", "--project", str(frozen), "--locked", "--no-editable", "--no-install-project",
                           "--python", executable, "--no-default-groups"]
                for group in selection["groups"]:
                    command.extend(("--group" if selection["project"] else "--only-group", group))
                for extra in selection["extras"]:
                    command.extend(("--extra", extra))
                command.extend(transport)
                environment = {name: value for name, value in os.environ.items()
                               if not name.startswith("UV_") and name not in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH", PIN_ENV)}
                environment["UV_PROJECT_ENVIRONMENT"] = str(root)
                _install(command, environment, lock_fd)
            environment_python = root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
            verification = subprocess.run([str(environment_python), "-I", "-X", "utf8", "-c",
                                          "import importlib.metadata,sys,json;list(importlib.metadata.distributions());print(json.dumps({'prefix':sys.prefix,'base':sys._base_executable}))"],
                                         capture_output=True, encoding="utf-8", check=False)
            facts = json.loads(verification.stdout) if verification.returncode == 0 else {}
            if (verification.returncode or Path(facts["prefix"]).resolve() != root.resolve()
                    or Path(facts["base"]).resolve() != Path(executable)):
                raise EnvironmentError("installed interpreter failed its permanent-path verification")
            receipt = {"schema_version": 1, "recipe_version": RECIPE_VERSION, "key": key, "root": str(root), "python": str(environment_python),
                       "platform": identity["platform"], "arch": identity["arch"], "abi": identity["abi"],
                       "python_version": identity["python_version"], "python_identity": identity, "input_id": input_id,
                       "lock_sha256": lock_sha, "selection": selection, "store": str(store), "receipt": str(receipt_path),
                       "base_python": executable}
            _atomic_json(receipt_path, receipt)
            receipt = read_receipt(receipt_path, expected_platform=sys.platform)
    select_environment(repo_root, receipt)
    return receipt
