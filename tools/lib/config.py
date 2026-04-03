from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RepoPaths:
    root: Path

    @property
    def local_overlay(self) -> Path:
        return self.root / ".workspace.local"

    @property
    def local_servers_file(self) -> Path:
        return self.local_overlay / "servers.yaml"

    @property
    def local_auth_file(self) -> Path:
        return self.local_overlay / "auth.yaml"

    @property
    def local_repos_file(self) -> Path:
        return self.local_overlay / "repos.yaml"

    @property
    def reset_request_file(self) -> Path:
        return self.local_overlay / "reset-request.json"

    @property
    def local_benchmark_runs_dir(self) -> Path:
        return self.local_overlay / "benchmark-runs"
