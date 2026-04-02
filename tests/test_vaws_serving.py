import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import vaws


def test_serving_start_parser_requires_server_preset_and_weights_path():
    parser = vaws.build_parser()
    args = parser.parse_args(
        [
            "serving",
            "start",
            "--server-name",
            "box-a",
            "--preset",
            "qwen3_5_35b_tp4",
            "--weights-path",
            "/home/weights/Qwen3.5-35B-A3B",
        ]
    )
    assert args.command == "serving"
    assert args.serving_command == "start"
    assert args.server_name == "box-a"
    assert args.preset == "qwen3_5_35b_tp4"
    assert args.weights_path == "/home/weights/Qwen3.5-35B-A3B"


def test_serving_status_dispatches_by_service_id(monkeypatch, vaws_repo):
    called = {}
    monkeypatch.chdir(vaws_repo)

    def fake_status(paths, service_id):
        called["root"] = str(paths.root)
        called["service_id"] = service_id
        return 0

    monkeypatch.setattr(vaws, "status_service", fake_status, raising=False)

    rc = vaws.main(["serving", "status", "svc-123"])

    assert rc == 0
    assert called == {
        "root": str(vaws_repo),
        "service_id": "svc-123",
    }
