from __future__ import annotations

from typing import Any


ENDPOINT_PROPS: dict[str, Any] = {
    "host": {"type": "string"},
    "port": {"type": "integer"},
    "user": {"type": "string", "default": "root"},
    "root": {"type": "string", "default": "/"},
    "cwd": {"type": "string"},
    "runtime_env": {"type": "boolean", "default": True},
    "identity_file": {"type": "string"},
    "connect_timeout_ms": {"type": "integer", "default": 10000},
    "alias": {"type": "string"},
    "session_id": {"type": "string"},
    "session_file": {"type": "string"},
    "machine": {"type": "string"},
}

ENDPOINT_SELECTOR_ANY_OF: list[dict[str, Any]] = [
    {"required": ["host", "port"]},
    {"required": ["alias"]},
    {"required": ["session_id"]},
    {"required": ["session_file"]},
    {"required": ["machine"]},
]

def schema(props: dict[str, Any], required: list[str] | None = None, *, endpoint_selector: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "object",
        "additionalProperties": True,
        "properties": {**ENDPOINT_PROPS, **props},
        "required": required or [],
    }
    if endpoint_selector:
        payload["allOf"] = [{"anyOf": ENDPOINT_SELECTOR_ANY_OF}]
    return payload


TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "remote.read": schema({"file_path": {"type": "string"}, "offset": {"type": "integer", "default": 1}, "limit": {"type": "integer", "default": 200, "maximum": 500}, "client_context_id": {"type": "string"}}, ["file_path"]),
    "remote.write": schema({"file_path": {"type": "string"}, "content": {"type": "string"}, "overwrite": {"type": "boolean"}, "create_dirs": {"type": "boolean"}, "client_context_id": {"type": "string"}}, ["file_path", "content"]),
    "remote.edit": schema({"file_path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}, "replace_all": {"type": "boolean"}, "client_context_id": {"type": "string"}}, ["file_path", "old_string", "new_string"]),
    "remote.multi_edit": schema({"file_path": {"type": "string"}, "edits": {"type": "array", "items": {"type": "object"}}, "client_context_id": {"type": "string"}}, ["file_path", "edits"]),
    "remote.bash": schema({"command": {"type": "string"}, "description": {"type": "string"}, "timeout_ms": {"type": "integer"}, "timeout": {"type": "integer"}, "run_in_background": {"type": "boolean"}, "env": {"type": "object", "additionalProperties": {"type": "string"}}}, ["command"]),
    "remote.glob": schema({"pattern": {"type": "string"}, "path": {"type": "string"}, "limit": {"type": "integer"}, "respect_gitignore": {"type": "boolean", "description": "Currently returns a warning; exact gitignore filtering is not implemented."}}, ["pattern"]),
    "remote.grep": schema({"pattern": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string"}, "type": {"type": "string"}, "output_mode": {"type": "string", "enum": ["files_with_matches", "content", "count"]}, "multiline": {"type": "boolean"}, "limit": {"type": "integer", "maximum": 500}}, ["pattern"]),
    "remote.ls": schema({"path": {"type": "string"}, "limit": {"type": "integer"}, "all": {"type": "boolean"}}),
    "remote.monitor": schema({"command": {"type": "string"}, "description": {"type": "string"}, "timeout_ms": {"type": "integer"}, "pattern": {"type": "string"}, "env": {"type": "object", "additionalProperties": {"type": "string"}}}, ["command"]),
    "remote.apply_patch": {
        **schema({"patch": {"type": "string"}, "command": {"type": "string"}, "timeout_ms": {"type": "integer"}}),
        "anyOf": [{"required": ["patch"]}, {"required": ["command"]}],
    },
    "remote.job_status": schema({"job_id": {"type": "string"}}, ["job_id"], endpoint_selector=False),
    "remote.job_tail": schema({"job_id": {"type": "string"}, "lines": {"type": "integer", "maximum": 500}, "stream": {"type": "string", "enum": ["stdout", "stderr", "both"]}}, ["job_id"], endpoint_selector=False),
    "remote.job_stop": schema({"job_id": {"type": "string"}, "force": {"type": "boolean"}}, ["job_id"], endpoint_selector=False),
    "remote.artifact_manifest": schema({"remote_path": {"type": "string"}}, ["remote_path"]),
    "remote.artifact_pull": schema({"remote_path": {"type": "string"}, "local_dir": {"type": "string"}}, ["remote_path"]),
    "remote.artifact_push": schema({"local_path": {"type": "string"}, "remote_path": {"type": "string"}}, ["local_path", "remote_path"]),
    "remote.context_snapshot": schema({"live_probe": {"type": "boolean", "default": True}}),
    "remote.probe": schema({}),
}


ALIASES: dict[str, str] = {name.replace(".", "_"): name for name in TOOL_SCHEMAS}
