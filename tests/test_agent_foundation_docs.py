from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
FOUNDATION_DOC = ROOT / "docs/superpowers/contracts/2026-04-02-agent-tooling-foundation.md"
INVENTORY = ROOT / "docs/superpowers/inventories/2026-04-02-remote-load-bearing-behaviors.yaml"
def test_foundation_doc_records_phase1_exit_bar_and_vaws_decision():
    text = FOUNDATION_DOC.read_text(encoding="utf-8")
    assert "## Phase 1 Exit Bar" in text
    assert "## vaws Namespace Decision" in text
    assert "do not edit shared skills in phase 1" in text.lower()


def test_remote_load_bearing_inventory_is_preserved_as_historical_input():
    payload = yaml.safe_load(INVENTORY.read_text(encoding="utf-8"))

    assert payload["source_file"] == "tools/lib/remote.py"
    assert payload["coverage_status"] == "starter-triage"
    assert payload["phase2_requirement"] == "expand before deleting or replacing load-bearing remote behavior"
    assert len(payload["triage_method"]) >= 2
    assert len(payload["entries"]) >= 6
    for entry in payload["entries"]:
        assert entry["source_symbol"]
        assert entry["action_family"]
        assert entry["extraction_risk"]
