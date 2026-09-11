# Workflow usability validation

Status: dated evidence, 2026-09-11 to 2026-09-12

This pass audited all 20 business skills and exercised managed execution on
two available hosts in a four-host Ascend fleet. Two other hosts had observed
workloads and were left available to their existing users. This is lifecycle
and usability evidence, not a four-node inference or performance result.

## Changes and ownership

- Five overlapping descriptions were narrowed: change validation, correctness
  validation, benchmark, profiling analysis and HBM attribution. Ordinary PR
  reading, review and test suggestions can use native tools directly.
- Serving start follows preparation, launch and HTTP/model/first-token checks
  within one wait budget. It reports preparation-step changes; `--no-wait`
  returns an execution reference immediately. Timeout retains that reference.
- Business launch settings are isolated by service name and written only after
  admission succeeds. Rejected changes retain the previous configuration.
- Terminal startup results extract a bounded import/runtime exception from the
  owned log tail. An import failure is not automatically called a version mismatch.
- Coordinator resolves an actual Codex native identity when the local hook has
  not exported its context. Ambiguous identities are rejected and MCP callers
  still supply explicit context. No task identity comes from cwd or chat history.
- Coordinator handles completed preparation failures as terminal failures,
  checks build compatibility before installation and preserves POSIX remote
  paths on Windows. Git long paths are enabled per operation without changing
  user Git configuration.
- Build attestation can use the latest completed installer log when successful
  editable installation removed build caches. Missing or conflicting build
  evidence remains an error. Default remote operation logs stay in local state.
- Managed source materialization skips transport/reset only after checking the
  exact remote snapshots and absence of tracked and nonignored untracked changes
  under its existing lock. Runtime and allocation checks still run.
- Fleet monitoring uses the coordinator inventory by default. The monitor
  package fixes Windows SSH newline handling and closes SQLite reads reliably.

