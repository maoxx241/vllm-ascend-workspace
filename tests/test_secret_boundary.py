import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.secret_boundary import (
    SecretBoundaryError,
    ensure_bootstrap_secret_refs,
    require_pre_staged_env_handle,
)


def test_require_pre_staged_env_handle_rejects_missing_handle(monkeypatch):
    monkeypatch.delenv("SERVER_PASSWORD", raising=False)

    with pytest.raises(SecretBoundaryError) as exc:
        require_pre_staged_env_handle("SERVER_PASSWORD", field_label="server password")

    assert "pre-stage" in str(exc.value).lower()
    assert "server_password" in str(exc.value).lower()


def test_require_pre_staged_env_handle_rejects_invalid_handle_name():
    with pytest.raises(SecretBoundaryError) as exc:
        require_pre_staged_env_handle("bad-name", field_label="server password")

    assert "invalid secret handle" in str(exc.value).lower()


def test_ensure_bootstrap_secret_refs_allows_pre_staged_password(monkeypatch):
    monkeypatch.setenv("SERVER_PASSWORD", "already-staged")

    ensure_bootstrap_secret_refs(
        server_auth_mode="password",
        server_password_env="SERVER_PASSWORD",
        server_password_scope="machine-management:attach",
        git_auth_mode="ssh-agent",
        git_token_env=None,
    )


def test_ensure_bootstrap_secret_refs_requires_git_token_handle(monkeypatch):
    monkeypatch.delenv("GIT_TOKEN", raising=False)

    with pytest.raises(SecretBoundaryError) as exc:
        ensure_bootstrap_secret_refs(
            server_auth_mode="ssh-key",
            server_password_env=None,
            git_auth_mode="token",
            git_token_env="GIT_TOKEN",
        )

    assert "git token" in str(exc.value).lower()
