# 审计判决：validation / debug 家族

Status: dated 2026-09-07 at 161fed1 — historical evidence

本文档是 2026-09-07 对脚手架提交
`161fed1b0fe6b48359be3f0cf33bb7d8befae113`（`161fed1`）的日期快照记录，
不是当前操作指南。文中所有陈述与 `file:line` 引用都描述该树。这是只读审计：
当时没有在远端 Ascend 主机上执行命令，本跟进也不补做真机验证。文中建议的
命令、整合形状与硬件矩阵是该快照上的设计意见，不是现已可调用的新命令。
之后的改动必须用各自的验收证据判断。摘要里“今天没有任何一处机械地建立
可比性”以及文中的复现路径，都只描述 `161fed1` 当时的树；它们既不否定后来
的 #92/#95 证据，也不把那些工作说成不存在或已由本文验收。

审计对象（5 个 skill / 7 个入口 / 16 个动词 / 4,924 行 Python）：

| Skill | 入口 | 脚本行数 | 测试行数 |
|---|---|---:|---:|
| `vllm-ascend-correctness-validation` | 3 | 1,363 | 291 |
| `vllm-ascend-change-validation` | 1 | 616 | 172 |
| `vllm-ascend-performance-regression` | 1 | 670 | 260 |
| `vllm-ascend-graph-debug` | 1 | 646 | 160 |
| `vllm-ascend-distributed-debug` | 1 | 576 | 170 |
| 合计 | **7** | **3,871** | **1,053** |

审计方法：只读代码与文档。本文档不改动任何代码、不重命名、不删除。所有判断
都给出 `file:line` 证据。本次审计没有在远端 Ascend 主机上执行任何命令。

对照基准：`AGENTS.md`、`.agents/README.md`、
`.agents/knowledge/known-failure-signatures.yaml`、
`.agents/lib/vaws_run_manifest.py`、
`.agents/schemas/run-manifest-v1.schema.json`、
`.agents/coordinator/README.md`，以及分支
`feat/multinode-and-experiment-ledger` 上的 experiment-ledger 设计与它的两条
commit message。

---

## 0. 结论摘要

1. **这五个 skill 今天没有任何一处机械地建立可比性。** 最接近的是
   `performance_regression.py` 的 `config_hash` 顺序门禁，但它比对的是同一份
   operator 手写声明的哈希——同一份文档与自己相等是恒真的，结构上不可能发现
   confounder。
2. **本仓库确实有一处机械建立可比性的实现，但它不在这五个 skill 里。**
   `.agents/skills/vllm-ascend-benchmark/scripts/bench_compare.py:634-685` 是
   fail-closed 的 native-input digest 门禁：两个源码状态的
   `csrc/cmake/requirements` 摘要不一致时拒绝对比，而不是照样出数。这正是
   experiment-ledger 想要的形状，而 performance-regression 完全没有消费它。
3. **有三条路径可以在没有任何可复核证据的情况下报告 `passed`**，其中
   `graph_debug_case.py finalize` 最严重：`init` + `finalize` 两条命令、零个
   实验记录、零个对比产物，就能得到一份 `status: passed` 的 Run Manifest，并
   被 `change_validation.py link` 收进 PR 报告。
4. **两种相反的错误都真实存在且分量相当。** 写死了本应留白的判断：14 条基于
   文件名正则的证据清单、缺失指标被判为 regression、无规则时任意准确率变化
   都算通过、结案被折算成布尔表达式。把机制留给手工拼装：每 rank 证据采集完
   全没有生产者、80 行图内打点模板要手工移植进模型代码、两态 harness 配置由
   人手写、交替调度靠人自觉执行并手贴 `--state` 标签。
5. **可比性 / 解释这条缝在五个 skill 里都划错了位置**：可比性被假设，解释被
   自动化。这恰好是最坏的组合——带着证据附件的、自信的、错误的结论。
6. **最该先钉住的机制：两态实验的可比性凭证（comparability receipt）。** 它
   同时修掉第 3 节的全部空洞、堵住第 5 节的两条无证据 `passed` 路径，并且是
   唯一一个"不修则其余整合只会更高效地输出错误结论"的机制。

---

## 1. 入口清单

七个入口没有任何一个被其他脚本以编程方式调用（`rg --hidden` 全仓检索：命中仅
限自身 SKILL.md、references、tests 与 `.agents/README.md` 的清单）。全部由
agent 按 SKILL.md 手工调用。但它们各自有 grep 在调用点看不见的依赖，列在下方
"隐式依赖"里——这些依赖是审计的重点，因为契约不匹配时没有任何地方会报错。

### 1.1 `vllm-ascend-correctness-validation/scripts/correctness_run.py`（686 行）

| 动词 | 参数 | 做什么 |
|---|---|---|
| `init` | `--run-dir --run-id --cases --baseline-label --candidate-label --workspace-snapshot --environment --model --topology --baseline-command(append) --candidate-command(append)` | 校验 cases 文档（`:101-143`），建 run 目录，写 `cases.json`/`environment.json`/`run.json`/`reproduction.sh`，创建 Run Manifest v1 |
| `compare` | `--run-dir --baseline --candidate` | 读两份归一化结果文件，逐 case 分类，写 `comparison.json`/`report.md`，把 manifest 推到终态（`:559-605`） |

四个身份参数 `--workspace-snapshot/--environment/--model/--topology` 默认值都是
`"{}"`（`:627-630`），即允许创建一个身份完全为空的 correctness 运行。

隐式依赖：`.agents/lib/vaws_run_manifest`（`:24-31`）；`--cases` 文档的
schema 由本脚本定义，而实际执行读的是另一份文档（见 1.2）；`aisbench` 模式的
结果由 1.3 生产。

### 1.2 `.../scripts/remote_correctness_harness.py`（290 行）

单动词（无子命令），参数 `--config --output`。在远端容器内构造 `LLM` 或调用
`/v1/chat/completions`，把输出归一化成结果契约（`:250-254`）。

关键事实：它与 1.1 **没有任何代码连接**。1.1 从不调用它，它也不读 1.1 写的
`cases.json`——它读自己 `--config` 里内嵌的 `cases` 数组（`:57-59`），并且
`load_config`（`:49-60`）**不校验** `temperature=0` 与 `seed`，而 1.1 校验
（`:126-136`）。`engine_args`（`:144-148`）决定 eager/graph、TP、特性开关，
既不进结果文件也不进任何产物。

