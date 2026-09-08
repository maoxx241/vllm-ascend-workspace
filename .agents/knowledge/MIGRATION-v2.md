# v1 -> v2 knowledge migration report

- migration date: 2026-09-07
- origin repo: `vllm-ascend-workspace/vllm-ascend-workspace`

The two migrated v2 entries in `known-failure-signatures.v2.yaml` keep
`provenance.origin_repo: maoxx241/vllm-ascend-workspace`. That field is an
identity coordinate: `derived_uuid(origin_repo, kind, slug)` in
`.agents/lib/vaws_knowledge_v2.py` hashes it into the entry UUID. Changing
it would mint a new identity for the same claim. The historical origin is
therefore preserved on those rows; new captures should use the organization
repository.
- redaction profile: declared by `vaws_knowledge.redact`
- migrated entries: 2
- blocked entries: 9

Migrated entries land as `status: unverified`. Every dimension below is an
explicit unresolved marker, not a guess: the entry cannot become `verified`
and cannot be exported until a human supplies the value.

## `model-capabilities`

### `model-qwen3-8b`

Not migrated:

- v2 rule accepts only summary/symptom/root_cause/resolution/avoidance/fingerprints; this document family carries structured payloads that would have to be flattened into prose
- v1 rule.symptom is missing; v2 requires it
- v1 rule.root_cause is missing; v2 requires it
- v1 rule.resolution is missing; v2 requires it
- v1 rule carries structured fields the v2 rule whitelist cannot hold: expected_layers, notes, structure, verified_at, verified_configs

### `model-deepseek-v2-lite`

Not migrated:

- v2 rule accepts only summary/symptom/root_cause/resolution/avoidance/fingerprints; this document family carries structured payloads that would have to be flattened into prose
- v1 rule.symptom is missing; v2 requires it
- v1 rule.root_cause is missing; v2 requires it
- v1 rule.resolution is missing; v2 requires it
- v1 rule carries structured fields the v2 rule whitelist cannot hold: expected_layers, notes, structure, verified_at, verified_configs

### `model-qwen3.5-35b-a3b`

Not migrated:

- v2 rule accepts only summary/symptom/root_cause/resolution/avoidance/fingerprints; this document family carries structured payloads that would have to be flattened into prose
- v1 rule.symptom is missing; v2 requires it
- v1 rule.root_cause is missing; v2 requires it
- v1 rule.resolution is missing; v2 requires it
- v1 rule carries structured fields the v2 rule whitelist cannot hold: attention_mode_disambiguation, expected_layers, notes, structure, verified_at, verified_configs

### `model-qwen3-vl-30b-a3b`

Not migrated:

- v2 rule accepts only summary/symptom/root_cause/resolution/avoidance/fingerprints; this document family carries structured payloads that would have to be flattened into prose
- v1 rule.symptom is missing; v2 requires it
- v1 rule.root_cause is missing; v2 requires it
- v1 rule.resolution is missing; v2 requires it
- v1 rule carries structured fields the v2 rule whitelist cannot hold: expected_layers, notes, structure, verified_at, verified_configs

### `model-deepseek-v4-flash`

Not migrated:

- v2 rule accepts only summary/symptom/root_cause/resolution/avoidance/fingerprints; this document family carries structured payloads that would have to be flattened into prose
- v1 rule.symptom is missing; v2 requires it
- v1 rule.root_cause is missing; v2 requires it
- v1 rule.resolution is missing; v2 requires it
- v1 rule carries structured fields the v2 rule whitelist cannot hold: expected_layers, notes, structure, verified_at, verified_configs

### `model-deepseek-v3.1`

Not migrated:

- v2 rule accepts only summary/symptom/root_cause/resolution/avoidance/fingerprints; this document family carries structured payloads that would have to be flattened into prose
- v1 rule.symptom is missing; v2 requires it
- v1 rule.root_cause is missing; v2 requires it
- v1 rule.resolution is missing; v2 requires it
- v1 rule carries structured fields the v2 rule whitelist cannot hold: expected_layers, notes, structure, verified_at, verified_configs

