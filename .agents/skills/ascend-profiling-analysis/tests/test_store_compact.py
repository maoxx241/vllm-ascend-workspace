"""``store.write_json`` compact mode: same parsed payload, tight encoding."""

from __future__ import annotations

import json
from pathlib import Path

import conftest  # noqa: F401 — registers sys.path

from ascend_profile import store


def test_write_json_compact_same_payload_tighter_encoding(tmp_path: Path) -> None:
    payload = {"b": [1, 2, {"k": "v"}], "a": {"x": 1.5, "y": None}, "z": "中文"}
    pretty_path = tmp_path / "pretty.json"
    compact_path = tmp_path / "compact.json"
    store.write_json(pretty_path, payload)
    store.write_json(compact_path, payload, compact=True)

    pretty_text = pretty_path.read_text(encoding="utf-8")
    compact_text = compact_path.read_text(encoding="utf-8")
    assert json.loads(pretty_text) == json.loads(compact_text) == payload
    assert compact_text == json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    assert "\n  " not in compact_text, "compact output must not be indented"
    assert "\n  " in pretty_text, "default output keeps the historic 2-space indent"
    assert len(compact_text) < len(pretty_text)


def test_write_json_default_unchanged(tmp_path: Path) -> None:
    payload = {"a": 1}
    path = tmp_path / "m.json"
    store.write_json(path, payload)
    assert path.read_text(encoding="utf-8") == '{\n  "a": 1\n}\n'


if __name__ == "__main__":
    test_write_json_default_unchanged(Path("."))
    print("ok")