隐式依赖：容器内的 `vllm` 包（`:135-148`）；`_prioritize_workspace_python_packages`
（`:23-42`）依赖 workspace 布局 `parents[4]/vllm`。

### 1.3 `.../scripts/aisbench_adapter.py`（387 行）

| 动词 | 参数 | 做什么 |
|---|---|---|
| `prepare` | `--output-dir --host --port --served-model --dataset(append) --work-dir --metric(默认 accuracy) --direction(默认 higher) --max-absolute-regression(默认 0.0) --max-relative-regression(默认 0.0) --max-out-len(512) --batch-size(1) --temperature(0.0) --seed(1) --num-prompts` | 渲染 AISBench 模型配置 py、`command.json`、`run.sh`，并生成**第二份 cases 文档** `aisbench-cases.json`（`:191-225`） |
| `normalize` | `--summary-csv --label --model-column --output` | 把 AISBench summary CSV 转成结果契约（`:234-306`） |

隐式依赖：AISBench 的 `VLLMCustomAPIChat` API 形状被硬编码在渲染模板里
（`:85-124`）；`--host/--port` 要求调用方自己解析服务端点，与
`vllm-ascend-serving` 的端点解析职责重叠。

### 1.4 `vllm-ascend-change-validation/scripts/change_validation.py`（616 行）

| 动词 | 参数 | 做什么 |
|---|---|---|
| `plan` | `--output-dir --run-id --baseline --candidate(默认 WORKTREE) --goal --target-repository(append) --diff-file │ --repo-root --knowledge(默认 references/validation-rules.yaml)` | 采集或读入 unified diff，按正则匹配规则生成 `impact-analysis.json`/`validation-plan.json`，建父 manifest |
| `link` | `--output-dir --run-manifest --covers(append)` | 把下游 manifest 记入 `linked-runs.json` |
| `finalize` | `--output-dir` | 评估必需项覆盖与子运行状态，出 `pr-validation-report.md` |

隐式依赖（grep 在调用点看不见）：**它 shell-out 调用 `git`**
（`_run_git`，`:86-98`；`collect_git_diff`，`:101-130`），因此它是子模块 Git
状态的实际消费者；`.agents/lib/vaws_knowledge.load_knowledge_file`（`:25`）；
以及最重要的——`link` 消费其余四个 skill 的 manifest 状态语义
（`:448-466`），这条跨 skill 契约没有任何测试跨越两个 skill 去验证。

### 1.5 `vllm-ascend-performance-regression/scripts/performance_regression.py`（670 行）

| 动词 | 参数 | 做什么 |
|---|---|---|
| `plan` | `--output-dir --config` | 校验实验配置，生成交替调度（`:202-229`），写 `parity-check.json`，建 manifest |
| `normalize` | `--result --output --state --phase --ordinal --config-hash --metric-map(append)` | 把一份 Benchmark 结果转成测量契约（`:108-151`） |
| `record` | `--output-dir --result` | 只接受与调度下一条 pending 完全一致的 state/phase/ordinal/hash（`:342-396`） |
| `analyze` | `--output-dir` | 排除 warmup，算 mean/stdev/CV/离群点/相对变化/阈值判决（`:445-527`） |

隐式依赖：`vllm-ascend-benchmark` 的输出契约被硬编码为
`DEFAULT_BENCHMARK_METRICS`（`:35-41`）。这是本家族里最典型的"grep 看不见的
依赖"，而且**它是错的**：见 §6.7。

### 1.6 `vllm-ascend-graph-debug/scripts/graph_debug_case.py`（646 行）

| 动词 | 参数 | 做什么 |
|---|---|---|
| `init` | `--case-dir --case-id --stage(compile│capture│replay│accuracy│unknown) --eager-result --graph-result --reproduction --environment --workspace-snapshot --model --topology` | 建 `case.json` 与 manifest（`:130-181`） |
| `record` | `--case-dir --variable --hypothesis --expected --observed --conclusion --next-step` | 追加一条受控实验记录（`:196-232`） |
| `compare` | `--case-dir --eager --graph --atol --rtol` | 按 `step/layer/rank/tag` 对齐两份 JSONL，找首个分叉（`:377-432`） |
| `finalize` | `--case-dir --root-cause --fix --minimal-result --original-result --cleanup-status` | 折算 `resolved` 并推 manifest 终态（`:477-527`） |

隐式依赖：`compare` 消费的 JSONL 由 `SKILL.md:105-183` 的**文档模板**生产——
生产者是一段要手工移植进模型代码的示例，不是代码。

### 1.7 `vllm-ascend-distributed-debug/scripts/distributed_debug.py`（576 行）

| 动词 | 参数 | 做什么 |
|---|---|---|
| `init` | `--output-dir --config` | 校验拓扑契约（`:110-198`），建证据目录布局与 manifest |
| `ingest` | `--output-dir --events` | 校验并追加归一化 rank 事件（`:289-315`） |
| `analyze` | `--output-dir` | 出结构性 findings、每 rank last progress、报告（`:329-542`） |

隐式依赖：`events.jsonl` **没有任何生产者**。schema 校验很严
（`validate_event`，`:201-225`），但没有一行代码去采集它。

---

## 2. 逐能力分类

标注含义：**[D]** 确定性机制、**[J]** 判断、**[M]** 混合（需划缝）、
**[R]** 冗余。

### 2.1 确定性机制 [D]

