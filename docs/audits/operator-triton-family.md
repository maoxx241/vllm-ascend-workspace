# 审计报告：operator 与 Triton 开发家族

Status: dated 2026-09-07 at 161fed1 — historical evidence

审计对象（5 个 skill 包，6 个 argparse 入口，约 2,618 行 Python）：

- `.agents/skills/ascend-operator-debug/`
- `.agents/skills/ascend-triton-operator-development/`
- `.agents/skills/ascend-triton-kernel-validation/`
- `.agents/skills/ascend-triton-kernel-optimization/`
- `.agents/skills/ascend-triton-workflow/`

审计基准：确定性机制（closed-world）应当收敛为少量、稳定、经真机验证
的命令；开放判断（open-world）应当只给指引与上下文，不写成脚本；每一个
结果都必须可诊断（做了什么、确切命令、环境身份、日志位置、失败发生在哪
一层）。

本审计只产出文档，不改动任何行为。所有陈述与 `file:line` 引用描述的是
2026-09-07 的 scaffold 提交
`161fed1b0fe6b48359be3f0cf33bb7d8befae113`（下文写作 `161fed1`），不是
其后的仓库状态。本次没有在远端硬件上重新执行、编译、启动、测量或采集。
文中的建设建议是该快照上的设计意见，不是现已可调用的新命令；此后的代码
或文档变更需要各自的验收证据，本记录不把后续修复回写为当时已存在的事实。

## 0. 结论摘要

这个家族与仓库其余部分的失衡方向相反。其余 skill 的风险是"把判断脚本化
了"；这里的实际问题是**把机制留给了 agent**：5 个包共 2,618 行代码，全部
是 plan / record / analyze / link / finalize 形态的**账本控制器**。它们不
执行远端 device 编译或启动、不测量或比对数值输出、也不采集 profiler 或
运行期 fallback 证据。缺少 SSH/NPU 执行并不能证明本地确定性机制不存在。

本地已经存在、且不依赖远端执行的确定性机制包括：
`triton_validation.py:178`-`:210` 的 AST 静态检查、产物/schema 落地与
kernel sha256 记录，`analyze` 的 case 覆盖门禁（`:235`-`:251`），以及
§4.1 列出的哈希、终态、父链接与配置守卫。这些是真实的结构门禁；它们不能
代替 device 执行、数值比对或 profiler 证据生产者。

- AST fallback 门禁本身仍有缺陷：`validate_triton_impl.py` 把类名
  `ModelNew` 与方法名 `forward` 写死在实现里
  （`.agents/skills/ascend-triton-kernel-validation/scripts/validate_triton_impl.py:66`、`:70`），
  并且只检查 `ast.Call`（`:80`-`:91`），因此 `x @ w`、`a * b` 这类
  BinOp 形态的 PyTorch 回退**完全不可见**。
- 容差（tolerance）在 4 个控制器里都被校验"存在"，但**从未被用于判定**任
  何一个 case 的通过与否；`status` 是 agent 手填的字符串。
- `nan_match` / `inf_match` 写在行为契约里
  （`.agents/skills/ascend-triton-kernel-validation/references/behavior.md:56`-`:61`），
  代码中**零引用**。
- "profiler-driven optimization" 的 profiler 证据（`profiler_signals`）在
  `triton_optimization.py` 中**零引用**：一轮实验可以在完全没有任何
  profiler 证据的情况下拿到 `KEEP`。
- 正确性门禁是**结构上强制、证据上不可验证**的：下游只信任 manifest 的
  `status` 与两个 artifact 名，不检查 `static-check`、不检查
  `results.json` 的逐 case 结果，因此一份手写的两 artifact manifest 即可
  通过（家族自带测试正是这样构造证据的）。

## 1. 能力清单

### 1.1 有入口支撑的能力（15 项）

