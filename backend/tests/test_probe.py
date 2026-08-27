from __future__ import annotations

import base64
import json
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from npu_fleet_monitor.probe import (
    attach_npu_telemetry,
    attach_process_details,
    build_process_detail_script,
    cpu_percent,
    extract_ownership_labels,
    is_device_busy,
    parse_disks,
    parse_docker,
    parse_meminfo,
    parse_process_details,
    split_sections,
)
from npu_fleet_monitor.workspace_adapter import WorkspaceDeviceAdapter


class ProbeTests(unittest.TestCase):
    def test_split_and_host_parsers(self) -> None:
        sections = split_sections("noise\n__NFM_SECTION__meminfo\nMemTotal: 1000 kB\nMemAvailable: 250 kB")
        memory = parse_meminfo(sections["meminfo"])
        self.assertEqual(memory["memory_total_bytes"], 1024000)
        self.assertEqual(memory["memory_used_bytes"], 768000)
        self.assertEqual(cpu_percent((100, 40), (200, 60)), 80.0)

    def test_disk_parser(self) -> None:
        rows = parse_disks("Filesystem 1-blocks Used Available Capacity Mounted on\n/dev/sda 1000 800 200 80% /")
        self.assertEqual(rows[0]["used_percent"], 80)
        self.assertEqual(rows[0]["mount"], "/")

    def test_docker_stats_and_npu_telemetry(self) -> None:
        docker = parse_docker(
            '{"ID":"abc","Names":"worker","Image":"vllm","Status":"Up","State":"running"}',
            '{"Name":"worker","CPUPerc":"12.4%","MemUsage":"2GiB / 8GiB","PIDs":"10"}',
            '{"ServerVersion":"28.0","Driver":"overlay2","DockerRootDir":"/var/lib/docker"}',
        )
        self.assertEqual(docker["containers"][0]["stats"]["cpu_percent"], "12.4%")
        devices = [{"npu_id": 0}]
        attach_npu_telemetry(devices, "| 0 910B4 | OK 91.8 41 0 / 0 |")
        self.assertEqual(devices[0]["temperature_c"], 41)
        self.assertEqual(devices[0]["power_w"], 91.8)

    def test_busy_threshold_ignores_a3_driver_baseline(self) -> None:
        idle = {"processes": [], "aicore_percent": 0, "hbm": {"used_mb": 5989}}
        self.assertFalse(is_device_busy(idle, 8192))
        self.assertTrue(is_device_busy({**idle, "aicore_percent": 2}, 8192))
        self.assertTrue(is_device_busy({**idle, "hbm": {"used_mb": 8192}}, 8192))
        self.assertTrue(is_device_busy({**idle, "processes": [{"pid": 1}]}, 8192))

    def test_process_details_are_decoded_and_attached_to_container(self) -> None:
        encoded = lambda value: base64.b64encode(value.encode()).decode()
        container_id = "a" * 64
        details = parse_process_details("\t".join([
            "421", encoded("root"), encoded("/workspace"), encoded("python -m vllm.entrypoints.openai.api_server"),
            encoded("/usr/bin/python3"), encoded("python3"), encoded(f"0::/system.slice/docker-{container_id}.scope"),
        ]))
        devices = [{"processes": [{"pid": 421, "npu_process_name": "python3", "npu_memory_mb": 2048}]}]
        attach_process_details(devices, details, {"containers": [{"id": container_id, "name": "vllm-a3", "image": "vllm:latest", "status": "Up"}]})
        process = devices[0]["processes"][0]
        self.assertEqual(process["cwd"], "/workspace")
        self.assertEqual(process["command"], "python -m vllm.entrypoints.openai.api_server")
        self.assertEqual(process["container"]["name"], "vllm-a3")
        self.assertEqual(process["npu_memory_mb"], 2048)

    def test_process_detail_script_only_contains_validated_pids(self) -> None:
        script = build_process_detail_script([23, 7, 23, -1])
        self.assertIn("for nfm_pid in 7 23; do", script)
        self.assertIn('/proc/$nfm_pid/cmdline', script)
        self.assertIn('/proc/$nfm_pid/cwd', script)

    def test_ownership_labels_extract_employee_ids_and_initials(self) -> None:
        labels = extract_ownership_labels(
            "/home/q00946761/workspace/wbj/project/abc1234567",
            "wbj_dsa_op_q00946761",
        )
        by_kind = {
            kind: [label["value"] for label in labels if label["kind"] == kind]
            for kind in ("employee_id", "initials")
        }
        self.assertEqual(by_kind["employee_id"], ["q00946761", "abc1234567"])
        self.assertEqual(by_kind["initials"], ["wbj", "dsa", "op"])
        q_label = next(label for label in labels if label["value"] == "q00946761")
        self.assertEqual(q_label["sources"], ["pwd", "container"])

    def test_ownership_label_boundaries_reject_overlong_candidates(self) -> None:
        labels = extract_ownership_labels(
            "/home/abcd1234567/a1234567890/abcde/team",
            None,
        )
        self.assertEqual(labels, [{"value": "team", "kind": "initials", "sources": ["pwd"]}])

    def test_workspace_npu_parser_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            project = Path(__file__).resolve().parents[2]
            adapter = WorkspaceDeviceAdapter(project, Path(state))
            parsed = adapter.parse_npu(
                """
| NPU Name | Health Power(W) Temp(C) Hugepages-Usage |
| 0 910B4 | OK 91.8 41 0 / 0 |
| NPU Chip | Bus-Id AICore(%) Memory-Usage(MB) HBM-Usage(MB) |
| 0 0 | 0000:C1:00.0 87 1024 / 2048 32768 / 65536 |
""",
                "NPU ID : 0\nAicore Usage Rate(%) : 55\nHBM Usage Rate(%) : 50",
            )
            self.assertEqual(parsed["devices"][0]["aicore_percent"], 55)
            self.assertEqual(parsed["devices"][0]["hbm"]["used_mb"], 32768)

    def test_control_path_stays_below_unix_socket_limit(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            project = Path(__file__).resolve().parents[2]
            adapter = WorkspaceDeviceAdapter(project, Path(state))
            command = adapter.ssh_base({"host":"10.0.0.1","port":22,"username":"root"})
            option = next(command[index + 1] for index, value in enumerate(command) if value == "-o" and command[index + 1].startswith("ControlPath="))
            expanded = option.split("=", 1)[1].replace("%C", "x" * 40)
            self.assertLess(len(expanded), 100)

    def test_windows_ssh_omits_unix_control_socket_options(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            project = Path(__file__).resolve().parents[2]
            adapter = WorkspaceDeviceAdapter(project, Path(state))
            adapter.is_windows = True
            with mock.patch.object(adapter, "ensure_key", return_value=adapter.private_key):
                command = adapter.ssh_base({"host": "10.0.0.1", "port": 22, "username": "root"})
            rendered = " ".join(command)
            self.assertNotIn("ControlMaster", rendered)
            self.assertNotIn("ControlPersist", rendered)
            self.assertNotIn("ControlPath", rendered)
            self.assertIn("IdentitiesOnly=yes", rendered)

    def test_windows_private_key_acl_is_scoped_to_current_user_once(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            project = Path(__file__).resolve().parents[2]
            adapter = WorkspaceDeviceAdapter(project, Path(state))
            adapter.is_windows = True
            adapter.private_key.parent.mkdir(parents=True)
            adapter.private_key.write_text("private", encoding="utf-8")
            adapter.public_key.write_text("public", encoding="utf-8")
            responses = [
                subprocess.CompletedProcess(["whoami"], 0, "DOMAIN\\monitor\n", ""),
                subprocess.CompletedProcess(["icacls"], 0, "processed", ""),
            ]
            with mock.patch("npu_fleet_monitor.workspace_adapter.subprocess.run", side_effect=responses) as run:
                adapter._secure_key_permissions()
                adapter._secure_key_permissions()
            self.assertEqual(run.call_count, 2)
            self.assertEqual(
                run.call_args_list[1].args[0],
                ["icacls", str(adapter.private_key), "/inheritance:r", "/grant:r", "DOMAIN\\monitor:(R,W)"],
            )

    def test_explicit_source_workspace_takes_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as state:
            workspace = Path(root)
            (workspace / ".agents/skills/machine-management").mkdir(parents=True)
            project = workspace / "detached-monitor-worktree"
            project.mkdir()
            with mock.patch.dict("os.environ", {"NFM_SOURCE_WORKSPACE": str(workspace)}):
                adapter = WorkspaceDeviceAdapter(project, Path(state))
            self.assertEqual(adapter.workspace_root, workspace)

    def test_workspace_discovery_includes_disabled_hosts_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as state:
            workspace = Path(root)
            (workspace / ".agents/skills/machine-management").mkdir(parents=True)
            inventory_dir = workspace / ".vaws-local"
            inventory_dir.mkdir()
            (inventory_dir / "machine-inventory.json").write_text(json.dumps({
                "machines": [{
                    "alias": "active-a3",
                    "host": {"ip": "10.0.0.1", "port": 22, "user": "root", "machine_type": "A3"},
                }],
            }), encoding="utf-8")
            (workspace / "hosts.txt").write_text(
                "10.0.0.1 active-password\n10.0.0.2 disabled-password\n",
                encoding="utf-8",
            )
            project = workspace / "monitor"
            project.mkdir()
            with mock.patch.dict("os.environ", {"NFM_SOURCE_WORKSPACE": str(workspace)}):
                adapter = WorkspaceDeviceAdapter(project, Path(state))
                servers = adapter.discover_workspace_servers()

            self.assertEqual(len(servers), 2)
            active = next(server for server in servers if server["host"] == "10.0.0.1")
            disabled = next(server for server in servers if server["host"] == "10.0.0.2")
            self.assertTrue(active["workspace_enabled"])
            self.assertEqual(active["tags"], ["A3"])
            self.assertFalse(disabled["workspace_enabled"])
            self.assertEqual(disabled["tags"], ["低优先级"])
            self.assertNotIn("password", json.dumps(servers))


if __name__ == "__main__":
    unittest.main()
