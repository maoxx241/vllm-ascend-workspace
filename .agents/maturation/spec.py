"""Load and validate the declared operation set (data, not code).

The operations document is YAML with this shape::

    schema_version: 1
    kind: maturation-operations
    classes:
      <class-name>:
        min_repetitions: 20
        pass_rate_threshold: 0.99
        p95_ms: 5000            # optional latency budget
    operations:
      - id: bash.echo
        class: transport
        shape: baseline
        tool: remote.bash
        args: {command: "printf ok-{trial}"}
        expect: {outcome: success, status: ok, stdout_contains: "ok-{trial}"}
        repetitions: 30         # optional, defaults to class.min_repetitions
        timeout_ms: 30000
        endpoint_kinds: [host, container]
        params: {}              # shape-specific knobs
        enabled: true

Templates in ``args``/``expect``/``params`` may reference ``{scratch}``,
``{trial}``, ``{worker}``, ``{label}``, ``{run_id}``.
"""

from __future__ import annotations

import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = 1
DOCUMENT_KIND = "maturation-operations"

SHAPES = frozenset(
    {
        "baseline",
        "long_stream",
        "stream_under_load",
        "concurrent",
        "multi_session",
        "large_transfer",
        "interrupted_transfer",
        "idempotent_rerun",
        "job_registry",
    }
)
SHAPES_WITHOUT_TOOL = frozenset(
    {"large_transfer", "interrupted_transfer", "job_registry", "stream_under_load"}
)
ENDPOINT_KINDS = frozenset({"host", "container"})
VIAS = frozenset({"inprocess", "cli"})
OUTCOMES = frozenset({"success", "needs_input", "blocked", "failed", "timeout", "cancelled"})

DEFAULT_OPERATIONS_PATH = Path(__file__).resolve().parent / "operations.yaml"


class SpecError(ValueError):
    """Raised when the operations document violates the contract."""


@dataclass(frozen=True)
class OperationClass:
    name: str
    min_repetitions: int
    pass_rate_threshold: float
    p95_ms: int | None = None
    description: str = ""


@dataclass(frozen=True)
class Operation:
    id: str
    op_class: str
    shape: str
    tool: str | None
    args: dict[str, Any]
    expect: dict[str, Any]
    repetitions: int
    timeout_ms: int
    endpoint_kinds: tuple[str, ...]
    params: dict[str, Any] = field(default_factory=dict)
    via: str = "inprocess"
    enabled: bool = True
    notes: str = ""

    def applies_to(self, endpoint_kind: str) -> bool:
        return endpoint_kind in self.endpoint_kinds


@dataclass(frozen=True)
class OperationSet:
    classes: dict[str, OperationClass]
    operations: tuple[Operation, ...]
    source: str

    def enabled(self) -> tuple[Operation, ...]:
        return tuple(op for op in self.operations if op.enabled)

    def class_for(self, op: Operation) -> OperationClass:
        return self.classes[op.op_class]


class _SafeFormatter(string.Formatter):
    """str.format that leaves unknown ``{name}`` placeholders untouched.

    Shell snippets legitimately contain braces (``${x}``, ``{a,b}``); only
    declared template names are substituted.
    """

    def __init__(self, values: Mapping[str, Any]) -> None:
        super().__init__()
        self.values = values

    def get_value(self, key: Any, args: Any, kwargs: Any) -> Any:  # noqa: ANN401
        if isinstance(key, str) and key in self.values:
            return self.values[key]
        return "{" + str(key) + "}"