| # | 能力 | 入口 (file:line) |
|---|---|---|
| A1 | operator case 矩阵校验 + 证据目录 + manifest | `ascend-operator-debug/scripts/operator_debug.py:209`-`:268`，CLI `:472`-`:474` |
| A2 | operator 单 case 结果写入（禁止覆盖） | `.../operator_debug.py:272`-`:313`，CLI `:475`-`:477` |
| A3 | operator 失败轴聚合与报告 | `.../operator_debug.py:330`-`:466`，CLI `:478`-`:479` |
| A4 | Triton 任务契约校验 + 语义/草图模板生成 | `ascend-triton-operator-development/scripts/triton_development.py:142`-`:195`，CLI `:305`-`:307` |
| A5 | 候选与证据哈希登记 + 消费终态正确性 manifest | `.../triton_development.py:215`-`:299`，CLI `:308`-`:313` |
| A6 | AST 静态 fallback / kernel 启动门禁 | `ascend-triton-kernel-validation/scripts/validate_triton_impl.py:52`-`:109`，CLI `:112`-`:119` |
| A7 | 验证配置校验 + 静态门禁 + case 矩阵冻结（含 kernel sha256） | `.../triton_validation.py:171`-`:210`，CLI `:289`-`:292` |
| A8 | 验证单 case 结果写入 | `.../triton_validation.py:213`-`:232`，CLI `:293`-`:295` |
| A9 | 验证矩阵状态汇总（`passed_cases == total_cases > 0`） | `.../triton_validation.py:235`-`:283`，CLI `:296`-`:297` |
| A10 | 优化基线/目标锁定 + 起始正确性证据核验 | `ascend-triton-kernel-optimization/scripts/triton_optimization.py:186`-`:243`，CLI `:408`-`:410` |
| A11 | 轮次控制：顺序、父哈希链、KEEP/DISCARD/NOISE/FAIL | `.../triton_optimization.py:260`-`:353`，CLI `:411`-`:413` |
| A12 | 最优 kernel、累计收益、终态判定 | `.../triton_optimization.py:356`-`:402`，CLI `:414`-`:415` |
| A13 | 工作流阶段计划 + 父 manifest | `ascend-triton-workflow/scripts/triton_workflow.py:118`-`:161`，CLI `:270`-`:272` |
| A14 | 子 manifest 绑定（一阶段一次，终态校验） | `.../triton_workflow.py:164`-`:200`，CLI `:273`-`:276` |
| A15 | 必需阶段证据聚合与工作流报告 | `.../triton_workflow.py:223`-`:264`，CLI `:277`-`:278` |

### 1.2 SKILL.md 描述但**没有任何入口**的能力（16 项）

这一节就是本次审计的核心发现。

