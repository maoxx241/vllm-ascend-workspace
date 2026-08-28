"""Shared native-input identity used by parity and prepared-runtime verification."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import subprocess
from pathlib import Path

VLLM_REINSTALL_PATTERNS = (
    'requirements*', 'pyproject.toml', 'setup.py', 'setup.cfg', 'CMakeLists.txt',
    'cmake/**', 'csrc/**', '**/*.cu', '**/*.cuh', '**/*.cpp', '**/*.cc', '**/*.h', '**/*.hpp',
    '*.c', '*.cc', '*.cpp', '*.cxx', '*.cu', '*.cuh', '*.h', '*.hpp', '*.hxx', '*.s', '*.S', '*.cmake', '*.proto',
)
VLLM_ASCEND_REINSTALL_PATTERNS = VLLM_REINSTALL_PATTERNS + ('vllm_ascend/_cann_ops_custom/**',)
DEPENDENCY_INSTALL_PATTERNS = ('requirements*', 'pyproject.toml', 'setup.py', 'setup.cfg')
BUILD_INPUT_ENV_KEYS = (
    'CMAKE_BUILD_TYPE', 'SOC_VERSION', 'VAWS_SOC_VERSION',
    'COMPILE_CUSTOM_KERNELS', 'VAWS_COMPILE_CUSTOM_KERNELS', 'VAWS_USE_CLANG15',
    'C_COMPILER', 'CXX_COMPILER', 'CFLAGS', 'CXXFLAGS', 'ASCEND_HOME_PATH',
    'VAWS_ENVIRONMENT_FINGERPRINT',
    'CMAKE_ARGS', 'LDFLAGS', 'CC', 'CXX', 'VLLM_TARGET_DEVICE',
)


def build_input_fingerprints(repo: Path, commit: str, patterns: tuple[str, ...], *, build_env=None) -> dict[str, str]:
    native, dependencies = [], []
    tree = subprocess.check_output(['git', '-C', str(repo), 'ls-tree', '-r', '-z', commit], text=True)
    for entry in filter(None, tree.split('\0')):
        metadata, path = entry.split('\t', 1)
        mode, kind, oid = metadata.split()
        token = f'{mode}\0{path}\0{oid}'
        if kind == 'commit' or any(fnmatch.fnmatch(path, pattern) for pattern in patterns):
            native.append(token)
        if any(fnmatch.fnmatch(path, pattern) for pattern in DEPENDENCY_INSTALL_PATTERNS):
            dependencies.append(token)
    source = os.environ if build_env is None else build_env
    environment = {key: source[key] for key in BUILD_INPUT_ENV_KEYS if key in source}
    return {
        'native': hashlib.sha256(json.dumps(sorted(native)).encode()).hexdigest(),
        'dependencies': hashlib.sha256(json.dumps(sorted(dependencies)).encode()).hexdigest(),
        'build_env': hashlib.sha256(json.dumps(environment, sort_keys=True).encode()).hexdigest(),
    }


def runtime_build_inputs(root: Path, profile: dict, fingerprint: str) -> dict:
    environment = {**profile['build_env']}
    environment['VAWS_ENVIRONMENT_FINGERPRINT'] = fingerprint
    return {name: build_input_fingerprints(root / name, 'HEAD', patterns, build_env=environment)
            for name, patterns in [('vllm', VLLM_REINSTALL_PATTERNS), ('vllm-ascend', VLLM_ASCEND_REINSTALL_PATTERNS)]}
