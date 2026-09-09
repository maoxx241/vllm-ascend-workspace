#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[4]
LIB_DIR = ROOT / ".agents" / "lib"
REPO_INIT_SCRIPTS = ROOT / ".agents" / "skills" / "repo-init" / "scripts"
for value in (str(LIB_DIR), str(REPO_INIT_SCRIPTS)):
    if value not in sys.path:
        sys.path.insert(0, value)

import repo_init_probe as probe  # noqa: E402
from _profile_choice_common import fixed_workspace_alias_question  # noqa: E402
from vaws_local_state import (  # noqa: E402
    WorkspaceStateError,
    effective_workspace_alias,
    ensure_workspace_identity,
    set_workspace_alias,
    workspace_identity_summary,
)

GIT_IDENTITY_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Repo Init Fixture",
    "GIT_AUTHOR_EMAIL": "repo-init-fixture@example.invalid",
    "GIT_COMMITTER_NAME": "Repo Init Fixture",
    "GIT_COMMITTER_EMAIL": "repo-init-fixture@example.invalid",
}


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "safe.directory=*", *args],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
        env=GIT_IDENTITY_ENV,
    )


def init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init")
    git(path, "config", "user.name", "Repo Init Fixture")
    git(path, "config", "user.email", "repo-init-fixture@example.invalid")


def github_payload(
    full_name: str,
    *,
    repo_id: int,
    is_fork: bool,
    parent: Optional[str] = None,
    ssh_url: Optional[str] = None,
    clone_url: Optional[str] = None,
) -> Dict[str, Any]:
    owner, name = full_name.split("/", 1)
    payload: Dict[str, Any] = {
        "id": repo_id,
        "full_name": full_name,
        "name": name,
        "owner": {"login": owner},
        "fork": is_fork,
        "default_branch": "main",
        "ssh_url": ssh_url or f"git@github.com:{full_name}.git",
        "clone_url": clone_url or f"https://github.com/{full_name}.git",
    }
    if parent:
        payload["parent"] = {"full_name": parent}
        payload["source"] = {"full_name": parent}
    return payload