| 能力 | 证据 | 应归属的整合命令 | 可信所需的真实硬件验证 |
|---|---|---|---|
| 归一化输出对比与分类优先级 | `correctness_run.py:332-487` | `experiment compare` | 在真机上以 `temperature=0`+seed 重复 3 次，验证 `exact_match` 真的稳定；再以 `temperature>0` 验证 `flaky_or_nondeterministic` 真的触发。两者都是今天没有被真实运行证实的假设 |
| AISBench summary CSV → 结果契约 | `aisbench_adapter.py:234-306` | 同上（作为 importer） | 用一次真实 AISBench 运行的 CSV 验证列名/`model_column` 与实际产物一致 |
| 交替调度生成与顺序门禁 | `performance_regression.py:202-229, 342-396` | `experiment schedule` / `record` | 已是本家族最扎实的机制。真机验证：跑一次 warmup+3 的完整 ABBA，确认服务重启与 parity 步骤不会破坏顺序门禁 |
| 统计量与离群点 | `:399-442` | `experiment analyze` | 用同一状态跑 6 次得到真实 run-to-run 分布，据此校准 `max_cv` 与 MAD 阈值（今天两者都是常量猜测） |
| Benchmark → measurement 指标映射 | `:108-151` + `:35-41` | 同上 | 必须用真实 `bench_run.py` 产物做契约测试（今天 5 个默认映射里 2 个不可能命中，见 §6.7） |
| diff 解析与 `sha256` | `change_validation.py:101-130, 133-163` | `evidence plan` | 跨仓（脚手架 + 两个子模块）合并 diff 的真实用例 |
| 计划项覆盖聚合 | `:479-542` | `evidence finalize` | 一次真实 PR 的端到端链接 |
| eager/graph snapshot key 对齐与首个分叉 | `graph_debug_case.py:377-432` | `experiment compare`（同一比较器的 tensor 分支） | 用一次真实 graph/eager 打点验证 key 完整性与 `missing-in-graph` 语义 |
| 拓扑契约校验 | `distributed_debug.py:110-198` | `topology check` | 在 A3 上验证逻辑设备数（1 卡 2 die）与 `tp*dp*pp*ep` 的换算，这是 ledger 分支第二条 commit 记录的真实陷阱，今天完全缺失 |
| collective 事件配对与参与者比对 | `:329-412` | `rank evidence analyze` | 一次真实 TP≥2 的挂起 case |
| **每 rank 证据采集与归一化** | **不存在**（消费者在 `:201-225`，生产者无） | `rank evidence collect` | 一次真实多 rank 挂起：拉全 rank 日志 + 栈、时间戳对齐、collective 序号打点 |
| **图内预分配 buffer 打点 / 图外 flush** | 仅文档 `SKILL.md:105-183` | `graph snapshot instrument` | 一次真实 capture+replay：验证图内只做 `copy_`、图外 `synchronize` 不破坏 capture |
| **确定性开关设置** | 仅散文 `SKILL.md:40, 200` | 上述命令的前置步骤 | 真机确认 `HCCL_DETERMINISTIC` + `use_deterministic_algorithms` 组合确实消除 run-to-run 抖动 |
| **两态 harness 配置派生（只差一个变量）** | 不存在 | `experiment plan` | 见 §3 |

### 2.2 判断 [J]

| 能力 | 现状证据 | 应替换成的指引与上下文 |
|---|---|---|
| 一个 diff 需要什么证据 | 被写成 14 条文件名正则：`references/validation-rules.yaml:1-249`；匹配逻辑 `change_validation.py:177, 186-199` | 保留规则文件，但降级为**候选清单与提问单**，不再作为 required 的产生者。指引应回答"怎么读这个 diff"：改的是哪一层（前端/调度/runner/backend/算子/构建）、是否改变了 replay 期读取的地址或 metadata、是否改变了跨 rank 状态更新顺序、是否只在某些 shape 下生效。外加把 `.agents/knowledge/` 的失败签名作为"这类改动历史上怎么坏的"上下文 |
| 差异是良性数值差异还是功能性错误 | `graph-debug/SKILL.md:74-80` 的判据表——**形状是对的，应作为范本** | 保持为指引。同时把 `correctness_run.py:508-516` 的自动 failed 降级为"分类 + 待判断"（见 §4.A） |
| 观测到的差异是不是真回归 | 阈值由 config 声明（`performance_regression.py:180-194`），这一半是对的 | 指引应补：如何在本机噪声下选 `max_cv`、几次重复才有意义、什么时候该先修噪声而不是判回归 |
| 分布式挂起的根因与下一步降维 | `distributed-debug/SKILL.md:20-23` 是指引（对），但 `distributed_debug.py:455-457` 的 `diagnosed` 越界（见 §4.A） | 保留降维顺序指引；把脚本输出限制为"不变量被违反"的事实陈述 |
| 阶段定性（compile/capture/replay/accuracy） | operator 通过 `--stage` 提供（`graph_debug_case.py:537`）——**留白正确** | 保持。但需要机制去支撑它（见 §6.3） |
| 结案：根因与修复是否成立 | `graph_debug_case.py:496-500` 折算成布尔（错） | 结案应产出"证据 + 声明"，`passed` 由复核者给，不由三个字符串给 |

### 2.3 混合 [M]：可比性 / 解释这条缝在每个 skill 里的位置

这是本次审计要求定位的关键缝。**五个里没有一个划在正确位置**：

| Skill | 缝**应该**在哪 | 缝**实际**在哪 | 证据 |
|---|---|---|---|
| correctness-validation | "两份结果描述同一实验、只差被测变量"（机械）↔ "token 分歧意味着回归"（判断） | 可比性完全被假设，解释被完全自动化 | 机械侧缺失：`:559-568` 接受任意两个路径；判断侧越界：`:508-516` 直接给 `passed/failed` 并写进 manifest `:603` |
| change-validation | "diff 内容与身份是确定的"（机械，**已有**）↔ "它需要什么证据"（判断） | 机械侧只做到 diff 哈希；判断侧被正则接管 | 机械：`:156` 记 `sha256`；越界：`:186-199` + `validation-rules.yaml` |
| performance-regression | "两态只差代码"（机械）↔ "这个差值是不是回归"（判断，阈值应由人声明） | 顺序与哈希是机械的（对），但哈希对象是声明而非观测；判决自动化（可接受，前提是质量门禁成立） | 机械：`:352-367`；空洞：`:89-93` + `:171-173`；`parity-check.json` 无条件 passed：`:267-273` |
| graph-debug | "两份快照来自同一最小复现的 eager/graph 两次运行"（机械）↔ "首个分叉说明什么"（判断） | key 对齐是真机械（本家族最好），但两份文件的来源不在记录里；结案判断被自动化 | 机械：`:377-432`；空洞：`:235-247` 的 key 只有 `step/layer/rank/tag`，无 run 身份；越界：`:496-500, 524` |
| distributed-debug | "结构性不变量被违反"（机械）↔ "这就是根因"（判断） | 机械侧做得不错，但 `diagnosed` 与 manifest `failed` 把结论替读者做了 | 机械：`:367-412`；越界：`:455-457, 533` |

