from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional


ENV_HANDLE_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
ALLOWED_SERVER_PASSWORD_SCOPES = frozenset(
    {
        "workspace-init:first-machine-attach",
        "machine-management:attach",
    }
)


class SecretBoundaryError(RuntimeError):
    """Unsafe or missing secret handle for workspace control-plane flows."""


@dataclass(frozen=True)
class SecretHandle:
    source: str
    name: str


def require_pre_staged_env_handle(
    name: Optional[str],
    *,
    field_label: str,
) -> SecretHandle:
    if not isinstance(name, str) or not name.strip():
        raise SecretBoundaryError(
            f"missing pre-staged secret handle for {field_label}; stage it outside the agent session"
        )

    normalized = name.strip()
    if not ENV_HANDLE_RE.fullmatch(normalized):
        raise SecretBoundaryError(
            f"invalid secret handle for {field_label}: {normalized}"
        )

    value = os.environ.get(normalized)
    if not value:
        raise SecretBoundaryError(
            f"missing pre-staged secret handle for {field_label}: {normalized}; stage it outside the agent session"
        )

    return SecretHandle(source="env", name=normalized)


def assert_server_password_allowed(scope: str) -> None:
    if scope not in ALLOWED_SERVER_PASSWORD_SCOPES:
        raise SecretBoundaryError(
            f"server password bootstrap not allowed in this flow: {scope}"
        )


def ensure_bootstrap_secret_refs(
    *,
    server_auth_mode: str,
    server_password_env: Optional[str],
    server_password_scope: Optional[str] = None,
    git_auth_mode: str,
    git_token_env: Optional[str],
) -> None:
    if server_auth_mode == "password":
        if not isinstance(server_password_scope, str) or not server_password_scope.strip():
            raise SecretBoundaryError(
                "server password bootstrap requires an explicit allowed flow"
            )
        assert_server_password_allowed(server_password_scope.strip())
        require_pre_staged_env_handle(
            server_password_env,
            field_label="server password",
        )

    if git_auth_mode == "token":
        require_pre_staged_env_handle(
            git_token_env,
            field_label="git token",
        )
