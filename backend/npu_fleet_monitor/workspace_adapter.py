from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


HOST_PATTERN = re.compile(r"^[A-Za-z0-9_.:\-]+$")
USER_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]+$")
LOW_PRIORITY_TAG = "低优先级"


class WorkspaceDeviceAdapter:
    """Bridge to the workspace's tested host-key and NPU parsing utilities."""

    def __init__(self, project_root: Path, state_dir: Path) -> None:
        self.project_root = project_root
        self.state_dir = state_dir
        self.workspace_root = self._find_workspace_root()
        self.management_script = (
            self.workspace_root / ".agents/skills/machine-management/scripts/manage_machine.py"
            if self.workspace_root else None
        )
        self._npu_module: ModuleType | None = None
        self.is_windows = os.name == "nt"
        self._key_permissions_ready = False

    def _find_workspace_root(self) -> Path | None:
        candidates: list[Path] = []
        configured = os.environ.get("NFM_SOURCE_WORKSPACE")
        if configured:
            candidates.append(Path(configured).expanduser())

        candidates.extend((self.project_root, *self.project_root.parents))
        try:
            result = subprocess.run(
                ["git", "-C", str(self.project_root), "rev-parse", "--git-common-dir"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5, check=False,
            )
            if result.returncode == 0:
                common = Path(result.stdout.strip())
                if not common.is_absolute():
                    common = (self.project_root / common).resolve()
                candidates.append(common.parent)
        except (OSError, subprocess.TimeoutExpired):
            pass

        seen: set[Path] = set()
        for candidate in candidates:
            candidate = candidate.resolve()
            if candidate in seen:
                continue
            seen.add(candidate)
            if (candidate / ".agents/skills/machine-management").is_dir():
                return candidate
        return None

    @staticmethod
    def validate_endpoint(host: str, port: int, username: str) -> None:
        if not host or host.startswith("-") or not HOST_PATTERN.fullmatch(host):
            raise ValueError("主机地址只允许域名、IPv4 或 IPv6 字符")
        if not USER_PATTERN.fullmatch(username):
            raise ValueError("SSH 用户名包含不支持的字符")
        if port < 1 or port > 65535:
            raise ValueError("SSH 端口必须在 1 到 65535 之间")

    @property
    def private_key(self) -> Path:
        return self.state_dir / "keys" / "id_ed25519"

    @property
    def public_key(self) -> Path:
        return self.private_key.with_suffix(".pub")

    @property
    def known_hosts(self) -> Path:
        return self.state_dir / "known_hosts"

    def ensure_key(self) -> Path:
        if self.private_key.exists() and self.public_key.exists():
            self._secure_key_permissions()
            return self.private_key
        self.private_key.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "npu-fleet-monitor", "-f", str(self.private_key)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or "ssh-keygen failed")[-1000:])
        self._secure_key_permissions()
        return self.private_key

    def _secure_key_permissions(self) -> None:
        if self._key_permissions_ready:
            return
        self.private_key.chmod(0o600)
        if self.public_key.exists():
            self.public_key.chmod(0o600)
        if self.is_windows:
            identity = subprocess.run(
                ["whoami"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=5, check=False,
            )
            user = identity.stdout.strip()
            if identity.returncode != 0 or not user:
                raise RuntimeError("无法确定当前 Windows 用户，不能收紧监控私钥 ACL")
            acl = subprocess.run(
                ["icacls", str(self.private_key), "/inheritance:r", "/grant:r", f"{user}:(R,W)"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10, check=False,
            )
            if acl.returncode != 0:
                raise RuntimeError((acl.stderr or acl.stdout or "icacls failed")[-1000:])
        self._key_permissions_ready = True

    def ssh_base(self, server: dict[str, Any], *, batch_mode: bool = True) -> list[str]:
        self.validate_endpoint(server["host"], int(server["port"]), server["username"])
        self.ensure_key()
        command = [
            "ssh", "-T",
            "-o", f"BatchMode={'yes' if batch_mode else 'no'}",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={self.known_hosts}",
            "-o", "LogLevel=ERROR",
            "-o", "ConnectTimeout=8",
            "-o", "ConnectionAttempts=1",
            "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=1",
        ]
        if not self.is_windows:
            control_path = Path("data") / "ssh-control" / "%C"
            command.extend([
                "-o", "ControlMaster=auto",
                "-o", "ControlPersist=90",
                "-o", f"ControlPath={control_path}",
            ])
        command.extend([
            "-i", str(self.private_key),
            "-o", "IdentitiesOnly=yes",
            "-p", str(server["port"]),
            f"{server['username']}@{server['host']}",
        ])
        return command

    def preflight(self, server: dict[str, Any]) -> dict[str, Any]:
        self.validate_endpoint(server["host"], int(server["port"]), server["username"])
        command = ["ssh", "-G", "-p", str(server["port"]), f"{server['username']}@{server['host']}"]
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, timeout=8, check=False,
        )
        return {
            "ok": result.returncode == 0,
            "category": "ssh_config_valid" if result.returncode == 0 else "ssh_config_invalid",
            "error": None if result.returncode == 0 else (result.stderr or "SSH configuration rejected")[-1000:],
        }

    def key_auth_works(self, server: dict[str, Any]) -> bool:
        try:
            result = subprocess.run(
                [*self.ssh_base(server), "true"], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                timeout=12, check=False, cwd=self.project_root,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def _default_key_base(self, server: dict[str, Any]) -> list[str]:
        self.validate_endpoint(server["host"], int(server["port"]), server["username"])
        return [
            "ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={self.known_hosts}", "-o", "LogLevel=ERROR",
            "-o", "ConnectTimeout=8", "-o", "ConnectionAttempts=1",
            "-p", str(server["port"]), f"{server['username']}@{server['host']}",
        ]

    def bootstrap_from_existing_key(self, server: dict[str, Any]) -> bool:
        try:
            check = subprocess.run(
                [*self._default_key_base(server), "true"], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=12, check=False,
                cwd=self.project_root,
            )
            if check.returncode != 0:
                return False
            public_key = self.public_key.read_text(encoding="utf-8").strip()
            quoted = shlex.quote(public_key)
            remote = (
                "umask 077; mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys; "
                "chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys; "
                f"grep -qxF {quoted} ~/.ssh/authorized_keys 2>/dev/null || printf '%s\\n' {quoted} >> ~/.ssh/authorized_keys"
            )
            install = subprocess.run(
                [*self._default_key_base(server), "sh", "-c", shlex.quote(remote)], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=15, check=False,
                cwd=self.project_root,
            )
            return install.returncode == 0 and self.key_auth_works(server)
        except (OSError, subprocess.TimeoutExpired):
            return False

    def bootstrap_with_passwords(self, server: dict[str, Any], passwords: list[str]) -> dict[str, Any]:
        preflight = self.preflight(server)
        if not preflight["ok"]:
            return {"ok": False, "method": None, "attempts": 0, "error": preflight["error"]}
        if self.key_auth_works(server):
            return {"ok": True, "method": "existing-key", "attempts": 0}
        if self.bootstrap_from_existing_key(server):
            return {"ok": True, "method": "workspace-existing-key", "attempts": 0}
        if not passwords:
            return {"ok": False, "method": None, "attempts": 0, "error": "密钥登录失败，且未提供一次性密码"}
        if not self.management_script or not self.management_script.exists():
            return {"ok": False, "method": None, "attempts": 0, "error": "未找到工作区 machine-management 密钥引导入口"}

        error = "密码候选均未通过认证"
        for index, password in enumerate(passwords, start=1):
            if not isinstance(password, str) or not password:
                continue
            command = [
                sys.executable, str(self.management_script), "bootstrap-host-key",
                "--host", server["host"], "--host-port", str(server["port"]),
                "--user", server["username"], "--public-key-file", str(self.public_key),
                "--password-stdin",
            ]
            try:
                result = subprocess.run(
                    command, input=password + "\n", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, timeout=35, check=False,
                )
            except subprocess.TimeoutExpired:
                error = "密码认证超时"
                continue
            if result.returncode == 0 and self.key_auth_works(server):
                return {"ok": True, "method": "workspace-password-once", "attempts": index}
            error = self._safe_management_error(result.stdout, result.stderr)
        return {"ok": False, "method": None, "attempts": len(passwords), "error": error}

    def discover_workspace_servers(self) -> list[dict[str, Any]]:
        candidates: list[Path] = []
        workspace_roots: list[Path] = []
        if self.workspace_root:
            workspace_roots.append(self.workspace_root)
            candidates.append(self.workspace_root / ".vaws-local" / "machine-inventory.json")
            try:
                result = subprocess.run(
                    ["git", "-C", str(self.workspace_root), "rev-parse", "--git-common-dir"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5, check=False,
                )
                if result.returncode == 0:
                    common = Path(result.stdout.strip())
                    if not common.is_absolute():
                        common = (self.workspace_root / common).resolve()
                    workspace_roots.append(common.parent)
                    candidates.append(common.parent / ".vaws-local" / "machine-inventory.json")
            except (OSError, subprocess.TimeoutExpired):
                pass
        seen_paths: set[Path] = set()
        servers: list[dict[str, Any]] = []
        seen_endpoints: set[tuple[str, int, str]] = set()
        for path in candidates:
            path = path.resolve()
            if path in seen_paths or not path.is_file():
                continue
            seen_paths.add(path)
            try:
                machines = json.loads(path.read_text(encoding="utf-8")).get("machines", [])
            except (OSError, json.JSONDecodeError, AttributeError):
                continue
            for machine in machines:
                host_data = machine.get("host") or {}
                host = str(host_data.get("ip") or host_data.get("host") or "").strip()
                port = int(host_data.get("port") or 22)
                username = str(host_data.get("user") or "root")
                try:
                    self.validate_endpoint(host, port, username)
                except ValueError:
                    continue
                endpoint = (host, port, username)
                if endpoint in seen_endpoints:
                    continue
                seen_endpoints.add(endpoint)
                machine_type = host_data.get("machine_type") or (machine.get("container") or {}).get("machine_type")
                servers.append({
                    "name": str(machine.get("alias") or host), "host": host, "port": port,
                    "username": username, "tags": [str(machine_type)] if machine_type else [],
                    "inventory_path": str(path), "workspace_enabled": True,
                })

        # hosts.txt is the workspace's complete host pool. It may contain
        # additional authentication columns; only the first address field is
        # read here. Hosts absent from the active machine inventory remain
        # monitorable, but are clearly marked as low priority.
        seen_roots: set[Path] = set()
        for root in workspace_roots:
            root = root.resolve()
            if root in seen_roots:
                continue
            seen_roots.add(root)
            hosts_path = root / "hosts.txt"
            if not hosts_path.is_file():
                continue
            try:
                lines = hosts_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                host = stripped.split(maxsplit=1)[0]
                endpoint = (host, 22, "root")
                if endpoint in seen_endpoints:
                    continue
                try:
                    self.validate_endpoint(*endpoint)
                except ValueError:
                    continue
                seen_endpoints.add(endpoint)
                servers.append({
                    "name": host, "host": host, "port": 22, "username": "root",
                    "tags": [LOW_PRIORITY_TAG], "workspace_enabled": False,
                    "inventory_path": str(hosts_path),
                })
        return servers

    @staticmethod
    def _safe_management_error(stdout: str, stderr: str) -> str:
        try:
            payload = json.loads(stdout)
            if isinstance(payload, dict):
                return str(payload.get("message") or payload.get("error") or "密钥引导失败")[-1000:]
        except json.JSONDecodeError:
            pass
        lines = [line for line in stderr.splitlines() if not line.startswith("__VAWS_PROGRESS__=")]
        return ("\n".join(lines) or "密钥引导失败")[-1000:]

    def parse_npu(self, info: str, usages: str) -> dict[str, Any]:
        module = self._load_npu_module()
        if module:
            parsed = module.parse_npu_smi_info(info)
            module.apply_usage_overrides(parsed["devices"], module.parse_npu_smi_usages(usages))
            return parsed
        return self._fallback_parse_npu(info)

    def _load_npu_module(self) -> ModuleType | None:
        if self._npu_module is not None:
            return self._npu_module
        if not self.workspace_root:
            return None
        path = self.workspace_root / ".agents/skills/machine-management/scripts/npu_occupancy.py"
        if not path.exists():
            return None
        spec = importlib.util.spec_from_file_location("nfm_workspace_npu_occupancy", path)
        if not spec or not spec.loader:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._npu_module = module
        return module

    @staticmethod
    def _fallback_parse_npu(info: str) -> dict[str, Any]:
        devices: list[dict[str, Any]] = []
        for line in info.splitlines():
            match = re.search(
                r"\|\s*(\d+)\s+(\d+)\s+\|\s*([0-9A-Fa-f:.]+).*?(\d+(?:\.\d+)?)\s+(\d+)\s*/\s*(\d+)\s+(\d+)\s*/\s*(\d+)",
                line,
            )
            if match:
                devices.append({
                    "npu_id": int(match.group(1)), "chip_id": int(match.group(2)),
                    "bus_id": match.group(3), "aicore_percent": float(match.group(4)),
                    "memory": {"used_mb": int(match.group(5)), "total_mb": int(match.group(6))},
                    "hbm": {"used_mb": int(match.group(7)), "total_mb": int(match.group(8))},
                    "health": None, "name": None, "processes": [],
                })
        return {"devices": devices, "process_records": []}
