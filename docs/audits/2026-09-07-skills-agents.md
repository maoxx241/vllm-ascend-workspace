# Skills 与 AGENTS.md 审计

审计日期：2026-09-07。对象：`refactor/profiling-skills-v2` 本地工作树，包含两个已初始化子模块。本文先列 #73 的处置，再保留修改前的发现及逐项覆盖记录；发现中的旧行号属于审计快照，不是修订后的行号。

**结论：保留现有领域技能拆分，优先修正错误行为、过度约束和失效接口，再缩短入口。** 目前的问题已经超出文案冗长：部分指令会扩大任务范围、阻断远端假设验证、停止无归属证明的进程，或把正常产物当作失败。

依据是 eric provencher 的 [Rethinking skills and prompts for GPT-6 Astra](https://x.com/pvncher/status/2095991462416490862)。本次通过浏览器读到了原帖全文；其审计原则可归纳为：描述应准确而简短，细节按需读取，固定步骤需要实际理由，决策边界应尊重已有授权，完成标准应支持把工作推进到用户需要的终态。下列项目结论来自本地文件检查，并非作者对本项目的评价。

## 与 #73 的对照及本次处置

对照起点是 [PR #73](https://github.com/maoxx241/vllm-ascend-workspace/pull/73)
的 `5acd592ce647493e5f0815707faa302c63128758`，基于 `main`。
其依据是 [Claude Fable 5.1 prompting guide](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5-1)。
保留持续推进、范围控制、独立工具批量调用和有效汇报；补正“所有 skill 决策门
都要再问一次”“只能已有同类测试才可加测试”“看到最后一段是问题就继续执行”
等过度概括。授权、依赖和用户要求的交付终态优先。

| 发现 | #73 中的处置 | 后续归属 |
|---|---|---|
| F01 | 根规则明确清理前证明归属 | 子模块 model-adapter 的广泛 pkill 仍需在子模块修复 |
| F02 | 修 parity/serving，保留原始证据后允许隔离的假设验证；不自动回退用户代码 | 已修指令，未做 NPU 行为实验 |
| F03 | 根规则明确用户范围和授权 | 子模块 adapter 的默认全功能交付及对外评论需独立修改 |
| F04 | 记录错误设备同步示例 | 子模块 AGENTS.md 独立修复 |
| F05 | 本地允许可移植 scaffold 检查，NPU/torch 相关验证仍走远端 | 子模块全量测试规则后续收窄 |
| F06 | 根规则区分 direct endpoint、pool 和 legacy session | 子模块固定路径/端口后续适配 |
| F07 | 区分捕获缺失与导出恢复；完整 HTML 需验证状态和实际页面 | DB/fast/XLSX 与 sweep 问题属于 profiling 分支，不移植其实现到本 PR |
| F08 | benchmark 负责测量；正式回退走受控交替实验；去除无条件 3% 判定 | 未改变测量与回退脚本 |
| F09 | repo-init/parity/ModelScope 复用已有授权；PD start 保持运行；KV 传输需独立证据 | 保留真实的身份、参数、能力边界 |
| F10 | 修 Trae 的 machine/session 参数及 HBM wrapper 返回路径示例 | 子模块 release-note/main2main 问题单独处理 |
| F11 | 显存归因区分测量/推断/未知，说明采样遗漏峰值和版本限制 | 当前报告的缺 baseline 零占位仍是实现后续项 |
| F12 | 生成器让 YAML 位于首行，重新生成 24 个 Claude shim；增加独立结构断言 | Trae 自动生成及跨客户端显式触发策略仍需后续设计；子模块 name 单独修复 |
| F13 | 缩短全部 24 个描述，精简根 Working agreement、远端导航及 repo-init 提问流程 | 大型入口的 references 拆分及 catalog 去重后续推进 |

相对 #73 原 head，主 skill 的 description 从 **10,044 → 3,243 字符**（减少
67.7%，去掉外层 YAML 引号计数），根 AGENTS.md 从 **192 → 125 行**。
这是文本量变化，不是实测模型性能提升。保留 24 个领域 skill 和现有目录校验，
没有用统一字数限制替代任务判断。

本 PR 没有更新 Git submodule 指针、启动远端服务或修改 profiling 实现。
子模块与 profiling 分支的问题在本文保留证据和后续归属，不算已修复。

## 本次修改的验证

- `python3 -B .agents/scripts/skill_catalog.py`：24 skills，0 findings。
- `python3 -B .remote-dev/tools/sync_claude_skills.py --check`：通过；24 个生成入口同步。
- catalog 单元测试：5 个通过。
- remote-dev CLI/shim 测试：10 个通过，包含首行 frontmatter 结构断言。
- Trae 参数与当前 CLI help 对照，ModelScope 两份包内容一致；Markdown 本地目标及 diff whitespace 检查通过。
- 未启动 Claude/Trae 客户端验证自动触发，也未声称远端/NPU 功能、准确率或性能已验证。

## 范围与证据

| 范围 | 数量 | 处理方式 |
|---|---:|---|
| `.agents/skills/*/SKILL.md` | 24 | 全部阅读，检查触发条件、流程、限制、终态与相互路由 |
| `.claude/skills/*/SKILL.md` | 24 | 全文与生成模板及对应主 skill 比对 |
| `.trae/skills/*/SKILL.md` | 5 | 全部阅读，对照主入口和脚本参数 |
| `vllm-ascend/.agents/skills/*/SKILL.md` | 3 | 全部阅读，检查与工作区规则的冲突 |
| 根目录、`vllm/`、`vllm-ascend/` 的 `AGENTS.md` | 3 | 全部阅读 |
| 合计 | **59** | **56 个 SKILL.md 文件，27 个独立技能** |

主 skill 共 3,491 行、208,005 字符；description 合计 10,090 字符，其中 18/24 超过 300 字符、7/24 超过 500 字符。10 个超过 150 行的入口占主 skill 正文及元数据总字符数的 78.2%。这些是实测文本量，不是 token 数，也不是性能或误触发率测量；300/500/150 仅用于筛查，不是建议新增的硬门槛。

快照记录 HEAD 为 `4342b1bbd1c4252d0714dcd8138ed379d8ae2b53`；后续复查时 HEAD 已前进到 `59ed1c33fa0d3383272d85ff8a9fb68f8fb272c7`，analysis/collection 两份入口有并发追加的共享存储归档说明。表中行数、哈希和发现定位保留原始快照，不代表 #73 的基线或复查后的完整内容。子模块分别为 `vllm@bcf2be96120005e9aea171927f85055a6a5c0cf6`、`vllm-ascend@b36dc06d8e1b914e7a1318ee32310ef1d502007a`。结论针对这些本地版本，不代表社区最新 main。

[快照文件清单](2026-09-07-skills-agents-inventory.csv) 记录每个文件的路径、SHA256、
行数、字符数和描述长度。[支持文件摘录](2026-09-07-skills-agents-evidence.json)
保留对实现/引用文件的相关原文。最初审计的显式 Markdown 本地路径均存在；
这不覆盖反引号裸路径、CLI 参数、锚点语义或客户端加载行为。支持脚本和
references 按疑点抽查，未对全部脚本进行功能或安全审计。下面是修改前的
发现；当前修复状态以上方处置表为准。

## 优先修正的行为问题

**F01 · P1：model-adapter 的前置清理没有进程归属边界。**

[入口第 14 行](https://github.com/vllm-project/vllm-ascend/blob/b36dc06d8e1b914e7a1318ee32310ef1d502007a/.agents/skills/vllm-ascend-model-adapter/SKILL.md#L14) 要求先读 checklist，而 [checklist 第 61 行](https://github.com/vllm-project/vllm-ascend/blob/b36dc06d8e1b914e7a1318ee32310ef1d502007a/.agents/skills/vllm-ascend-model-adapter/references/workflow-checklist.md#L61) 在每次重跑前执行 `pkill -f "vllm serve|api_server|EngineCore"`。它按名称匹配当前进程命名空间中的所有目标，没有 session、PID 身份或容器独占证明。在共享容器中可能终止别人的服务，也与工作区按 session 清理的规则冲突。应改为调用目标 session 的生命周期工具，或在非托管环境中验证具体 PID/容器归属后再停止。这里的 `git reset --hard` 示例另有“用户明确要求 reset”的前提，不应与无条件 pkill 混为一谈。

**F02 · P1：parity/serving 把验证假设所需的修改挡在“根因确认”之后。**

[parity 第 75–76 行](../../.agents/skills/remote-code-parity/SKILL.md) 要求同步前回退临时调试改动，失败后必须先从远端日志确认根因才能重新同步；[serving 第 226–233 行](../../.agents/skills/vllm-ascend-serving/SKILL.md) 也禁止根因未确认时改代码。遇到需要 instrumentation、候选补丁或 A/B 才能确认根因的问题，会形成循环前提，还可能丢弃用户希望在远端验证的改动。

应要求保存原始错误与原始配置、明确本轮假设和源代码快照，并允许在已授权的隔离运行环境验证候选改动。只有确认属于本任务且用户要求清理的临时改动才能回退；不要把“尚未证明是最终修复”解释成“不可同步”。保留运行中禁止覆盖代码、启动不确定时先确认进程状态的约束。

**F03 · P1：model-adapter 用固定交付清单覆盖用户任务范围。**

[第 46–47 行](https://github.com/vllm-project/vllm-ascend/blob/b36dc06d8e1b914e7a1318ee32310ef1d502007a/.agents/skills/vllm-ascend-model-adapter/SKILL.md#L46) 规定用户需求只能扩展默认特性集合；[第 108 行](https://github.com/vllm-project/vllm-ascend/blob/b36dc06d8e1b914e7a1318ee32310ef1d502007a/.agents/skills/vllm-ascend-model-adapter/SKILL.md#L108) 默认验证 `128k + bs16`；[第 111–140 行](https://github.com/vllm-project/vllm-ascend/blob/b36dc06d8e1b914e7a1318ee32310ef1d502007a/.agents/skills/vllm-ascend-model-adapter/SKILL.md#L111) 对已有模型调试也要求教程、测试配置、单一提交，并在 GitHub issue 发表评论。一次窄范围 loader 修复会因此被扩为全面适配和对外发布工作。

应拆分“已有模型故障修复”和“完整新模型交付”两个分支。测试矩阵由请求与代码影响确定；真实权重验证仍是声称加载链路成立的重要证据，但容量、EP/MTP/VL、教程和提交应有适用条件。对外评论必须有用户授权，不能由 skill 自行授予。

**F04 · P1：vllm-ascend 的常驻 NPU 示例含错误语义。**

[AGENTS.md 第 184–190 行](https://github.com/vllm-project/vllm-ascend/blob/b36dc06d8e1b914e7a1318ee32310ef1d502007a/AGENTS.md#L184) 把 `[t.item() for t in tensors]` 标作单次批量同步，并称 Python 的 `if max_value > threshold` 可以留在设备。列表推导仍逐次执行 `.item()`；在示例所示的普通 Python/eager 控制流里，`if` 需要把设备标量判为 Python bool。Tensor 的单元素布尔判定语义也可见 [PyTorch 文档](https://docs.pytorch.org/docs/2.14/generated/torch.is_nonzero.html)。这会把同步引入热路径或图相关代码。

应删除这两个“正确写法”示例，保留准确的提醒：需要 CPU 决策时显式控制读回位置；只有语义允许时才使用设备侧选择。不能把任意 Python 分支机械换成 `torch.where`。

## 路由、终态与契约问题

**F05 · P2：测试策略一端过严，另一端范围失控。**

[根 AGENTS.md 第 108 行](../../AGENTS.md) 从“Mac 不能运行 torch_npu”扩展为禁止一切本地测试；[子模块 AGENTS.md 第 105 行](https://github.com/vllm-project/vllm-ascend/blob/b36dc06d8e1b914e7a1318ee32310ef1d502007a/AGENTS.md#L105) 又要求请求 review 前跑所有本地测试，第 391 行附近还要求完整套件。与此同时，[remote-dev CI](../../.github/workflows/remote-dev.yml) 明确存在不访问 SSH/NPU 的可移植测试。

建议按依赖和副作用划界：控制面、文档、schema 与使用临时 fixtures 的测试允许本地执行；torch/torch_npu、算子和硬件推理转远端；验证范围按改动影响选择。上游贡献政策应保留，但通用的全量测试教程和重复检查表可移到贡献指南。初次只读审计未执行测试；本 PR 明确依赖边界后运行了上面列出的可移植检查。

**F06 · P2：direct endpoint、任务池和 legacy session 的边界没有收敛。**

[根 AGENTS.md 第 35 行](../../AGENTS.md) 把 direct endpoint 作为普通远端工作的首选，第 85 行引入任务池，第 107 行却概括为所有远端工作都必须在 session 内。更深处的 [model-adapter 第 23–27 行](https://github.com/vllm-project/vllm-ascend/blob/b36dc06d8e1b914e7a1318ee32310ef1d502007a/.agents/skills/vllm-ascend-model-adapter/SKILL.md#L23) 又固定绝对路径、直接起服务和端口 8000。

建议根文件只保留三条明确路由：已知 endpoint 的普通读写使用 remote-dev；任务池执行使用其自身绑定与租约；legacy domain wrapper 才要求 legacy session。由运行环境提供路径和已分配端口。已有 [serving 第 10–16 行](../../.agents/skills/vllm-ascend-serving/SKILL.md) 对 pool binding 不可冒充 session-id 的说明应保留。

**F07 · P2：profiling 的入口文档不能一致接受默认产物。**

- [analysis 第 80 行](../../.agents/skills/ascend-profiling-analysis/SKILL.md) 说明默认 fast 跳过 XLSX；[第 327 行](../../.agents/skills/ascend-profiling-analysis/SKILL.md) 却说没有 XLSX 必须失败。`profile_analyze.py` 实际按 mode/stage 选择必需产物，属于文档漂移。
- [analysis 第 33 行](../../.agents/skills/ascend-profiling-analysis/SKILL.md) 以缺少 CSV 为由拒绝分析，但 [collection 第 15 行](../../.agents/skills/ascend-profiling-collection/SKILL.md) 默认只输出 DB，单 root 的 source discovery 已支持 DB。
- 多 root 情况不止是文案：[sweep.py 第 45–85 行](../../.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/sweep.py) 仍仅发现 CSV。纯 DB capture 不会进入 sweep 的 root 列表。这是静态确认的实现覆盖缺口，未做远端复现。
- [collection 第 177–179 行](../../.agents/skills/ascend-profiling-collection/SKILL.md) 将这些失败统称为必须重新采集，但缺导出也可能只有 analyse 阶段失败，且同一 skill 提供了重跑 analyse 的入口。

建议用 `输入来源 × export mode × analysis mode × stage × 用户要求产物` 的共享契约统一入口与校验。先区分原始捕获缺失和导出失败；可恢复的导出走重分析。用户明确要 HTML 时，应选择能生成完整 HTML 的模式，并验证实际 HTML 状态和可用内容，不能把占位文件视作完成。

**F08 · P2：benchmark 与 performance-regression 都吸引同一类请求，但实验设计不同。**

[benchmark 第 18–19 行](../../.agents/skills/vllm-ascend-benchmark/SKILL.md) 同时接受前后对比和回退判断，第 39 行要求多状态走 `bench_compare.py`。该脚本外层遍历 state、内层跑多轮测量；[performance-regression 第 15–18、51–52 行](../../.agents/skills/vllm-ascend-performance-regression/SKILL.md) 则要求固定交替顺序，禁止先跑完 baseline 再跑 candidate。

建议：benchmark 负责单状态测量与显式请求的探索性状态比较；正式 A/B 回退判定交给 performance-regression，并由后者调用前者。保留两者各自的实现，不必为缩减数量而合并。明确比较结果的证据等级，避免把顺序跑出的均值差直接作为受控回退结论。

**F09 · P2：多处重复询问和固定停止点忽略已有授权。**

| 位置 | 问题 | 建议边界 |
|---|---|---|
| [repo-init:30、89–119、161–165](../../.agents/skills/repo-init/SKILL.md) | 每类修改都问，固定分组问题和二次自定义问题，即使请求已给出对应值 | 只询问无法从请求/持久配置确定的选择；保留自定义值校验和用户未选时不擅自改 remotes |
| [parity:170](../../.agents/skills/remote-code-parity/SKILL.md) | 首次状态 unset 必须问，与第 80 行已有明确授权可直接记录的规则表述不一致 | 先解释已有用户授权，再询问剩余歧义；首次替换镜像环境的身份边界保留 |
| [modelscope:42](../../.agents/skills/modelscope/SKILL.md) | 请求已要求补全/修复时，发现缺文件仍机械要求再问 | 缺失文件续传在既定模型、revision、目录内推进；覆盖现有文件等实际扩大范围的操作单独处理 |
| [PD serving:24–34](../../.agents/skills/vllm-ascend-pd-serving/SKILL.md) | 线性流程总以 stop 收尾，用户要求拉起服务也可能被停掉 | 按 plan/start/status/smoke/stop 分支；拉起请求以服务就绪并保留运行为终态，临时实验才做约定清理 |

PD 的外部 proxy 前置条件属于当前实现能力边界，可以保留；若用户要求端到端部署，需路由到 proxy 的实施方式，不能仅在前置条件处结束。其 [behavior:40](../../.agents/skills/vllm-ascend-pd-serving/references/behavior.md) 正确区分代理请求成功与 KV 传输证明，入口第 60–61 行也应同样明确要求后者有日志/指标佐证。

## 失效示例、陈旧知识和发现机制

**F10 · P2：若干入口示例无法按字面运行，或会查错证据。**

| 位置 | 已核实的问题 | 建议 |
|---|---|---|
| [Trae machine-management:13](../../.trae/skills/machine-management/SKILL.md) | 使用 `machine_verify.py --name`；解析器要求 `--machine` | 与主入口一致 |
| [Trae serving:15](../../.trae/skills/vllm-ascend-serving/SKILL.md) | start/status/stop 均传 `--machine`；当前解析器没有此选项 | 使用 session-id/session-file 或 cwd 绑定；probe 的 machine 例外保留 |
| [memory profiling:89–106](../../.agents/skills/ascend-memory-profiling/SKILL.md) | 上传器返回带 UUID 的 wrapper 路径；示例却传固定 `/tmp/_vaws_msprof_wrap.sh` | 捕获并使用上传器的实际返回值；实现见 [_common.py:218](../../.agents/skills/ascend-memory-profiling/scripts/_common.py) |
| [release-note-writer:10、23、28](https://github.com/vllm-project/vllm-ascend/blob/b36dc06d8e1b914e7a1318ee32310ef1d502007a/.agents/skills/vllm-ascend-release-note-writer/SKILL.md#L10) | 引用省略 `references/`、`scripts/`；还把不存在的 `commit-analysis-draft.csv` 称为工具 | 使用 skill-relative 路径，明确 CSV 是输出工作文件 |
| [release-note-writer:78](https://github.com/vllm-project/vllm-ascend/blob/b36dc06d8e1b914e7a1318ee32310ef1d502007a/.agents/skills/vllm-ascend-release-note-writer/SKILL.md#L78) | Ascend release 的 PR 默认查询 `vllm-project/vllm` | 保留实际来源仓库，按仓库+PR号取详情 |
| [main2main:21–27](https://github.com/vllm-project/vllm-ascend/blob/b36dc06d8e1b914e7a1318ee32310ef1d502007a/.agents/skills/main2main/SKILL.md#L21) | 把本地当前 HEAD 当“latest main”，没有核对分支或远端 ref | 固定实际目标 ref 和获取时间；不能用本地旧/dirty checkout 代替最新 main |

这些问题也说明：当前“Markdown 链接全存在”和 catalog 检查不足以证明使用路径可用。

**F11 · P2：历史经验被写成跨环境事实。**

[memory profiling 第 282 行](../../.agents/skills/ascend-memory-profiling/SKILL.md) 把 torch profiler 不产生 device 数据写成无版本前提的当前事实，和当前 collection 工作流支持的能力表述冲突。应记录观察时的版本、图模式和证据，不能推广为全部 Ascend 环境的限制。本次没有据此声称所有当前环境都已可用。

同一文件 [第 55 行](../../.agents/skills/ascend-memory-profiling/SKILL.md) 声称全部归因不含估算，却在第 38、243–266 行使用 config 兜底和分片规则推断；第 170 行把缺少 baseline 的固定开销显示为 0；第 281 行把 npu-smi 状态差称作捕获峰值。应分别标明测量、估计、未知和采样限制，不能从两次快照保证期间真实峰值。

machine-management/parity 中的 A3 已测镜像源与安装策略可以作为维护中的实现约束保留；应将详细版本与故障经验移到对应知识/行为文档，不能因为文章主张精简就直接放开未经验证的依赖变更。

**F12 · P2：客户端发现元数据与生成检查有盲区。**

24 个 Claude shim 都与生成器一致，但它们的第一行是 HTML 注释，`---` 在第二行。当前 [Claude Code 官方文档](https://code.claude.com/docs/en/skills#frontmatter-reference) 明确 frontmatter 的起始 delimiter 必须在第一行，否则整个文件作为正文处理。因此对应 YAML description 不会按元数据解析，影响按意图选 skill；这不是“24 个副本失步”，而是生成模板本身的问题。见 [sync_claude_skills.py:43](../../.remote-dev/tools/sync_claude_skills.py)。当前 tests 主要验证与同一模板相等和行数，没有独立检查实际发现语义。未启动 Claude 客户端做实测，因此不宣称技能完全不可用。

[release-note-writer 的 name](https://github.com/vllm-project/vllm-ascend/blob/b36dc06d8e1b914e7a1318ee32310ef1d502007a/.agents/skills/vllm-ascend-release-note-writer/SKILL.md#L2) 为带空格与大写的展示名，违反 [Agent Skills 命名规范](https://agentskills.io/specification#name-field)。展示名应与用于发现的标识分离。

Trae 的 ModelScope 是整包复制，当前 6 个文件与主包逐字节一致；其余 4 个 Trae 入口为手写 stub，已经出现参数漂移。建议同样生成薄入口。不要把跨客户端入口的同名视为同一客户端必然重复加载，也不要仅因数量多就删除它们。

## 入口减负方案

**F13 · P3：领域入口混入实现手册、历史说明和输出 schema。**

建议保留有区分度的 24 个主 skill，把 10 个较长入口按操作分支缩为“适用请求、完成标准、必要不变量、执行入口、条件引用”。无需统一行数上限。尤其 profiling analysis 的约 30,269 字符涵盖字段字典、报表章节、硬件系数、分类算法和维护知识，这些应放到对应 references/knowledge；分析使用者不需要每次读完整维护手册。graph-debug 的约百行 instrumentation 模板也宜单独存放。

根 AGENTS.md 的完整 24 项 skill 目录与客户端发现描述重复；远端工具字段表、legacy 参数表、共用 JSON 协议和历史规则可移到 `.agents/README.md` / remote-dev 导航。当前 [skill_catalog.py:184](../../.agents/scripts/skill_catalog.py) 要求多个文档重复列出全部技能，精简根目录时需同步调整校验器，避免用 tests 固化冗余目录。

审计时的触发描述草案（本 PR 的实际描述见各 SKILL.md；这里的 DB 示例属于 profiling 分支）：

```yaml
# ascend-profiling-analysis
description: Analyze existing Ascend profiler DB or CSV captures and produce evidence-linked reports; use for step, layer, operator, or cross-rank analysis.

# vllm-ascend-benchmark
description: Measure online-serving performance for one Ascend configuration; use performance-regression for controlled baseline/candidate verdicts.

# remote-code-parity
description: Synchronize the intended local source snapshot before remote execution that depends on it. Ordinary remote inspection needs no parity.
```

保留的约束包括：明确代码与硬件身份、进程归属、租约隔离、运行中不可替换代码、图内不做 CPU 读回、有效掩码与 dtype/stride 语义、正确性与性能证据分离、原始证据可追溯、真实权重与 dummy 的区别。它们有具体故障机制，不属于可随意删除的通用鼓励语。

## 逐项处置清单

下面 24 行一一覆盖主 skill；其同名 Claude 入口统一处理 F12。数字为入口文件总行数 / description 字符数。

| 主 skill | 行 / 描述字符 | 建议 |
|---|---:|---|
| ascend-memory-profiling | 289 / 397 | 修 wrapper 示例、证据等级和历史限制；拆 attach、完整采集、已有产物分析 |
| ascend-operator-debug | 57 / 438 | 保留短入口与原模型复验；缩短描述，保留 operator 归因边界 |
| ascend-profiling-analysis | 432 / 709 | 优先 F07；按 single/sweep/HTML/维护拆引用，字段 schema 外移 |
| ascend-profiling-collection | 225 / 594 | 区分采集与导出恢复；manifest schema 和模式参数外移 |
| ascend-triton-kernel-optimization | 44 / 574 | 保留 exact candidate 正确性门槛及噪声判断；缩短描述中的机制枚举 |
| ascend-triton-kernel-validation | 42 / 584 | 保留 fallback、完整 case 和 tolerance 约束；描述聚焦单 kernel 校验 |
| ascend-triton-operator-development | 43 / 567 | 保留首次正确实现与验证联动；语义审计、sketch 在复杂迁移时展开 |
| ascend-triton-workflow | 44 / 456 | 保留多阶段总入口；缩短描述，避免无关单阶段任务进入整套编排 |
| curate-workspace-knowledge | 56 / 530 | 保留显式触发策略和证据门槛；inspect/review 与 promote/commit 分支分开 |
| machine-management | 247 / 212 | 保留 add/verify/repair/remove；实现及版本细节外移，已有参数不重复询问 |
| modelscope | 84 / 271 | 结构基本可保留；修 F09，明确续传与覆盖边界 |
| npu-fleet-monitor | 54 / 309 | 保留定位/bootstrap 路由、只读查询与资源非预约语义；压缩 description |
| remote-code-parity | 337 / 388 | 优先 F02/F06/F09；快照/传输/安装实现说明外移 |
| remote-toolbox | 110 / 187 | 保留兼容后端定位；description 增加适用边界，按操作查命令 |
| repo-init | 198 / 224 | 去除重复确认；保留初始化子模块后才改其 remotes 的关键顺序 |
| session-management | 192 / 338 | 开头明确 task/pool/legacy 选择；历史 PR 与完整状态 schema 外移 |
| vllm-ascend-benchmark | 242 / 254 | 优先 F08；探索性比较与正式回退路由分开，presets/完整 CLI 外移 |
| vllm-ascend-change-validation | 53 / 487 | 保留最小充分证据原则；缩短 description，不为普通小改动强制引入编排 |
| vllm-ascend-correctness-validation | 73 / 522 | 保留小矩阵和不支持≠通过；source import 修复说明移行为文档 |
| vllm-ascend-distributed-debug | 58 / 428 | 保留跨 rank 证据；需要 profiling 才能解释当前 hang 时不要额外要求用户另提任务 |
| vllm-ascend-graph-debug | 233 / 404 | 保留分阶段诊断；快照模板外移，确定性开关按问题使用；统计量相同不能证明张量逐元素相同 |
| vllm-ascend-pd-serving | 62 / 442 | 优先 F09；区分操作终态、proxy 请求路径和 KV 传输证据 |
| vllm-ascend-performance-regression | 57 / 489 | 保留受控交替实验；预算耗尽或实验失败时也应能输出明确的未完成结论 |
| vllm-ascend-serving | 259 / 286 | 修 F02；区分 start/status/stop 的所需上下文，保留 lease 与真实请求 readiness |

| 其余文件/组 | 建议 |
|---|---|
| 根 AGENTS.md，112 行 | 修 F05/F06，保留必要导航与跨项目不变量；knowledge 查询限定实际领域故障，不用于所有用户交互 |
| vllm/AGENTS.md，113 行 | 保留重复 PR 检查、责任与署名政策；安装/全量 lint 示例移贡献文档，“trivial busywork 不继续”限定为对外 PR 范围 |
| vllm-ascend/AGENTS.md，417 行 | 先修 F04/F05；命名/魔数教程和重复 checklist 外移；架构 review 是提交评审要求，不应自动变为本地编辑前批准 |
| 子模块 main2main，277 行 | 修“latest”取值；模板和路径地图外移；兼容范围读当前 version policy，不无限扩展历史版本 |
| 子模块 model-adapter，140 行 | 优先 F01/F03/F06；工作区执行适配与模型语义规则分开 |
| 子模块 release-note-writer，79 行 | 修 F10/F12；保留用户影响分类，把多份强制 draft 和重复检查改为一次证据核对 |
| Claude 全部 24 个 shim，均 18 行 | 保留薄入口架构；修首行 frontmatter，并检查明确触发型 skill 的跨客户端策略一致性 |
| Trae machine-management，14 行 | 修 `--name`，改为生成入口 |
| Trae remote-code-parity，14 行 | 当前入口有效；与主入口同步收窄只读远端请求的触发条件 |
| Trae repo-init，14 行 | 当前入口有效；同步主入口授权边界 |
| Trae serving，28 行 | 修三个 `--machine` 示例，保留 probe 例外 |
| Trae modelscope，84 行 | 当前复制一致；改为引用主包，避免维护第二份实现 |

## 建议实施顺序与验收

1. 先修 F01–F04，以及 F10/F12 的可确定失效项；工作区和子模块改动分别管理。
2. 再统一 F05–F09 的测试、执行模式、产物和终态契约；先让文字与当前实现一致，再决定是否补齐实现。
3. 最后移动长篇细节并缩短描述，同步生成客户端入口与 catalog 校验；不为缩减 skill 数量牺牲合理的领域边界。

后续验证应选能区分行为的场景：只查远端日志无需新建 session 或同步；指定本地代码做远端假设实验可持续推进；用户要求 PD 拉起后保持运行；DB-only capture 可进入单 root/sweep 对应路径；fast 摘要不要求 XLSX、HTML 请求不以占位文件完成；已有模型窄修复不自动写 issue 评论；所有被清理 PID 均有归属证据。客户端应验证实际展示的 description，CLI 应验证示例参数被当前 parser 接受。这些是后续行为验证场景；本 PR 实际通过的检查以上方验证记录为准。