| # | 能力 | SKILL/参考文档要求 (file:line) | 代码现状 |
|---|---|---|---|
| B1 | 在远端 Ascend 上构建并执行一个 case（编译、启动、抓 stdout/stderr/stack、取输出） | `ascend-triton-kernel-validation/SKILL.md:15`；`ascend-operator-debug/SKILL.md:18`；`ascend-operator-debug/references/command-recipes.md:11`-`:16` | 不存在。`command-recipes.md` 的 "Execute remotely" 是纯散文 |
| B2 | 用预声明容差把 comparisons 判成 pass/fail | `ascend-triton-kernel-validation/SKILL.md:16`；`references/case-design.md:44`-`:51` | 不存在。容差只做存在性校验：`triton_validation.py:104`-`:137`、`operator_debug.py:115`-`:126`、`triton_development.py:136`-`:137`；`status` 由 agent 手填并直接采信（`triton_validation.py:148`-`:152`） |
| B3 | shape/dtype/NaN/Inf 结构比对 | `references/behavior.md:56`-`:61`（`nan_match`/`inf_match`）；`SKILL.md:16` | 不存在。`nan_match`/`inf_match` 在全家族 Python 中零引用；`comparisons` 可为空数组（`triton_validation.py:153`-`:156`） |
| B4 | 运行期 fallback 检测（kernel 是否真的在 device 上跑过） | `SKILL.md:14`（仅静态）；`references/acceptance.md:8` 明确要求"人工复核 AST 无法证明的动态调用" | 不存在。只有 AST 门禁（`validate_triton_impl.py:52`-`:109`），跨模块导入的回退、按 shape 分支的运行期回退均不可见 |
| B5 | 延迟测量（warmup / repeat / median） | `ascend-triton-kernel-optimization/SKILL.md:13`；`references/behavior.md:43`-`:49` | 不存在。控制器只接收 agent 提供的 `median_us`/`repeats`（`triton_optimization.py:112`-`:133`） |
| B6 | 噪声/CV 实测 | `ascend-triton-kernel-optimization/SKILL.md:42`（"remeasure noise before small decisions"） | 不存在。`noise_floor` 是配置常量（`triton_optimization.py:309`-`:310`） |
| B7 | profiler 采集与信号提取（`msprof op`） | `ascend-triton-kernel-optimization/SKILL.md:16`；`references/behavior.md:65`-`:66` | 不存在。`profiler_signals` 在 `triton_optimization.py` 中零引用，既不校验也不要求 |
| B8 | 理论下限计算（memory/vector/launch floor） | `references/profiling-decision-tree.md:12`-`:21` | 不存在（公式给了，无计算入口） |
| B9 | 边界 case 生成（tail、非 2 的幂、全掩码、核数邻域） | `references/case-design.md:20`-`:36`；`SKILL.md:41` | 不存在。plan 只校验 agent 手写的 cases（`triton_validation.py:108`-`:137`） |
| B10 | 最小失败用例收敛 | `ascend-operator-debug/SKILL.md:23`；`references/acceptance.md:19` | 不存在 |
| B11 | 回归测试落地 | `ascend-operator-debug/SKILL.md:23`-`:24` | 不存在 |
| B12 | 目标能力探测（SoC、核数、UB、CANN/`triton-ascend`/`torch_npu` 版本） | `ascend-triton-operator-development/references/architecture-and-codegen.md:12`-`:16` | 不存在。`environment` 在 5 个控制器里都是可选自由字典（`triton_validation.py:196`、`triton_development.py:184`、`triton_optimization.py:229`、`operator_debug.py:249`、`triton_workflow.py:149`），空对象即可通过 |
| B13 | 语义审计/草图完成度检查 | `ascend-triton-operator-development/SKILL.md:15`；`references/behavior.md:50`-`:52` 自认"控制器无法判断" | 只有非空检查（`triton_development.py:198`-`:200`），而 plan 生成的模板本身即非空（`:163`-`:177`），**未修改的模板可直接通过 finalize** |
| B14 | 远端代码 parity 前置 | `ascend-triton-kernel-validation/SKILL.md:15`；`ascend-triton-workflow/SKILL.md:19` | 机制已存在于 `remote-code-parity/scripts/parity_sync.py`，但本家族不调用、不记录、不强制 |
| B15 | 跨阶段 kernel 哈希链（workflow 层） | `ascend-triton-workflow/references/acceptance.md:12`-`:13` | 不存在。`link` 只校验 `parent_run_id`/`run_type`/终态（`triton_workflow.py:174`-`:181`），validation 阶段与 optimization 阶段可指向不同 kernel |
| B16 | 知识库查询/回写 | 4 个 SKILL.md 只写 "Query `.agents/knowledge/`"（`ascend-triton-kernel-validation/SKILL.md:13`、`ascend-triton-operator-development/SKILL.md:13`、`ascend-triton-kernel-optimization/SKILL.md:14`、`ascend-triton-workflow/SKILL.md:13`）；`ascend-operator-debug/SKILL.md` 完全没有这一步 | 机制已存在（`.agents/scripts/knowledge_query.py`、`knowledge_capture.py`），但无一处点名脚本，且缺少 capture 环节，与 `AGENTS.md` 的知识库规则不一致 |

补充：5 个 SKILL.md 中 **`session` 一词零出现**，而 `AGENTS.md` 要求远端
工作运行在 `session-management` 会话内。这属于 B14 同类的路由缺口。

### 1.3 正确保留为开放判断的能力（7 项）

| # | 能力 | 支撑指引 |
|---|---|---|
| C1 | 写 kernel / GPU→NPU 迁移实现 | `ascend-triton-operator-development/references/semantic-review.md`、`architecture-and-codegen.md` |
| C2 | 瓶颈归因 | `ascend-triton-kernel-optimization/references/profiling-decision-tree.md:24`-`:34` |
| C3 | 下一轮优化手段选择 | `references/ascend-techniques.md` |
| C4 | 容差数值的取值与数值论证 | `references/semantic-review.md:60`-`:61` |
| C5 | 哪些 shape/dtype 真正重要 | `references/case-design.md:12`-`:18` |
| C6 | 参考实现是否足够独立 | `ascend-operator-debug/references/acceptance.md:7` |
| C7 | "算子无缺陷 → 查集成边界"的后续判断 | 机制侧已有提示：`operator_debug.py:372`-`:376`、`:413`-`:423` |

