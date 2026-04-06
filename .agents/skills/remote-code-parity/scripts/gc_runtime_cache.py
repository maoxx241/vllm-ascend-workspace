#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from common import SshEndpoint, json_dump, quoted, ssh_exec
from remote_code_parity import DEFAULT_CONTAINER_CACHE_ROOT, cache_workspace_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Prune old remote-code-parity manifests inside the container-local cache root.')
    parser.add_argument('--container-host', required=True)
    parser.add_argument('--container-port', type=int, required=True)
    parser.add_argument('--container-user', required=True)
    parser.add_argument('--workspace-id', required=True)
    parser.add_argument('--container-cache-root', default=DEFAULT_CONTAINER_CACHE_ROOT)
    parser.add_argument('--keep-manifests', type=int, default=5)
    parser.add_argument('--dry-run', action='store_true')
    return parser


def run_gc(args: argparse.Namespace) -> int:
    container = SshEndpoint(host=args.container_host, port=args.container_port, user=args.container_user)
    workspace_root = cache_workspace_root(args.container_cache_root, args.workspace_id)
    manifests_dir = str(Path(workspace_root) / 'manifests')
    script = '\n'.join(
        [
            'set -eo pipefail',
            f"python3 - {quoted(manifests_dir)} {args.keep_manifests} {1 if args.dry_run else 0} <<'PY'",
            'import json',
            'import os',
            'import sys',
            'from pathlib import Path',
            'manifests_dir = Path(sys.argv[1])',
            'keep = int(sys.argv[2])',
            'dry_run = bool(int(sys.argv[3]))',
            "files = sorted([item for item in manifests_dir.iterdir() if item.is_file()], key=lambda item: item.stat().st_mtime_ns, reverse=True) if manifests_dir.exists() else []",
            'kept = [str(item) for item in files[:keep]]',
            'removed = [str(item) for item in files[keep:]]',
            'if not dry_run:',
            '    for item in files[keep:]:',
            '        item.unlink(missing_ok=True)',
            "print(json.dumps({'workspace_root': str(manifests_dir.parent), 'kept': kept, 'removed': removed}, indent=2, sort_keys=True))",
            'PY',
        ]
    )
    result = ssh_exec(container, script)
    payload: dict[str, Any] = json.loads(result.stdout)
    payload['dry_run'] = args.dry_run
    print(json_dump(payload))
    return 0


def main() -> int:
    return run_gc(build_parser().parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
