"""Property tests for endpoint resolution (``core.endpoint``).

Property under test: resolution is deterministic and total. Every payload
either yields a well-formed ``Endpoint`` (all fields typed, port in range,
absolute root, destination safe to hand to ``ssh``) or raises ``EndpointError``
with a precise message — never a partially populated endpoint and never a
foreign exception type.

Managed (session/machine) resolution needs ``.vaws-local`` state and a VAWS
target resolver; it is exercised through a fake resolver module so the
wrapping and field mapping are covered without any real state or host.
"""

from __future__ import annotations

import json
import posixpath
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import core.endpoint as endpoint_mod  # noqa: E402
from core.endpoint import Endpoint, EndpointError, resolve_endpoint  # noqa: E402
from core.ssh_transport import ssh_base_cmd  # noqa: E402
from test_property_support import DOC_HOSTS, Gen, run_cases  # noqa: E402

GARBAGE_VALUES: tuple[Any, ...] = (None, "", 0, -1, 1.5, True, False, [], {}, "0", "x", " ", "1e3", b"bytes", ["a"], {"k": "v"})


def well_formed_payload(gen: Gen) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "host": gen.choice(DOC_HOSTS),
        "port": gen.choice((gen.integer(1, 65535), str(gen.integer(1, 65535)))),
    }
    if gen.boolean():
        payload["user"] = gen.choice(("root", "dev", "agent01"))
    if gen.boolean():
        payload["root"] = gen.choice(("/", "/vllm-workspace", "/vllm-workspace/", "/data/a/b"))
    if gen.boolean():
        payload["cwd"] = gen.choice(("/vllm-workspace", "/vllm-workspace/src", None, ""))
    if gen.boolean():
        payload["runtime_env"] = gen.choice((True, False, 0, 1, "yes"))
    if gen.boolean():
        payload["identity_file"] = gen.choice(("~/.ssh/id_example", None, ""))
    if gen.boolean():
        payload["connect_timeout_ms"] = gen.choice((1000, 10000, "2500", None, 0))
    if gen.boolean():
        payload["alias"] = gen.choice(("dev", None, ""))
    if gen.boolean():
        payload["source"] = gen.choice(({"origin": "test"}, "not-a-dict", None))
    if gen.boolean():
        payload["kind"] = gen.choice(("direct-endpoint", None, ""))
    return payload


def garbage_payload(gen: Gen) -> dict[str, Any]:
    keys = ("host", "port", "user", "root", "cwd", "alias", "session_id", "session_file", "machine", "runtime_env", "identity_file", "kind", "source", "unknown_field")
    payload: dict[str, Any] = {}
    for key in gen.subset(keys):
        payload[key] = gen.one_of(lambda: gen.choice(GARBAGE_VALUES), lambda: gen.word(0, 5), lambda: gen.integer(-5, 70000))
    return payload


def assert_well_formed(test: unittest.TestCase, endpoint: Endpoint) -> None:
    test.assertIsInstance(endpoint.host, str)
    test.assertTrue(endpoint.host, "host must be non-empty")
    test.assertIsInstance(endpoint.port, int)
    test.assertNotIsInstance(endpoint.port, bool)
    test.assertIsInstance(endpoint.user, str)
    test.assertTrue(endpoint.user)
    test.assertIsInstance(endpoint.root, str)
    test.assertTrue(endpoint.root.startswith("/"), f"root must be absolute: {endpoint.root!r}")
    test.assertTrue(endpoint.effective_cwd.startswith("/"), f"cwd must be absolute: {endpoint.effective_cwd!r}")
    test.assertIsInstance(endpoint.connect_timeout_ms, int)
    test.assertIsInstance(endpoint.runtime_env, bool)
    test.assertIn(endpoint.kind, {"direct-endpoint", "managed-session", "managed-machine"})
    target = endpoint.to_result_target()
    for key in ("kind", "endpoint_id", "host", "port", "user", "root", "cwd", "runtime_env"):
        test.assertIn(key, target)
        test.assertIsNotNone(target[key], f"result target {key} is None")
    test.assertRegex(endpoint.endpoint_id, r"^[0-9a-f]{16}$")


