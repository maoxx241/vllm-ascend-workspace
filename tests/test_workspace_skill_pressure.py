from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = {
    "workspace-init": {
        "intent_groups": (
            ("prepare this repo for development", "first-time setup"),
            ("git setup", "first machine"),
        ),
        "pressure_only_variants": (
            "把第一套开发环境先搭起来",
            "先把仓库基础环境准备好",
        ),
    },
    "machine-management": {
        "intent_groups": (
            ("attach a machine", "verify whether a machine is ready"),
            ("remove a machine", "machine attach"),
        ),
        "pressure_only_variants": (
            "把另一台开发机挂上来",
            "这台新 server 也纳入当前 workspace",
        ),
    },
    "serving": {
        "intent_groups": (
            ("start service", "status service"),
            ("list services", "stop service"),
        ),
        "pressure_only_variants": (
            "先把模型服务起起来，我后面再跑 benchmark",
            "把当前代码对应的服务先拉起来",
        ),
    },
    "benchmark": {
        "intent_groups": (
            ("run benchmark", "benchmark execution"),
            ("service session", "ready environment"),
        ),
        "pressure_only_variants": (
            "把 qwen3.5 35b 的压测跑起来",
            "帮我把 serving benchmark 直接打一遍",
        ),
    },
    "workspace-reset": {
        "intent_groups": (
            ("destructive teardown", "reset this workspace"),
            ("post-clone state", "explicit destructive teardown"),
        ),
        "pressure_only_variants": (
            "把这套实验环境彻底清场",
            "把本地和远端的初始化痕迹都抹掉",
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
