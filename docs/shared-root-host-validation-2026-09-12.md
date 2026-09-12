# 共享开发机实测与主仓自动更新

Status: dated validation evidence — 2026-09-12

在用户指定的四台 Ascend 开发机上，通过已安装的 coordinator `0.4.1.dev1`
（提交 `0d0e814dc673677be62d2fa60e204552d57e22ce`）执行实际 SSH、容器和
宿主机队列测试。更新源同时改为官方默认分支，不要求发布 Release。
本次复用第一版本地测试证据，仅补受影响的更新测试和实机验证。

## 四台机器上的受管任务

四机已有固定用户容器均运行正常，namespace 与初始化用户相符。
由当前原生会话自动绑定用户，通过正常 `TaskClient.run` 提交四个 CPU 任务：
`sources={}`、`npu_count=0`，只指定此次验收的目标主机。
没有在任务中额外填写 user、容器名或解释器，没有创建新的用户容器。

四个任务均 `succeeded`，沿用现有镜像解释器，未安装 vLLM 或重建环境；
每个任务有独立 execution 目录，最终 `resources_released=true`。

四机均直接读取已有共享 NFS 权重目录中的同一个 DeepSeek-V2-Lite shard：
文件大小和 inode 一致，读取 safetensors 的 8 字节长度头成功。
没有下载、复制权重或计算全量哈希。这证明共享路径可访问和复用，
不等于完成模型加载或推理精度验收。

## 忙碌设备、留言与释放

实测时四机各 16 个 NPU 均被既有推理进程占用，PID/cgroup 对应到其他现存
容器。每台另提交一个请求 1 个 NPU 的短任务，均进入 `queued`，
role 的 `lease_state=queued`，没有获得设备。CPU 任务已能在此占用条件下完成。

留言经真实 SSH 写入各宿主机的现有 SQLite，发送方使用隔离测试身份，
收件方是本次原生会话。没有借用其他人的账号或向实际用户发送测试消息。
在四台机器分别验证：

- 消息持久写入、发送人和任务归属、线程与回复关系。
- 当前 Agent 通过正常 `status` 自动收到消息，直接使用返回的 reply reference 回复。
- 独立 peer 读取回复，使用返回游标再次读取为空；正常调用不重复交付同一消息。
- 收件期间采样的四次正常状态调用为 16–28 毫秒，没有等待 SSH 完成。

留言不会改变设备分配。最后只通过正常 execution stop 撤销本次四个排队
请求，均到达 `cancelled` 且 `resources_released=true`，没有停止其他人的服务。
本轮 peer 是协议测试身份；证据不声称有第二个真人完成 GitHub 初始化。

独立后验通过 binding 到宿主机请求的实际记录核对八个执行：四个 CPU 请求
为 released、四个排队请求为 cancelled，无对应活动请求或残留端口。
原用户容器、原工作负载容器的 ID/启动时间/PID 以及原有 64 个 NPU Worker
的 PID/cgroup 均保持一致。四机宿主机消息表已按既有增量迁移建立。

## 真实编译产物复用

在一台机器找到现有完整 native preparation，使用实际 package 的共享缓存
保存操作读取并校验它，原 donor 文件保持不变。保存耗时约 16.5 秒。

为消费方准备独立源码副本和自己的解释器，实际 restore 返回 `hit`，
恢复 1103 个已枚举文件，耗时约 20.1 秒。随后运行实际 profile capture 和
inspect，vLLM、vLLM-Ascend、C extension 和 ACL 的加载检查通过，
确认使用消费方目录中的产物；没有调用 native 编译步骤。
加载及 profile 检查约 28.8 秒，不把它当作完整编译速度对比。

同一已验证 bundle 和真实源码提交随后复制到其余三台机器，各自在新的任务
目录和解释器中 restore 命中，1103 个文件的实际加载/profile 检查也全部通过。
其中一次命令启动回执报错，但原命令已完成并留下通过的 smoke/profile；
读取保留证据后仅重试 inspect，没有重复传输、恢复或编译。
测试传输曾遇到跨文件系统 rename，改为在目标文件系统准备后重命名；
这两类初始失败记录均保留，生产 package 无需修改。

缓存没有按创建者设置权限或个人/公开类别；仍使用原有相关构建输入、镜像、
ABI 和 profile 检查。测试没有伪造通过结果、补造 donor 哈希，或改写原 donor
的旧 profile 来绕过兼容性。实际 NPU kernel 计算未执行，因为设备仍被占用。
本轮缓存实机测试直接调用 package 的保存/恢复和实际加载接口；普通受管
prepare 自动选择缓存、命中后省去 native 编译的调用顺序沿用第一版本地测试。
本次没有重新跑完整的 native 源码依赖安装和准备流程，也没有增加 reuse-only
参数或另一套人工缓存登记。

## 周期性跟随主仓

更新器默认约每五分钟查询官方仓库 default_branch 的最新提交。
真实查询得到 `main @ b63c78269549e0f16ae6c4554f0b47b5e774a3da`，
不再返回“没有稳定 Release”而等待。后台成功准备此提交的锁定依赖和 monitor
包，并确认个人 Fork 默认分支已到该提交。当前开发分支和运行环境保持原状。

本机只重启了已识别的旧更新 watcher，使常驻进程加载新的主仓跟随逻辑。
对应提交的 pyproject、lock、子模块 gitlink 和 monitor pin 决定配套组件；
重复发现同一提交复用已有准备结果。主仓继续前进时，下一轮准备新提交。
既有知识模型准备因本机代理依赖缺失处于 pending，按原有契约不阻塞更新准备
或普通工具，本轮没有扩大为知识组件修复。

更新测试覆盖无 tag/Release、默认分支持续前进、准备复用、个人 Fork 快进、
脏目录和业务分支保留。实测还修正了旧失败 reason 残留在 ready 状态中的
显示问题：当前状态与历史错误日志分开，原始日志仍保留。
更新器测试 35 项通过，先前通过的原生入口测试继续适用；实际 watcher 已跨
两轮五分钟检查复用同一准备结果，状态为 ready 且没有遗留失败原因。

## 证据与范围

完整主机清点、任务接收/状态/tail、留言往返、释放结果、缓存输入和加载日志
保存在未跟踪的 `.vaws-local/implementation/20260912-shared-root/four-host-validation/`；
公开报告不复制私有主机地址、个人容器名称和客户端路径。

本轮未做 NPU kernel 数值、模型 serving、吞吐或跨 SoC 兼容性验收；
没有用已有 NPU 工作负载的结果代替本次验证。实现与首次本地测试见
[第一版记录](shared-root-first-version-validation-2026-09-12.md)，当前行为见
[主仓更新](forks-and-updates.md)及[身份和协调](identity-and-agent-coordination.md)。