这一节值得肯定：本家族的 `references/` 在"不替 agent 做决定"上非常克制，
例如 85 KB UB 预算被明确标注为启发式而非契约
（`architecture-and-codegen.md:39`-`:41`、`ascend-techniques.md:30`-`:32`）。

## 2. 分类结果

| 分类 | 数量 | 条目 |
|---|---|---|
| closed-world，机制已存在且归属清晰 | 10 | A5, A6, A7, A8, A9, A10, A12, A13, A14, A15 |
| closed-world，机制**完全不存在** | 11 | B1, B2, B3, B4, B5, B6, B7, B11, B12, B13, B15 |
| mixed（缝在何处见 2.3） | 5 | A4, A11, B8, B9, B10 |
| open-world judgment | 7 | C1–C7 |
| redundant | 5 | A1, A2, A3（实现级），B14, B16（路由级） |
| 合计 | 38 | |

### 2.1 closed-world：应归属的收敛命令与所需真机验证

**A6 / B4 — fallback 检测（最高风险）。** 应由
`triton_validation.py fallback` 一个子命令统一拥有静态与运行期两层：静态
层保留 AST（但需摆脱写死的 `ModelNew`，见 3.2），运行期层从实际执行的
case 中确认 device 侧 kernel 出现过。真机成熟度验证：构造一个"对齐 shape
走 Triton、tail shape 悄悄回退 PyTorch"的 kernel，断言 tail case 必须
`failed` 而不是 `passed`；再构造一个通过跨模块导入回退的 kernel，断言静态
层报告"无法证明"而不是"通过"。

**B1 / B2 / B3 / B14 — case 执行与判定。** 应由一个
`triton_validation.py run --case <id>`（operator 侧对应
`operator_debug.py run`）拥有：parity → 编译 → 启动 → 抓
stdout/stderr/stack → 取输出 → 按预声明容差与 NaN/Inf 结构比对 → 直接产出
`record` 已接受的规范化结果 JSON 并落 `raw-results/`。真机验证：在 ≥2 代
SoC × {eager, compile, graph} × 全 dtype 上跑通；并故意注入编译错误、UB 溢
出、错误结果、静默回退、进程被杀五种故障，断言输出五个互不相同的层标签。

**B5 / B6 — 测量与噪声。** 应由 `triton_optimization.py measure` 拥有
warmup/repeat/median/CV，并让 `noise_floor` 由实测 CV 推导而非配置常量。真
机验证：同一 kernel 在空载与有负载设备上各重复 N 轮，断言判定在噪声下不
翻转。

**B7 — profiler 采集。** 应由 `triton_optimization.py profile` 拥有，复用
`ascend-memory-profiling/scripts/mem_collect.py` 已验证过的 msprof 包装路
径（`mem_collect.py:9`-`:21`），产物直接填入 `profiler_signals` 并成为
`record` 的必需 artifact。真机验证：≥2 个 CANN 版本；profiler 不可用时必须
退化为 `inconclusive`，不得退化为 `passed`。

**B11 — 回归用例落地。** 应由 `operator_debug.py emit-regression` 拥有，从
最小失败 case 直接生成可执行测试。

**B12 — 目标能力探测。** 应由一个共享的 `triton env-probe` 拥有（或折叠进
B1 的执行器），把 SoC、核数、UB、CANN/`triton-ascend`/`torch_npu` 版本写入
manifest `environment`，未知项必须显式标记 unknown。真机验证：覆盖机队里
全部机型，断言"标记未知"而不是"编造常量"。

**B13 / B15 — 门禁补强。** 无需新入口：`triton_development.py finalize` 应
在非空检查之外拒绝仍含未勾选 `- [ ]` 的模板；`triton_workflow.py link` 应
校验 validation 与 optimization 子证据指向同一 kernel 哈希。