### 2.4 冗余 [R]

| # | 冗余能力 | 已有实现 | 说明 |
|---|---|---|---|
| R1 | **完整的 baseline-vs-candidate 性能对比执行器** | `vllm-ascend-benchmark/scripts/bench_compare.py:2, 358, 478, 687-701` | 它按 `--state LABEL=REF` 对齐源码、跑多 case、聚合（`:205` 支持 warmup 排除）、比对（`:492-506`）。与 `vllm-ascend-performance-regression` 的职责几乎完全重叠。两者各有对方缺的一半：`bench_compare` **会执行但不交替**（`:687-701` 顺序跑完 A 再跑 B），`performance_regression` **会交替但不执行**（`SKILL.md:16-17` 让读者手工按调度跑）。这是整合的首要目标 |
| R2 | **可比性门禁** | `bench_compare.py:634-685` | fail-closed 的 native-input digest 门禁 + 固定数据集 sha256（`:604-632`）。performance-regression 的 `parity-check.json`（`:267-273`）是同一职责的空壳版本 |
| R3 | **回归阈值评估器** | `correctness_run.py:292-329`（direction + absolute + relative）与 `performance_regression.py:465-509`（direction + relative） | 两套语义不同的阈值实现，一套支持绝对回归一套不支持；同一个概念在两个 skill 里给出不同答案 |
| R4 | **浮点容差比较与嵌套数值展平** | `correctness_run.py:247-266` 与 `graph_debug_case.py:274-292` | 逻辑等价的两份实现（`_flatten_numbers` / `_close` vs `_numbers_close`），只是字段名从 `numerics` 变成 `stats`/`sample` |
| R5 | **原子写、`utc_now`、`emit_progress`、`_load_json`** | 五个脚本各一份（如 `correctness_run.py:68-98`、`performance_regression.py:56-86`、`distributed_debug.py:56-86`、`graph_debug_case.py:50-66`、`change_validation.py:53-83`） | 纯机制在文件之间被手工复制，而 `.agents/lib/` 就是它们该在的地方 |
| R6 | **第二份 cases 文档生成器** | `aisbench_adapter.py:191-225` 生成 `aisbench-cases.json`，而 `correctness_run.py init --cases` 期待人手写的 `cases.json` | 同一份 schema 两个入口生产，且没有合并路径 |

---

## 3. 可比性（最重要的发现）

问题：可比性是在比较发生**之前**被机械建立的，还是被假设的？

### 3.1 逐 skill 判定

| Skill | 比较什么 | 可比性 | 判定 |
|---|---|---|---|
| correctness-validation | 两份归一化结果 | 完全假设 | **无门禁** |
| performance-regression | 两组测量 | 声明的哈希相等 + 顺序 | **声明式，非观测式** |
| graph-debug | 两份 JSONL 快照 | key 集合对齐（真机械），来源不校验 | **对齐是真的，配对不是** |
| change-validation | 聚合子运行 | 仅 `parent_run_id` 兼容性 | **等价于无门禁**（见 3.2 C10） |
| distributed-debug | case 之间的降维对比 | 无（case 目录互不关联） | **无门禁** |

### 3.2 两次运行可以在被测变量之外相差而无人察觉的具体位置

- **C1｜correctness：可以把同一个文件当两态对比。**
  `compare --baseline X --candidate Y`（`:559-568, 635-637`）接受任意两个路径。
  结果文件里的 `label`（`remote_correctness_harness.py:250-254`）既不与
  `run.json` 的 `baseline_label`/`candidate_label` 比对，也不互相比对。传同一
  个文件两次得到 `exact_match` → `passed`。
- **C2｜correctness：eager vs graph 会被报告成"代码回归"。**
  `engine_args`（`remote_correctness_harness.py:144-148`）决定 `enforce_eager`、
  TP、KV、特性开关，它既不进结果文件（`:250-254`）、不进 run 目录、也不进
  manifest。baseline 用 `enforce_eager=true`、candidate 用 `false` 跑出的
  `token_divergence`，会被 `:508-516` 判成 `failed`，并被 change-validation
  当作代码问题写进 PR 报告。**这条正是"两次实验差两个变量所以不可归因"的教科
  书案例，而本家族的核心比较器结构上看不见它。**
- **C3｜correctness：结构上无法表达两态各自的身份。**
  `init` 只接受一份 `--environment/--model/--topology`（`:203, 229-232`），
  并且默认 `"{}"`（`:627-630`）。baseline 跑在一台机器、candidate 跑在另一台，
  产物里没有任何字段能记录这件事。
- **C4｜correctness：采样参数在两份文档里各说各话。**
  `temperature=0` 与 `seed` 只在 `cases.json` 上被校验
  （`correctness_run.py:126-136`），而真正执行时读的是 harness config 的
  `sampling`（`remote_correctness_harness.py:189, 212`），`load_config`
  （`:49-60`）不做这项校验。实际以 `temperature=0.7` 跑完、再拿声明
  `temperature=0` 的 cases 去对比，没有任何地方会报错。
- **C5｜perf：`shared` 不要求任何具体键。**
  `validate_config` 只要求 `shared` 是非空对象（`:171-173`），`config_hash`
  是它的规范化 SHA256（`:89-93, 246`）。因此 `{"note":"same"}` 就足以产出一个
  两态"通过"的 parity 凭证，而并发度、请求速率、DP 度、权重哈希、卡数一个都
  不必写。ledger commit 记录的第三个陷阱（并发受限 runner 的 per-item latency
  不能当性能信号）正好落在这里：`ttft/tpot/itl` 默认就在阈值集合里
  （`:35-41`），而 `max_concurrency` 不是必填。
- **C6｜perf：`parity-check.json` 是一份自称通过的空壳。**
  `plan` 时无条件写 `"status": "passed"`（`:267-273`）并登记为 manifest
  artifact（`:298`）。它没有检查任何东西，但它的文件名和 `status` 字段在复核
  时最容易被当成"parity 已验证"。
- **C7｜perf：state 标签是人贴的。**
  `normalize --state baseline`（`:616-619`）由调用者断言，`record` 校验的是
  标签是否与调度顺序一致（`:362-367`），不是测量是否真的来自那个状态。把
  candidate 的结果贴成 baseline 会顺利通过全部门禁。
