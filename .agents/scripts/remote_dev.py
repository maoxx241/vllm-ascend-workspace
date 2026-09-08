#!/usr/bin/env python3
"""Launch the remote-dev substrate from its external checkout with scaffold wiring.

remote-dev (`vllm-ascend-workspace/remote-dev`) is no longer vendored under
`.remote-dev/`. Every client configuration in this repository goes through this
launcher so that one place knows where the checkout is and which environment
the substrate needs to behave like the old in-tree copy:

    REMOTE_DEV_RESOLVERS        the scaffold resolver (machine / session_id /
                                session_file / worktree auto-bind)
    REMOTE_DEV_RUNTIME_ENV_FILE the Ascend profile sourced before commands
    REMOTE_DEV_STATE_DIR        <repo>/.vaws-local/remote-dev-state
    REMOTE_DEV_SSH_MUX_DIR      ~/.ssh/vaws-mux, shared with vaws_ssh
    REMOTE_DEV_DEFAULT_*        user / root / cwd defaults

Values already present in the environment (for example from `.mcp.json`) win.

Subcommands:

    status              JSON: checkout location, revision, pin drift
    bootstrap           clone / fast-forward the pinned revision
    server              exec the stdio MCP server (`mcp/server.py`)
    hook {claude,codex} exec the client PreToolUse guard; allows when the
                        checkout is missing (guards are observe-only)
    tool NAME [ARGS..]  exec `tools/NAME.py`; `--machine X`, `--session-id X`
                        and `--session-file X` are rewritten to
                        `--selector key=X` so older recipes keep working
    env [--json]        print the substrate environment

Progress goes to stderr; `status`, `bootstrap` and `env --json` print one JSON
object on stdout. `server`, `hook` and `tool` replace this process.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents" / "lib"))
from vaws_dependency import (  # noqa: E402
    USABLE_STATES,
    hook_skip_message,
    inspect,
    record_hook_degradation,
    status_exit_code,
)
from vaws_remote_dev import (  # noqa: E402
    REMOTE_DEV_ROOT_ENV,
    RemoteDevUnavailable,
    checkout_commit,
    checkout_status,
    default_checkout_dir,
    load_dependency,
    looks_like_checkout,
    remote_dev_root,
    substrate_environment,
)

HOOKS = {"claude": "hooks/claude_remote_guard.py", "codex": "hooks/codex_remote_guard.py"}
LEGACY_SELECTOR_FLAGS = {"--machine": "machine", "--session-id": "session_id", "--session-file": "session_file"}


def progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def translate_legacy_selectors(argv: list[str]) -> list[str]:
    """Rewrite the substrate's former selector flags into `--selector KEY=VALUE`.

    The standalone CLI wrappers only know `--selector`; the scaffold resolver
    turns the selector back into an endpoint. Both `--flag value` and
    `--flag=value` forms are handled; everything else passes through.
    """
    translated: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        flag, _, inline = item.partition("=")
        if flag in LEGACY_SELECTOR_FLAGS:
            key = LEGACY_SELECTOR_FLAGS[flag]
            if _:
                value = inline
            else:
                index += 1
                if index >= len(argv):
                    raise ValueError(f"{flag} requires a value")
                value = argv[index]
            translated.extend(["--selector", f"{key}={value}"])
        else:
            translated.append(item)
        index += 1
    return translated


def _exec(root: Path, relative: str, args: list[str]) -> int:
    script = root / relative
    if not script.is_file():
        progress(f"remote-dev checkout {root} has no {relative}")
        return 2
    env = substrate_environment()
    command = [sys.executable, str(script), *args]
    progress(f"exec {shlex.join([str(script), *args])}")
    os.execve(sys.executable, command, env)
    return 0  # pragma: no cover - execve does not return


def cmd_status(args: argparse.Namespace) -> int:
    payload = checkout_status()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return status_exit_code({"remote-dev": payload["state"]})


def _git(*argv: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *argv], cwd=str(cwd) if cwd else None, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, check=False)


def cmd_bootstrap(args: argparse.Namespace) -> int:
    pin = load_dependency()
    dest = Path(args.dest).expanduser() if args.dest else Path(os.environ.get(REMOTE_DEV_ROOT_ENV) or default_checkout_dir()).expanduser()
    url = args.url or pin["url"]
    ref = args.ref or pin["ref"]
    commit = None if args.track_ref else (args.commit or pin["commit"])
    payload = {"dest": str(dest), "url": url, "ref": ref, "pinned_commit": commit}
    if dest.exists() and any(dest.iterdir()) and not (dest / ".git").exists():
        print(json.dumps({**payload, "state": "blocked", "error": f"{dest} exists and is not a git checkout"}))
        return 2
    if not (dest / ".git").exists():
        progress(f"cloning {pin['repository']} ({ref}) into {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        result = _git("clone", "--quiet", "--branch", ref, url, str(dest))
        if result.returncode != 0 and not args.url and shutil.which("gh"):
            # The repository is private, so a bare HTTPS clone prompts for
            # credentials in a non-interactive shell. `gh` already holds a
            # token for whoever was granted access.
            progress("git clone failed; retrying through gh with its stored credentials")
            gh = subprocess.run(["gh", "repo", "clone", pin["repository"], str(dest), "--", "--quiet", "--branch", ref],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
            result = gh if gh.returncode == 0 else result
        if result.returncode != 0:
            print(json.dumps({**payload, "state": "failed", "error": result.stderr.strip()[-2000:],
                              "hint": "the repository is private; run `gh auth login` (or `gh auth setup-git`), "
                                      "pass --url with a URL your git can authenticate, or clone it by hand and set "
                                      f"{REMOTE_DEV_ROOT_ENV}"}))
            return 1
    progress(f"fetching {ref} in {dest}")
    result = _git("fetch", "--quiet", "origin", ref, cwd=dest)
    if result.returncode != 0:
        print(json.dumps({**payload, "state": "failed", "error": result.stderr.strip()[-2000:]}))
        return 1
    if commit is None:
        result = _git("checkout", "--quiet", "-B", ref, "FETCH_HEAD", cwd=dest)
    else:
        result = _git("checkout", "--quiet", "--detach", commit, cwd=dest)
    if result.returncode != 0:
        print(json.dumps({**payload, "state": "failed", "error": result.stderr.strip()[-2000:],
                          "hint": f"pinned commit {commit or ref} is not reachable from {ref}; the substrate history may have been rewritten - update .agents/deps/remote-dev.json"}))
        return 1
    head = checkout_commit(dest)
    state = "ready" if looks_like_checkout(dest) else "invalid"
    print(json.dumps({**payload, "state": state, "commit": head, "pin_matches": (head == commit) if commit else None,
                      "next": f"export {REMOTE_DEV_ROOT_ENV}={shlex.quote(str(dest))} if this is not the default location"}, ensure_ascii=False))
    return 0 if state == "ready" else 1


def cmd_server(args: argparse.Namespace) -> int:
    try:
        root = remote_dev_root()
    except RemoteDevUnavailable as exc:
        progress(f"remote-dev MCP server cannot start: {exc}")
        return 2
    return _exec(root, "mcp/server.py", [])


def cmd_hook(args: argparse.Namespace) -> int:
    info = inspect("remote-dev")
    if info["state"] not in USABLE_STATES:
        # Guards default to allow and only observe; a missing substrate must
        # not block the client's own tools. Consume stdin so the client does
        # not see a broken pipe, and return an allow decision (empty output).
        sys.stdin.read()
        progress(hook_skip_message("remote-dev", info))
        record_hook_degradation(hook="remote_dev", dep="remote-dev", state=info["state"])
        return 0
    return _exec(Path(info["path"]), HOOKS[args.client], [])


def cmd_tool(args: argparse.Namespace) -> int:
    try:
        root = remote_dev_root()
    except RemoteDevUnavailable as exc:
        progress(str(exc))
        return 2
    name = args.name[:-3] if args.name.endswith(".py") else args.name
    if not name.startswith("remote_"):
        name = "remote_" + name
    try:
        argv = translate_legacy_selectors(list(args.args))
    except ValueError as exc:
        progress(str(exc))
        return 2
    return _exec(root, f"tools/{name}.py", argv)


def cmd_env(args: argparse.Namespace) -> int:
    env = substrate_environment()
    keys = sorted(key for key in env if key.startswith("REMOTE_DEV_"))
    if args.json:
        print(json.dumps({key: env[key] for key in keys}, ensure_ascii=False, indent=2))
    else:
        for key in keys:
            print(f"{key}={shlex.quote(env[key])}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="report the configured checkout and pin drift")
    status.set_defaults(func=cmd_status)

    bootstrap = sub.add_parser("bootstrap", help="clone or update the pinned substrate checkout")
    bootstrap.add_argument("--dest", help=f"checkout directory (default: ${REMOTE_DEV_ROOT_ENV} or the shared .vaws-local/remote-dev)")
    bootstrap.add_argument("--url", help="override the repository URL from .agents/deps/remote-dev.json")
    bootstrap.add_argument("--ref", help="override the branch from .agents/deps/remote-dev.json")
    bootstrap.add_argument("--commit", help="override the pinned commit")
    bootstrap.add_argument("--track-ref", action="store_true", help="check out the branch tip instead of the pinned commit")
    bootstrap.set_defaults(func=cmd_bootstrap)

    server = sub.add_parser("server", help="exec the stdio MCP server")
    server.set_defaults(func=cmd_server)

    hook = sub.add_parser("hook", help="exec a client PreToolUse guard")
    hook.add_argument("client", choices=sorted(HOOKS))
    hook.set_defaults(func=cmd_hook)

    tool = sub.add_parser("tool", help="exec a CLI wrapper, e.g. `tool remote_bash --selector machine=<alias> --command nproc`")
    tool.add_argument("name", help="wrapper name such as remote_bash (the remote_ prefix is optional)")
    tool.add_argument("args", nargs=argparse.REMAINDER, help="arguments passed to the wrapper")
    tool.set_defaults(func=cmd_tool)

    env = sub.add_parser("env", help="print the substrate environment")
    env.add_argument("--json", action="store_true")
    env.set_defaults(func=cmd_env)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