### 2.2 open-world：用什么替代脚本

C1–C7 不需要任何入口，现有 `references/` 已是恰当形态。唯一需要补的是
**输入**：C2/C3 依赖 B7 的真实 profiler 证据，C5 依赖 B9 生成的候选边界集，
C4 依赖 B2 把它选定的容差真正执行。指引已经到位，缺的是喂给指引的机制。

### 2.3 mixed：缝的确切位置

- **A4**：契约校验（closed）与语义审计质量（open）混在一个 plan 里。缝应
  落在"模板结构完成度"上：结构可机检（B13），内容判断留给 agent。
  `references/behavior.md:50`-`:52` 已经自己承认了这条缝，但没有把可机检
  的那一半实现。
- **A11**：阈值算术（closed，`triton_optimization.py:246`-`:257`）与
  hypothesis/change 的内容（open，`:273`-`:275` 仅校验非空字符串）混在一
  起。缝正确；问题在于 `KEEP` 判定所依赖的 measurements 与 profiler 证据
  两侧都不是机制产物。
- **B8**：公式代入（closed）与"有效带宽/吞吐取值"（open）。缝应落在"agent
  提供 effective 值，脚本算下限并与实测对比"。
- **B9**：轴的选择（open）与"围绕已声明 tile/对齐边界展开变体"（closed）。
  缝应落在"agent 声明关心哪些轴，`triton_cases expand` 生成变体"。
- **B10**：收敛策略中"沿已声明轴二分"（closed）与"先动哪个轴"（open）。

### 2.4 redundant

- **A1/A2/A3 与 A7/A8/A9 是同一个控制器的两份实现。** 输入 schema 校验
  函数几乎逐行重复：`operator_debug.py:79`-`:99` vs
  `triton_validation.py:77`-`:88` vs `triton_development.py:73`-`:84`；
  `_atomic_write`/`_write_json`/`_load_json` 在 5 个脚本中各写一遍
  （如 `operator_debug.py:46`-`:76`、`triton_validation.py:48`-`:74`、
  `triton_optimization.py:44`-`:70`、`triton_development.py:44`-`:70`、
  `triton_workflow.py:50`-`:80`）。这不是"两个技能"，而是一个
  case-matrix 控制器加两套状态词表（`operator_debug.py:32`-`:33` vs
  `triton_validation.py:36`-`:37`）。合并可直接消去两个入口的参数形状分歧
  （例如 record 是否检查 `case["status"] != "pending"`：
  `operator_debug.py:283`-`:284` 有，`triton_validation.py:221` 无）。
- **B14/B16**：机制已在仓库其他位置成熟存在，本家族只是没有点名调用。

## 3. 两个相反方向的错误

### 3.1 把机制留给 agent（本家族的主导错误，10 处）

1. `ascend-triton-kernel-validation/SKILL.md:15`-`:17`：agent 手工"在远端
   跑完每个 case"再"规范化成一份 JSON 交给 record"。构建、启动、抓日志、
   落 raw evidence、拼 schema 全部手工，且这是**每个 case 重复一次**的手工
   动作。
2. `ascend-triton-kernel-validation/SKILL.md:16` + `case-design.md:44`-`:51`：
   "先比 shape/dtype，再比 NaN/Inf，再比数值"由 agent 每次自行措辞与执行；
   容差（`triton_validation.py:104`-`:137`）只被校验存在。
3. `ascend-operator-debug/SKILL.md:18`-`:20` +
   `references/command-recipes.md:11`-`:16`：整个"Execute remotely"章节是散
   文；`plan` 生成的 `reproduction.md`（`operator_debug.py:237`-`:243`）也只
   是一段说明文字，不是可执行复现物。
4. `ascend-triton-kernel-optimization/SKILL.md:16`：手工调用 `msprof op` 并
   自行摘取 MTE2/MTE3/Vector/Scalar 指标；落点 `profiler_signals` 是自由字
   典且代码零引用。
5. `ascend-triton-kernel-optimization/SKILL.md:13` +
   `references/behavior.md:43`-`:49`：warmup/repeat 策略、device time 与
   wrapper time 的分离全靠人守约，控制器只做 `median_us > 0` 与
   `repeats >= 1` 的形状校验（`triton_optimization.py:124`-`:129`）。
