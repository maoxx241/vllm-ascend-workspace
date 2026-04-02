from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RepoPaths:
    root: Path

    @property
    def local_overlay(self) -> Path:
        return self.root / ".workspace.local"

    @property
    def local_state_file(self) -> Path:
        return self.local_overlay / "state.json"

    @property
    def state_lock_file(self) -> Path:
        return self.local_overlay / "state.lock"

    @property
    def local_servers_file(self) -> Path:
        return self.local_overlay / "servers.yaml"

    @property
    def local_targets_file(self) -> Path:
        return self.local_overlay / "targets.yaml"

    @property
    def local_auth_file(self) -> Path:
        return self.local_overlay / "auth.yaml"

    @property
    def local_repos_file(self) -> Path:
        return self.local_overlay / "repos.yaml"

    @property
    def local_sessions_dir(self) -> Path:
        return self.local_overlay / "sessions"

    @property
    def reset_request_file(self) -> Path:
        return self.local_overlay / "reset-request.json"