### `model-deepseek-v4-pro`

Not migrated:

- v2 rule accepts only summary/symptom/root_cause/resolution/avoidance/fingerprints; this document family carries structured payloads that would have to be flattened into prose
- v1 rule.symptom is missing; v2 requires it
- v1 rule.root_cause is missing; v2 requires it
- v1 rule.resolution is missing; v2 requires it
- v1 rule carries structured fields the v2 rule whitelist cannot hold: expected_layers, notes, structure, verified_at, verified_configs

### `model-glm-5`

Not migrated:

- v2 rule accepts only summary/symptom/root_cause/resolution/avoidance/fingerprints; this document family carries structured payloads that would have to be flattened into prose
- v1 rule.symptom is missing; v2 requires it
- v1 rule.root_cause is missing; v2 requires it
- v1 rule.resolution is missing; v2 requires it
- v1 rule carries structured fields the v2 rule whitelist cannot hold: expected_layers, notes, structure, verified_at, verified_configs

### `model-kimi-k3`

Not migrated:

- v2 rule accepts only summary/symptom/root_cause/resolution/avoidance/fingerprints; this document family carries structured payloads that would have to be flattened into prose
- v1 rule.summary is missing; v2 requires it
- v1 rule.symptom is missing; v2 requires it
- v1 rule.root_cause is missing; v2 requires it
- v1 rule.resolution is missing; v2 requires it
- v1 rule carries structured fields the v2 rule whitelist cannot hold: attention_mode_disambiguation, expected_layers, layer_pattern, structure, upcoming_attention_operator, verified_configs

## `known-failure-signatures`

Output: `.agents/knowledge/known-failure-signatures.v2.yaml`

### `gloo-init-container-hostname-missing-from-etc-hosts`

- migrated as: `gloo-init-container-hostname-missing-from-etc-hosts` (status `unverified`)
- removed at migration: v1 applicable_versions carries a redaction placeholder; the removed value is not recoverable from the v1 document and is not reproduced here
- note: v1 text claims verification on 2026-09-02; not written to verification.last_verified_at because v2 requires a complete verified_against environment alongside it
- note: v1 'source' prose has no v2 field; the v1 document remains readable for it
- needs human input:

  - `cann` — the CANN version of the container the fix was verified on
  - `driver` — the NPU driver / firmware version of the verification host
  - `python_abi` — the Python ABI tag of the verification container
  - `torch` — the torch version installed at verification time
  - `torch_npu` — the torch_npu version installed at verification time
  - `vllm` — the vllm version (or commit) used at verification time
  - `vllm_ascend` — the vllm-ascend version (or commit) used at verification time
  - `model` — the model(s) this claim was established on, or an examined independence basis
  - `execution_mode` — the execution mode(s) this claim was established on

### `ssh-stream-over-shared-controlmaster-mux-dies-early`

- migrated as: `ssh-stream-over-shared-controlmaster-mux-dies-early` (status `unverified`)
- note: v1 text claims verification on 2026-09-02; not written to verification.last_verified_at because v2 requires a complete verified_against environment alongside it
- note: v1 'source' prose has no v2 field; the v1 document remains readable for it
- needs human input:

  - `soc` — the exact SoC the fix was observed on (e.g. Ascend910_93)
  - `cann` — the CANN version of the container the fix was verified on
  - `driver` — the NPU driver / firmware version of the verification host
  - `python_abi` — the Python ABI tag of the verification container
  - `torch` — the torch version installed at verification time
  - `torch_npu` — the torch_npu version installed at verification time
  - `vllm` — the vllm version (or commit) used at verification time
  - `vllm_ascend` — the vllm-ascend version (or commit) used at verification time
  - `model` — the model(s) this claim was established on, or an examined independence basis
  - `topology` — the parallel topologies this claim was established on
  - `execution_mode` — the execution mode(s) this claim was established on