def render(value: Any, context: Mapping[str, Any]) -> Any:  # noqa: ANN401
    """Recursively substitute ``{name}`` templates in strings."""
    if isinstance(value, str):
        if "{" not in value:
            return value
        try:
            return _SafeFormatter(context).vformat(value, (), {})
        except (ValueError, IndexError, KeyError):
            # Unbalanced braces in shell text: leave as-is.
            return value
    if isinstance(value, dict):
        return {key: render(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [render(item, context) for item in value]
    return value


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SpecError(message)


def parse_operation_set(document: Mapping[str, Any], *, source: str = "<memory>") -> OperationSet:
    _require(isinstance(document, Mapping), "operations document root must be a mapping")
    _require(document.get("schema_version") == SCHEMA_VERSION, f"schema_version must be {SCHEMA_VERSION}")
    _require(document.get("kind") == DOCUMENT_KIND, f"kind must be {DOCUMENT_KIND!r}")
    raw_classes = document.get("classes")
    _require(isinstance(raw_classes, Mapping) and raw_classes, "classes must be a non-empty mapping")
    classes: dict[str, OperationClass] = {}
    for name, raw in raw_classes.items():
        _require(isinstance(raw, Mapping), f"class {name!r} must be a mapping")
        min_reps = raw.get("min_repetitions")
        threshold = raw.get("pass_rate_threshold")
        _require(isinstance(min_reps, int) and min_reps >= 1, f"class {name!r}: min_repetitions must be an int >= 1")
        _require(
            isinstance(threshold, (int, float)) and 0 < float(threshold) <= 1,
            f"class {name!r}: pass_rate_threshold must be in (0, 1]",
        )
        p95 = raw.get("p95_ms")
        _require(p95 is None or (isinstance(p95, int) and p95 > 0), f"class {name!r}: p95_ms must be a positive int")
        classes[str(name)] = OperationClass(
            name=str(name),
            min_repetitions=min_reps,
            pass_rate_threshold=float(threshold),
            p95_ms=p95,
            description=str(raw.get("description", "")),
        )

    raw_ops = document.get("operations")
    _require(isinstance(raw_ops, list) and raw_ops, "operations must be a non-empty list")
    operations: list[Operation] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_ops):
        where = f"operations[{index}]"
        _require(isinstance(raw, Mapping), f"{where} must be a mapping")
        op_id = raw.get("id")
        _require(isinstance(op_id, str) and op_id.strip(), f"{where}.id must be a non-empty string")
        _require(op_id not in seen, f"duplicate operation id {op_id!r}")
        seen.add(op_id)
        op_class = raw.get("class")
        _require(op_class in classes, f"{op_id}: class {op_class!r} is not declared")
        shape = raw.get("shape", "baseline")
        _require(shape in SHAPES, f"{op_id}: shape {shape!r} not in {sorted(SHAPES)}")
        tool = raw.get("tool")
        if shape in SHAPES_WITHOUT_TOOL:
            _require(tool is None or isinstance(tool, str), f"{op_id}: tool must be a string when given")
        else:
            _require(isinstance(tool, str) and tool.startswith("remote."), f"{op_id}: tool must be a 'remote.*' name")
        args = raw.get("args", {})
        _require(isinstance(args, Mapping), f"{op_id}: args must be a mapping")
        expect = raw.get("expect", {})
        _require(isinstance(expect, Mapping), f"{op_id}: expect must be a mapping")
        if "outcome" in expect:
            outcomes = expect["outcome"] if isinstance(expect["outcome"], list) else [expect["outcome"]]
            for item in outcomes:
                _require(item in OUTCOMES, f"{op_id}: expect.outcome {item!r} not in {sorted(OUTCOMES)}")
        repetitions = raw.get("repetitions", classes[op_class].min_repetitions)
        _require(isinstance(repetitions, int) and repetitions >= 1, f"{op_id}: repetitions must be an int >= 1")
        timeout_ms = raw.get("timeout_ms", 60000)
        _require(isinstance(timeout_ms, int) and timeout_ms >= 100, f"{op_id}: timeout_ms must be an int >= 100")
        kinds = raw.get("endpoint_kinds", sorted(ENDPOINT_KINDS))
        _require(
            isinstance(kinds, list) and kinds and all(k in ENDPOINT_KINDS for k in kinds),
            f"{op_id}: endpoint_kinds must be a non-empty subset of {sorted(ENDPOINT_KINDS)}",
        )
        params = raw.get("params", {})
        _require(isinstance(params, Mapping), f"{op_id}: params must be a mapping")
        via = raw.get("via", "inprocess")
        _require(via in VIAS, f"{op_id}: via must be one of {sorted(VIAS)}")
        enabled = raw.get("enabled", True)
        _require(isinstance(enabled, bool), f"{op_id}: enabled must be a boolean")
        operations.append(
            Operation(
                id=op_id,
                op_class=str(op_class),
                shape=str(shape),
                tool=tool,
                args=dict(args),
                expect=dict(expect),
                repetitions=repetitions,
                timeout_ms=timeout_ms,
                endpoint_kinds=tuple(kinds),
                params=dict(params),
                via=str(via),
                enabled=enabled,
                notes=str(raw.get("notes", "")),
            )
        )
    return OperationSet(classes=classes, operations=tuple(operations), source=source)


def load_operation_set(path: Path | None = None) -> OperationSet:
    import yaml  # PyYAML is available in this workspace; keep the import local.

    target = path or DEFAULT_OPERATIONS_PATH
    try:
        document = yaml.safe_load(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SpecError(f"cannot read operations file {target}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise SpecError(f"invalid YAML in {target}: {exc}") from exc
    return parse_operation_set(document, source=str(target))