6. `ascend-triton-kernel-optimization/SKILL.md:42`："remeasure noise" 无任何
   机制支撑。
7. `ascend-triton-operator-development/SKILL.md:15`-`:16` +
   `architecture-and-codegen.md:30`-`:37`：UB 峰值活跃集预算给了公式却没有
   计算入口，且所需的 UB/核数事实本身也没有探测入口（B12）。
8. `ascend-operator-debug/SKILL.md:23`："把最小失败 case 加成回归测试"是纯
   人工步骤。
9. `ascend-triton-kernel-validation/SKILL.md:15`、
   `ascend-triton-workflow/SKILL.md:19`："establish `remote-code-parity`" 不
   点名 `parity_sync.py`，也没有任何字段记录 parity 是否真的建立过。
10. 4 个 SKILL.md 的知识库步骤只写目录名不写脚本名；
    `ascend-operator-debug/SKILL.md` 连这一步都没有。

### 3.2 把决定写成脚本（5 处）

1. **写死 `ModelNew.forward`**：
   `validate_triton_impl.py:66`、`:70`、`:93`-`:94`。这是 KernelBench 式
   基准 harness 的命名约定，被固化进家族里唯一的强制机制。后果不是"漏
   检"而是"误拒"：任何不定义 `ModelNew` 的真实 vllm-ascend kernel 模块，
   `kernel_called_from_forward` 恒为 `False`（`:97`），
   `triton_validation.py plan` 直接抛错（`:179`-`:180`）——于是正确做法变
   成"给真实 kernel 套一个假的 `ModelNew`"，即让机制去适配约定而非相反。
2. **名单式回退判定**：`validate_triton_impl.py:12`-`:28` 的
   `ALLOWED_TORCH_CALLS` / `FORBIDDEN_METHODS` 是白名单+黑名单常量，且遍
   历只看 `ast.Call`（`:80`-`:91`）。因此 `x @ w`、`a * b`、
   `a += b`（BinOp/AugAssign）形态的 PyTorch 计算回退**静默通过**；反之，
   合法但未列入白名单的 `torch.npu.*` 辅助调用被判为违规。fallback 检测本
   该是家族里最稳的机制，这里却由两个常量列表在替 agent 判断"什么算回
   退"。
3. **固定 `noise_floor` 常量代替噪声实测**：
   `triton_optimization.py:309`-`:310` 用配置里的常数决定 `NOISE`，而
   `SKILL.md:42` 要求的是重测噪声。数字站在了统计判断的位置上。
4. **烘死的聚合函数**：`triton_optimization.py:246`-`:257` 固定为"加权平均
   相对收益 + 单 case 回退上限"。权重可配（合理），但聚合形式不可配，
   "这个 shape 的回退可以接受，因为线上不服务它"只能通过改权重迂回表达。
5. **`unsupported` 一律降级为 `inconclusive`**：
   `triton_validation.py:248`-`:249`、`operator_debug.py:365`-`:371`。"该
   算子/该 dtype 在该版本上确实不支持"往往正是结论本身，却被脚本判成"证据
   缺失"，agent 无法表达一个已完成的负面结论。

## 4. 正确性门禁：真的被机械强制了吗

**结论：结构上强制，证据上不可验证。** 门禁挡住的是 manifest 的元数据形
状，而不是"某个工具真的比对过输出"。

### 4.1 确实机械成立的部分

- `triton_optimization.py plan` 要求起始证据是 `run_type == "correctness"`
  且 `status == "passed"`（`:195`-`:197`），并校验 kernel sha256 与 case id
  集合一致（`:86`-`:106`、`:202`-`:208`）。
- 每一轮 `record` 强制父哈希链（`:271`-`:272`）、要求本轮候选自带终态
  correctness manifest 且 hash/case 集合匹配（`:283`-`:298`）；未通过则
  `FAIL` 且不更新 best（`:305`-`:325`）。
- `triton_development.py finalize` 要求终态 correctness manifest、父子关系
  合法、kernel 哈希与 case 集合一致（`:226`-`:250`）。
- `triton_validation.py analyze` 不允许"有缺失 case 还 passed"
  （`:246`-`:251`）。
