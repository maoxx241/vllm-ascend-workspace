"""Real locked installs, concurrent publishers and live clients across lock changes."""
from __future__ import annotations

import hashlib
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import threading
import zipfile

import pytest
import vaws_environment as envs

LIB = Path(__file__).resolve().parents[1] / "lib"
BASE = getattr(sys, "_base_executable", sys.executable)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("VAWS_ENV_HOME", str(tmp_path / "shared store 中文"))
    monkeypatch.delenv(envs.PIN_ENV, raising=False)
    root = tmp_path / "checkout"
    root.mkdir()
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(tmp_path)))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    (root / ".test-wheel-url").write_text(f"http://127.0.0.1:{server.server_port}", encoding="utf-8")
    try:
        write_project(root)
        yield root
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def write_project(root, version="1"):
    wheel = root.parent / f"vaws_env_fixture-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        files = {
            "vaws_env_fixture.py": f"VALUE = {version!r}\ndef main():\n    print(VALUE)\n",
            f"vaws_env_fixture-{version}.dist-info/METADATA": f"Metadata-Version: 2.1\nName: vaws-env-fixture\nVersion: {version}\n",
            f"vaws_env_fixture-{version}.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            f"vaws_env_fixture-{version}.dist-info/entry_points.txt": "[console_scripts]\nvaws-env-fixture = vaws_env_fixture:main\n",
            f"vaws_env_fixture-{version}.dist-info/RECORD": "",
        }
        for name, data in files.items(): archive.writestr(name, data)
    url = (root / ".test-wheel-url").read_text(encoding="utf-8") + "/" + wheel.name
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'env-test'\nversion = '0'\nrequires-python = '>=3.11'\n"
        f"dependencies = ['vaws-env-fixture @ {url}']\n"
        "[project.optional-dependencies]\nexample = []\n[dependency-groups]\ndev = []\nother = []\n[tool.uv]\npackage = false\n",
        encoding="utf-8")
    result = subprocess.run(["uv", "lock", "--project", str(root), "--python", BASE],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def clean_environment():
    environment = dict(os.environ)
    for key in (envs.PIN_ENV, "VAWS_SKIP_VENV_REEXEC", "VAWS_VENV_REEXEC", "VIRTUAL_ENV"):
        environment.pop(key, None)
    environment["PYTHONPATH"] = str(LIB)
    return environment


def execute(python, code, environment=None):
    result = subprocess.run([str(python), "-X", "utf8", "-c", code], env=environment,
                            capture_output=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_ready_is_shared_between_checkouts_and_runtime_lookup_has_no_subprocess(workspace, monkeypatch):
    ready = envs.prepare_environment(workspace)
    assert execute(ready["python"], "import vaws_env_fixture;print(vaws_env_fixture.VALUE)") == "1"
    second = workspace.parent / "other checkout"
    shutil.copytree(workspace, second, ignore=shutil.ignore_patterns(".vaws-local", ".venv"))
    assert envs.prepare_environment(second)["key"] == ready["key"]
    monkeypatch.setattr(envs.subprocess, "run", lambda *args, **kwargs: pytest.fail("ready lookup spawned a probe"))
    monkeypatch.setattr(envs, "_install", lambda *args: pytest.fail("ready reuse attempted an install"))
    assert envs.native_ready(second) == ready
    assert envs.native_ready(workspace) == ready
    script = Path(ready["root"]) / ("Scripts/vaws-env-fixture.exe" if os.name == "nt" else "bin/vaws-env-fixture")
    assert script.is_file()
    if os.name != "nt": assert ready["root"] in script.read_text(encoding="utf-8")


def test_keys_cover_platform_abi_lock_selection_but_not_checkout_or_pytest_config(workspace):
    _, _, document, input_id, _ = envs._inputs(workspace)
    identity = envs._identity()
    selection = envs._selection(document)[0]
    key = envs._key(identity, input_id, selection)
    for field in ("platform", "arch", "abi", "python_version", "build"):
        assert envs._key({**identity, field: "different"}, input_id, selection) != key
    assert envs._key(identity, "changed-lock", selection) != key
    assert envs._key(identity, input_id, {**selection, "groups": ["other"]}) != key
    assert envs._key(identity, input_id, {**selection, "extras": ["example"]}) != key
    with (workspace / "pyproject.toml").open("a", encoding="utf-8") as stream:
        stream.write("\n[tool.pytest.ini_options]\naddopts = '-q'\n")
    assert envs._inputs(workspace)[3] == input_id
    left = envs._selection(document, options=("--no-default-groups", "--group", "other", "--group", "dev"))[0]
    right = envs._selection(document, groups=("dev", "other"))[0]
    assert left == right


@pytest.mark.parametrize("option", ["--active", "--inexact", "--frozen", "--no-sources", "--upgrade", "--with", "--editable"])
def test_unkeyed_install_mutations_are_rejected(workspace, option):
    with pytest.raises(envs.EnvironmentError, match="unsupported immutable sync option"):
        envs.prepare_environment(workspace, install_options=(option,))


def test_checkout_changes_during_install_do_not_change_frozen_inputs(workspace, monkeypatch):
    lock_before = (workspace / "uv.lock").read_bytes()
    install = envs._install
    def change_then_install(command, environment, lock_fd):
        frozen = Path(command[command.index("--project") + 1])
        assert (frozen / "uv.lock").read_bytes() == lock_before
        write_project(workspace, "2")
        install(command, environment, lock_fd)
    monkeypatch.setattr(envs, "_install", change_then_install)
    ready = envs.prepare_environment(workspace)
    assert ready["lock_sha256"] == hashlib.sha256(lock_before).hexdigest()
    assert execute(ready["python"], "import vaws_env_fixture;print(vaws_env_fixture.VALUE)") == "1"
    assert envs.native_ready(workspace, pin=ready["receipt"]) == ready
    with pytest.raises(envs.EnvironmentError): envs.native_ready(workspace)


def test_failed_install_never_publishes_ready_and_retry_succeeds(workspace, monkeypatch):
    install = envs._install
    def failure(command, environment, lock_fd):
        Path(environment["UV_PROJECT_ENVIRONMENT"]).mkdir(parents=True)
        raise envs.EnvironmentError("deliberate interrupted install")
    monkeypatch.setattr(envs, "_install", failure)
    with pytest.raises(envs.EnvironmentError, match="deliberate"):
        envs.prepare_environment(workspace)
    assert not list(envs._store().glob("*/" + envs.READY_NAME))
    monkeypatch.setattr(envs, "_install", install)
    assert envs.read_receipt(envs.prepare_environment(workspace)["receipt"])["platform"] == sys.platform


def test_two_processes_publish_one_environment_and_no_partial_ready(workspace):
    marker = workspace.parent / "installs.txt"
    code = (
        "import sys,time,json;from pathlib import Path;import vaws_environment as e\n"
        "install=e._install\n"
        "def slow(command,environment,lock_fd):\n"
        f"    with Path({str(marker)!r}).open('a') as stream: stream.write('install\\n')\n"
        "    time.sleep(1)\n"
        "    install(command,environment,lock_fd)\n"
        "e._install=slow\n"
        f"print(json.dumps(e.prepare_environment(Path({str(workspace)!r}))))\n")
    environment = clean_environment()
    first = subprocess.Popen([BASE, "-X", "utf8", "-c", code], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    second = subprocess.Popen([BASE, "-X", "utf8", "-c", code], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline: time.sleep(0.03)
    assert marker.exists()
    assert not list(envs._store().glob("*/" + envs.READY_NAME))
    replies = [process.communicate(timeout=30) for process in (first, second)]
    assert [first.returncode, second.returncode] == [0, 0], replies
    assert marker.read_text().splitlines() == ["install"]
    receipts = [json.loads(stdout) for stdout, _ in replies]
    assert receipts[0] == receipts[1]


def test_live_pinned_process_and_child_remain_old_while_new_entry_selects_new(workspace):
    old = envs.prepare_environment(workspace)
    trigger, started, answer = [workspace.parent / name for name in ("continue", "started", "answer.json")]
    code = (
        "import os,sys,time,json,subprocess;from pathlib import Path\n"
        "from vaws_venv import ensure_workspace_interpreter\n"
        f"ensure_workspace_interpreter(repo_root=Path({str(workspace)!r}))\n"
        f"Path({str(started)!r}).write_text(sys.prefix)\n"
        f"while not Path({str(trigger)!r}).exists(): time.sleep(.05)\n"
        "import vaws_env_fixture\n"
        "child=subprocess.check_output([sys.executable,'-c','import vaws_env_fixture;print(vaws_env_fixture.VALUE)'],text=True).strip()\n"
        f"Path({str(answer)!r}).write_text(json.dumps([vaws_env_fixture.VALUE,child,sys.prefix]))\n")
    environment = clean_environment()
    environment[envs.PIN_ENV] = old["receipt"]
    process = subprocess.Popen([old["python"], "-X", "utf8", "-c", code], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not started.exists() and time.monotonic() < deadline: time.sleep(.03)
        assert started.exists()
        write_project(workspace, "2")
        new = envs.prepare_environment(workspace)
        assert old["key"] != new["key"]
        trigger.touch()
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, (stdout, stderr)
        assert json.loads(answer.read_text()) == ["1", "1", old["root"]]
        entry = ("from pathlib import Path;from vaws_venv import ensure_workspace_interpreter;"
                 f"ensure_workspace_interpreter(repo_root=Path({str(workspace)!r}));"
                 "import vaws_env_fixture;print(vaws_env_fixture.VALUE)")
        assert execute(old["python"], entry, clean_environment()) == "2"
    finally:
        if process.poll() is None: process.kill(); process.wait()


def test_native_module_hop_preserves_unicode_flags_and_nonzero_exit(workspace):
    ready = envs.prepare_environment(workspace)
    module = workspace / "agent_entry.py"
    module.write_text(
        "import json,sys;from pathlib import Path\nfrom vaws_venv import ensure_workspace_interpreter\n"
        f"ensure_workspace_interpreter(repo_root=Path({str(workspace)!r}))\n"
        "print(json.dumps({'args':sys.argv[1:],'utf8':sys.flags.utf8_mode,'prefix':sys.prefix},ensure_ascii=False))\nsys.exit(7)\n",
        encoding="utf-8")
    environment = clean_environment()
    environment["PYTHONPATH"] = os.pathsep.join((str(LIB), str(workspace)))
    result = subprocess.run([BASE, "-X", "utf8", "-m", "agent_entry", "中文 path", "--business-option"],
                            env=environment, cwd=workspace, capture_output=True, encoding="utf-8", timeout=30)
    assert result.returncode == 7, result.stderr
    assert json.loads(result.stdout) == {"args": ["中文 path", "--business-option"], "utf8": 1, "prefix": ready["root"]}


def test_mutable_python_alias_is_resolved_before_environment_construction(workspace):
    actual = Path(BASE).resolve(strict=True)
    alias = workspace.parent / "mutable-python-alias"
    empty = workspace.parent / "different-python-home"
    empty.mkdir()
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(actual.parent), str(alias))
    else:
        alias.symlink_to(actual.parent, target_is_directory=True)
    ready = envs.prepare_environment(workspace, python=str(alias / actual.name))
    assert ready["base_python"] == str(actual)
    configuration = (Path(ready["root"]) / "pyvenv.cfg").read_text(encoding="utf-8")
    assert str(alias) not in configuration
    if os.name == "nt":
        os.rmdir(alias)
        _winapi.CreateJunction(str(empty), str(alias))
    else:
        alias.unlink()
        alias.symlink_to(empty, target_is_directory=True)
    assert execute(ready["python"], "import vaws_env_fixture;print(vaws_env_fixture.VALUE)") == "1"


def test_receipt_uses_physical_store_after_alias_changes(workspace, monkeypatch):
    physical = workspace.parent / "physical-store"
    physical.mkdir()
    alias = workspace.parent / "virtual-store"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(physical), str(alias))
    else:
        alias.symlink_to(physical, target_is_directory=True)
    monkeypatch.setenv("VAWS_ENV_HOME", str(alias))
    ready = envs.prepare_environment(workspace)
    assert Path(ready["store"]) == physical.resolve()
    for field in ("root", "store", "receipt"):
        assert Path(ready[field]) == Path(ready[field]).resolve(strict=True)
    if os.name == "nt": os.rmdir(alias)
    else: alias.unlink()
    alias.mkdir()
    # A fresh process no longer sees the old virtual mapping. Both its launch
    # path and the saved project selection still resolve to the published root.
    assert execute(ready["python"], "import vaws_env_fixture;print(vaws_env_fixture.VALUE)") == "1"
    assert envs.native_ready(workspace) == ready


def test_receipt_tampering_and_cross_platform_pins_are_rejected(workspace):
    ready = envs.prepare_environment(workspace)
    with pytest.raises(envs.EnvironmentError, match="belongs to"):
        envs.read_receipt(ready["receipt"], expected_platform="a-different-platform")
    copied = workspace / envs.READY_NAME
    copied.write_text(json.dumps(ready), encoding="utf-8")
    with pytest.raises(envs.EnvironmentError, match="content-addressed root"):
        envs.read_receipt(copied)


def test_hard_killed_builder_cannot_publish_and_retry_waits_for_owned_install(workspace):
    marker = workspace.parent / "install-started"
    child_code = ("import os,time;from pathlib import Path;"
                  "Path(os.environ['UV_PROJECT_ENVIRONMENT']).mkdir(parents=True);"
                  f"Path({str(marker)!r}).write_text(str(os.getpid()));time.sleep(2)")
    code = (
        "from pathlib import Path;import vaws_environment as e\ninstall=e._install\n"
        f"def slow(command,environment,lock_fd): install({[BASE, '-X', 'utf8', '-c', child_code]!r},environment,lock_fd)\n"
        "e._install=slow\n"
        f"e.prepare_environment(Path({str(workspace)!r}))\n")
    process = subprocess.Popen([BASE, "-X", "utf8", "-c", code], env=clean_environment(),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline: time.sleep(.03)
        assert marker.exists()
        assert not list(envs._store().glob("*/" + envs.READY_NAME))
        process.kill()
        process.wait(timeout=10)
        started = time.monotonic()
        ready = envs.prepare_environment(workspace)
        # POSIX installers inherit the lock, even after SIGKILL of the parent.
        # Windows closes its job and kills the owned installer before retry.
        if os.name != "nt": assert time.monotonic() - started >= 1
        assert execute(ready["python"], "import vaws_env_fixture;print(vaws_env_fixture.VALUE)") == "1"
    finally:
        if process.poll() is None: process.kill()
        process.communicate(timeout=10)