class DirectEndpointProperties(unittest.TestCase):
    def setUp(self) -> None:
        # An empty substrate directory: no alias files can leak in from the
        # developer's real (gitignored) endpoints.local.json.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_well_typed_payloads_always_resolve_to_well_formed_endpoints(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            payload = well_formed_payload(gen)
            endpoint = resolve_endpoint(payload)
            assert_well_formed(self, endpoint)
            self.assertEqual(endpoint.kind, "direct-endpoint")
            self.assertEqual(endpoint.host, payload["host"])
            self.assertEqual(endpoint.port, int(payload["port"]))
            self.assertEqual(endpoint.user, payload.get("user") or endpoint_mod.DEFAULT_USER)
            self.assertEqual(endpoint.root, payload.get("root") or endpoint_mod.DEFAULT_ROOT)
            self.assertEqual(endpoint.effective_cwd, payload.get("cwd") or endpoint_mod.DEFAULT_CWD)
            # Determinism: the same payload resolves to an identical endpoint.
            self.assertEqual(resolve_endpoint(dict(payload)), endpoint)
            self.assertEqual(resolve_endpoint(dict(payload)).endpoint_id, endpoint.endpoint_id)

        run_cases(400, body, label="direct endpoint well-formed")

    def test_endpoint_id_depends_only_on_identity_fields(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            base = {"host": gen.choice(DOC_HOSTS), "port": gen.integer(1, 65535), "user": gen.word(1, 6), "root": gen.choice(("/", "/vllm-workspace"))}
            a = resolve_endpoint({**base, "cwd": "/vllm-workspace", "connect_timeout_ms": 1000, "runtime_env": True})
            b = resolve_endpoint({**base, "cwd": "/vllm-workspace/other", "connect_timeout_ms": 9000, "runtime_env": False, "identity_file": "~/.ssh/k"})
            self.assertEqual(a.endpoint_id, b.endpoint_id, "cwd/timeout/runtime_env/identity must not change endpoint identity")
            for field, value in (("host", gen.choice([h for h in DOC_HOSTS if h != base["host"]])), ("port", (base["port"] % 65535) + 1), ("user", base["user"] + "x"), ("root", "/elsewhere")):
                other = resolve_endpoint({**base, field: value})
                self.assertNotEqual(other.endpoint_id, a.endpoint_id, f"changing {field} must change endpoint identity")
            self.assertNotIn(base["host"], a.endpoint_id)

        run_cases(150, body, label="endpoint_id identity")

    def test_garbage_payloads_raise_only_endpoint_error(self) -> None:
        # Managed resolution is a boundary to VAWS state; it is stubbed to the
        # error it raises without any configured session so this test stays
        # offline and deterministic.
        def body(gen: Gen, _index: int) -> None:
            payload = garbage_payload(gen)
            with mock.patch.object(endpoint_mod, "_endpoint_from_managed", side_effect=EndpointError("no managed state")), \
                    mock.patch.object(endpoint_mod, "substrate_root", return_value=Path(self._tmp.name)):
                try:
                    endpoint = resolve_endpoint(payload)
                except EndpointError as exc:
                    self.assertTrue(str(exc), "error must carry a message")
                    return
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"non-EndpointError {type(exc).__name__} for payload {payload!r}: {exc}")
            # Any accepted endpoint must be fully typed, even if the payload was odd.
            self.assertIsInstance(endpoint.host, str)
            self.assertIsInstance(endpoint.port, int)
            self.assertIsInstance(endpoint.user, str)
            self.assertIsInstance(endpoint.root, str)

        run_cases(600, body, label="garbage payload totality")

    def test_missing_target_message_lists_every_selector(self) -> None:
        with mock.patch.object(endpoint_mod, "_endpoint_from_managed", side_effect=EndpointError("no binding")):
            with self.assertRaises(EndpointError) as ctx:
                resolve_endpoint({})
        message = str(ctx.exception)
        for token in ("host+port", "alias", "session_id", "machine", "no binding"):
            self.assertIn(token, message)

    @unittest.expectedFailure
    def test_known_defect_port_range_is_not_validated(self) -> None:
        """KNOWN DEFECT (low): ``_direct_endpoint`` accepts any integer port,
        so ``port=70000`` / ``port=-1`` produce an Endpoint that can never be
        reached and fails later inside ``ssh`` instead of at resolution.
        Evidence: ``resolve_endpoint({'host': '203.0.113.5', 'port': 70000}).port == 70000``."""
        for port in (70000, -1, 65536):
            with self.assertRaises(EndpointError, msg=f"port {port} accepted"):
                resolve_endpoint({"host": "203.0.113.5", "port": port})

    @unittest.expectedFailure
    def test_known_defect_connect_timeout_garbage_leaks_value_error(self) -> None:
        """KNOWN DEFECT (low): ``connect_timeout_ms`` is converted with a bare
        ``int(...)``; a non-numeric string raises ``ValueError`` rather than
        ``EndpointError``, so MCP callers receive an internal error instead of a
        precise resolution failure. Evidence: ``connect_timeout_ms='abc'``."""
        with self.assertRaises(EndpointError):
            resolve_endpoint({"host": "203.0.113.5", "port": 22, "connect_timeout_ms": "abc"})

    @unittest.expectedFailure
    def test_known_defect_relative_root_or_cwd_is_accepted(self) -> None:
        """KNOWN DEFECT (medium-low): a relative ``root`` (or ``cwd``) is
        accepted at resolution time. Every later path check then fails with
        ``PathPolicyError('remote path must be absolute')``, which surfaces as
        ``path_outside_root`` on unrelated tool calls — the symptom appears far
        from the cause. Evidence: ``root='relative'`` resolves successfully."""
        with self.assertRaises(EndpointError):
            resolve_endpoint({"host": "203.0.113.5", "port": 22, "root": "relative"})
        with self.assertRaises(EndpointError):
            resolve_endpoint({"host": "203.0.113.5", "port": 22, "cwd": "relative"})

    @unittest.expectedFailure
    def test_known_defect_user_field_can_inject_ssh_options(self) -> None:
        """KNOWN DEFECT (high): ``Endpoint.destination()`` is ``f"{user}@{host}"``
        and is appended to ``ssh`` argv positionally. A ``user`` beginning with
        ``-`` (from a tool argument or an alias file) is parsed by ``ssh`` as an
        option — e.g. ``-oProxyCommand=...`` runs a local command. Neither
        ``_direct_endpoint`` nor ``ssh_base_cmd`` rejects it or uses ``-l``.
        Evidence: argv ends with ``'-oProxyCommand=marker@203.0.113.5'``."""
        endpoint = resolve_endpoint({"host": "203.0.113.5", "port": 22, "user": "-oProxyCommand=marker"})
        argv = ssh_base_cmd(endpoint)
        destination = argv[-1]
        self.assertFalse(destination.startswith("-"), f"destination is parsed as an ssh option: {argv}")

    def test_ssh_argv_shape_is_stable_for_safe_users(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            user = gen.choice(("root", "dev", "agent01")) + gen.text("abc0123", 0, 3)
            endpoint = resolve_endpoint({"host": gen.choice(DOC_HOSTS), "port": gen.integer(1, 65535), "user": user})
            argv = ssh_base_cmd(endpoint)
            self.assertEqual(argv[0], "ssh")
            self.assertEqual(argv[-1], f"{user}@{endpoint.host}")
            self.assertEqual(argv[-3:-1], ["-p", str(endpoint.port)])
            timeout_options = [item for item in argv if item.startswith("ConnectTimeout=")]
            self.assertEqual(len(timeout_options), 1)
            self.assertGreaterEqual(int(timeout_options[0].split("=")[1]), 1)

        run_cases(100, body, label="ssh argv shape")


class AliasResolutionProperties(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.substrate = Path(self._tmp.name)
        patcher = mock.patch.object(endpoint_mod, "substrate_root", return_value=self.substrate)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_aliases(self, entries: dict[str, Any], *, local: dict[str, Any] | None = None) -> None:
        (self.substrate / "endpoints.json").write_text(json.dumps({"endpoints": entries}), encoding="utf-8")
        if local is not None:
            (self.substrate / "endpoints.local.json").write_text(json.dumps({"endpoints": local}), encoding="utf-8")

    def test_alias_fields_are_used_unless_payload_overrides_with_a_value(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            alias_name = gen.word(1, 8)
            entry = {"host": gen.choice(DOC_HOSTS), "port": gen.integer(1, 65535), "user": "aliasuser", "root": "/vllm-workspace"}
            self._write_aliases({alias_name: entry})
            payload: dict[str, Any] = {"alias": alias_name}
            override_user = gen.boolean()
            override_root = gen.boolean()
            if override_user:
                payload["user"] = "override"
            if override_root:
                payload["root"] = "/data"
            endpoint = resolve_endpoint(payload)
            self.assertEqual(endpoint.host, entry["host"])
            self.assertEqual(endpoint.port, entry["port"])
            self.assertEqual(endpoint.user, "override" if override_user else "aliasuser")
            self.assertEqual(endpoint.root, "/data" if override_root else "/vllm-workspace")
            self.assertEqual(endpoint.alias, alias_name)
            self.assertEqual(resolve_endpoint(dict(payload)), endpoint)

        run_cases(120, body, label="alias merge")

    def test_local_alias_file_overrides_shared_entries(self) -> None:
        self._write_aliases({"dev": {"host": DOC_HOSTS[0], "port": 1}}, local={"dev": {"host": DOC_HOSTS[1], "port": 2}})
        endpoint = resolve_endpoint({"alias": "dev"})
        self.assertEqual((endpoint.host, endpoint.port), (DOC_HOSTS[1], 2))

    def test_unknown_or_malformed_alias_files_raise_endpoint_error(self) -> None:
        self._write_aliases({"dev": {"host": DOC_HOSTS[0], "port": 1}})
        with self.assertRaises(EndpointError):
            resolve_endpoint({"alias": "missing"})
        (self.substrate / "endpoints.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(EndpointError):
            resolve_endpoint({"alias": "dev"})
        (self.substrate / "endpoints.json").write_text(json.dumps({"endpoints": ["list"]}), encoding="utf-8")
        with self.assertRaises(EndpointError):
            resolve_endpoint({"alias": "dev"})
        (self.substrate / "endpoints.json").write_text(json.dumps({"endpoints": {"dev": {"host": DOC_HOSTS[0]}}}), encoding="utf-8")
        with self.assertRaises(EndpointError):
            resolve_endpoint({"alias": "dev"})

    def test_direct_host_port_wins_over_alias(self) -> None:
        self._write_aliases({"dev": {"host": DOC_HOSTS[0], "port": 1}})
        endpoint = resolve_endpoint({"alias": "dev", "host": DOC_HOSTS[2], "port": 46000})
        self.assertEqual((endpoint.host, endpoint.port), (DOC_HOSTS[2], 46000))

    @unittest.expectedFailure
    def test_known_defect_explicit_null_fields_override_alias_values(self) -> None:
        """KNOWN DEFECT (low): ``resolve_endpoint`` merges ``{**alias, **payload}``
        without dropping ``None`` values, so an MCP client that sends optional
        fields as explicit ``null`` (``host: null, port: null, alias: 'dev'``)
        cannot use a configured alias — it fails with the misleading message
        ``endpoint port must be an integer``. Evidence: see assertion below."""
        self._write_aliases({"dev": {"host": DOC_HOSTS[0], "port": 46000, "user": "dev"}})
        endpoint = resolve_endpoint({"alias": "dev", "host": None, "port": None, "user": None, "root": None})
        self.assertEqual((endpoint.host, endpoint.port, endpoint.user), (DOC_HOSTS[0], 46000, "dev"))


class ManagedResolutionProperties(unittest.TestCase):
    def _fake_toolbox(self, *, session_id: str | None, alias: str, host: str, port: int, user: str, runtime_root: str, error: Exception | None = None) -> types.ModuleType:
        module = types.ModuleType("vaws_remote_toolbox")

        def resolve_remote_target(**kwargs: Any) -> Any:
            if error is not None:
                raise error
            return SimpleNamespace(
                session_id=session_id,
                alias=alias,
                runtime_root=runtime_root,
                container_endpoint=SimpleNamespace(host=host, port=port, user=user),
                to_dict=lambda: {"alias": alias, "session_id": session_id, "kwargs": {k: v for k, v in kwargs.items() if k != "repo_root"}},
            )

        module.resolve_remote_target = resolve_remote_target  # type: ignore[attr-defined]
        return module

    def test_managed_targets_map_to_well_formed_endpoints(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            has_session = gen.boolean()
            session_id = "sess-" + gen.text("abcdef0123", 4, 8) if has_session else None
            alias = "machine-" + gen.text("abc", 1, 3)
            fake = self._fake_toolbox(session_id=session_id, alias=alias, host=gen.choice(DOC_HOSTS), port=gen.integer(1, 65535), user="root", runtime_root="/vllm-workspace")
            payload: dict[str, Any] = {"session_id": session_id} if has_session else {"machine": alias}
            if gen.boolean():
                payload["root"] = "/vllm-workspace"
            if gen.boolean():
                payload["cwd"] = "/vllm-workspace/src"
            with mock.patch.dict(sys.modules, {"vaws_remote_toolbox": fake}):
                endpoint = resolve_endpoint(payload)
            assert_well_formed(self, endpoint)
            self.assertEqual(endpoint.kind, "managed-session" if has_session else "managed-machine")
            self.assertEqual(endpoint.alias, session_id if has_session else alias)
            self.assertEqual(endpoint.effective_cwd, payload.get("cwd") or "/vllm-workspace")
            self.assertIn("vaws_target", endpoint.source or {})
            self.assertEqual(endpoint.to_result_target()["source"]["vaws_target"]["kwargs"]["session_id"], session_id)

        run_cases(120, body, label="managed endpoint mapping")

    def test_resolver_failures_are_wrapped_as_endpoint_errors(self) -> None:
        for error in (RuntimeError("boom"), KeyError("session"), ValueError("bad"), FileNotFoundError("state")):
            with self.subTest(error=type(error).__name__):
                fake = self._fake_toolbox(session_id="sess-a", alias="m", host=DOC_HOSTS[0], port=1, user="root", runtime_root="/w", error=error)
                with mock.patch.dict(sys.modules, {"vaws_remote_toolbox": fake}):
                    with self.assertRaises(EndpointError) as ctx:
                        resolve_endpoint({"session_id": "sess-a"})
                self.assertIn("failed to resolve managed target", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