- `triton_workflow.py` 在配置层拒绝"只做 optimization 不做 validation"
  （`:112`-`:113`），link 要求子 manifest 终态（`:180`-`:181`）。

### 4.2 使门禁失效的部分

1. **逐 case 的 pass/fail 是 agent 的自述。** 没有任何代码把
   `comparisons` 与预声明容差相比；`comparisons` 甚至可以是空数组
   （`triton_validation.py:153`-`:156`），`{"status": "passed"}` + 空
   comparisons 是合法输入。于是 `passed_cases == total_cases`
   （`:246`-`:251`）统计的是自述而非度量。
2. **`nan_match` / `inf_match` 从未被校验**（`behavior.md:56`-`:61` vs 全
   家族零引用）：契约承诺的 NaN/Inf 门禁不存在。
3. **下游只信 manifest 状态，不要求工具产物。**
   `_require_validation_coverage`（`triton_optimization.py:85`-`:106`）与
   `_artifact`（`triton_development.py:203`-`:207`）只索取 `kernel` 与
   `case-matrix` 两个 artifact，**从不索取 `static-check`**（该 artifact 仅
   在 `triton_validation.py:204` 处生成），也从不读取
   `results.json`/`analysis.json` 的逐 case 结果。因此一份手写的、只有两个
   artifact 的 "passed" correctness manifest 足以通过优化门禁——家族自带测
   试正是这么造证据的：
   `ascend-triton-kernel-optimization/tests/test_triton_optimization.py:36`-`:63`
   与
   `ascend-triton-operator-development/tests/test_triton_development.py:74`-`:109`
   手工拼出 manifest 并成功拿到 `KEEP` / `passed`。这直接意味着 fallback
   门禁**不是**优化路径上的传递性必经点。
4. **case 身份只按 id 匹配。** `triton_optimization.py:103`-`:105` 与
   `triton_development.py:245`-`:250` 只比较 case id 集合，不比较 shape /
   dtype / mode。用小 shape 的 `case-1` 通过验证、用大 shape 的 `case-1`
   做基准与计时，门禁察觉不到。
5. **"profiler-driven" 与 "correctness-gated" 一样是描述性的。**
   `profiler_signals` 非必填、无校验，`hypothesis`/`change` 只校验非空字符
   串（`:273`-`:275`）。一轮不带任何 profiler 证据的纯参数试探可以合法
   `KEEP`。
6. **workflow 层没有跨阶段哈希链**（`triton_workflow.py:174`-`:181`）：
   validation 阶段验证 kernel A、optimization 阶段优化 kernel B，
   `finalize` 仍可判 `passed`。
7. **语义审计门禁被生成物自满足**：`plan` 写入模板
   （`triton_development.py:163`-`:177`），`finalize` 只查非空
   （`:198`-`:200`），未编辑的模板即可通过。

一句话：现在这套门禁能防住"忘了跑验证"和"拿旧 kernel 冒充新 kernel"，但
防不住"验证跑了却没有真的比对"，也防不住"手写一份通过的证据"。

## 5. 可诊断性缺口

按"脚本/步骤 → 失败形态 → agent 还得猜什么"列出。

1. **`triton_validation.py record` / `operator_debug.py record` — 失败记录
   无日志、无命令。** 非通过状态只强制一个自由文本 `error`
   （`triton_validation.py:148`-`:152`、`operator_debug.py:193`-`:196`）。
   `raw-results/` 目录被创建（`triton_validation.py:183`、
   `operator_debug.py:218`-`:219`）但从不强制填充，`source` 默认回退为结果
   JSON 自身路径（`triton_validation.py:223`、`operator_debug.py:298`）。
   agent 事后只能看到 `"compilation_error: failed"`，猜的是：编译器报了什
   么、在哪个 case、日志在哪。
2. **全家族 — 没有环境身份。** `environment` 在 5 个控制器里都是
   `config.get("environment", {})`（`triton_validation.py:196`、
   `triton_development.py:184`、`triton_optimization.py:229`、
   `operator_debug.py:249`、`triton_workflow.py:149`），空对象合法，且从不
   与真实远端主机/容器/device 核对；逐 case 结果里也没有环境字段。无法回答
   "这两个 case 是不是在同一个容器、同一版 CANN、同一颗 device 上跑的"。
