#!/usr/bin/env python3
"""Compare full snapshot bundles with incremental Git receive-pack transport."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import tempfile
import time
from pathlib import Path


def run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    input_bytes: bytes | None = None,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        input=input_bytes,
        stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"stderr:\n{result.stderr.decode('utf-8', errors='replace')}"
        )
    return result


def git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    return run(['git', '-C', str(repo), *args], input_bytes=input_bytes).stdout


def deterministic_bytes(file_index: int, size: int) -> bytes:
    output = bytearray()
    block = 0
    while len(output) < size:
        output.extend(hashlib.sha256(f'{file_index}:{block}'.encode()).digest())
        block += 1
    return bytes(output[:size])


def snapshot(repo: Path, ref: str, message: str) -> str:
    git(repo, 'add', '-A')
    tree = git(repo, 'write-tree').decode().strip()
    env = os.environ.copy()
    env.update(
        {
            'GIT_AUTHOR_NAME': 'parity-benchmark',
            'GIT_AUTHOR_EMAIL': 'parity-benchmark@example.invalid',
            'GIT_AUTHOR_DATE': '1970-01-01T00:00:00Z',
            'GIT_COMMITTER_NAME': 'parity-benchmark',
            'GIT_COMMITTER_EMAIL': 'parity-benchmark@example.invalid',
            'GIT_COMMITTER_DATE': '1970-01-01T00:00:00Z',
        }
    )
    result = subprocess.run(
        ['git', '-C', str(repo), 'commit-tree', tree, '-m', message],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    commit = result.stdout.strip()
    git(repo, 'update-ref', ref, commit)
    return commit


def carrier(repo: Path, ref: str, tree: str, parent: str) -> str:
    env = os.environ.copy()
    env.update(
        {
            'GIT_AUTHOR_NAME': 'parity-benchmark',
            'GIT_AUTHOR_EMAIL': 'parity-benchmark@example.invalid',
            'GIT_AUTHOR_DATE': '1970-01-01T00:00:00Z',
            'GIT_COMMITTER_NAME': 'parity-benchmark',
            'GIT_COMMITTER_EMAIL': 'parity-benchmark@example.invalid',
            'GIT_COMMITTER_DATE': '1970-01-01T00:00:00Z',
        }
    )
    result = subprocess.run(
        [
            'git',
            '-C',
            str(repo),
            'commit-tree',
            tree,
            '-p',
            parent,
            '-m',
            'transport carrier',
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    commit = result.stdout.strip()
    git(repo, 'update-ref', ref, commit)
    return commit


def init_bare(path: Path, repo: Path, old_ref: str) -> None:
    run(['git', 'init', '--bare', str(path)])
    git(
        repo,
        'push',
        '--force',
        f'file://{path}',
        f'{old_ref}:refs/parity/bench/current',
        f'{old_ref}:refs/heads/parity-current',
        f'{old_ref}:refs/parity/bench/transport-carrier',
    )


def directory_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob('*') if item.is_file())


def first_sync_round(
    root: Path,
    repo: Path,
    old_ref: str,
    round_id: int,
) -> dict[str, float | int]:
    bundle_mirror = root / f'first-bundle-mirror-{round_id}.git'
    git_mirror = root / f'first-git-mirror-{round_id}.git'
    run(['git', 'init', '--bare', str(bundle_mirror)])
    run(['git', 'init', '--bare', str(git_mirror)])

    bundle = root / f'first-snapshot-{round_id}.bundle'
    uploaded = root / f'first-uploaded-{round_id}.bundle'
    started = time.perf_counter()
    git(repo, 'bundle', 'create', str(bundle), old_ref)
    shutil.copyfile(bundle, uploaded)
    run(
        [
            'git',
            '-C',
            str(bundle_mirror),
            'fetch',
            '--force',
            str(uploaded),
            f'{old_ref}:refs/parity/bench/current',
            f'{old_ref}:refs/heads/parity-current',
        ]
    )
    bundle_seconds = time.perf_counter() - started

    started = time.perf_counter()
    git(
        repo,
        'push',
        '--porcelain',
        '--force',
        f'file://{git_mirror}',
        f'{old_ref}:refs/parity/bench/current',
        f'{old_ref}:refs/heads/parity-current',
        f'{old_ref}:refs/parity/bench/transport-carrier',
    )
    git_seconds = time.perf_counter() - started
    bundle_bytes = bundle.stat().st_size
    return {
        'bundle_seconds_local_no_rtt': bundle_seconds,
        'git_push_seconds_local': git_seconds,
        'full_bundle_bytes': bundle_bytes,
        'git_remote_bytes': directory_bytes(git_mirror),
    }


def one_round(
    root: Path,
    repo: Path,
    old_ref: str,
    old_commit: str,
    new_ref: str,
    new_commit: str,
    carrier_ref: str,
    carrier_commit: str,
    round_id: int,
) -> dict[str, float | int]:
    bundle_mirror = root / f'bundle-mirror-{round_id}.git'
    git_mirror = root / f'git-mirror-{round_id}.git'
    init_bare(bundle_mirror, repo, old_ref)
    init_bare(git_mirror, repo, old_ref)

    bundle = root / f'snapshot-{round_id}.bundle'
    uploaded = root / f'uploaded-{round_id}.bundle'
    started = time.perf_counter()
    git(repo, 'bundle', 'create', str(bundle), new_ref)
    shutil.copyfile(bundle, uploaded)
    run(
        [
            'git',
            '-C',
            str(bundle_mirror),
            'fetch',
            '--force',
            str(uploaded),
            f'{new_ref}:refs/parity/bench/current',
            f'{new_ref}:refs/heads/parity-current',
        ]
    )
    bundle_seconds = time.perf_counter() - started

    before = directory_bytes(git_mirror)
    started = time.perf_counter()
    git(
        repo,
        'push',
        '--porcelain',
        '--force',
        f'file://{git_mirror}',
        f'{new_ref}:refs/parity/bench/current',
        f'{new_ref}:refs/heads/parity-current',
        f'{carrier_ref}:refs/parity/bench/transport-carrier',
    )
    git_seconds = time.perf_counter() - started
    after = directory_bytes(git_mirror)

    incremental_pack = run(
        [
            'git',
            '-C',
            str(repo),
            'pack-objects',
            '--thin',
            '--stdout',
            '--revs',
        ],
        input_bytes=f'{new_commit}\n{carrier_commit}\n^{old_commit}\n'.encode(),
    ).stdout
    bundle_bytes = bundle.stat().st_size
    return {
        'bundle_seconds_local_no_rtt': bundle_seconds,
        'git_push_seconds_local': git_seconds,
        'full_bundle_bytes': bundle_bytes,
        'incremental_pack_bytes': len(incremental_pack),
        'git_remote_disk_growth_bytes': max(0, after - before),
    }


def median(records: list[dict[str, float | int]], key: str) -> float:
    return statistics.median(float(record[key]) for record in records)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--files', type=int, default=256)
    parser.add_argument('--bytes-per-file', type=int, default=4096)
    parser.add_argument('--changed-bytes', type=int, default=4096)
    parser.add_argument('--repeats', type=int, default=3)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if min(args.files, args.bytes_per_file, args.changed_bytes, args.repeats) <= 0:
        raise SystemExit('all size and repeat arguments must be positive')
    with tempfile.TemporaryDirectory(prefix='parity-transport-bench-') as temp:
        root = Path(temp)
        repo = root / 'source'
        run(['git', 'init', str(repo)])
        data = repo / 'data'
        data.mkdir()
        for index in range(args.files):
            (data / f'{index:05d}.bin').write_bytes(
                deterministic_bytes(index, args.bytes_per_file)
            )

        old_ref = 'refs/bench/old'
        new_ref = 'refs/bench/new'
        old_commit = snapshot(repo, old_ref, 'old snapshot')
        (data / '00000.bin').write_bytes(
            deterministic_bytes(args.files + 1, args.changed_bytes)
        )
        new_commit = snapshot(repo, new_ref, 'new snapshot')
        new_tree = git(repo, 'rev-parse', f'{new_commit}^{{tree}}').decode().strip()
        carrier_ref = 'refs/bench/carrier'
        carrier_commit = carrier(repo, carrier_ref, new_tree, old_commit)

        first_sync_records = [
            first_sync_round(root, repo, old_ref, round_id)
            for round_id in range(args.repeats)
        ]
        records = [
            one_round(
                root,
                repo,
                old_ref,
                old_commit,
                new_ref,
                new_commit,
                carrier_ref,
                carrier_commit,
                round_id,
            )
            for round_id in range(args.repeats)
        ]

        full_bytes = int(median(records, 'full_bundle_bytes'))
        incremental_bytes = int(median(records, 'incremental_pack_bytes'))
        payload_reduction = (
            full_bytes / incremental_bytes if incremental_bytes else None
        )
        payload = {
            'fixture': {
                'files': args.files,
                'bytes_per_file': args.bytes_per_file,
                'changed_bytes': args.changed_bytes,
                'snapshot_payload_bytes': args.files * args.bytes_per_file,
                'repeats': args.repeats,
            },
            'first_sync_median': {
                'bundle_seconds_local_no_rtt': median(
                    first_sync_records, 'bundle_seconds_local_no_rtt'
                ),
                'git_push_seconds_local': median(
                    first_sync_records, 'git_push_seconds_local'
                ),
                'full_bundle_bytes': int(
                    median(first_sync_records, 'full_bundle_bytes')
                ),
            },
            'incremental_sync_median': {
                'bundle_seconds_local_no_rtt': median(
                    records, 'bundle_seconds_local_no_rtt'
                ),
                'git_push_seconds_local': median(records, 'git_push_seconds_local'),
                'full_bundle_bytes': full_bytes,
                'incremental_pack_bytes': incremental_bytes,
                'git_remote_disk_growth_bytes': int(
                    median(records, 'git_remote_disk_growth_bytes')
                ),
                'payload_reduction_ratio': payload_reduction,
            },
            'first_sync_rounds': first_sync_records,
            'incremental_sync_rounds': records,
            'note': (
                'Local elapsed times exclude network RTT and compare filesystem-local '
                'bundle import with filesystem-local Git receive-pack.'
            ),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