class WorkspaceIdentityTests(unittest.TestCase):
    def identity_path(self, temp_dir: str) -> Path:
        return Path(temp_dir) / ".vaws-local" / "workspace-identity.json"

    def test_ensure_silently_creates_one_persistent_uuid4(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.identity_path(temp_dir)
            first, first_action = ensure_workspace_identity(path=path)
            second, second_action = ensure_workspace_identity(path=path)
            self.assertEqual(first_action, "created")
            self.assertEqual(second_action, "existing")
            self.assertEqual(first["agent_id"], second["agent_id"])
            self.assertEqual(uuid.UUID(first["agent_id"]).version, 4)
            self.assertEqual(first["alias_decision"], "pending")
            self.assertTrue(workspace_identity_summary(path)["alias_choice_required"])

    def test_concurrent_ensure_returns_one_shared_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.identity_path(temp_dir)
            results: list[str] = []
            errors: list[BaseException] = []

            def create() -> None:
                try:
                    identity, _ = ensure_workspace_identity(path=path)
                    results.append(identity["agent_id"])
                except BaseException as exc:  # pragma: no cover
                    errors.append(exc)

            threads = [threading.Thread(target=create) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(set(results)), 1)

    def test_alias_set_and_decline_are_persisted_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.identity_path(temp_dir)
            identity, _ = set_workspace_alias("Team42", path=path)
            self.assertEqual(identity["alias"], "team42")
            self.assertEqual(effective_workspace_alias(path), "team42")
            self.assertFalse(workspace_identity_summary(path)["alias_choice_required"])

            identity, _ = set_workspace_alias(None, path=path, declined=True)
            self.assertIsNone(identity["alias"])
            self.assertEqual(identity["alias_decision"], "declined")
            self.assertIsNone(effective_workspace_alias(path))
            self.assertFalse(workspace_identity_summary(path)["alias_choice_required"])

    def test_alias_rejects_resource_unsafe_characters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(WorkspaceStateError):
                set_workspace_alias("team-name", path=self.identity_path(temp_dir))

    def test_init_question_has_machine_custom_and_none_choices(self) -> None:
        question = fixed_workspace_alias_question("agent12345")
        self.assertEqual(
            [option["id"] for option in question["options"]],
            ["machine-username", "custom", "none"],
        )


class CanonicalTopologyTests(unittest.TestCase):
    def test_community_workspace_is_organization_canonical(self) -> None:
        self.assertEqual(
            probe.COMMUNITY["workspace"],
            "vllm-ascend-workspace/vllm-ascend-workspace",
        )
        self.assertEqual(probe.COMMUNITY["vllm"], "vllm-project/vllm")
        self.assertEqual(probe.COMMUNITY["vllm-ascend"], "vllm-project/vllm-ascend")
        self.assertFalse(hasattr(probe, "ORGANIZATION"))
        self.assertFalse(hasattr(probe, "ORGANIZATION_DEV_FORKS"))
        self.assertFalse(hasattr(probe, "gh_organization_fork_info"))

    def test_classify_remote_keeps_community_personal_and_other_distinct(self) -> None:
        self.assertEqual(
            probe.classify_remote(
                "workspace",
                "vllm-ascend-workspace/vllm-ascend-workspace",
                "alice",
            ),
            "community",
        )
        self.assertEqual(
            probe.classify_remote("vllm", "vllm-project/vllm", "alice"),
            "community",
        )
        self.assertEqual(
            probe.classify_remote("vllm", "vllm-ascend-workspace/vllm", "alice"),
            "other",
        )
        self.assertEqual(
            probe.classify_remote(
                "vllm-ascend",
                "vllm-ascend-workspace/vllm-ascend",
                "alice",
            ),
            "other",
        )
        self.assertEqual(
            probe.classify_remote("vllm", "alice/vllm", "alice"),
            "user-fork",
        )
        self.assertEqual(probe.classify_remote("vllm", None, "alice"), "missing")
        self.assertEqual(
            probe.classify_remote("vllm", "other-org/vllm", "alice"),
            "other",
        )
        self.assertNotEqual(
            probe.classify_remote("vllm", "vllm-ascend-workspace/vllm", "alice"),
            "community",
        )


class MockedGitHubForkTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_run = probe.run
        self._original_which = probe.which
        self.api_payloads: Dict[str, Dict[str, Any]] = {}
        self.missing: set[str] = set()
        self.commands: List[List[str]] = []
        probe.which = lambda name: "/usr/bin/gh" if name == "gh" else None  # type: ignore[method-assign]

        def fake_run(cmd: List[str], cwd: Optional[Path] = None) -> Tuple[int, str, str]:
            self.commands.append(list(cmd))
            if cmd[:2] == ["gh", "api"] and len(cmd) >= 3 and cmd[2].startswith("repos/"):
                requested = cmd[2][len("repos/") :]
                if requested in self.missing:
                    return 1, "", "Not Found"
                payload = self.api_payloads.get(requested)
                if payload is None:
                    return 1, "", "Not Found"
                return 0, json.dumps(payload), ""
            return 1, "", "unexpected command"

        probe.run = fake_run  # type: ignore[method-assign]

    def tearDown(self) -> None:
        probe.run = self._original_run  # type: ignore[method-assign]
        probe.which = self._original_which  # type: ignore[method-assign]

    def test_gh_fork_info_queries_only_authenticated_user_repos(self) -> None:
        self.api_payloads["alice/vllm"] = github_payload(
            "alice/vllm",
            repo_id=11,
            is_fork=True,
            parent="vllm-project/vllm",
        )
        self.api_payloads["alice/vllm-ascend"] = github_payload(
            "alice/vllm-ascend",
            repo_id=12,
            is_fork=True,
            parent="vllm-project/vllm-ascend",
        )
        self.api_payloads["alice/vllm-ascend-workspace"] = github_payload(
            "alice/vllm-ascend-workspace",
            repo_id=13,
            is_fork=True,
            parent="vllm-ascend-workspace/vllm-ascend-workspace",
        )
        self.api_payloads["vllm-ascend-workspace/vllm"] = github_payload(
            "vllm-ascend-workspace/vllm",
            repo_id=99,
            is_fork=True,
            parent="vllm-project/vllm",
        )

        personal = probe.gh_fork_info("alice")
        requested = [
            cmd[2][len("repos/") :]
            for cmd in self.commands
            if len(cmd) >= 3 and cmd[:2] == ["gh", "api"] and cmd[2].startswith("repos/")
        ]
        self.assertEqual(
            requested,
            [
                "alice/vllm-ascend-workspace",
                "alice/vllm",
                "alice/vllm-ascend",
            ],
        )
        self.assertTrue(personal["vllm"]["exists"])
        self.assertEqual(personal["vllm"]["full_name"], "alice/vllm")
        self.assertEqual(personal["vllm"]["id"], 11)
        self.assertEqual(
            personal["vllm"]["ssh_url"],
            "git@github.com:alice/vllm.git",
        )
        self.assertEqual(
            personal["vllm"]["clone_url"],
            "https://github.com/alice/vllm.git",
        )
        self.assertEqual(personal["vllm"]["classification"], "user-fork")
        self.assertTrue(personal["vllm"]["personal_fork"])
        self.assertTrue(personal["vllm-ascend"]["exists"])
        self.assertTrue(personal["workspace"]["exists"])
        self.assertFalse(hasattr(probe, "gh_organization_fork_info"))

    def test_generic_personal_fork_output_semantics_are_preserved(self) -> None:
        self.api_payloads["alice/vllm"] = github_payload(
            "alice/vllm",
            repo_id=22,
            is_fork=True,
            parent="vllm-project/vllm",
        )
        self.api_payloads["alice/vllm-ascend"] = github_payload(
            "alice/vllm-ascend",
            repo_id=23,
            is_fork=True,
            parent="vllm-project/vllm-ascend",
        )
        self.api_payloads["alice/vllm-ascend-workspace"] = github_payload(
            "alice/vllm-ascend-workspace",
            repo_id=24,
            is_fork=True,
            parent="vllm-ascend-workspace/vllm-ascend-workspace",
        )
        personal = probe.gh_fork_info("alice")
        self.assertEqual(
            personal["vllm"]["full_name"],
            "alice/vllm",
        )
        self.assertTrue(personal["vllm"]["exists"])
        self.assertTrue(personal["vllm"]["is_fork"])
        self.assertEqual(personal["vllm"]["parent_full_name"], "vllm-project/vllm")
        self.assertEqual(personal["vllm"]["default_branch"], "main")
        self.assertIn("ssh_url", personal["vllm"])
        self.assertIn("clone_url", personal["vllm"])
        self.assertTrue(personal["workspace"]["exists"])
        self.assertEqual(
            personal["workspace"]["full_name"],
            "alice/vllm-ascend-workspace",
        )
        self.assertEqual(personal["workspace"]["classification"], "user-fork")

    def test_restored_personal_login_follows_ordinary_user_fork_path(self) -> None:
        self.api_payloads["maoxx241/vllm"] = github_payload(
            "maoxx241/vllm",
            repo_id=1009465986,
            is_fork=True,
            parent="vllm-project/vllm",
        )
        self.api_payloads["maoxx241/vllm-ascend"] = github_payload(
            "maoxx241/vllm-ascend",
            repo_id=924147541,
            is_fork=True,
            parent="vllm-project/vllm-ascend",
        )
        self.missing.add("maoxx241/vllm-ascend-workspace")
        personal = probe.gh_fork_info("maoxx241")
        self.assertTrue(personal["vllm"]["exists"])
        self.assertTrue(personal["vllm"]["personal_fork"])
        self.assertEqual(personal["vllm"]["classification"], "user-fork")
        self.assertEqual(personal["vllm"]["full_name"], "maoxx241/vllm")
        self.assertEqual(personal["vllm"]["id"], 1009465986)
        self.assertTrue(personal["vllm-ascend"]["exists"])
        self.assertTrue(personal["vllm-ascend"]["personal_fork"])
        self.assertEqual(personal["vllm-ascend"]["classification"], "user-fork")
        self.assertFalse(personal["workspace"]["exists"])
        self.assertFalse(personal["workspace"]["personal_fork"])

    def test_legacy_personal_redirect_reports_resolved_identity(self) -> None:
        self.api_payloads["former-user/vllm-ascend-workspace"] = github_payload(
            "vllm-ascend-workspace/vllm-ascend-workspace",
            repo_id=1196723340,
            is_fork=False,
        )
        self.api_payloads["former-user/vllm"] = github_payload(
            "new-user/vllm",
            repo_id=55,
            is_fork=True,
            parent="vllm-project/vllm",
        )
        self.api_payloads["former-user/vllm-ascend"] = github_payload(
            "new-user/vllm-ascend",
            repo_id=56,
            is_fork=True,
            parent="vllm-project/vllm-ascend",
        )
        personal = probe.gh_fork_info("former-user")

        workspace = personal["workspace"]
        self.assertTrue(workspace["redirected"])
        self.assertFalse(workspace["exists"])
        self.assertFalse(workspace["personal_fork"])
        self.assertEqual(
            workspace["full_name"],
            "vllm-ascend-workspace/vllm-ascend-workspace",
        )
        self.assertEqual(workspace["id"], 1196723340)
        self.assertEqual(workspace["classification"], "community")

        vllm = personal["vllm"]
        self.assertTrue(vllm["redirected"])
        self.assertFalse(vllm["exists"])
        self.assertFalse(vllm["personal_fork"])
        self.assertEqual(vllm["full_name"], "new-user/vllm")
        self.assertEqual(vllm["id"], 55)
        self.assertEqual(
            vllm["ssh_url"],
            "git@github.com:new-user/vllm.git",
        )
        self.assertEqual(vllm["classification"], "other")
        self.assertNotEqual(vllm["classification"], "user-fork")

    def test_keep_current_and_community_only_remain_topology_options(self) -> None:
        original_detect = probe.detect_git_username_candidate
        original_question = probe.fixed_machine_username_question
        probe.detect_git_username_candidate = lambda repo_root=None: {  # type: ignore[method-assign]
            "available": False,
            "candidate": None,
            "source": None,
            "raw_value": None,
        }
        probe.fixed_machine_username_question = lambda repo_root=None: {  # type: ignore[method-assign]
            "options": []
        }
        try:
            compact = probe.compact_payload(
                {
                    "platform": {"kind": "macos", "machine": "arm64"},
                    "repo_root": None,
                    "workspace_profile": {
                        "exists": True,
                        "choice_required": False,
                        "username_rules": "letters and digits",
                        "default_generated_pattern": "agent#####",
                        "machine_username": "agent12345",
                    },
                    "workspace_identity": {"alias_choice_required": False},
                    "gh": {},
                    "gh_install_plan": {
                        "preferred": {"label": "Homebrew"},
                        "fallback": {"label": "user-space installer"},
                    },
                    "submodules": [],
                    "repos": {},
                    "forks": {},
                }
            )
        finally:
            probe.detect_git_username_candidate = original_detect  # type: ignore[method-assign]
            probe.fixed_machine_username_question = original_question  # type: ignore[method-assign]
        self.assertEqual(
            compact["decision_checkpoint"]["repo_topology"]["options"],
            ["keep-current", "recommended-fork-mode", "community-only"],
        )
        self.assertNotIn("organization_forks", compact)


class EstablishedRemotePreservationTests(unittest.TestCase):
    def test_inspect_repo_does_not_mutate_fetch_push_or_extra_remotes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            vllm = workspace / "vllm"
            init_git_repo(workspace)
            init_git_repo(vllm)
            git(
                vllm,
                "remote",
                "add",
                "origin",
                "git@github.com:alice/vllm.git",
            )
            git(
                vllm,
                "remote",
                "set-url",
                "--push",
                "origin",
                "https://github.com/alice/vllm.git",
            )
            git(
                vllm,
                "remote",
                "add",
                "upstream",
                "https://github.com/vllm-project/vllm.git",
            )
            git(
                vllm,
                "remote",
                "set-url",
                "--push",
                "upstream",
                "git@github.com:vllm-project/vllm.git",
            )
            git(
                vllm,
                "remote",
                "add",
                "upstream2",
                "git@github.com:example/vllm-mirror.git",
            )
            before = git(vllm, "config", "--local", "--list").stdout
            inspected = probe.inspect_repo(workspace, "vllm", "alice")
            after = git(vllm, "config", "--local", "--list").stdout
            self.assertEqual(before, after)
            remotes = inspected["remotes"]
            self.assertEqual(remotes["origin"]["fetch_url"], "git@github.com:alice/vllm.git")
            self.assertEqual(
                remotes["origin"]["push_url"],
                "https://github.com/alice/vllm.git",
            )
            self.assertEqual(
                remotes["upstream"]["fetch_url"],
                "https://github.com/vllm-project/vllm.git",
            )
            self.assertEqual(
                remotes["upstream"]["push_url"],
                "git@github.com:vllm-project/vllm.git",
            )
            self.assertEqual(
                remotes["upstream2"]["fetch_url"],
                "git@github.com:example/vllm-mirror.git",
            )
            self.assertEqual(inspected["origin_kind"], "user-fork")
            self.assertEqual(inspected["upstream_kind"], "community")

    def test_unrelated_organization_origin_is_other(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            vllm = workspace / "vllm"
            init_git_repo(workspace)
            init_git_repo(vllm)
            git(
                vllm,
                "remote",
                "add",
                "origin",
                "git@github.com:vllm-ascend-workspace/vllm.git",
            )
            git(
                vllm,
                "remote",
                "add",
                "upstream",
                "https://github.com/vllm-project/vllm.git",
            )
            inspected = probe.inspect_repo(workspace, "vllm", "alice")
            self.assertEqual(inspected["origin_kind"], "other")
            self.assertEqual(inspected["upstream_kind"], "community")
            self.assertNotEqual(inspected["origin_kind"], "community")
            self.assertNotEqual(inspected["origin_kind"], "user-fork")


if __name__ == "__main__":
    unittest.main()
