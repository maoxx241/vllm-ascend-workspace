#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from common import WORKSPACE_ID_PATTERN, json_dump, repo_root_from
from remote_code_parity import DEFAULT_CONTAINER_CACHE_ROOT


DEFAULT_CONTAINER_USER = 'root'


def derive_workspace_id(repo_root: Path) -> str:
    base = WORKSPACE_ID_PATTERN.sub('-', repo_root.name.lower()).strip('.-') or 'workspace'
    digest = hashlib.sha1(str(repo_root.resolve()).encode('utf-8')).hexdigest()[:8]
    return f'{base}-{digest}'


def canonical_inventory_path(repo_root: Path) -> Path:
    return repo_root / '.vaws-local' / 'machine-inventory.json'


def legacy_inventory_path(repo_root: Path) -> Path:
    return repo_root / '.machine-inventory.json'


def load_machine_inventory(repo_root: Path) -> dict[str, Any]:
    for path in (canonical_inventory_path(repo_root), legacy_inventory_path(repo_root)):
        if path.exists():
            return json.loads(path.read_text(encoding='utf-8'))
    return {'schema_version': 1, 'machines': []}


def resolve_machine_record(inventory: dict[str, Any], identifier: str) -> dict[str, Any]:
    matches = []
    for record in inventory.get('machines', []):
        if record.get('alias') == identifier or record.get('host', {}).get('ip') == identifier:
            matches.append(record)
    if not matches:
        raise RuntimeError(f'machine {identifier!r} was not found in local inventory')
    if len(matches) > 1:
        raise RuntimeError(f'machine {identifier!r} matched multiple inventory records')
    return matches[0]


def build_derived_args(repo_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    inventory = load_machine_inventory(repo_root)
    record = resolve_machine_record(inventory, args.machine)
    runtime_root = args.runtime_root or record.get('container', {}).get('workdir') or '/vllm-workspace'
    workspace_id = args.workspace_id or derive_workspace_id(repo_root)
    server_name = record.get('alias') or record.get('host', {}).get('ip')
    container_name = record.get('container', {}).get('name')
    if not container_name:
        raise RuntimeError(f'machine {args.machine!r} is missing container.name in inventory')
    container_port = record.get('container', {}).get('ssh_port')
    if not isinstance(container_port, int):
        raise RuntimeError(f'machine {args.machine!r} is missing container.ssh_port in inventory')
    container_host = record.get('host', {}).get('ip')
    if not container_host:
        raise RuntimeError(f'machine {args.machine!r} is missing host.ip in inventory')
    container_identity = f'{container_name}@{runtime_root}'
    return {
        'workspace_root': str(repo_root),
        'workspace_id': workspace_id,
        'server_name': server_name,
        'runtime_root': runtime_root,
        'container_identity': container_identity,
        'container_cache_root': args.container_cache_root,
        'container_host': container_host,
        'container_port': container_port,
        'container_user': args.container_user,
        'preserve_path': list(args.preserve_path),
        'machine_record': record,
        'inventory_path': str(canonical_inventory_path(repo_root) if canonical_inventory_path(repo_root).exists() else legacy_inventory_path(repo_root)),
    }


def build_low_level_command(derived: dict[str, Any], args: argparse.Namespace) -> list[str]:
    script_path = Path(__file__).with_name('remote_code_parity.py')
    cmd = [
        sys.executable,
        str(script_path),
        'sync',
        '--workspace-root', derived['workspace_root'],
        '--workspace-id', derived['workspace_id'],
        '--server-name', derived['server_name'],
        '--runtime-root', derived['runtime_root'],
        '--container-identity', derived['container_identity'],
        '--container-cache-root', derived['container_cache_root'],
        '--container-host', derived['container_host'],
        '--container-port', str(derived['container_port']),
        '--container-user', derived['container_user'],
    ]
    for preserve_path in derived['preserve_path']:
        cmd.extend(['--preserve-path', preserve_path])
    if args.snapshot_id:
        cmd.extend(['--snapshot-id', args.snapshot_id])
    if args.print_manifest:
        cmd.append('--print-manifest')
    if args.force_reinstall:
        cmd.append('--force-reinstall')
    if args.dry_run:
        cmd.append('--dry-run')
    return cmd


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Resolve a managed machine from inventory and run container-only remote-code-parity sync.', allow_abbrev=False)
    parser.add_argument('--machine', required=True, help='machine alias or host IP from inventory')
    parser.add_argument('--repo-root', default='.')
    parser.add_argument('--workspace-id', default=None)
    parser.add_argument('--runtime-root', default=None)
    parser.add_argument('--container-user', default=DEFAULT_CONTAINER_USER)
    parser.add_argument('--container-cache-root', default=DEFAULT_CONTAINER_CACHE_ROOT)
    parser.add_argument('--preserve-path', action='append', default=[])
    parser.add_argument('--snapshot-id', default=None)
    parser.add_argument('--print-manifest', action='store_true')
    parser.add_argument('--force-reinstall', action='store_true', help='Force reinstall of vllm and vllm-ascend regardless of what changed.')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--print-derived-args', action='store_true')
    return parser


def main() -> int:
    args = build_parser().parse_args()
    repo_root = repo_root_from(Path(args.repo_root))
    derived = build_derived_args(repo_root, args)
    low_level_cmd = build_low_level_command(derived, args)
    if args.print_derived_args:
        payload = dict(derived)
        payload['command'] = low_level_cmd
        print(json_dump(payload))
        return 0
    result = subprocess.run(low_level_cmd)
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