- **C8｜perf：原始产物不落盘、不哈希。**
  `source` 只是一个路径字符串（`:148, 376`）。对比之下 correctness 会
  `shutil.copy2` 原始输出到 `raw_outputs/`
  （`correctness_run.py:576-577`），perf 的 manifest 可
  以是 `passed` 而它引用的每一份 benchmark 原始 JSON 都已被覆盖或删除——而
  `references/acceptance.md:14` 明确要求"每个结果链接到它的原始 Benchmark
  产物"。
- **C9｜graph：可以拿不同 prompt/seed/commit 的两次运行对拍。**
  `compare --eager A --graph B`（`:557-559`）接受任意两份 JSONL；记录 key 只
  有 `step/layer/rank/tag`（`:35, 235-247`），没有 run/代码/环境标识。对齐机制
  是真的，被对齐的两个东西是否构成一对，无人负责。
- **C10｜change-validation：父子链接检查恒真。**
  `link` 只检查 `child.get("parent_run_id") not in {None, parent["run_id"]}`
  （`:450-453`）。而 correctness/perf/graph/distributed 全部通过
  `new_manifest` 创建 manifest 且从不传 `parent_run_id`
  （`vaws_run_manifest.py:64-95` 默认 `None`），所以这条检查**永远通过**。子
  运行的 `workspace_snapshot` 与计划的 `baseline`/`candidate`/`diff_sha256`
  （`:402-406`）从不比对：一份 PR 报告可以合法聚合来自完全另一个代码状态的
  证据。
- **C11｜distributed：降维 case 之间没有链接。**
  `SKILL.md:22` 要求"每次只降一个并行维度、每个降维 case 单独记录"，但 case
  config（`:110-198`）没有 `derived_from`/`parent_case` 字段，也没有"相对上一
  个 case 改了哪个维度"的记录。降维序列的正确性完全靠人的记忆。

### 3.3 该建立什么

一条命令，从**两次实际运行各自观测到的**身份（代码快照 + 环境版本 + 模型与权
重哈希 + 拓扑与逻辑设备数 + 启动参数 + 采样参数 + 并发/请求速率 + native build
digest）做逐键 diff；被测变量之外的每一处差异记为 confounder；一个 confounder
即 `not-comparable`，且这是硬判决而不是警告。correctness / perf / graph 的比较
入口必须先消费这份凭证才允许输出 `passed`/`failed`。

`bench_compare.py:634-685` 已经证明这种门禁可以 fail-closed 地做出来，并且它
连"允许带警告继续"的逃生门（`--allow-stale-native`）都设计好了——那正是
`not-comparable` 应有的表达方式。

---

## 4. 两种相反的错误

### 4.A 写死了本应留白的判断

- **A1｜用文件名正则代替读 diff。** `references/validation-rules.yaml` 的 14
  条规则全部靠路径正则触发。`(?i)(custom_op|ops/|kernel)`（`:32-34`）会让任何
  路径里含 `kernel` 的改动——包括一篇 `docs/kernel-notes.md`——都要求
  `operator:dtype-shape-layout` 与 `correctness:model-smoke`；
  `(?i)(attention|attn_backend)`（`:86-89`）、`(?i)(worker|executor)`
  （`:146-148`）同理。更糟的是匹配对象是把**所有**改动路径拼成的单一 haystack
  （`change_validation.py:177, 190-194`），所以规则只能回答"这次改动里有没有任
  何一个文件像 X"，无法回答"哪个文件"，也无法给出行级理由。而
  `SKILL.md:14` 把纠正误报的成本推给读者。
- **A2｜"需要人判断"被写死成"永远不能通过"。**
  `manual_review_required` 一旦置位（`:240, 263`），`finalize` 永久返回
  `inconclusive`（`:501-504`），而没有任何入口可以记录"人已经审过并接受了这个
  分类"。
- **A3｜缺测量被判成产品回归。** `_compare_metrics` 对两侧都不存在的指标
  追加 `{"metric": name, "reason": "missing"}`（`:302-304`），它进入
  `metric_regressions` → `task_metric_regression`（`:370-375`）→ 运行状态
  `failed`（`:513-514`）。而 `remote_correctness_harness.py` 永远写
  `"metrics": {}`（`:246-248`），所以任何为非 aisbench case 声明了
  `metric_rules` 的用法都会稳定地把"我们没量到"输出成"它变差了"。
- **A4｜无规则时任意准确率变化都算通过。** aisbench 分支只要没触发显式
  `metric_rule`，任何指标变化都归为 `numerical_difference_within_tolerance`
  （`:376-389`），而它在 `PASS_CLASSES` 里（`:37`）。默认配置下准确率从 0.75
  掉到 0.05 也是 `passed`。
- **A5｜同一个脚本在两端各写死一个极端。** 非 aisbench 模式下
  `atol`/`rtol` 缺省 0.0（`:434-435`），于是任何浮点差异 →
  `numerical_regression` → `failed`。A4 是全放，A5 是全禁，两者都不是判断。
- **A6｜统计学决定被写成常量。** `max_cv` 缺省 0.1（`:195, 460`）、MAD 修正
  z 阈值 3.5 硬编码（`:410`）、每态至少 2 个决策值（`:488`）、`runs >= 2`
  （`:175`）。也就是说一次"2 次重复 + CV≤10%"的 A/B 就足以输出 `failed`，而
  这套参数从未针对本仓库真实的 run-to-run 分布校准过。
- **A7｜零容差准确率对比是默认。** `aisbench_adapter.py` 的
  `--max-absolute-regression` / `--max-relative-regression` 默认都是 0.0
  （`:321-322`），配合 `--metric accuracy`（`:319`）与 `--seed 1`（`:326`）：
  数据集层面的任何抖动都会成为 `task_metric_regression`。
- **A8｜结案被折算成布尔表达式。** `finalize_case` 把三个 CLI 字符串
  （`minimal_result`/`original_result`/`cleanup_status`）折算成 `resolved`
  （`:496-500`），并直接把 manifest 推到 `passed`（`:524`）。它不要求存在任何
  `experiments` 记录或 `comparisons` 产物。
