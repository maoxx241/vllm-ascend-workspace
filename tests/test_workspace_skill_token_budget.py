from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORD_BUDGETS = {
    ".agents/skills/workspace-init/SKILL.md": 520,
    ".agents/skills/machine-management/SKILL.md": 500,
    ".agents/discovery/README.md": 180,
}


def test_frequently_loaded_skill_files_stay_within_word_budget():
    for relative_path, budget in WORD_BUDGETS.items():
        word_count = len((ROOT / relative_path).read_text(encoding="utf-8").split())
        assert word_count <= budget, f"{relative_path} exceeded budget: {word_count} > {budget}"