Package changes: [monitor PR 5](https://github.com/vllm-ascend-workspace/vaws-top/pull/5),
[coordinator PR 16](https://github.com/vllm-ascend-workspace/vaws-coordinator/pull/16),
[coordinator PR 17](https://github.com/vllm-ascend-workspace/vaws-coordinator/pull/17),
[coordinator PR 18](https://github.com/vllm-ascend-workspace/vaws-coordinator/pull/18).

## Skill boundary review

The following is a manual semantic review of positive and adjacent requests.
It is not a measured model-routing accuracy result. Automatic skill invocation
remains enabled; no additional mandatory router or confirmation step was added.
The skill catalog and client projections are checked separately for consistency.

| Skill | Example that selects it | Adjacent request and expected path |
|---|---|---|
| repo-init | 初始化工作区和客户端依赖 | 普通 Git pull uses native Git |
| npu-fleet-monitor | 看四机空卡、启动监控 | Service allocation belongs to coordinator |
| modelscope | 下载并校验模型权重 | Starting an installed model uses serving |
| vllm-ascend-serving | 拉一个 TP=2 服务并检查首 token | PD topology uses pd-serving |
| vllm-ascend-pd-serving | 拉起 prefill/decode 分离部署 | One colocated service uses serving |
| vllm-ascend-benchmark | 测服务吞吐、扫并发 | Code A/B regression uses performance-regression |
| vllm-ascend-performance-regression | 比较修改前后 TTFT 是否回退 | A single-state load test uses benchmark |
| vllm-ascend-correctness-validation | 对比 eager/graph 输出 token | Ordinary unit tests use native tools |
| vllm-ascend-change-validation | 汇总这个改动的实验验证报告 | Read/review a PR or suggest tests directly |
| ascend-profiling-collection | 采一份 torch profiler 数据 | Existing traces use profiling-analysis |
| ascend-profiling-analysis | 分析已有 DB 的跨 rank 时间线 | A slow-service complaint alone supplies no trace |
| ascend-memory-profiling | 拆分权重、KV 和 HCCL 显存 | Current usage/idle-card lookup uses fleet observation |
| vllm-ascend-graph-debug | eager 通过但 graph replay 卡死 | Initial correctness comparison uses correctness-validation |
| vllm-ascend-distributed-debug | 多 rank HCCL 初始化挂住 | An isolated operator error uses operator-debug |
| ascend-operator-debug | 一个 ACLNN 算子在特定 dtype 崩溃 | Whole-model graph localization uses graph-debug |
| ascend-tensor-dump | 固定输入复现后定位首个中间张量差异 | Establish deterministic reproduction first |
| ascend-triton-operator-development | 从 PyTorch 写第一个正确 kernel | An already correct kernel uses optimization |
| ascend-triton-kernel-validation | 验证候选 kernel 的 shape/dtype 矩阵 | Implementing a missing kernel uses development |
| ascend-triton-kernel-optimization | 优化已通过正确性的 kernel | Failed correctness returns to development/validation |
| ascend-triton-workflow | 完整交付迁移、验证和优化 | A single scoped stage uses its owning skill |

## Real managed scenarios

Host aliases below omit private infrastructure identifiers. All device commands
used coordinator ownership and existing user containers; fleet observations
were never treated as device reservations.

| Scenario | Observed result |
|---|---|
| Four-host discovery and monitor | All four endpoints observable after the Windows transport fix; hosts A and D had capacity, B and C had workloads |
| Native context without exported context file | Local managed entry resolved this actual native task |
| Host A, single NPU | `arange(8).sum()` returned 28; succeeded, quiet and released |
| Host D, two NPUs | Both device results were 28; succeeded, quiet and released |
| Host A, Qwen3-0.6B, TP=1 eager | Health, model discovery and first token succeeded through one start call |
| Reconnect to the same named eager service | Same execution reference; no second service allocation |
| Change live eager service to TP=2 without restart | Rejected; saved TP=1 configuration retained |
| Unavailable requested machine type, two-second wait | Returned waiting state and same reference; no provisioning started; cancellation released it |
| Initial version-conditional import failure | Original missing-module evidence retained; execution resources released |
| Host A, Qwen3-0.6B, TP=2 graph | Health, model discovery and first token succeeded; owned logs explicitly recorded ACL graph replay |
| Source reuse followed by explicit process failure | Exact clean-source shortcut observed; NPU sum returned 28, process exited with code 7, failed state and resource release confirmed |

All 13 executions created during diagnosis and validation ended in terminal
states with resource release confirmed. Both smoke services were stopped.

The eager and TP=2 graph readiness loops took 132.4 and 118.6 seconds in these
runs. Those are readiness-loop durations, not full cold-start times or
comparable throughput/latency measurements. Graph capture sizes were 1, 2 and 4;
the smoke service used max model length 512 and max sequences 4.

The source checkout was verified as the v0.27.1 release while its installed
snapshot SCM version caused the plugin to select an older import branch.
Successful service runs recorded the plugin's `VLLM_VERSION=0.27.1` setting.
This is a conditional compatibility setting for that verified source, not a
new universal default or fabricated package version.

Host D also exposed a completed native build whose profile lacked SoC metadata.
The fix recovered the actual selection from completed build evidence. Recovery
used the package's profile writer and registry after the failed execution was
stopped; it did not manufacture a ready profile. A later managed dual-device
execution then succeeded. This recovery is not evidence of a fresh unattended
cold install after the fix.

## Verification and limits

Windows local tests covered all 58 discovered suites/files (1,587 JUnit cases,
including subtests). Two failures were a stale generated CLI table and a
missing-context fixture inheriting the newly supported native identity. Both
were corrected and their affected tests rerun. Coordinator changes also had
Windows and Linux package CI and focused source/build-evidence tests. The clean
source probe was tested against real Git/Bash repositories on Windows and Linux,
including tracked modifications and nonignored untracked files.
The affected consumer checks on WSL/Linux passed 127 tests and 39 subtests, with
one platform-specific skip. The Windows rerun covering both corrected fixtures
and per-service settings passed 24 tests; repository guard tests passed 107 tests
and 133 subtests.

A concurrent remote-dev delivery advanced the final dependency combination to
remote-dev 0.6.0 (`9ef9902`) and coordinator 0.3.3 (`b2ee686`), including the
synchronous coordinator adapter. Both platform environments were synchronized.
Affected consumer tests against those installed packages passed 145 tests and
74 subtests on Windows, and 102 tests and 44 subtests on Linux (one skip each).
Doctor passed and the restarted idle daemon reported the same loaded and
installed commits. Earlier model lifecycle evidence above predates this last
dependency update; it is not presented as a fresh model run on the final pins.
A further managed single-NPU run on the final installed combination returned
28, succeeded and released its resources; its source materialization also used
the verified clean-snapshot shortcut.

Raw receipts, logs and native execution references remain under untracked local
state. Public reports contain no private endpoint, container or credential data.
The observed model runs are smoke checks; no accuracy benchmark, sustained load
test, HBM attribution, or multi-host collective test is claimed. User containers
and unrelated workloads were preserved throughout.