- **A9｜结构不一致被直接称为"已诊断"。** 任一 `confirmed` finding 就令
  `status = "diagnosed"`（`:455-457`），并把 manifest 打成 `failed`（`:533`）。
  其中 `unknown-process-group`（`:359-366`）实际只说明事件里的 group 名不在
  operator 手写的 `topology` 里——可能只是拼写不一致——却输出 confirmed +
  diagnosed。这正是"自动化的、自信的、错误的结论，还附着证据"。

### 4.B 把确定性机制留给手工拼装

- **B1｜每 rank 证据采集完全没有生产者。** `SKILL.md:16-18` 要读者"用
  `ingest` 加入结构化 per-rank 事件"并自行"保留原始 rank 日志与栈"；
  `references/command-recipes.md:15-20` 直接给出
  `--events /path/to/rank-events.jsonl`。多 rank 日志拉取、时间戳对齐、
  collective 序号打点、栈 dump 采集全部靠手。`init` 创建了 `rank-logs/`、
  `stack-dumps/`、`metadata-samples/`（`:238`）之后再也不管它们。
- **B2｜80 行图内打点模板要手工移植进模型代码。**
  `graph-debug/SKILL.md:105-183` 是一段 `DebuggableImpl` 示例，读者需要自己
  改名、接进 model runner（`:186-195`）、并保证 JSONL key 恰好是
  `step/layer/rank/tag`（`SKILL.md:206` 对应
  `graph_debug_case.py:35`）。"buffer 在初始化期预分配、图内只做 `copy_`、
  图外统一 `synchronize` + 落盘"是只有一种正确形状的机制，现在靠复制粘贴。
- **B3｜确定性设置靠散文提醒。** `HCCL_DETERMINISTIC=true` 与
  `torch.use_deterministic_algorithms(True)`（`graph-debug/SKILL.md:40, 200`）
  是纯机制，没有任何入口负责设置或校验。correctness 同理：
  `SKILL.md:29-36` 要读者自行保证 prompts、chat template、权重、tokenizer、
  拓扑、特性开关一致。
- **B4｜两态 harness 配置由人手写。**
  `correctness-validation/references/command-recipes.md:38-52` 给出配置样例，
  但没有"从 baseline 配置派生 candidate 配置、只改一个声明变量"的生成器。于是
  `SKILL.md:33` 的一致性规则和 `SKILL.md:35`（"绝不用不同 case 文件产生的结果
  互相对比"）都只是道德约束。
- **B5｜控制面与执行面之间的搬运全靠手。** `correctness_run.py` 与
  `remote_correctness_harness.py` 没有代码连接（§1.2）：配置下发、容器内执行、
  结果回收、路径对应，全部由读者用 remote-dev 拼。
- **B6｜交替调度靠人自觉执行并手贴标签。**
  `perf/SKILL.md:16-17` 要读者逐条按 `schedule.json` 做 parity → 起服务 →
  跑 benchmark → `normalize`（手填 `--state --phase --ordinal --config-hash`）
  → `record`。调度是脚本发的，"真的按顺序去跑"和"贴对 state 标签"是手工的。
  而 R1 指出仓库里已经有一个会自己执行两态的实现。
- **B7｜跨仓 diff 手工合并。** `collect_git_diff` 只接受一个 `--repo-root`
  （`:557`），而 `references/acceptance.md:7` 要求"跨仓改动使用合并 diff 或等
  价的完整证据"。脚手架 + `vllm/` + `vllm-ascend/` 三个仓的 diff 拼接是手工的。
- **B8｜机制在文件之间被手工复制。** 见 R4/R5。

---

## 5. "passed" 允许意味着什么

`.agents/coordinator/README.md:404-406` 明确："它绝不会因为资源分配被释放就把
一个 run 标记为 `passed`。域验证工作流附上它们自己的验收证据。"

逐个检查这五个是否遵守：

| Skill | 能否报告 `passed` | 是否需要真实证据 | 判定 |
|---|---|---|---|
| graph-debug | 能（`:524`） | **不需要任何证据** | **违反** |
| correctness-validation | 能（`:516, 603`） | 只需两份符合 schema 的 JSON | **违反** |
| performance-regression | 能（`:516, 591`） | 需要 2×(warmups+runs) 份 schema 合法、哈希一致、顺序正确的测量文件 | **弱**（可伪造，且 parity 凭证是空壳，原始产物不留存） |
| change-validation | 能（`:506`） | 结构上要求每个 required 项都有 `passed` 子运行 | **结构正确，传递性不安全** |
| distributed-debug | **不能**（`:533` 只能 `failed`/`inconclusive`） | — | **相反的失败**（见下） |

### 5.1 无证据 `passed` 的具体路径

**路径 1（最短，2 条命令，零证据）：**

```
graph_debug_case.py init --case-dir D --case-id c --stage unknown \
  --eager-result pass --graph-result fail --reproduction "x"
graph_debug_case.py finalize --case-dir D --root-cause "x" --fix "y" \
  --minimal-result pass --original-result pass --cleanup-status removed
```

`finalize_case`（`:477-527`）不检查 `case["experiments"]` 或
`case["comparisons"]` 是否非空，manifest 直接 `planned → running → passed`
（`:515, 523-525`），身份字段全为 `{}`（`init` 默认 `:541-544`）。这份
manifest 随后可被 `change_validation.py link` 收下（`:448-466`，`parent_run_id`
检查恒真），并使父运行走向 `passed`（`:506`）。

**路径 2（correctness，两份手写 JSON）：**
`compare_run`（`:559-605`）只做 schema 校验（`:146-177`）。两份满足契约的
JSON——甚至可以是同一份传两次（C1）——即产出 `exact_match` → `passed`。没有任
何字段要求证明它跑在 NPU 上、跑的是哪两个代码状态、命令是什么（`init` 的
`--baseline-command/--candidate-command` 默认空列表，`:631-632`，`reproduction.sh`
会写 `# command not recorded`，`:216-223`）。

**路径 3（correctness，真实但无意义的通过）：** 一个在线服务返回 200 且
`content` 为空字符串时，两态都归一化成 `{"text": ""}`
（`remote_correctness_harness.py:75-84`），`_output_signature`
（`correctness_run.py:268-276`）返回 `("text", "")` 而非 `None`，两侧相等 →
`exact_match` → `passed`。**两个空回答等于"正确性通过"。**

