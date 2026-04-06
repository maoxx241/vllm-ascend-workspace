#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from common import json_dump, load_state, now_utc, repo_root_from, save_state


FILENAME = 'install-consents.json'


def load_consent_state(repo_root: Path) -> dict[str, Any]:
    return load_state(repo_root, FILENAME, {'schema_version': 1, 'consents': {}})


def save_consent_state(repo_root: Path, state: dict[str, Any]) -> Path:
    return save_state(repo_root, FILENAME, state)


def set_decision(
    state: dict[str, Any],
    server_name: str,
    container_identity: str,
    decision: str,
    note: str | None,
    *,
    approved_by_user: bool,
) -> None:
    if not approved_by_user:
        raise RuntimeError('explicit --approved-by-user is required before writing first-install consent')
    containers = state.setdefault('consents', {}).setdefault(server_name, {}).setdefault('containers', {})
    containers[container_identity] = {
        'decision': decision,
        'updated_at': now_utc(),
        'note': note or '',
        'approved_by_user': True,
    }


def run_resolve(args: argparse.Namespace) -> int:
    repo_root = repo_root_from(Path(args.repo_root))
    state = load_consent_state(repo_root)
    record = state.get('consents', {}).get(args.server_name, {}).get('containers', {}).get(args.container_identity)
    payload = {
        'server_name': args.server_name,
        'container_identity': args.container_identity,
        'decision': record.get('decision') if record else 'unknown',
        'record': record,
    }
    print(json_dump(payload))
    return 0


def run_set(args: argparse.Namespace) -> int:
    repo_root = repo_root_from(Path(args.repo_root))
    state = load_consent_state(repo_root)
    set_decision(
        state,
        args.server_name,
        args.container_identity,
        args.decision,
        args.note,
        approved_by_user=args.approved_by_user,
    )
    path = save_consent_state(repo_root, state)
    print(json_dump({'status': 'updated', 'path': str(path)}))
    return 0


def run_batch_set(args: argparse.Namespace) -> int:
    repo_root = repo_root_from(Path(args.repo_root))
    state = load_consent_state(repo_root)
    items = json.loads(Path(args.input).read_text(encoding='utf-8'))
    if not isinstance(items, list):
        raise RuntimeError('batch-set input must be a JSON list')
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError('each batch-set entry must be an object')
        set_decision(
            state,
            item['server_name'],
            item['container_identity'],
            item['decision'],
            item.get('note'),
            approved_by_user=args.approved_by_user,
        )
    path = save_consent_state(repo_root, state)
    print(json_dump({'status': 'updated', 'count': len(items), 'path': str(path)}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Manage first-install consent for remote-code-parity.')
    subparsers = parser.add_subparsers(dest='command', required=True)

    def add_common(target: argparse.ArgumentParser) -> None:
        target.add_argument('--repo-root', default='.')
        target.add_argument('--server-name', required=True)
        target.add_argument('--container-identity', required=True)

    resolve = subparsers.add_parser('resolve')
    add_common(resolve)

    set_parser = subparsers.add_parser('set')
    add_common(set_parser)
    set_parser.add_argument('--decision', required=True, choices=('allow', 'deny'))
    set_parser.add_argument('--note', default=None)
    set_parser.add_argument('--approved-by-user', action='store_true')

    batch = subparsers.add_parser('batch-set')
    batch.add_argument('--repo-root', default='.')
    batch.add_argument('--input', required=True)
    batch.add_argument('--approved-by-user', action='store_true')

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == 'resolve':
            return run_resolve(args)
        if args.command == 'set':
            return run_set(args)
        if args.command == 'batch-set':
            return run_batch_set(args)
        parser.error(f'unsupported command: {args.command}')
        return 2
    except Exception as exc:
        print(json_dump({'status': 'failed', 'reason': str(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
