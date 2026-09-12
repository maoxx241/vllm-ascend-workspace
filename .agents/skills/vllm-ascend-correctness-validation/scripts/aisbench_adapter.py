#!/usr/bin/env python3
"""Prepare AISBench accuracy runs and normalize summary CSV metrics."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=ROOT)

import csv
import json
import os
import re
import shlex
import tempfile
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = 1
SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
MODEL_TASK = "vaws_correctness"
MODEL_ABBR = "vaws-correctness"


class AisbenchAdapterError(ValueError):
    """Raised when AISBench preparation or normalization is invalid."""


def emit_progress(phase: str, **details: Any) -> None:
    print(json.dumps({"phase": phase, **details}, ensure_ascii=False), file=sys.stderr)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not slug:
        raise AisbenchAdapterError(f"cannot create safe case id from {value!r}")
    return slug[:96]


def _validate_prepare(
    *,
    host: str,
    port: int,
    served_model: str,
    datasets: Sequence[str],
    metric: str,
    max_out_len: int,
    batch_size: int,
    num_prompts: int | None,
) -> None:
    if not host.strip() or any(character.isspace() for character in host):
        raise AisbenchAdapterError("host must be a non-empty address without whitespace")
    if not 1 <= port <= 65535:
        raise AisbenchAdapterError("port must be between 1 and 65535")
    if not served_model.strip():
        raise AisbenchAdapterError("served-model must be non-empty")
    if not datasets:
        raise AisbenchAdapterError("at least one dataset is required")
    for dataset in datasets:
        if not SAFE_NAME_RE.fullmatch(dataset):
            raise AisbenchAdapterError(f"invalid dataset name: {dataset!r}")
    if not SAFE_NAME_RE.fullmatch(metric):
        raise AisbenchAdapterError(f"invalid metric name: {metric!r}")
    if max_out_len < 1 or batch_size < 1:
        raise AisbenchAdapterError("max-out-len and batch-size must be positive")
    if num_prompts is not None and num_prompts < 1:
        raise AisbenchAdapterError("num-prompts must be positive")


def render_model_config(
    *,
    host: str,
    port: int,
    served_model: str,
    max_out_len: int,
    batch_size: int,
    temperature: float,
    seed: int,
) -> str:
    return f'''from ais_bench.benchmark.models import VLLMCustomAPIChat
from ais_bench.benchmark.utils.postprocess.model_postprocessors import extract_non_reasoning_content

models = [
    dict(
        attr="service",
        type=VLLMCustomAPIChat,
        abbr={MODEL_ABBR!r},
        path="",
        model={served_model!r},
        stream=False,
        request_rate=0,
        use_timestamp=False,
        retry=2,
        api_key="",
        host_ip={host!r},
        host_port={port},
        url="",
        max_out_len={max_out_len},
        batch_size={batch_size},
        trust_remote_code=False,
        generation_kwargs=dict(
            temperature={temperature!r},
            seed={seed},
            ignore_eos=False,
        ),
        pred_postprocessor=dict(type=extract_non_reasoning_content),
    )
]
'''


def prepare(
    output_dir: Path,
    *,
    host: str,
    port: int,
    served_model: str,
    datasets: Sequence[str],
    work_dir: Path,
    metric: str,
    direction: str,
    max_absolute_regression: float,
    max_relative_regression: float,
    max_out_len: int,
    batch_size: int,
    temperature: float,
    seed: int,
    num_prompts: int | None,
) -> dict[str, Any]:
    _validate_prepare(
        host=host,
        port=port,
        served_model=served_model,
        datasets=datasets,
        metric=metric,
        max_out_len=max_out_len,
        batch_size=batch_size,
        num_prompts=num_prompts,
    )
    if direction not in {"higher", "lower"}:
        raise AisbenchAdapterError("direction must be higher or lower")
    if max_absolute_regression < 0 or max_relative_regression < 0:
        raise AisbenchAdapterError("regression thresholds must be non-negative")
    if temperature != 0:
        raise AisbenchAdapterError("temperature must be 0 for deterministic accuracy runs")

    model_config = output_dir / "configs" / "models" / f"{MODEL_TASK}.py"
    _atomic_write(
        model_config,
        render_model_config(
            host=host,
            port=port,
            served_model=served_model,
            max_out_len=max_out_len,
            batch_size=batch_size,
            temperature=temperature,
            seed=seed,
        ),
    )
    command = [
        "ais_bench",
        "--config-dir",
        str((output_dir / "configs").resolve()),
        "--models",
        MODEL_TASK,
        "--datasets",
        *datasets,
        "--work-dir",
        str(work_dir),
        "--dump-eval-details",
        "--num-warmups",
        "0",
    ]
    if num_prompts is not None:
        command.extend(["--num-prompts", str(num_prompts)])
    cases = {
        "schema_version": SCHEMA_VERSION,
        "cases": [
            {
                "id": f"aisbench-{_slug(dataset)}-{_slug(metric)}",
                "mode": "aisbench",
                "repeats": 1,
                "sampling": {},
                "request": {},
                "comparison": {
                    "metric_rules": {
                        metric: {
                            "direction": direction,
                            "max_absolute_regression": max_absolute_regression,
                            "max_relative_regression": max_relative_regression,
                        }
                    }
                },
                "matrix": {"dataset": dataset, "model_task": MODEL_TASK},
            }
            for dataset in datasets
        ],
    }
    _atomic_write(
        output_dir / "command.json",
        json.dumps({"command": command}, ensure_ascii=False, indent=2) + "\n",
    )
    _atomic_write(
        output_dir / "run.sh",
        "#!/usr/bin/env bash\nset -euo pipefail\n" + shlex.join(command) + "\n",
    )
    _atomic_write(
        output_dir / "aisbench-cases.json",
        json.dumps(cases, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return {
        "status": "prepared",
        "model_config": str(model_config.resolve()),
        "command": command,
        "cases": str((output_dir / "aisbench-cases.json").resolve()),
    }


def validate_execution(execution: Any) -> dict[str, Any]:
    """Require the execution identity the comparator needs to attribute results.

    AISBench does not expose the engine. The CLI derives this block from the
    coordinator-owned service launch and preserves that observation beside
    the metrics. The comparator checks both inputs and recorded runtime facts.
    """
    if not isinstance(execution, Mapping):
        raise AisbenchAdapterError("execution must be a JSON object")
    engine_args = execution.get("engine_args")
    if not isinstance(engine_args, Mapping):
        raise AisbenchAdapterError(
            "execution.engine_args must be an object describing how the "
            "benchmarked service was started (use {} only if no non-default "
            "engine arguments were passed)"
        )
    served_model = execution.get("served_model")
    if not isinstance(served_model, str) or not served_model.strip():
        raise AisbenchAdapterError("execution.served_model must be a non-empty string")
    return dict(execution)


def normalize_summary(
    summary_csv: Path,
    *,
    label: str,
    execution: Mapping[str, Any],
    observation: Mapping[str, Any] | None = None,
    model_column: str = MODEL_ABBR,
) -> dict[str, Any]:
    execution_block = validate_execution(execution)
    try:
        stream = summary_csv.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise AisbenchAdapterError(f"cannot read summary CSV {summary_csv}: {exc}") from exc
    with stream:
        reader = csv.DictReader(stream)
        required = {"dataset", "metric", model_column}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise AisbenchAdapterError(
                f"summary CSV is missing columns: {', '.join(sorted(missing))}"
            )
        cases: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row_number, row in enumerate(reader, start=2):
            dataset = (row.get("dataset") or "").strip()
            metric = (row.get("metric") or "").strip()
            case_id = f"aisbench-{_slug(dataset)}-{_slug(metric)}"
            if case_id in seen:
                raise AisbenchAdapterError(
                    f"{summary_csv}:{row_number}: duplicate dataset/metric row"
                )
            seen.add(case_id)
            raw_value = (row.get(model_column) or "").strip()
            try:
                metric_value = float(raw_value)
            except ValueError:
                cases.append(
                    {
                        "id": case_id,
                        "status": "error",
                        "error": f"non-numeric AISBench metric: {raw_value!r}",
                        "outputs": [],
                        "metrics": {},
                        "source": {
                            "dataset": dataset,
                            "metric": metric,
                            "row": row_number,
                        },
                    }
                )
            else:
                cases.append(
                    {
                        "id": case_id,
                        "status": "ok",
                        "outputs": [],
                        "metrics": {metric: metric_value},
                        "source": {
                            "dataset": dataset,
                            "metric": metric,
                            "row": row_number,
                        },
                    }
                )
    if not cases:
        raise AisbenchAdapterError("summary CSV contains no metric rows")
    return {
        "schema_version": SCHEMA_VERSION,
        "label": label,
        "execution": execution_block,
        **({"observation": dict(observation)} if observation else {}),
        "adapter": {
            "name": "aisbench",
            "summary_csv": str(summary_csv.resolve()),
            "model_column": model_column,
        },
        "cases": cases,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    prepare_parser = subparsers.add_parser("prepare", help="prepare an AISBench run")
    prepare_parser.add_argument("--output-dir", required=True, type=Path)
    prepare_parser.add_argument("--host", required=True)
    prepare_parser.add_argument("--port", required=True, type=int)
    prepare_parser.add_argument("--served-model", required=True)
    prepare_parser.add_argument("--dataset", action="append", required=True)
    prepare_parser.add_argument("--work-dir", required=True, type=Path)
    prepare_parser.add_argument("--metric", default="accuracy")
    prepare_parser.add_argument("--direction", choices=("higher", "lower"), default="higher")
    prepare_parser.add_argument("--max-absolute-regression", type=float, default=0.0)
    prepare_parser.add_argument("--max-relative-regression", type=float, default=0.0)
    prepare_parser.add_argument("--max-out-len", type=int, default=512)
    prepare_parser.add_argument("--batch-size", type=int, default=1)
    prepare_parser.add_argument("--temperature", type=float, default=0.0)
    prepare_parser.add_argument("--seed", type=int, default=1)
    prepare_parser.add_argument("--num-prompts", type=int)

    normalize = subparsers.add_parser("normalize", help="normalize AISBench summary CSV")
    normalize.add_argument("--summary-csv", required=True, type=Path)
    normalize.add_argument("--label", required=True)
    normalize.add_argument("--model-column", default=MODEL_ABBR)
    normalize.add_argument("--context-file")
    normalize.add_argument("--execution-id", help="Owned service execution that produced the metrics")
    normalize.add_argument("--service", default="vllm")
    normalize.add_argument("--output", required=True, type=Path)
    return parser


def _json_object(raw: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AisbenchAdapterError(f"{label} must be valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise AisbenchAdapterError(f"{label} must be a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "prepare":
            emit_progress("prepare-aisbench", output_dir=str(args.output_dir))
            payload = prepare(
                args.output_dir,
                host=args.host,
                port=args.port,
                served_model=args.served_model,
                datasets=args.dataset,
                work_dir=args.work_dir,
                metric=args.metric,
                direction=args.direction,
                max_absolute_regression=args.max_absolute_regression,
                max_relative_regression=args.max_relative_regression,
                max_out_len=args.max_out_len,
                batch_size=args.batch_size,
                temperature=args.temperature,
                seed=args.seed,
                num_prompts=args.num_prompts,
            )
        else:
            emit_progress("normalize-aisbench", summary=str(args.summary_csv))
            from vaws_task_target import task_client
            from vaws_serving_observation import serving_observation
            client = task_client(args.context_file)
            execution_id = client.resolve_execution(args.execution_id, service=None if args.execution_id else args.service)
            if execution_id is None:
                raise AisbenchAdapterError("the native task has no matching service execution")
            observation, execution = serving_observation(client.target(execution_id))
            if execution is None:
                raise AisbenchAdapterError("the service has no recorded vLLM launch configuration")
            normalized = normalize_summary(
                args.summary_csv,
                label=args.label,
                execution=execution,
                observation=observation,
                model_column=args.model_column,
            )
            _atomic_write(
                args.output,
                json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
            )
            payload = {
                "status": "normalized",
                "output": str(args.output.resolve()),
                "case_count": len(normalized["cases"]),
                "error_count": sum(
                    case["status"] == "error" for case in normalized["cases"]
                ),
            }
    except (AisbenchAdapterError, OSError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    if build_parser().parse_args().action == "normalize":
        from vaws_managed_entry import ensure_managed_entry
        ensure_managed_entry(repo_root=ROOT, entry_file=__file__, local_options=("--summary-csv", "--output"))
    raise SystemExit(main())