### 5.2 相反的失败：distributed-debug 无法表达成功

`analyze_case` 的终态映射是 `"failed" if diagnosed else "inconclusive"`
（`:533`），因此：

- 一次成功的分布式诊断被记为 `failed`（语义倒置）；
- 一次修复后的验证（最小复现 + 原拓扑都通过，`acceptance.md:20-23` 要求的）
  **无法产出任何 `passed` 证据**；
- 任何被 change-validation 链接的 distributed 运行都会把父运行拉到
  `failed`/`inconclusive`（`:497-506`）。也就是说
  `validation-rules.yaml:58, 154` 的 `route_on_hang` 路由一旦被走过，PR 报告
  就再也不可能 `passed`。

另外 `analyze` 只能成功执行一次：第二次调用时 manifest 已是终态，
`transition_status(failed → failed)` 抛 `RunManifestError`
（`vaws_run_manifest.py:200-208`），而 `SKILL.md:11-23` 的工作流是"ingest →
analyze → 形成假设 → 降维 → 再记录"的迭代过程。

---

## 6. 可诊断性缺口

`.agents/knowledge/known-failure-signatures.yaml:36-63` 记录了这件事为什么重
要：本地工具服务的**瞬时** timeout 与远端命令超时是两种完全不同的故障，
"不要在 mux 上重试同一个 stream 命令期待不同结果；先查 mux 选项。把 MCP
`remote_bash` 的瞬时 timeout 当作工具服务故障签名，而不是远端命令故障"
（`:52-54`）。查错了层是最贵的错误。

- **6.1｜所有失败都塌缩成一个字符串。** 七个入口的错误出口形状完全一致：
  `{"status": "failed", "error": str(exc)}`（`correctness_run.py:678-680`、
  `change_validation.py:608-610`、`performance_regression.py:662-664`、
  `graph_debug_case.py:638-640`、`distributed_debug.py:568-570`、
  `remote_correctness_harness.py:270-272`、`aisbench_adapter.py:379-381`）。
  没有错误码、没有 `phase`、没有 `layer`（本地 / 传输 / 容器 / 运行时 /
  模型）。而这些脚本已经在 stderr 上发 phase 进度
  （如 `correctness_run.py:64-65`），信息就在手边却没有进入失败载荷。
- **6.2｜correctness 的失败没有落点。** harness 的错误只有
  `f"{type(exc).__name__}: {exc}"`（`remote_correctness_harness.py:236`），
  没有日志路径、没有容器/端点标识、没有版本。更糟的是引擎构建失败会被记录一
  次然后**重放进每一个 offline case**（`:160-165, 192-193`）：N 个 case 显示
  同一条消息，读者无法区分"引擎起不来"与"这个 case 本身失败"，也无法知道
  哪些 case 其实根本没跑。分类侧则把两者都归为 `infrastructure_failure`
  （`correctness_run.py:344-353`）。
- **6.3｜graph：挂起、编译失败、结果错误在记录里长得一样。**
  `--stage` 是 operator 声明的（`:537`）且从不与证据交叉校验；`case.json`
  （`init`，`:147-166`）没有任何 artifact/日志槽位，只有 `comparisons` 会进
  manifest（`:466-472`）。`SKILL.md:56` 要求"记录最后一个成功阶段"，但结构里
  没有这个字段。挂起完全没有表示：没有超时、没有 `last_successful_stage`、
  没有栈 dump 引用。于是 `acceptance.md:6-10` 要求的东西无法被产物证明。
- **6.4｜distributed：无法区分"rank 卡住"与"我们的采集停了"。**
  `collective-enter-without-exit` 的严重度是 `candidate`（`:405`）——这是对
  的——但事件 schema（`:201-225`）没有"本 rank 采集正常终止"的标记，所以
  "rank 停在 collective 里"和"日志采集在这里断了"产生完全相同的证据。
  同时 `rank-logs/`、`stack-dumps/`、`metadata-samples/` 建了就不管
  （`:238`）：不枚举、不哈希、不进 manifest，因此 case 的 manifest 并不指向
  `acceptance.md:9` 要求保留的原始证据。
- **6.5｜逻辑设备 vs 物理卡的换算完全缺失。** `validate_config`
  （`:124-146`）逐字段校验 10 个 rank 坐标，但**不检查 `(node, device)` 是否
  重复**，也不检查 `tp*pp*dp*ep` 与 `expected_world_size` 是否自洽。
  `feat/multinode-and-experiment-ledger` 的第二条 commit 正是记录这个陷阱：
  A3 单卡 2 die，8 卡节点是 16 个逻辑设备，按卡数定拓扑会把宽度砍半，而错误
  不在配置期暴露，而是以 fused-MoE 形状错误的形式出现并被当成算子 bug 排查。
  这个 skill 是唯一该在配置期拦住它的地方，而它没有这项检查。
- **6.6｜唯一的端点机制是死代码，真正的端点机制不存在。**
  `endpoint-collision` finding（`:413-427`）读的是 `network-endpoints.json`，
  而该文件由 `init` 写入（`:249-251`），`init` 的校验已经拒绝重复的
  `(address, port)`（`:192-196`，`references/behavior.md:13-14` 也这么写）。
  因此这条 finding 只有在手工编辑产物后才可能触发。与此同时，没有任何入口去
  观测各 rank 实际监听的 socket——真正的端点冲突机制缺失。
- **6.7｜perf 的指标名不匹配，且报错报错了对象。**
  `DEFAULT_BENCHMARK_METRICS`（`:35-41`）声明
  `itl → mean_itl_ms` 与 `acceptance_rate → acceptance_rate`
  （`references/behavior.md:61-67` 也这么文档化）。但
  `vllm-ascend-benchmark/scripts/_common.py:1186-1190` 的实际提取列表里既没有
  `mean_itl_ms`，也没有 `acceptance_rate`——该函数的 docstring 甚至明写
  "spec-decode acceptance 指标是 `spec_decode_acceptance_rate`；裸的
  `acceptance_rate` 键从未在结果 JSON 中存在过"（`:1179-1182`）。于是 5 个默
  认映射里 2 个不可能命中；`normalize` 静默跳过（`:133-135` `continue`），
  `analyze` 在 `missing_metrics` 里报出的是**目标名** `itl`（`:469-470`），而
  真相是"我们找的是 `mean_itl_ms`，而 benchmark 从不产出它"。SKILL.md 的
  description 却把 acceptance-rate 回归检查列为本 skill 的能力。
