"""Read business configuration from the owner's recorded vLLM launch command."""
from __future__ import annotations

from copy import deepcopy
import shlex


def option(argv, name, default=None):
    value = default
    for index, token in enumerate(argv):
        if token == name and index + 1 < len(argv):
            value = argv[index + 1]
        elif token.startswith(name + "="):
            value = token.split("=", 1)[1]
    return value


def workload_args(argv, transient=("--host", "--port", "--result-filename")):
    result = []
    skip = False
    for token in argv:
        if skip:
            skip = False
        elif token in transient:
            skip = True
        elif not any(token.startswith(name + "=") for name in transient):
            result.append(token)
    return result


def serving_observation(target):
    launch = target.get("launch_observation") or {}
    observation = {key: deepcopy(launch[key]) for key in
                   ("workspace_snapshot", "environment", "native_digest", "machine", "npu_devices") if key in launch}
    execution = None
    for line in str(launch.get("command") or "").splitlines():
        if not line.startswith("exec "):
            continue
        argv = shlex.split(line)
        if "vllm.entrypoints.cli.main" not in argv:
            continue
        index = argv.index("vllm.entrypoints.cli.main") + 1
        if argv[index:index + 1] != ["serve"] or len(argv) <= index + 1:
            continue
        serve = argv[index + 1:]
        normalized = workload_args(serve, ("--host", "--port", "--served-model-name"))
        observation.update({"serve_args": normalized, "model": {"path": serve[0]},
            "topology": {"tp": int(option(serve, "--tensor-parallel-size", "1")),
                         "dp": int(option(serve, "--data-parallel-size", "1"))}})
        engine_args = {"argv": normalized, "enforce_eager": "--enforce-eager" in serve}
        observation["engine_args"] = engine_args
        endpoint = target.get("endpoint") or {}
        execution = {"engine_args": engine_args, "model": serve[0],
                     "served_model": option(serve, "--served-model-name", serve[0]),
                     "base_url": f"http://{endpoint['host']}:{target['service_port']}" if endpoint.get("host") and target.get("service_port") else None}
    return observation, execution
