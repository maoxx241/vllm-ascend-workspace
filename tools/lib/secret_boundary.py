from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional


ENV_HANDLE_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


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


def ensure_bootstrap_secret_refs(
    *,
    server_auth_mode: str,
    server_password_env: Optional[str],
    git_auth_mode: str,
    git_token_env: Optional[str],
) -> None:
    if server_auth_mode == "password":
        require_pre_staged_env_handle(
            server_password_env,
            field_label="server password",
        )

    if git_auth_mode == "token":
        require_pre_staged_env_handle(
            git_token_env,
            field_label="git token",
        )
