"""Evidence retention under untracked ``.vaws-local/maturation/``.

Layout per run::

    .vaws-local/maturation/runs/<run-id>/
      hosts.json        label -> real endpoint (the only place identities live)
      trials.jsonl      one record per trial, appended as they complete
      failures/<trial-id>.json   full raw payloads for failed trials
      candidates/<candidate-id>.json  prepared knowledge candidates (redacted)
      run.json          full report (identities included; never tracked)
      summary.md        anonymised human summary, safe to paste into a PR
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EVIDENCE_ROOT = REPO_ROOT / ".vaws-local" / "maturation" / "runs"


def atomic_write_json(path: Path, data: Any) -> None:  # noqa: ANN401
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


class EvidenceStore:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "failures").mkdir(exist_ok=True)
        (self.run_dir / "candidates").mkdir(exist_ok=True)
        self._lock = threading.Lock()
        self._trials_path = self.run_dir / "trials.jsonl"

    @property
    def relative_dir(self) -> str:
        try:
            return str(self.run_dir.relative_to(REPO_ROOT))
        except ValueError:
            return str(self.run_dir)

    def write_hosts(self, endpoints: list[Mapping[str, Any]]) -> None:
        atomic_write_json(self.run_dir / "hosts.json", {"schema_version": 1, "endpoints": list(endpoints)})

    def append_trial(self, trial: Mapping[str, Any]) -> None:
        line = json.dumps(trial, ensure_ascii=False, sort_keys=True)
        with self._lock:
            with self._trials_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            if not trial.get("passed"):
                atomic_write_json(self.run_dir / "failures" / f"{trial['trial_id']}.json", trial)

    def write_candidate(self, candidate: Mapping[str, Any]) -> Path:
        path = self.run_dir / "candidates" / f"{candidate['candidate_id']}.json"
        atomic_write_json(path, candidate)
        return path

    def prune_candidates(self, keep_ids: set[str]) -> list[str]:
        """Drop candidate files not produced by the latest finalize.

        A replay re-derives candidates from the trials; files left over from an
        earlier attribution pass would otherwise misrepresent the run.
        """
        removed: list[str] = []
        for path in (self.run_dir / "candidates").glob("*.json"):
            if path.stem not in keep_ids:
                path.unlink()
                removed.append(path.stem)
        return removed

    def write_run(self, report: Mapping[str, Any]) -> Path:
        path = self.run_dir / "run.json"
        atomic_write_json(path, report)
        return path

    def write_summary(self, markdown: str) -> Path:
        path = self.run_dir / "summary.md"
        path.write_text(markdown, encoding="utf-8")
        return path

    def load_trials(self) -> list[dict[str, Any]]:
        if not self._trials_path.exists():
            return []
        trials: list[dict[str, Any]] = []
        for line in self._trials_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                trials.append(json.loads(line))
        return trials
