#!/usr/bin/env python3
"""Hardware-free tests for the deterministic-core maturation harness."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
AGENTS = ROOT / ".agents"
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

from maturation import attribution, knowledge, redact, spec, stats, targets  # noqa: E402
from maturation.invoke import CliResult  # noqa: E402
from maturation.runner import RunConfig, render_markdown, replay, run  # noqa: E402
from maturation.scenarios import EndpointContext, evaluate, run_operation  # noqa: E402

IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _result(tool: str, outcome: str, status: str, **extra: Any) -> dict[str, Any]:
    payload = {
        "schema_version": "remote-dev.result.v1",
        "tool": tool,
        "invocation_id": "inv-1",
        "target": {"kind": "direct-endpoint"},
        "outcome": outcome,
        "status": status,
        "summary": f"{tool} {status}",
        "started_at": "2026-09-07T00:00:00Z",
        "duration_ms": extra.pop("duration_ms", 50),
        "preview": extra.pop("preview", {}),
        "refs": {},
        "artifacts": extra.pop("artifacts", []),
        "changed_files": [],
        "warnings": [],
        "next": None,
    }
    payload.update(extra)
    return payload


def _payload(result: Mapping[str, Any], text: str = "") -> dict[str, Any]:
    return {"text": text, "result": dict(result)}


class FakeInvoker:
    """Simulates the remote-dev dispatcher against an in-memory endpoint.

    ``failing_hosts`` never answer the probe; ``flaky_echo_every`` makes every
    n-th ``printf`` command time out instantly (the recorded tool-service
    signature) so flake detection and attribution can be exercised.
    """

    def __init__(self, *, failing_hosts: set[str] | None = None, flaky_echo_every: int = 0, glob_broken: bool = False) -> None:
        self.failing_hosts = failing_hosts or set()
        self.flaky_echo_every = flaky_echo_every
        self.glob_broken = glob_broken
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.files: dict[str, dict[str, str]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self._echo_count = 0

    # -- helpers --------------------------------------------------------- #
    def _fs(self, args: Mapping[str, Any]) -> dict[str, str]:
        return self.files.setdefault(str(args.get("host")), {})

    def call(self, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        args = dict(args)
        self.calls.append((tool, args))
        host = str(args.get("host"))
        if host in self.failing_hosts:
            return _payload(_result(tool, "failed", "exception", error="ssh: connect to host failed", exit_code=255))
        if tool == "remote.probe" or tool == "remote.context_snapshot":
            return _payload(_result(tool, "success", "ok", probe={"status": "ok", "summary": {"python": "3.9.9", "hostname": "node-1"}}))
        if tool == "remote.bash":
            return self._bash(args)
        if tool == "remote.read":
            content = self._fs(args).get(str(args["file_path"]))
            if content is None:
                return _payload(_result(tool, "failed", "not_found", error="remote path does not exist"))
            return _payload(_result(tool, "success", "ok", preview={"content": content}), text=content)
        if tool == "remote.write":
            fs = self._fs(args)
            path = str(args["file_path"])
            if path in fs and not args.get("overwrite"):
                return _payload(_result(tool, "blocked", "file_exists"))
            fs[path] = str(args.get("content", ""))
            return _payload(_result(tool, "success", "written"))
        if tool == "remote.edit":
            fs = self._fs(args)
            path = str(args["file_path"])
            content = fs.get(path, "")
            if str(args["old_string"]) not in content:
                return _payload(_result(tool, "failed", "old_string_not_found"))
            fs[path] = content.replace(str(args["old_string"]), str(args["new_string"]), 1)
            return _payload(_result(tool, "success", "edited"))
        if tool == "remote.glob":
            if self.glob_broken:
                return _payload(_result(tool, "failed", "failed", error="remote python failed"))
            names = sorted(Path(p).name for p in self._fs(args) if p.startswith(str(args.get("path", ""))))
            return _payload(_result(tool, "success", "ok", preview={"matches": names}), text="\n".join(names))
        if tool == "remote.artifact_pull":
            return self._pull(args)
        if tool == "remote.job_status":
            job = self.jobs[str(args["job_id"])]
            job["polls"] += 1
            status = "running" if job["polls"] < 2 and job["final"] == "succeeded" else job["final"]
            return _payload(_result(tool, "success", status))
        if tool == "remote.job_tail":
            job = self.jobs[str(args["job_id"])]
            return _payload(_result(tool, "success", "ok", preview={"tail": job["output"]}), text=job["output"])
        if tool == "remote.job_stop":
            self.jobs[str(args["job_id"])]["final"] = "cancelled"
            return _payload(_result(tool, "cancelled", "cancelled"))
        raise AssertionError(f"unexpected tool {tool}")

    def _bash(self, args: dict[str, Any]) -> dict[str, Any]:
        command = str(args.get("command", ""))
        if args.get("run_in_background"):
            job_id = f"job-{len(self.jobs) + 1}"
            output = command.split("printf ")[-1].strip("'\"\\n ") if "printf" in command else "done"
            self.jobs[job_id] = {"polls": 0, "final": "succeeded", "output": output.replace("\\n", "")}
            return _payload(_result("remote.bash", "success", "running", job={"job_id": job_id}))
        if command.startswith("printf"):
            self._echo_count += 1
            if self.flaky_echo_every and self._echo_count % self.flaky_echo_every == 0:
                return _payload(_result("remote.bash", "timeout", "timeout", duration_ms=3, timed_out=True, exit_code=None))
            text = command.split(" ", 1)[1].strip("'\"")
            if ">" in text:
                target = text.split(">", 1)[1].split("&&")[0].strip()
                self._fs(args)[target] = text.split(">", 1)[0].strip("' ")
                text = text.split("'")[1] if "'" in text else text
            return _payload(_result("remote.bash", "success", "ok", exit_code=0, preview={"stdout": {"text": text}}), text=text)
        if "exit 7" in command:
            return _payload(_result("remote.bash", "failed", "nonzero_exit", exit_code=7))
        if command.startswith("sleep"):
            return _payload(_result("remote.bash", "timeout", "timeout", duration_ms=int(args.get("timeout_ms", 1000)), timed_out=True, exit_code=None))
        if "HEALTH" in command:
            scratch = command.split("mkdir -p ", 1)[1].split("/seed", 1)[0]
            for index in range(1, 9):
                self._fs(args)[f"{scratch}/seed/seed-{index}.txt"] = f"seed line alpha-{index}\nneedle-token\n"
            text = "HEALTH free_kib=2000000 load1=1.5 ncpu=8 hostname_ok=1 python=3.9.9\nready\n"
            return _payload(_result("remote.bash", "success", "ok", exit_code=0, preview={"stdout": {"text": text}}), text=text)
        if "python3 - <<'PY'" in command and "blob-" in command:
            remote_dir = command.split("base = pathlib.Path(", 1)[1].split(")", 1)[0].strip("'")
            count = int(command.split("for index in range(", 1)[1].split(")", 1)[0])
            fs = self._fs(args)
            for index in range(count):
                fs[f"{remote_dir}/blob-{index:03d}.bin"] = f"blob-{index}"
            text = f"made {count}\n"
            return _payload(_result("remote.bash", "success", "ok", exit_code=0, preview={"stdout": {"text": text}}), text=text)
        if command.startswith("rm -rf"):
            text = "gone\n"
            return _payload(_result("remote.bash", "success", "ok", exit_code=0, preview={"stdout": {"text": text}}), text=text)
        if "cat " in command:
            path = command.split("cat ", 1)[1].strip()
            content = self._fs(args).get(path, "")
            return _payload(_result("remote.bash", "success", "ok", exit_code=0, preview={"stdout": {"text": content}}), text=content)
        return _payload(_result("remote.bash", "success", "ok", exit_code=0, preview={"stdout": {"text": ""}}))

    def _pull(self, args: Mapping[str, Any], *, partial: int | None = None) -> dict[str, Any]:
        import hashlib

        fs = self._fs(args)
        remote_dir = str(args["remote_path"])
        local_dir = Path(str(args["local_dir"]))
        local_dir.mkdir(parents=True, exist_ok=True)
        files = sorted((path, content) for path, content in fs.items() if path.startswith(remote_dir + "/"))
        manifest_files = []
        pulled, skipped = [], []
        for index, (path, content) in enumerate(files):
            name = Path(path).name
            digest = hashlib.sha256(content.encode()).hexdigest()
            manifest_files.append({"relpath": name, "sha256": digest})
            if partial is not None and index >= partial:
                continue
            target = local_dir / name
            if target.exists():
                skipped.append({"relpath": name})
            else:
                target.write_text(content, encoding="utf-8")
                pulled.append({"relpath": name})
        manifest = {"status": "ok", "file_count": len(files), "files": manifest_files}
        return _payload(_result("remote.artifact_pull", "success", "ok", manifest=manifest, artifacts=[{"manifest": manifest, "pulled": pulled, "skipped": skipped}]))

    def call_cli(self, tool: str, args: Mapping[str, Any], *, kill_after_ms: int | None = None, kill_mode: str = "wrapper", timeout_s: float = 300.0) -> CliResult:
        self.calls.append((f"cli:{tool}", dict(args)))
        if tool == "remote.artifact_pull" and kill_after_ms is not None:
            self._pull(args, partial=3)
            return CliResult(payload=None, returncode=-9, killed=True, duration_ms=kill_after_ms)
        payload = self.call(tool, args)
        return CliResult(payload=payload, returncode=0, killed=False, duration_ms=10)


def _endpoint(label: str = "host-a", host: str = "10.0.0.1", kind: str = "host") -> EndpointContext:
    return EndpointContext(label=label, kind=kind, host=host, port=22, user="root", scratch=f"/tmp/vaws-maturation/run-x/{label}", run_id="run-x")


def _operation(**overrides: Any) -> spec.Operation:
    base: dict[str, Any] = {
        "id": "bash.echo",
        "op_class": "transport",
        "shape": "baseline",
        "tool": "remote.bash",
        "args": {"command": "printf maturation-{trial}"},
        "expect": {"outcome": "success", "status": "ok", "stdout_contains": "maturation-{trial}"},
        "repetitions": 4,
        "timeout_ms": 1000,
        "endpoint_kinds": ("host", "container"),
    }
    base.update(overrides)
    return spec.Operation(**base)


CLASSES = {
    "transport": spec.OperationClass("transport", 20, 0.995, 3000),
    "recovery": spec.OperationClass("recovery", 4, 0.95),
}


class SpecTests(unittest.TestCase):
    def test_default_operations_document_is_valid(self) -> None:
        operation_set = spec.load_operation_set()
        self.assertGreaterEqual(len(operation_set.enabled()), 25)
        ids = [op.id for op in operation_set.operations]
        self.assertEqual(len(ids), len(set(ids)))
        for op in operation_set.operations:
            self.assertIn(op.op_class, operation_set.classes)
            self.assertIn(op.shape, spec.SHAPES)
        shapes = {op.shape for op in operation_set.enabled()}
        for shape in ("long_stream", "stream_under_load", "concurrent", "multi_session", "large_transfer", "interrupted_transfer", "idempotent_rerun", "job_registry"):
            self.assertIn(shape, shapes, f"declared set must exercise {shape}")

    def test_render_substitutes_only_known_names(self) -> None:
        context = {"scratch": "/tmp/s", "trial": 3, "worker": 1}
        self.assertEqual(spec.render("cp {scratch}/a-{trial} ${HOME}/{x,y}", context), "cp /tmp/s/a-3 ${HOME}/{x,y}")
        self.assertEqual(spec.render({"a": ["{worker}", 5]}, context), {"a": ["1", 5]})

    def test_invalid_documents_are_rejected(self) -> None:
        good = {"schema_version": 1, "kind": "maturation-operations", "classes": {"c": {"min_repetitions": 1, "pass_rate_threshold": 1.0}}, "operations": [{"id": "x", "class": "c", "tool": "remote.bash"}]}
        spec.parse_operation_set(good)
        for mutate in (
            lambda d: d.update(schema_version=2),
            lambda d: d["operations"][0].update(shape="nope"),
            lambda d: d["operations"][0].update({"class": "missing"}),
            lambda d: d["operations"][0].update(tool="bash"),
            lambda d: d["operations"][0].update(expect={"outcome": "great"}),
            lambda d: d["operations"].append({"id": "x", "class": "c", "tool": "remote.bash"}),
            lambda d: d["classes"]["c"].update(pass_rate_threshold=1.5),
        ):
            document = json.loads(json.dumps(good))
            mutate(document)
            with self.assertRaises(spec.SpecError):
                spec.parse_operation_set(document)


class RedactTests(unittest.TestCase):
    def test_identities_become_labels_and_generic_addresses_are_scrubbed(self) -> None:
        redactor = redact.Redactor.for_endpoints([{"label": "host-a", "host": "10.1.2.3", "port": 22, "hostname": "node1.example.internal"}])
        home = "/Users" + "/someone"
        text = f"RemoteBash on root@10.1.2.3:22 (node1.example.internal) also 10.20.30.40 and {home}/x at 12:34:56 took 100ms"
        cleaned = redactor.text(text)
        self.assertIn("host-a", cleaned)
        self.assertNotIn("10.1.2.3", cleaned)
        self.assertNotIn("10.20.30.40", cleaned)
        self.assertNotIn(home, cleaned)
        self.assertIn("12:34:56", cleaned)
        self.assertIn("100ms", cleaned)
        self.assertFalse(redact.contains_address(cleaned))
        self.assertTrue(redact.contains_address({"a": ["root@10.0.0.1"]}))

    def test_ipv6_but_not_timestamps(self) -> None:
        redactor = redact.Redactor()
        self.assertEqual(redactor.text("at 2026-09-07T09:40:44Z"), "at 2026-09-07T09:40:44Z")
        self.assertNotIn("fe80", redactor.text("fe80::1 and 2001:db8:0:0:0:0:0:1"))


class AttributionTests(unittest.TestCase):
    def attribute(self, result: Mapping[str, Any] | None, **kwargs: Any) -> str:
        defaults = {"expectation_error": None, "duration_ms": 1000, "timeout_ms": 1000}
        defaults.update(kwargs)
        return attribution.attribute(result, **defaults)["layer"]

    def test_layers(self) -> None:
        self.assertEqual(self.attribute(None), "tool-service")
        self.assertEqual(self.attribute(_result("remote.bash", "timeout", "timeout"), duration_ms=5, timeout_ms=1000), "tool-service")
        self.assertEqual(self.attribute(_result("remote.bash", "timeout", "timeout"), duration_ms=1002, timeout_ms=1000), "timeout")
        self.assertEqual(self.attribute(_result("remote.bash", "failed", "nonzero_exit", exit_code=255)), "transport")
        self.assertEqual(self.attribute(_result("remote.bash", "failed", "nonzero_exit", exit_code=1, preview={"stderr": {"text": "kex_exchange_identification: Connection closed"}})), "transport")
        self.assertEqual(self.attribute(_result("remote.bash", "failed", "nonzero_exit", exit_code=7)), "remote-command")
        self.assertEqual(self.attribute(_result("remote.read", "blocked", "path_outside_root")), "path-policy")
        self.assertEqual(self.attribute(_result("remote.artifact_pull", "failed", "hash_mismatch")), "integrity")
        self.assertEqual(self.attribute(_result("remote.glob", "failed", "failed", error="remote python failed")), "tool-helper")
        self.assertEqual(self.attribute(_result("remote.job_tail", "failed", "log_not_found")), "remote-state")
        self.assertEqual(self.attribute(_result("remote.bash", "success", "ok"), expectation_error="expected text missing"), "contract")
        self.assertEqual(self.attribute(_result("remote.bash", "failed", "nonzero_exit", exit_code=3), op_class="environment"), "environment")
        self.assertEqual(self.attribute(None, exception="RuntimeError: boom"), "harness")
        self.assertEqual(
            self.attribute(
                None,
                exception="TimeoutExpired: Command '['ssh', '-o', 'ControlMaster=auto']'",
            ),
            "transport",
        )
        self.assertEqual(
            self.attribute(None, exception="subprocess.TimeoutExpired: Command '['python3', '-c', 'pass']'"),
            "timeout",
        )

    def test_leaked_ssh_timeout_keeps_reason_free_of_destinations(self) -> None:
        attr = attribution.attribute(
            None,
            expectation_error=None,
            duration_ms=180000,
            timeout_ms=180000,
            exception="TimeoutExpired: Command '['ssh', '-o', 'ControlMaster=auto', 'root@10.0.0.9']'",
        )
        self.assertEqual(attr["layer"], "transport")
        self.assertEqual(attr["fingerprint"], "timeout leaked ssh mux")
        self.assertNotIn("10.0.0.9", attr["reason"])
        self.assertFalse(redact.contains_address(attr))

    def test_reason_distinguishes_instant_from_real_timeout(self) -> None:
        instant = attribution.attribute(_result("remote.bash", "timeout", "timeout"), expectation_error=None, duration_ms=3, timeout_ms=1000)
        self.assertIn("instant timeout", instant["reason"])
        self.assertEqual(instant["fingerprint"], "instant timeout any command")


class StatsTests(unittest.TestCase):
    def test_wilson_bound(self) -> None:
        self.assertAlmostEqual(stats.wilson_lower_bound(20, 20), 0.8389, places=3)
        self.assertLess(stats.wilson_lower_bound(19, 20), 0.80)
        self.assertEqual(stats.wilson_lower_bound(0, 0), 0.0)

    def test_flake_versus_failure_versus_mature(self) -> None:
        cls = CLASSES["transport"]
        self.assertEqual(stats.classify(20, 20, cls, 100), "mature")
        self.assertEqual(stats.classify(19, 20, cls, 100), "flaky")
        self.assertEqual(stats.classify(0, 20, cls, 100), "broken")
        self.assertEqual(stats.classify(5, 5, cls, 100), "insufficient")
        self.assertEqual(stats.classify(20, 20, cls, 9000), "slow")

    def test_aggregate_and_ranking(self) -> None:
        def trial(op: str, label: str, passed: bool, layer: str = "transport") -> dict[str, Any]:
            return {"trial_id": f"{op}-{label}", "operation_id": op, "op_class": "transport", "shape": "baseline", "endpoint_label": label, "passed": passed, "duration_ms": 10, "attribution": None if passed else {"layer": layer, "fingerprint": "fp"}}

        trials = [trial("a", "host-a", True) for _ in range(20)]
        trials += [trial("b", "host-a", i != 3) for i in range(20)]
        trials += [trial("c", "host-a", False, "tool-helper") for _ in range(5)]
        trials += [trial("c", "host-b", True) for _ in range(5)]
        summary = stats.aggregate(trials, CLASSES)
        self.assertEqual(summary["operations"]["a"]["verdict"], "mature")
        self.assertEqual(summary["operations"]["b"]["verdict"], "flaky")
        self.assertEqual(summary["operations"]["b"]["pass_rate"], 0.95)
        self.assertEqual(summary["operations"]["c"]["verdict"], "flaky")
        self.assertEqual(summary["operations"]["c"]["per_endpoint"]["host-a"]["verdict"], "broken")
        self.assertEqual(summary["operations"]["c"]["failure_layers"], {"tool-helper": 5})
        ranking = [item["operation_id"] for item in summary["ranking"]]
        self.assertEqual(ranking, ["c", "b", "a"])
        self.assertEqual(summary["totals"]["failure_layers"], {"transport": 1, "tool-helper": 5})


class ScenarioTests(unittest.TestCase):
    def test_baseline_records_command_environment_and_timing(self) -> None:
        invoker = FakeInvoker()
        trials = run_operation(_operation(), _endpoint(), invoker, repetitions=3)
        self.assertEqual([t["passed"] for t in trials], [True, True, True])
        self.assertEqual(trials[1]["command"], "remote.bash: printf maturation-1")
        self.assertEqual(trials[0]["environment"]["endpoint_label"], "host-a")
        self.assertIsInstance(trials[0]["duration_ms"], int)
        self.assertIsNone(trials[0]["attribution"])

    def test_flaky_operation_attributes_instant_timeout(self) -> None:
        invoker = FakeInvoker(flaky_echo_every=3)
        trials = run_operation(_operation(), _endpoint(), invoker, repetitions=6)
        failed = [t for t in trials if not t["passed"]]
        self.assertEqual(len(failed), 2)
        self.assertEqual(failed[0]["attribution"]["layer"], "tool-service")
        self.assertEqual(failed[0]["raw"]["status"], "timeout")

    def test_idempotent_rerun_sequence(self) -> None:
        op = _operation(
            id="write.no_overwrite",
            op_class="transport",
            shape="idempotent_rerun",
            tool="remote.write",
            args={"file_path": "{scratch}/wn-{trial}.txt", "content": "once-{trial}\n"},
            expect={"outcome": "success", "status": "written"},
            params={"second_expect": {"outcome": "blocked", "status": "file_exists"}, "verify": {"tool": "remote.read", "args": {"file_path": "{scratch}/wn-{trial}.txt"}, "expect": {"outcome": "success", "status": "ok", "stdout_contains": "once-{trial}"}}},
        )
        trials = run_operation(op, _endpoint(), FakeInvoker(), repetitions=2)
        self.assertTrue(all(t["passed"] for t in trials))
        self.assertEqual([s["name"] for s in trials[0]["steps"]], ["first", "rerun-1", "verify"])

    def test_concurrent_shape_records_one_step_per_worker(self) -> None:
        op = _operation(shape="concurrent", params={"concurrency": 5}, args={"command": "printf w-{worker}-{trial}"}, expect={"outcome": "success", "stdout_contains": "w-{worker}-{trial}"})
        trials = run_operation(op, _endpoint(), FakeInvoker(), repetitions=1)
        self.assertTrue(trials[0]["passed"])
        self.assertEqual(len(trials[0]["steps"]), 5)
        self.assertEqual(trials[0]["notes"]["concurrency"], 5)

    def test_job_registry_polls_until_terminal(self) -> None:
        op = _operation(id="job.lifecycle", shape="job_registry", tool=None, params={"command": "sleep 2; printf 'job-done-{trial}\\n'", "tail_contains": "job-done-{trial}", "poll_interval_ms": 1, "stop": True})
        trials = run_operation(op, _endpoint(), FakeInvoker(), repetitions=1)
        self.assertTrue(trials[0]["passed"], trials[0]["steps"])
        names = [s["name"] for s in trials[0]["steps"]]
        self.assertEqual(names, ["start", "status-final", "tail", "status-again", "start-long", "stop", "status-after-stop"])
        self.assertEqual(trials[0]["notes"]["polls"], 2)

    def test_interrupted_pull_resumes_and_verifies(self) -> None:
        op = _operation(id="artifact.interrupted_pull", op_class="recovery", shape="interrupted_transfer", tool=None, params={"direction": "pull", "files": 6, "bytes_per_file": 10, "interrupt_after_ms": 5, "kill_mode": "wrapper"}, timeout_ms=5000)
        with tempfile.TemporaryDirectory() as tmp:
            trials = run_operation(op, _endpoint(), FakeInvoker(), repetitions=1, local_tmp=Path(tmp))
        trial = trials[0]
        self.assertTrue(trial["passed"], trial["steps"])
        self.assertTrue(trial["notes"]["interrupted"])
        self.assertEqual(trial["notes"]["partial_files_after_interrupt"], 3)
        self.assertEqual(trial["notes"]["rerun_skipped_hash_match"], 3)
        self.assertEqual([s["name"] for s in trial["steps"]], ["seed-remote", "interrupted-pull", "verify-no-temp-leftovers", "rerun-pull", "verify-final-hashes", "cleanup"])

    def test_evaluate_reports_specific_violation(self) -> None:
        result = _result("remote.bash", "success", "ok", duration_ms=10)
        self.assertIsNone(evaluate(result, {"outcome": "success"}, ""))
        self.assertIn("status", evaluate(result, {"status": "written"}, "") or "")
        self.assertIn("duration", evaluate(result, {"min_duration_ms": 500}, "") or "")
        self.assertIn("expected text", evaluate(result, {"stdout_contains": "zzz"}, "abc") or "")
        self.assertEqual(evaluate(None, {}, ""), "no result payload")


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.evidence_root = Path(self.temp.name) / "runs"
        quiet = mock.patch("maturation.runner.emit_progress")
        quiet.start()
        self.addCleanup(quiet.stop)
        self.operation_set = spec.parse_operation_set(
            {
                "schema_version": 1,
                "kind": "maturation-operations",
                "classes": {"transport": {"min_repetitions": 4, "pass_rate_threshold": 0.99}, "search": {"min_repetitions": 2, "pass_rate_threshold": 1.0}},
                "operations": [
                    {"id": "bash.echo", "class": "transport", "tool": "remote.bash", "repetitions": 4, "args": {"command": "printf maturation-{trial}"}, "expect": {"outcome": "success", "status": "ok", "stdout_contains": "maturation-{trial}"}},
                    {"id": "glob.seed", "class": "search", "tool": "remote.glob", "repetitions": 2, "args": {"pattern": "*.txt", "path": "{scratch}/seed"}, "expect": {"outcome": "success", "status": "ok", "stdout_contains": "seed-8.txt"}},
                    {"id": "ctr.only", "class": "transport", "tool": "remote.bash", "repetitions": 1, "endpoint_kinds": ["container"], "args": {"command": "printf ctr"}, "expect": {"outcome": "success"}},
                ],
            },
            source="memory.yaml",
        )
        self.endpoints = targets.assign_labels(
            [
                {"host": "10.9.9.1", "port": 22, "user": "root", "kind": "host"},
                {"host": "10.9.9.2", "port": 22, "user": "root", "kind": "host"},
                {"host": "10.9.9.2", "port": 46001, "user": "root", "kind": "container"},
            ]
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_run_skips_unhealthy_host_redacts_and_retains_evidence(self) -> None:
        invoker = FakeInvoker(failing_hosts={"10.9.9.1"}, glob_broken=True)
        report = run(operation_set=self.operation_set, endpoints=self.endpoints, invoker=invoker, config=RunConfig(endpoint_parallelism=3), evidence_root=self.evidence_root, run_id="unit-run")
        serialized = json.dumps(report)
        self.assertNotRegex(serialized, IPV4)
        by_label = {item["label"]: item for item in report["endpoints"]}
        self.assertTrue(by_label["host-a"]["skipped"])
        self.assertEqual(by_label["host-a"]["prepare"]["reason"], "probe_failed")
        self.assertFalse(by_label["host-b"]["skipped"])
        self.assertTrue(by_label["host-b"]["teardown"]["ok"])
        self.assertEqual(report["plan"]["host-b-ctr"], ["bash.echo", "glob.seed", "ctr.only"])
        self.assertEqual(report["plan"]["host-b"], ["bash.echo", "glob.seed"])
        ops = report["summary"]["operations"]
        self.assertEqual(ops["bash.echo"]["verdict"], "mature")
        self.assertEqual(ops["bash.echo"]["n"], 8)
        self.assertEqual(ops["glob.seed"]["verdict"], "broken")
        self.assertEqual(ops["glob.seed"]["failure_layers"], {"tool-helper": 4})
        self.assertEqual(report["summary"]["ranking"][0]["operation_id"], "glob.seed")
        self.assertEqual(report["status"], "partial")
        # Scratch paths are per endpoint so a shared /tmp cannot collide.
        scratches = {args.get("root") for tool, args in invoker.calls if tool == "remote.bash" and "HEALTH" in str(args.get("command"))}
        self.assertEqual(scratches, {"/"})
        seeds = {str(args.get("command")).split("mkdir -p ", 1)[1].split("/seed", 1)[0] for tool, args in invoker.calls if tool == "remote.bash" and "HEALTH" in str(args.get("command"))}
        self.assertEqual(len(seeds), 2)  # the skipped host never reached setup
        run_dir = self.evidence_root / "unit-run"
        self.assertTrue((run_dir / "trials.jsonl").exists())
        self.assertTrue((run_dir / "hosts.json").exists())
        self.assertTrue((run_dir / "summary.md").exists())
        self.assertTrue(any((run_dir / "failures").iterdir()))
        self.assertEqual(len(list((run_dir / "candidates").iterdir())), 1)
        hosts = json.loads((run_dir / "hosts.json").read_text())
        self.assertRegex(json.dumps(hosts), IPV4)  # identities live only here
        self.assertNotRegex((run_dir / "summary.md").read_text(), IPV4)
        self.assertEqual(len(report["knowledge"]["candidates_prepared"]), 1)
        self.assertEqual(report["knowledge"]["captured"], [])
        markdown = render_markdown(report)
        self.assertIn("glob.seed", markdown)
        self.assertIn("broken", markdown)

    def test_replay_rebuilds_report_without_invoker(self) -> None:
        run(operation_set=self.operation_set, endpoints=self.endpoints, invoker=FakeInvoker(glob_broken=True), config=RunConfig(), evidence_root=self.evidence_root, run_id="replay-run")
        report = replay(operation_set=self.operation_set, config=RunConfig(), evidence_root=self.evidence_root, run_id="replay-run")
        self.assertEqual(report["run_id"], "replay-run")
        self.assertEqual(report["summary"]["operations"]["glob.seed"]["verdict"], "broken")
        self.assertNotRegex(json.dumps(report), IPV4)

    def test_reveal_hosts_is_opt_in(self) -> None:
        report = run(operation_set=self.operation_set, endpoints=self.endpoints[1:2], invoker=FakeInvoker(), config=RunConfig(reveal_hosts=True), evidence_root=self.evidence_root, run_id="reveal")
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["summary"]["totals"]["failures"], 0)


class KnowledgeTests(unittest.TestCase):
    def _failing_trials(self) -> list[dict[str, Any]]:
        trials = []
        for index in range(4):
            trials.append(
                {
                    "trial_id": f"r-host-b-glob.seed-{index:03d}",
                    "operation_id": "glob.seed",
                    "op_class": "search",
                    "shape": "baseline",
                    "tool": "remote.glob",
                    "endpoint_label": "host-b",
                    "endpoint_kind": "host",
                    "passed": False,
                    "duration_ms": 100 + index,
                    "status": "failed",
                    "environment": {"remote_python": "3.9.9"},
                    "attribution": {"layer": "tool-helper", "reason": "tool helper failed on root@10.0.0.5:22 (remote python failed)", "fingerprint": "tool-helper failed remote python failed", "step": "call"},
                }
            )
        trials.append({**trials[0], "trial_id": "r-host-b-glob.seed-pass", "passed": True, "attribution": None})
        trials.append({**trials[0], "trial_id": "r-host-b-bash.echo-000", "operation_id": "bash.echo", "attribution": {"layer": "transport", "fingerprint": "x"}})
        return trials

    def test_candidates_are_redacted_and_require_reproduction(self) -> None:
        redactor = redact.Redactor.for_endpoints([{"label": "host-b", "host": "10.0.0.5", "port": 22}])
        candidates = knowledge.build_candidates(self._failing_trials(), run_id="r", evidence_relative_dir=".vaws-local/maturation/runs/r", redactor=redactor)
        self.assertEqual(len(candidates), 1)  # bash.echo failed once only
        candidate = candidates[0]
        self.assertFalse(redact.contains_address(candidate))
        self.assertIn("host-b", candidate["symptom"])
        self.assertIn("4 of 5 repetitions", candidate["symptom"])
        self.assertEqual(candidate["kind"], "known-failure-signatures")
        self.assertEqual(candidate["confidence"], "low")
        self.assertEqual(candidate["verification"]["status"], "inconclusive")
        self.assertEqual(candidate["source"], {"run_ids": ["r"]})

    def test_candidate_passes_the_real_capture_cli_contract(self) -> None:
        redactor = redact.Redactor.for_endpoints([{"label": "host-b", "host": "10.0.0.5", "port": 22}])
        candidate = knowledge.build_candidates(self._failing_trials(), run_id="r", evidence_relative_dir=".vaws-local/maturation/runs/r", redactor=redactor)[0]
        with tempfile.TemporaryDirectory() as tmp:
            result = knowledge.capture_candidate(candidate, extra_args=["--candidate-dir", str(Path(tmp) / "candidates"), "--knowledge-dir", str(ROOT / ".agents" / "knowledge")])
            self.assertEqual(result.get("status"), "passed", result)
            self.assertEqual(result.get("action"), "created")
            written = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
            self.assertNotRegex(json.dumps(written), IPV4)
            # Same signature again merges instead of duplicating.
            again = knowledge.capture_candidate(candidate, extra_args=["--candidate-dir", str(Path(tmp) / "candidates"), "--knowledge-dir", str(ROOT / ".agents" / "knowledge")])
            self.assertEqual(again.get("action"), "unchanged")

    def test_capture_refuses_addresses_and_detects_contract_drift(self) -> None:
        leaked = {"summary": "x", "symptom": "root@10.0.0.5 died"}
        self.assertEqual(knowledge.capture_candidate(leaked)["status"], "refused")
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "capture.py"
            fake.write_text("print('not json')\n", encoding="utf-8")
            result = knowledge.capture_candidate({"summary": "clean"}, capture_script=fake)
            self.assertEqual(result["status"], "contract-drift")


class TargetTests(unittest.TestCase):
    def test_inventory_and_labels(self) -> None:
        inventory = {
            "machines": [
                {"alias": "m1", "host": {"ip": "10.0.0.2", "port": 22, "user": "root"}, "container": {"ssh_port": 46001}},
                {"alias": "m0", "host": {"ip": "10.0.0.1", "port": 22}},
            ]
        }
        endpoints = targets.assign_labels(targets.endpoints_from_inventory(inventory, include_containers=True))
        self.assertEqual([(e["label"], e["kind"], e["port"]) for e in endpoints], [("host-a", "host", 22), ("host-b", "host", 22), ("host-b-ctr", "container", 46001)])
        only = targets.endpoints_from_inventory(inventory, select=["m0"])
        self.assertEqual([e["host"] for e in only], ["10.0.0.1"])
        self.assertEqual(targets.parse_endpoint_arg("10.0.0.3:46002:container")["kind"], "container")
        with self.assertRaises(targets.TargetError):
            targets.parse_endpoint_arg("nonsense")
        with self.assertRaises(targets.TargetError):
            targets.parse_endpoint_arg("h:22:pod")


class CliTests(unittest.TestCase):
    RUN = AGENTS / "maturation" / "run.py"

    def test_list_and_argument_errors(self) -> None:
        proc = subprocess.run([sys.executable, str(self.RUN), "--list"], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        listing = json.loads(proc.stdout)
        self.assertIn("bash.echo", [item["id"] for item in listing["operations"]])
        proc = subprocess.run([sys.executable, str(self.RUN)], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no endpoints", json.loads(proc.stdout)["error"])
        proc = subprocess.run([sys.executable, str(self.RUN), "--report", "does-not-exist", "--evidence-root", tempfile.gettempdir()], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 2)


class InvokeTests(unittest.TestCase):
    def test_run_cli_timeout_returns_instead_of_raising(self) -> None:
        from maturation.invoke import run_cli

        result = run_cli(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            {},
            timeout_s=0.2,
        )
        self.assertTrue(result.killed)
        self.assertGreaterEqual(result.duration_ms, 150)


class TrackedFileHygieneTests(unittest.TestCase):
    def test_no_addresses_or_user_paths_in_harness_or_doc(self) -> None:
        paths = list((AGENTS / "maturation").rglob("*.py")) + list((AGENTS / "maturation").rglob("*.yaml")) + list((AGENTS / "maturation").rglob("*.md"))
        doc = ROOT / "docs" / "deterministic-core-maturation.md"
        if doc.exists():
            paths.append(doc)
        paths.append(Path(__file__))
        # Built in pieces so this test file does not self-match the pattern.
        home_leak = re.compile("/" + "Users/" + r"[A-Za-z]")
        for path in paths:
            text = path.read_text(encoding="utf-8")
            for match in IPV4.finditer(text):
                octets = [int(part) for part in match.group(0).split(".")]
                self.assertTrue(all(o <= 255 for o in octets) and octets[0] in {10, 127}, f"{path.name}: {match.group(0)} looks like a real address")
            self.assertIsNone(home_leak.search(text), f"{path.name}: local user path leaked")


if __name__ == "__main__":
    unittest.main()
