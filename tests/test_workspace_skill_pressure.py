from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = {
    "workspace-init": {
        "intent_groups": (
            ("first usable workspace baseline", "first-time initialization"),
            ("staged re-initialization", "recovering after reset"),
        ),
        "pressure_only_variants": (
            "把第一套开发环境先搭起来",
            "先把仓库基础环境准备好",
        ),
    },
    "workspace-foundation": {
        "intent_groups": (
            ("local prerequisite", "control-plane readiness"),
            ("foundation checks", "missing gh"),
        ),
        "pressure_only_variants": (
            "先检查本地前置依赖",
            "把控制平面依赖状态看一下",
        ),
    },
    "workspace-git-profile": {
        "intent_groups": (
            ("git identity", "fork topology"),
            ("repo remotes", "personalized git setup"),
        ),
        "pressure_only_variants": (
            "把这套仓库的 fork 和远端配置好",
            "先把 Git 侧身份和 topology 准备好",
        ),
    },
    "workspace-fleet": {
        "intent_groups": (
            ("additional managed server", "additional server"),
            ("first server handoff", "post-bootstrap"),
        ),
        "pressure_only_variants": (
            "把另一台开发机挂上来",
            "这台新 server 也纳入当前 workspace",
        ),
    },
    "workspace-reset": {
        "intent_groups": (
            ("reset or deinitialized", "reset this workspace"),
            ("near post-clone state", "post-clone state"),
        ),
        "pressure_only_variants": (
            "把这套实验环境彻底清场",
            "把本地和远端的初始化痕迹都抹掉",
        ),
    },
    "workspace-session-switch": {
        "intent_groups": (
            ("create a new feature session", "new feature session"),
            ("switch the active session", "active session"),
        ),
        "pressure_only_variants": (
            "给我切到另一个工作分支会话",
            "新开一个隔离开发会话",
        ),
    },
    "workspace-sync": {
        "intent_groups": (
            ("sync repository or session state", "repository or session state"),
            ("check current sync status", "current sync status"),
        ),
        "pressure_only_variants": (
            "把当前工作树和兼容同步流程对一下",
            "同步一下 repo 和 session 状态",
        ),
    },
}


def _skill_text(skill_name: str) -> str:
    return (ROOT / ".agents" / "skills" / skill_name / "SKILL.md").read_text(
        encoding="utf-8"
    ).lower()


def test_pressure_scenarios_route_by_intent_not_phrase_lock():
    for skill_name, scenario in SCENARIOS.items():
        text = _skill_text(skill_name)
        assert "examples include, but are not limited to:" in text
        for group in scenario["intent_groups"]:
            assert any(marker.lower() in text for marker in group), (
                f"{skill_name} missing intent signal group: {group}"
            )
        for phrase in scenario["pressure_only_variants"]:
            assert phrase.lower() not in text, (
                f"{skill_name} copied pressure-only variant {phrase!r} into the contract body"
            )