3. **全家族 — 没有"确切命令"。** `command` 同样是可选透传，而真正执行的命
   令根本不经过任何脚本（B1 不存在），因此 Run Manifest 的 `command` 在实
   践中是空的。"做了什么"这一项在证据链里缺位。
4. **`triton_validation.py plan` — 静态门禁失败无法归因。**
   `:179`-`:180` 把整个 JSON 塞进一个错误字符串。"没有 `@triton.jit`
   函数"、"没有 `ModelNew` 类"、"第 N 行有回退调用"、"这个文件根本不符合
   harness 约定"四种完全不同的原因混在一条消息里，agent 猜的是：该改
   kernel，还是该给它套壳。
5. **`triton_optimization.py record` — `FAIL` 无原因。** 只落
   verification 的 `status`（`:331`-`:332`），不落失败 case、不落失败类
   别。"我这轮改动破坏了 fp16 tail" 与 "这轮验证因 harness/环境问题
   inconclusive" 在轮次记录里长得一样。
6. **`unsupported` 无能力出处。** `triton_validation.py:248`-`:249` 与
   `operator_debug.py:365`-`:371` 把不支持折叠进 `inconclusive`，且没有任何
   字段链接到 `.agents/knowledge/` 的能力或失败签名条目（
   `known-failure-signatures.yaml` 目前也没有任何 Triton/算子类条目）。同
   一个不支持组合会在下一次运行里被重新诊断一遍。
7. **`operator_debug.py analyze` — shape 轴不可分解。**
   `_case_axes`（`:316`-`:327`）把一个 case 全部输入的 shape 拼成**一个**
   字符串键（`:321`-`:323`）。于是"最后一维非对齐时失败"这种结论无法从
   `failure_axes` 读出，agent 必须回到原始结果重新人工归纳。mode/dtype/
   layout 轴是好的，shape 轴等于没有。
8. **`triton_development.py finalize` — 哈希不匹配的报错不带数据。**
   `:238`、`:243`、`:250` 只说"不匹配"，不打印期望值/实际值/涉及路径，agent
   得自己猜是哪一份文件漂移了。

**最严重的一处**是 1+2+3 的合成：一个失败的 case 同时缺少"执行命令""环境
身份""日志产物"。此时无法区分编译失败、启动失败、静默回退、数值错误、
harness 缺陷、环境漂移这六层中的任意一层，唯一的恢复手段是回到真机重跑一
遍——而这恰好是"结果必须让 agent 无需重跑即可定位失败"这条原则要禁止的
情况。

## 6. 建议优先建设的固化机制（不在本 PR 中实施）

1. `triton_validation.py run`（含 operator 侧对应命令）——一次执行一个
   case：parity → 编译 → 启动 → 抓 stdout/stderr/stack → 取输出 → 按预声明
   容差与 NaN/Inf 结构判定 → 直接产出 `record` 可吃的规范化结果并落
   `raw-results/`，同时写入环境身份、确切命令与层标签
   （`parity` / `compile` / `launch` / `fallback` / `numeric` / `harness` /
   `environment`）。这一个命令同时消掉 B1、B2、B3、B14 与第 5 节的第 1、2、
   3 条缺口，也是其他机制的输入基座。
2. `triton_validation.py fallback` 的运行期层（B4）：从实际执行证据确认
   device 侧 kernel 出现过，并把静态层从 `ModelNew` 约定中解耦。
3. `triton_optimization.py measure`（B5+B6）与 `profile`（B7），并把
   profiler artifact 变为 `record` 的必需输入。
4. 门禁补强（B13、B15 与 4.2 的第 3、4 条）：`finalize` 拒绝未勾选模板；
   下游必须索取 `static-check` 与逐 case 结果；case 身份按内容而非 id 匹
   配；`link` 校验跨阶段 kernel 哈希一致。
5. 把 A1–A3 与 A7–A9 合并到一个共享 case-matrix 控制器库，两套状态词表作
   为参数——这是本家族唯一的"入口数量可减"的机会，其余方向都是"应当增加
   机制"。
