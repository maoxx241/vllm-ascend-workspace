#!/usr/bin/env python3
"""Tests for the multi-node serving planner."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SKILL = ROOT / ".agents" / "skills" / "vllm-ascend-multinode-serving"


def load_module():
    name = "_multinode_plan_test"
    spec = importlib.util.spec_from_file_location(name, SKILL / "scripts" / "multinode_plan.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


planner = load_module()

FOUR_NODE = [
    "--node", "n153=192.168.13.153",
    "--node", "n154=192.168.13.154",
    "--node", "n155=192.168.13.155",
    "--node", "n156=192.168.13.156",
    "--tp", "16", "--dp", "4", "--dp-local", "1",
    "--data-nic", "enp210s0f0",
    "--model", "/home/weights/SomeModel",
]


def plan(extra: list[str] | None = None) -> dict:
    argv = ["plan", *FOUR_NODE, *(extra or [])]
    args = planner.build_parser().parse_args(argv)
    return planner.build_plan(args)


class TopologyTests(unittest.TestCase):
    def test_derives_world_and_per_node_devices(self) -> None:
        topology = plan()["topology"]
        self.assertEqual(topology["world_devices"], 64)
        self.assertEqual(topology["devices_per_node"], 16)
        self.assertEqual(topology["nodes"], 4)

    def test_dp_local_defaults_to_even_split(self) -> None:
        argv = ["plan", *[a for a in FOUR_NODE if a not in ("--dp-local", "1")]]
        args = planner.build_parser().parse_args(argv)
        self.assertEqual(planner.build_plan(args)["topology"]["data_parallel_size_local"], 1)

    def test_uneven_dp_without_dp_local_is_rejected(self) -> None:
        with self.assertRaises(planner.PlanError):
            planner.resolve_topology(tp=8, dp=3, dp_local=None, node_count=2, devices_per_node=None)

    def test_node_count_times_dp_local_must_equal_dp(self) -> None:
        with self.assertRaises(planner.PlanError):
            planner.resolve_topology(tp=8, dp=4, dp_local=1, node_count=2, devices_per_node=None)

    def test_declared_devices_per_node_must_match_tp_times_dp_local(self) -> None:
        with self.assertRaises(planner.PlanError):
            planner.resolve_topology(tp=8, dp=4, dp_local=2, node_count=2, devices_per_node=8)

    def test_two_ranks_per_node_splits_rank_offsets(self) -> None:
        args = planner.build_parser().parse_args(
            [
                "plan", "--node", "a=10.0.0.1", "--node", "b=10.0.0.2",
                "--tp", "8", "--dp", "4", "--dp-local", "2",
                "--data-nic", "eth0", "--model", "/w/m",
            ]
        )
        nodes = planner.build_plan(args)["nodes"]
        self.assertEqual(nodes[0]["data_parallel_ranks"], [0, 1])
        self.assertEqual(nodes[1]["data_parallel_ranks"], [2, 3])


class RoleTests(unittest.TestCase):
    def test_internal_lb_gives_one_api_server_on_master(self) -> None:
        nodes = plan()["nodes"]
        self.assertTrue(nodes[0]["is_master"])
        self.assertNotIn("--headless", nodes[0]["command"])
        self.assertIn("--port", nodes[0]["command"])
        for node in nodes[1:]:
            self.assertIn("--headless", node["command"])
            self.assertNotIn("--port", node["command"])

    def test_worker_nodes_carry_their_start_rank(self) -> None:
        nodes = plan()["nodes"]
        for index, node in enumerate(nodes[1:], start=1):
            command = node["command"]
            self.assertEqual(command[command.index("--data-parallel-start-rank") + 1], str(index))

    def test_hybrid_lb_gives_every_node_an_api_server(self) -> None:
        nodes = plan(["--lb-mode", "hybrid"])["nodes"]
        for node in nodes:
            self.assertIn("--data-parallel-hybrid-lb", node["command"])
            self.assertIn("--port", node["command"])
            self.assertNotIn("--headless", node["command"])

    def test_master_selection_is_explicit(self) -> None:
        result = plan(["--master", "n155"])
        self.assertEqual(result["master"]["alias"], "n155")
        self.assertEqual(result["master"]["data_address"], "192.168.13.155")
        by_alias = {node["alias"]: node for node in result["nodes"]}
        self.assertTrue(by_alias["n155"]["is_master"])
        self.assertNotIn("--headless", by_alias["n155"]["command"])
        self.assertIn("--headless", by_alias["n153"]["command"])

    def test_unknown_master_is_rejected(self) -> None:
        with self.assertRaises(planner.PlanError):
            plan(["--master", "nope"])

    def test_all_nodes_point_at_the_master_address(self) -> None:
        for node in plan()["nodes"]:
            command = node["command"]
            self.assertEqual(
                command[command.index("--data-parallel-address") + 1], "192.168.13.153"
            )


class EnvironmentTests(unittest.TestCase):
    def test_socket_interfaces_are_always_pinned(self) -> None:
        env = plan()["nodes"][0]["environment_variables"]
        for key in ("HCCL_SOCKET_IFNAME", "GLOO_SOCKET_IFNAME", "TP_SOCKET_IFNAME"):
            self.assertEqual(env[key], "enp210s0f0")

    def test_each_node_binds_its_own_data_address(self) -> None:
        for node in plan()["nodes"]:
            env = node["environment_variables"]
            self.assertEqual(env["HCCL_IF_IP"], node["data_address"])
            self.assertEqual(env["VLLM_HOST_IP"], node["data_address"])

    def test_visible_devices_covers_the_per_node_device_count(self) -> None:
        env = plan()["nodes"][0]["environment_variables"]
        self.assertEqual(env["ASCEND_RT_VISIBLE_DEVICES"], ",".join(str(i) for i in range(16)))

    def test_extra_env_overrides_defaults(self) -> None:
        env = plan(["--extra-env", "VLLM_LOGGING_LEVEL=DEBUG"])["nodes"][0][
            "environment_variables"
        ]
        self.assertEqual(env["VLLM_LOGGING_LEVEL"], "DEBUG")

    def test_malformed_extra_env_is_rejected(self) -> None:
        for spec in ("novalue", "lower=1", "9BAD=1"):
            with self.assertRaises(planner.PlanError):
                planner.parse_env_assignment(spec)


class PortTests(unittest.TestCase):
    def test_api_port_inside_hccl_range_is_rejected(self) -> None:
        with self.assertRaises(planner.PlanError):
            plan(["--api-port", "20050", "--hccl-port-range", "20000-20127"])

    def test_rpc_port_inside_hccl_range_is_rejected(self) -> None:
        with self.assertRaises(planner.PlanError):
            plan(["--dp-rpc-port", "20050", "--hccl-port-range", "20000-20127"])

    def test_api_port_equal_to_rpc_port_is_rejected(self) -> None:
        with self.assertRaises(planner.PlanError):
            plan(["--api-port", "29550", "--dp-rpc-port", "29550"])

    def test_malformed_port_range_is_rejected(self) -> None:
        for spec in ("abc", "20000", "20127-20000", "80-90"):
            with self.assertRaises(planner.PlanError):
                planner.parse_port_range(spec)


class NodeSpecTests(unittest.TestCase):
    def test_valid_spec(self) -> None:
        self.assertEqual(planner.parse_node("n1=10.0.0.1"), ("n1", "10.0.0.1"))

    def test_rejects_bad_specs(self) -> None:
        for spec in ("n1", "N1=10.0.0.1", "n1=host", "n1=10.0.0.999", "=10.0.0.1"):
            with self.assertRaises(planner.PlanError):
                planner.parse_node(spec)

    def test_duplicate_alias_is_rejected(self) -> None:
        args = planner.build_parser().parse_args(
            [
                "plan", "--node", "a=10.0.0.1", "--node", "a=10.0.0.2",
                "--tp", "8", "--dp", "2", "--data-nic", "eth0", "--model", "/w/m",
            ]
        )
        with self.assertRaises(planner.PlanError):
            planner.build_plan(args)

    def test_duplicate_data_address_is_rejected(self) -> None:
        args = planner.build_parser().parse_args(
            [
                "plan", "--node", "a=10.0.0.1", "--node", "b=10.0.0.1",
                "--tp", "8", "--dp", "2", "--data-nic", "eth0", "--model", "/w/m",
            ]
        )
        with self.assertRaises(planner.PlanError):
            planner.build_plan(args)

    def test_relative_model_path_is_rejected(self) -> None:
        with self.assertRaises(planner.PlanError):
            planner.build_plan(
                planner.build_parser().parse_args(
                    [
                        "plan", "--node", "a=10.0.0.1",
                        "--tp", "8", "--dp", "1",
                        "--data-nic", "eth0", "--model", "weights/m",
                    ]
                )
            )


class GateTests(unittest.TestCase):
    def test_readiness_gate_ends_with_a_real_completion(self) -> None:
        stages = [stage["stage"] for stage in plan()["readiness_gate"]]
        self.assertEqual(
            stages, ["processes-alive", "health", "models", "real-completion"]
        )

    def test_identity_probe_covers_declared_repositories(self) -> None:
        command = plan()["identity_probe"]["command"]
        self.assertIn("/vllm-workspace/vllm-ascend", command)
        self.assertIn("vllm_ascend_C", command)
        self.assertIn("sha256sum", command)

    def test_served_model_name_defaults_to_weight_basename(self) -> None:
        self.assertEqual(plan()["model"]["served_model_name"], "SomeModel")

    def test_ep_mismatch_warns_instead_of_failing(self) -> None:
        warnings = plan(["--ep", "32"])["warnings"]
        self.assertTrue(any("--ep 32" in item for item in warnings))

    def test_matching_ep_produces_no_warning(self) -> None:
        self.assertEqual(plan(["--ep", "64"])["warnings"], [])

    def test_single_node_plan_warns_to_use_the_serving_skill(self) -> None:
        args = planner.build_parser().parse_args(
            [
                "plan", "--node", "a=10.0.0.1", "--tp", "8", "--dp", "1",
                "--data-nic", "eth0", "--model", "/w/m",
            ]
        )
        warnings = planner.build_plan(args)["warnings"]
        self.assertTrue(any("vllm-ascend-serving" in item for item in warnings))


class RenderTests(unittest.TestCase):
    def test_render_launch_block_exports_env_and_redirects_log(self) -> None:
        result = plan()
        block = planner.render_node(result, "n154", "launch")
        self.assertIn("export HCCL_IF_IP=192.168.13.154", block)
        self.assertIn("--headless", block)
        self.assertIn("/vllm-workspace/logs/multinode/n154.log", block)

    def test_render_identity_matches_the_plan_probe(self) -> None:
        result = plan()
        self.assertEqual(
            planner.render_node(result, "n153", "identity"),
            result["identity_probe"]["command"],
        )

    def test_render_unknown_node_is_rejected(self) -> None:
        with self.assertRaises(planner.PlanError):
            planner.render_node(plan(), "nope", "launch")

    def test_serve_args_section_is_appended_verbatim(self) -> None:
        nodes = plan(["--serve-args", "--max-model-len", "8192", "--trust-remote-code"])[
            "nodes"
        ]
        for node in nodes:
            self.assertEqual(
                node["command"][-3:], ["--max-model-len", "8192", "--trust-remote-code"]
            )

    def test_serve_args_json_value_is_quoted_when_rendered(self) -> None:
        result = plan(
            ["--serve-args", "--compilation-config", '{"cudagraph_mode":"FULL_DECODE_ONLY"}']
        )
        block = planner.render_node(result, "n153", "launch")
        self.assertIn("'{\"cudagraph_mode\":\"FULL_DECODE_ONLY\"}'", block)

    def test_expert_parallel_flag_is_added_to_every_node(self) -> None:
        for node in plan(["--expert-parallel"])["nodes"]:
            self.assertIn("--enable-expert-parallel", node["command"])


class CliTests(unittest.TestCase):
    def test_plan_writes_output_file_and_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "nested" / "plan.json"
            code = planner.main(["plan", *FOUR_NODE, "--output", str(output)])
            self.assertEqual(code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["topology"]["world_devices"], 64)

    def test_invalid_topology_exits_two(self) -> None:
        code = planner.main(
            [
                "plan", "--node", "a=10.0.0.1", "--node", "b=10.0.0.2",
                "--tp", "8", "--dp", "4", "--dp-local", "1",
                "--data-nic", "eth0", "--model", "/w/m",
            ]
        )
        self.assertEqual(code, 2)

    def test_render_reads_a_written_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "plan.json"
            planner.main(["plan", *FOUR_NODE, "--output", str(output)])
            self.assertEqual(
                planner.main(["render", "--plan", str(output), "--node", "n156"]), 0
            )


if __name__ == "__main__":
    unittest.main()