- **6.8｜聚合测量把方差藏了一层。** `normalize` 允许一份测量来自 benchmark
  的多次运行聚合（取 `aggregated[...]["mean"]`，`:132-133`），随后 `analyze`
  在这些均值之上再算 stdev/CV（`:431-441`）。于是"测量内方差"被丢弃，CV 门禁
  评估的是均值的均值，而 `acceptance.md:20` 要求"报告原始值"这一条在聚合输入
  下已不成立。

---

## 7. 与 experiment-ledger 原则的差距

ledger 的核心断言是：两次运行之间任何未声明的差异都记为 confounder，一个
confounder 就足以判 `not-comparable`，因为差两处的两次实验无法归因于任一处；
并且身份必须在 run 创建时记录，事后补的身份只描述你以为跑了什么。

| ledger 原则 | 这五个的遵循程度 |
|---|---|
| 每个 run 写一份可溯源的 Run Manifest v1 | **5/5**（全部走 `.agents/lib/vaws_run_manifest.py`） |
| 身份在创建时被记录且非空 | **0.5/5**。四个允许身份为空：`correctness_run.py:627-630`、`graph_debug_case.py:541-544`、`distributed_debug.py:262-265`、`change_validation.py:399-408`（后者根本不传 `environment`/`model`/`topology`）。perf 唯一强制两态 `workspace_snapshot`（`:287-290`），但 `environment`/`model`/`topology` 取自 `shared` 的可选键（`:291-293`） |
| 未声明差异记为 confounder | **0/5**。没有任何入口把"被测变量之外还差什么"作为一等公民记录 |
| 一个 confounder 即 not-comparable（硬判决） | **0/5**。perf 的哈希相等是最接近的，但它比对声明与自身，恒真 |
| 不可索引/无身份的产物要报出而不是静默跳过 | **1.5/5**。`change_validation.py:443-446` 拒绝未知 plan item id；`distributed_debug.py:349-357` 把缺 rank 报成 evidence gap（同精神）。反例是 `correctness_run.py:302-304`：缺失被直接当成结论 |
| 非终态不是结果 | **4/5**。change-validation 显式处理非终态子运行（`:497-506`）；correctness/perf/graph 各自状态机自洽；distributed 无法产出终态 `passed`（§5.2） |

总体：**写了账本的载体，没有实现账本的核心断言。** 约 1.5–2 / 6。

ledger commit 记录的三个测量陷阱在本家族中的状态：

1. **DP 下按 engine label 求和 `/metrics`** —— 这五个都不读 `/metrics`，perf
   委托给 benchmark。但 perf 的 `shared` 不要求记录 DP 度（C5），所以一次
   DP≠1 与 DP=1 的对比只有在 operator 自己写下来时才会被哈希拦住。
2. **计数快照取在稳定窗口边缘** —— perf 把整次 benchmark 当作一个测量，没有
   窗口概念；而 `references/behavior.md:90-91` 已经把
   `service_start_time`/`hbm` 列进测量指标，这两个是无窗口定义的 gauge。
3. **并发受限 runner 的 per-item latency 不能当性能信号** —— `ttft/tpot/itl`
   默认就在阈值集合里（`:35-41`），而 `max_concurrency`/`request_rate` 不是
   `shared` 的必填键（`:171-173`）。这条陷阱在本家族里完全没有防护。

---

## 8. 整合形状与优先级

### 8.1 建议的命令归属（仅为分类结论的落点，不在本 PR 实施）

| 整合命令 | 吸收 |
|---|---|
| `experiment plan` | perf `plan` + correctness `init` + 两态 harness 配置派生（B4）+ 拓扑/逻辑设备换算（6.5） |
| `experiment receipt` | **新机制**：观测式可比性凭证（§3.3），复用 `bench_compare.py:634-685` 的 fail-closed 门禁形状 |
| `experiment run` | perf 的按调度执行（B6）+ `bench_compare.py` 的多态执行器（R1），交替顺序取 perf 的，执行取 bench_compare 的 |
| `experiment compare` | correctness `compare` + graph `compare` + R3/R4 的两套阈值与容差实现，输出**分类而非判决** |
| `evidence plan/link/finalize` | change-validation 三动词，required 的产生者从正则改为人（A1），并在 `link` 加子运行身份校验（C10） |
| `rank evidence collect/analyze` | distributed `ingest`/`analyze` + **缺失的采集器**（B1） |
| `graph snapshot instrument` | B2/B3 的模板与确定性开关 |

### 8.2 最该先钉住的机制

**观测式可比性凭证。** 理由：

1. 它是 §3 的十一个空洞的共同修法；
2. 它堵住 §5 路径 2/3（比较入口在没有凭证时不得输出 `passed`）；
3. 它是 §4.A 的前置条件——只有可比性成立，自动判决才有资格存在，否则任何
   整合只会让错误结论产出得更快；
4. 它已经有一个可参照的、fail-closed 的实现在仓内
   （`bench_compare.py:634-685`），不必从零设计。

**它的真实硬件验证矩阵**（六次真机运行，在一台已注册机器的单个 session 内可
完成）：

| # | 设计 | 期望 |
|---|---|---|
| 1 | 同代码、同配置跑两次 | `comparable`；correctness 稳定 `exact_match` |
| 2 | 只改 candidate 代码 | `comparable`，且 native digest 门禁按需触发重建要求 |
| 3 | candidate 额外带一个 `--enforce-eager` 差异 | **必须 `not-comparable`**（今天会静默当成代码回归，C2） |
| 4 | candidate 换 TP / 卡数 | **必须 `not-comparable`**（今天完全不可见，C3） |
| 5 | 在 DP>1 下重复 #3 | `not-comparable`；并顺带证实 `/metrics` 按 label 求和的处理 |
| 6 | 两态 `max_concurrency` 不同 | **必须 `not-comparable`**（今天 `shared` 无必填键，C5） |

第二优先是 **per-rank 证据采集器**（B1）：它是 distributed-debug 唯一缺失的
生产者，而没有它，6.4 的"rank 卡住 vs 采集停了"永远无法区分，`analyze` 那 200
行严谨的不变量检查也拿不到输入。
