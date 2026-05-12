# CCBD Ask Control Plane 收敛方案

## 1. 文档定位

这份文档定义 `ccb ask` / `ccbd submit` 控制面快路径与命令面去重的统一收敛方案。

目标不是继续通过更长 timeout、更多补偿状态、或额外 wrapper 去掩盖 submit timeout，而是把：

- `submit`
- `cancel`
- control-plane mounted probe

恢复成小而快、稳定可预测的控制面 fast path。

这份文档补充但不替代以下文档：

- `docs/ccbd-startup-supervision-contract.md`
- `docs/ccbd-lifecycle-stability-plan.md`
- `docs/ask-native-async-job-architecture.md`
- `docs/agent-message-timeout-retry-contract.md`

其中：

- `ask-native-async-job-architecture.md` 关注“ask 默认异步提交”的产品与作业模型
- 本文关注两件事：
  - `submit ack` 为什么会慢，以及控制面快慢路径如何拆开
  - 哪些命令入口只是同一事实的不同视图，应该合并或降级

本文合并了原 `docs/ccbd-command-surface-simplification-plan.md` 的内容，避免两份半重叠方案长期并存。

若后续实现出现以下倾向，则以本文为收敛目标：

- 把 submit timeout 当成 CLI 文案问题处理
- 继续在 `submit` 路径里同步跑 mailbox refresh / health check / runtime ensure
- 继续把 request response 与 maintenance 复用同一个串行 worker
- 先引入更复杂的 token / unknown 协议，而不先修复控制面执行模型

## 2. 事故与症状

### 2.1 事故现象

现场症状是：

- `ask helper` 客户端多次返回 `timed out` / `command_status: failed`
- 但服务端事实显示消息已经 accepted、入队，并最终 completed
- 用户据此重发后，helper 重复处理同一逻辑任务

### 2.2 现象分层

必须区分以下两类情况：

1. 真失败
   - 例如 `project ccbd is unmounted; run ccb first`
   - 消息未进入队列
   - 可安全重发

2. 伪失败
   - 客户端看到 `timed out`
   - 但服务端实际上已经写入 message / attempt / job
   - 后续任务照常被 helper 消费

第二类不是 mailbox queue 失败，而是 control-plane submit feedback 与真实 accepted 状态脱钩。

## 3. 当前根因

### 3.1 submit ack 理论边界

`submit` 理论上只应等待：

1. 请求被 `ccbd` 收到
2. sender / target 校验完成
3. job / message / attempt / inbound event durable append 完成
4. queue enqueue 完成
5. `SubmitReceipt` 写回 socket

它不应等待：

- agent 当前是否忙
- tmux pane 是否健康
- provider 是否正在长思考
- reply delivery
- mailbox summary 刷新
- deep health check
- runtime 恢复

### 3.2 submit path 自身混入了 maintenance

当前 submit path 不是纯 append-and-return。

最明显的同步慢路径是：

- `message_bureau.record_submission()`
- `refresh_mailbox()`
- `InboundEventStore.list_agent()`
- `JsonlStore.read_all()`

这意味着一次 submit ack 可能同步读取整个 agent inbox JSONL。

这类“派生 summary 刷新”不属于 acceptance receipt 所需边界，却直接挂在 submit fast path 上。

### 3.3 submit 前 compatible probe 走 deep ping

`ask` 提交前会先连 mounted daemon：

- `invoke_mounted_daemon()`
- `connect_mounted_daemon()`
- compatible probe
- `client.ping('ccbd')`

而当前 `ping('ccbd')` 会先执行：

- `health_monitor.check_all()`

这会把 tmux / provider / runtime health 检查混入 control-plane mounted probe。

所以用户看到的 submit timeout，不一定是 submit handler 本身慢，也可能是提交前的 compatible ping 已经被 deep health 拖慢。

### 3.4 request response 与 maintenance 共用单 worker lane

6.0.28 之后，socket server 已把：

- accept loop
- worker loop

做了分离。

但当前仍只有一个 request worker，且该 worker 串行承载：

- request handling
- post-request tick
- heartbeat
- runtime supervision
- dispatcher tick
- completion polling
- reply delivery preparation

因此，只要某次 maintenance 长跑占住 worker，后续 submit / ping / watch 的 response 都会排队。

### 3.5 single-target submit 可能同步 ensure_ready

当前 single-target ask 在 target runtime 非 active 时，可能在 submit 路径中同步进入：

- `RuntimeService.ensure_ready()`
- attach runtime
- load restore state
- restore runtime

这类恢复逻辑不属于接单确认，应从 submit path 剥离。

但当前实现与测试已经显式依赖一条语义：

- single-target submit 对 stopped agent 会 lazy restore

因此这条路径不能在没有替代接管点的情况下直接删除。  
后续必须先把 lazy restore 从 submit path 迁移到 queued job start / recovery lane，再移除 submit 同步 `ensure_ready()`。

## 4. 设计目标

本方案第一阶段目标固定为：

```text
submit / cancel / mounted probe
  = control-plane fast path

heartbeat / health / tick / recovery / reply preparation
  = maintenance slow path
```

关键约束：

- request worker 不应被 maintenance 长时间占住
- submit ack 必须只依赖最小 durable accept 边界
- mounted probe 不应同步执行 deep health
- mailbox summary 不应在 submit 时通过全量 `read_all()` 重算

## 5. 目标架构

### 5.1 request worker

request worker 只负责：

- socket request decode
- handler 执行
- response encode/write-back

它承载的 handler 必须保持 fast path 性质：

- bounded
- deterministic
- 无 deep maintenance

### 5.2 maintenance worker

maintenance worker 负责：

- heartbeat
- health check
- runtime supervision / reconcile
- dispatcher tick
- completion polling
- reply delivery preparation
- mailbox / summary maintenance

request worker 不再同步执行完整 maintenance。

### 5.3 dirty signal 而非 post-request 重维护

submit / cancel 等 mutating request 完成后：

- 只发 `maintenance_dirty` signal
- 不立即在 request worker 上跑完整 heartbeat / double tick

maintenance worker 自己在短预算内收敛。

## 6. Submit Fast Path 约束

### 6.1 ack 权威边界

`SubmitReceipt` 只代表：

- request 已通过 validation
- message / job / inbound event 已 durable append
- control-plane 已接管后续投递
- receipt 已生成并写回

`SubmitReceipt` 不代表：

- agent ready
- pane ready
- prompt sent
- execution started
- reply 即将到达
- mailbox summary 已刷新

这条边界必须固定；后续实现不得再把 runtime readiness、health、reply delivery 等语义回填进 submit ack。

### 6.2 允许的同步操作

submit fast path 只保留：

- sender / target 基础校验
- 从已加载 config / runtime state 做 routing
- durable append：
  - `JobRecord`
  - job event
  - `MessageRecord`
  - `AttemptRecord`
  - `InboundEventRecord`
- 最小 queue index 更新
- enqueue
- `SubmitReceipt`

The only allowed synchronous operations are validation, routing from already-loaded config/state, durable append, minimal queue index update, and receipt serialization.

### 6.3 禁止的同步操作

submit fast path MUST NOT perform:

- `refresh_mailbox()`
- 任何 `read_all()`
- deep health check
- runtime ensure / restore
- reply delivery 准备
- completion polling

### 6.4 延迟预算

submit handler 必须满足：

- handler latency 短且稳定
- 不依赖历史日志大小线性增长
- 不依赖 tmux/provider 当前健康状态
- 不依赖后台 maintenance 是否正在追赶

后续实现与观测应以此为 fast path latency budget 的判断基线。

## 7. Slow Path / Maintenance 约束

### 7.1 dirty signal 与合并

mutating request 完成后：

- 只发 `maintenance_dirty` signal
- dirty signal 可以 coalesce
- 不要求一条 request 对应一轮完整 maintenance

### 7.2 bounded maintenance pass

maintenance pass 必须：

- bounded
- 可跳过
- 可延期
- 可 backoff

heartbeat / health / reconcile 不能以“必须本轮完成”为前提占住 request response。

### 7.3 slow path 不得反向阻塞 fast path

slow path 失败或落后：

- 不影响已经 accepted 的 submit receipt
- 不得通过全局锁反向阻塞 response worker
- 不得因为 tmux/provider 深检查未完成而推迟 fast path response

即使采用 request worker + maintenance worker，也不得再通过共享大锁把它们重新串回一条同步路径。

## 8. Ping 语义收敛

### 8.1 light ping

control-plane mounted probe 只需要：

- lease/lifecycle state
- config signature
- socket/generation 基础事实
- cached runtime / namespace 摘要

它不应主动触发 deep health。

### 8.2 deep doctor

tmux / provider / runtime 的深检查应走：

- `doctor`
- 明确的 deep ping
- maintenance worker 周期刷新

而不是成为 `connect_mounted_daemon()` 的前置同步成本。

## 9. Mailbox / State 视图收敛

### 9.1 authoritative ledger vs derived summary

必须明确区分：

- authoritative ledger
  - job / message / attempt / inbound event
- derived summary
  - mailbox head
  - queue depth
  - pending reply count
  - last started / finished

authoritative ledger 是运行真相。
derived summary 可以 eventual，但不能反向定义 ledger。

运行控制依赖的 mailbox 视图必须从“每次重扫历史”改为“增量事实”。

目标：

- mailbox head
- queue depth
- pending reply count
- last started / finished

都通过增量索引或异步维护得到。

### 9.2 一致性与顺序约束

快慢路径解耦后，仍必须保持：

- submit durable append 顺序稳定
- 同一 agent 的 queued job 顺序稳定
- deferred reconcile 不得重排 request queue 语义
- `pend/trace/queue` 必须明确各自读的是 ledger 还是 summary

不能为了让 fast path 变快而破坏队列顺序或 authority 来源。

全量历史扫描只保留给：

- trace
- doctor
- 事故排查

不再进入 submit / ping fast path。

## 10. 可观测性与预算

后续实现至少应能观测：

- submit handler latency
- request queue wait before handler start
- light ping latency
- maintenance tick duration
- mailbox refresh / `read_all()` duration
- tmux health duration
- maintenance dirty backlog

没有这些指标，后续无法证明 fast path 已经恢复。

## 11. 分阶段实施

### Phase 1：Fast Path 收窄

目标：

- `ping('ccbd')` 从 deep health 改为 light ping
- submit path 移除 `refresh_mailbox()`
- submit 后同步 maintenance 从 double tick 收窄为 single tick
- 保持现有协议不变

这是第一阶段止血，也是必须先做的架构收敛。

### Phase 2a：Shutdown / Stop-All Contract

目标：

- 明确定义 `shutdown` / `stop_all` 的 response-before-teardown contract
- 明确定义 shutdown 时 pending maintenance 的处理语义
- 明确定义 request worker / maintenance worker / socket accept path 的停机 ownership

约束：

- `shutdown` / `stop_all` 必须 bounded
- 不等待长 maintenance 完成
- 可以丢弃 pending maintenance
- 必须先关闭 accept path，再有界停止 worker
- `stop_all` finalize 完成后，runtime 不得再从 `STOPPED` 回到 `IDLE`
- `stop_all` finalize 完成后，running job 必须 terminalize，不得在下一代 `restore_running_jobs()` 中继续恢复

### Phase 2b：Background Maintenance Fail-Fast

目标：

- background maintenance 进入 namespace / tmux / runtime recovery 时必须 fail-fast
- expected health/tmux/provider failure 记录为 degraded，不无限阻塞 shutdown
- uncaught maintenance loop bug 才视为 fatal

约束：

- background maintenance 不得进入长 tmux wait
- shutdown 期间 maintenance failure 不得阻塞退出
- namespace ensure/reflow 在 background maintenance 下必须走 non-blocking / `timeout_s=0.0` 语义

### Phase 2c：Stop-All State Gate

目标：

- `stop_all` finalize 之后，runtime state 必须稳定停在 `STOPPED`
- `stop_all` finalize 之后，running job 必须 terminalize
- 后续 maintenance / recovery 不得把已停机 runtime 再推回 `IDLE`

约束：

- 这一步必须先于真正的 request / maintenance lane 分离完成
- 如果没有这个 gate，lane split 会继续撞回同一个语义坑

### Phase 2d：Request / Maintenance Lane 分离

目标：

- request worker 专职 request/response
- maintenance worker 专职 heartbeat / health / tick / recovery
- submit/cancel 后只打 dirty signal

前置约束：

- `stop_all` 的 finalize ownership 必须先定义清楚
- lane 分离前必须明确：runtime stop、job terminalize、shutdown report、socket teardown 分别由哪一侧负责
- 未先定义这条 ownership，lane 分离会把 runtime/job state 再次撞回 `IDLE` 或留下未终结作业

### Phase 2.5：lazy restore handoff

目标：

- 保留 single-target submit 的现有 lazy restore 语义
- 但把 stopped/missing runtime 的恢复从 submit path 迁移到 queued job start / recovery lane
- 在接管点明确之前，不直接删除 submit 同步 `ensure_ready()`

### Phase 3：Mailbox 增量索引

目标：

- mailbox summary/head 不再依赖 `read_all()`
- queue/control 面读增量事实
- trace/doctor 保留全量历史入口

### Phase 4：协议层再评估

只有在前三阶段完成后，若仍存在不可接受的 accepted-but-unconfirmed 窗口，才评估：

- submit token
- idempotent submit
- `acceptance_unknown`

协议扩展不是第一阶段前提。

## 12. 测试计划

### 12.1 Fast path 测试

- submit 不因 mailbox summary 读取而阻塞
- mounted probe 不触发 deep health
- submit 后同步 maintenance 不再双 tick
- single-target submit 的 lazy restore 合同在 dedicated handoff phase 前保持不变

### 12.2 Scheduling 测试

- maintenance worker 长跑时，submit response 仍可快速返回
- heartbeat / tick 被 defer 时，不影响后续 submit ack

### 12.3 Regression 测试

- helper 忙时连续 ask 仍可稳定 accepted
- 真失败仍可正确报错
- queue / inbox / trace 语义不回退

## 13. 非目标

本方案第一阶段明确不做：

- 修改 `ask` 对外语义
- 引入 `client_submission_token`
- 增加 `acceptance_unknown`
- 通过调大全局 timeout 掩盖问题
- 把 `pend` 提升为 ask 主链路前置动作或主命令

## 14. 命令面收敛

### 14.1 入口角色

推荐先按入口角色收敛，而不是按命令名硬删。

建议分成四类：

| 类别 | 命令 | 建议 |
|---|---|---|
| 主链路 | `ask` | 保留，作为唯一主通讯入口 |
| 次级观察 | `pend` | 降级为补充结果/状态查询，不平级宣传 |
| 观测视图 | `queue`, `inbox`, `watch` | 合并/降级，不平级宣传 |
| 高级排障 | `trace`, `doctor`, `ps`, `logs` | 保留，但放到 diagnostics 层 |
| 恢复/维护 | `ack`, `resubmit`, `retry` | 降级为高级操作，合并语义 |

### 14.2 同一事实的不同视图

#### `pend` / `inbox` / `queue` / `watch`

这四个高度重叠，都是在看：

- message 是否入队
- agent 当前是否忙
- job 是否 completed
- reply 是否 ready
- mailbox 是否还有 pending item

应统一到一个底层 read model：

```text
conversation/job/mailbox status view
```

然后不同命令只是过滤/展示方式。

建议：

- `ask` 是用户主入口，默认承担第一手提交反馈
- `pend` 不是主入口，只作为补充结果/状态查询
- `watch` 变成 `pend --watch` 或 `ask --wait` 的内部模式
- `inbox` 合并进 `pend --inbox` 或 `queue --agent`
- `queue` 变成观测入口，只看 backlog，不作为主通讯入口

#### `trace` / `pend` / `queue`

这三个也看同一条事实链，只是深度不同：

```text
submission -> message -> attempt -> job -> reply -> mailbox event
```

建议：

- `pend`：用户级摘要
- `queue`：队列级摘要
- `trace`：完整 lineage / ledger 级排障

#### `doctor` / `ps` / `logs`

这三个都是 runtime diagnostics：

- `doctor`：综合诊断
- `ps`：进程 / 会话 / agent runtime 视图
- `logs`：文件证据视图

建议：

- `doctor` 是主诊断入口
- `ps` 是兼容 alias；收敛子视图为 `doctor ps`
- `logs` 是兼容 alias；收敛子视图为 `doctor logs`

#### `ack` / `resubmit` / `retry`

这三个都是恢复动作，但语义容易混：

- `ack`：确认已读 / 已处理某个 reply
- `retry`：同一个 job / attempt 失败后重跑
- `resubmit`：从原 message 创建新 submission

建议放入统一的恢复视图或 `repair` 组。

### 14.3 最小去重顺序

优先收敛这三组：

1. `pend` / `inbox` / `watch`
2. `doctor` / `ps` / `logs`
3. `ack` / `resubmit` / `retry`

理由：

- 第一组是用户最容易误用的“结果确认”入口
- 第二组是重复最多的 diagnostics 面
- 第三组是高危恢复动作，应该最先降级为 advanced

### 14.4 推荐主文案

主文案建议尽量只让用户记住：

```text
ask    发
doctor 查问题
```

补充观察入口：

```text
pend   查补充状态/结果
```

如果需要恢复：

```text
repair 修复/重试
```

### 14.5 合并原则

1. 同一事实，只保留一个 authoritative collector
2. 多个命令可以有不同展示格式，但不能各自重算事实
3. 主入口优先表达用户意图，不优先表达内部存储细节
4. 平级命令数量越少越好
5. 兼容 alias 可以保留，但不进入主文案

### 14.6 命令迁移矩阵

| 当前命令 | 最终角色 | 建议收敛方式 |
|---|---|---|
| `ask` | 主链路 | 保留为唯一主通讯入口 |
| `pend` | 次级观察 | 保留，但降级为补充结果/状态查询 |
| `watch` | 次级观察 | 收敛为 `pend --watch` 或 `ask --wait` 模式 |
| `inbox` | 次级观察 | 收敛为 `pend --inbox` 或 `queue --agent`，保留兼容 alias |
| `queue` | 观测视图 | 保留 backlog 视图，不再承担结果确认 |
| `trace` | 高级排障 | 保留为 lineage / ledger 级排障入口 |
| `doctor` | 主诊断 | 保留为诊断主入口 |
| `ps` | 高级排障 | 收敛为 `doctor ps`，保留兼容 alias |
| `logs` | 高级排障 | 收敛为 `doctor logs`，保留兼容 alias |
| `ack` | 恢复/维护 | 降级为 `repair ack` 或 advanced recovery |
| `retry` | 恢复/维护 | 降级为 `repair retry` 或 advanced recovery |
| `resubmit` | 恢复/维护 | 降级为 `repair resubmit` 或 advanced recovery |

### 14.7 最终帮助文案目标

最终主帮助建议只高亮：

```text
ask      Send a request
doctor   Diagnose runtime / control-plane
```

补充观察入口：

```text
pend     Check extra status / latest result
```

高级入口：

```text
queue    Inspect backlog
trace    Trace lineage
repair   Recovery operations
```

## 15. 一句话结论

当前 ask submit timeout 的最像根因不是 mailbox queue，也不是 helper 长任务，而是：

> `ccbd` 把 submit/ping 这类 control-plane fast path，与 heartbeat/health/runtime recovery/mailbox refresh 这类 maintenance slow path 混在同一个同步执行模型里。

第一阶段正确修复方向是：

> 先把 control-plane fast path 变小、变快、变稳定，再考虑是否需要协议层幂等扩展，同时把命令面收敛到少数几个稳定角色。

## 16. 执行表

### 16.1 Phase 执行顺序

| Phase | 目标 | 主要动作 | 预期收益 | 风险 |
|---|---|---|---|---|
| P0 | 建立观测基线 | 为 submit/ping/maintenance 增加延迟与队列等待指标；保留现有行为 | 能证明后续优化是否生效 | 低 |
| P1 | 收窄 fast path | `ping('ccbd')` 改 light ping；submit 移除 `refresh_mailbox()`；submit 后同步 maintenance 从 double tick 收窄为 single tick | 直接降低 submit ack timeout | 中 |
| P2a | shutdown / stop_all contract | 明确 response-before-teardown、pending maintenance discard、worker stop ownership | 避免 lane 分离时 shutdown 语义反复漂移 | 中 |
| P2b | background maintenance fail-fast | namespace/tmux/recovery 后台路径改为 fail-fast、bounded、degraded-aware | 避免 maintenance 线程在 shutdown 时卡进长等待 | 中高 |
| P2c | stop_all state gate | `stop_all` finalize 后 runtime/job state 不回退，避免后续 maintenance 把状态撞回 `IDLE` | 给 lane 分离提供稳定停机边界 | 中 |
| P2d | 拆 request / maintenance lane | request worker 只做 request/response；maintenance worker 处理 heartbeat/health/tick/recovery；mutating request 只打 dirty signal | 避免 maintenance 卡住 control-plane ack；前提是 stop_all state gate 已定义 | 中高 |
| P2.5 | lazy restore handoff | 把 stopped/missing runtime 的 lazy restore 从 submit path 迁到 queued job start / recovery lane | 解除 submit 对 runtime recovery 的同步依赖 | 中高 |
| P3 | mailbox 视图增量化 | mailbox head/summary 改增量索引或异步维护；`read_all()` 退出控制面热路径；详细方案见 `docs/ccbd-p3-p4-mailbox-cli-plan.md` | 彻底消除日志历史增长对 ack 的影响 | 中 |
| P4 | 命令面收敛 | observer group / diagnostics group / recovery group 收敛；详细方案见 `docs/ccbd-p3-p4-mailbox-cli-plan.md` | 减少认知负担和误用 | 中 |
| P5 | 协议层再评估 | 仅在前三阶段后仍存在 accepted-but-unconfirmed 窗口时，再评估 token/idempotency | 避免过早增加协议复杂度 | 中 |

### 16.2 每阶段可交付物

| Phase | 可交付物 | 验收标准 |
|---|---|---|
| P0 | 指标埋点 + 基线报告 | 能看到 submit handler latency、queue wait、maintenance tick duration |
| P1 | fast path 精简代码 + 测试 | submit/ping 路径不再同步 deep health / mailbox refresh；submit 后同步 maintenance 不再双 tick |
| P2a | shutdown / stop_all 合同实现 | `shutdown` / `stop_all` response bounded，teardown 顺序稳定，且 stop_all finalize 后 runtime/job state 不回退 |
| P2b | background maintenance fail-fast 实现 | background maintenance 不再进入长 tmux wait，shutdown 不被 maintenance 卡住 |
| P2c | stop_all state gate 实现 | `stop_all` finalize 后 runtime/job state 不回退 |
| P2d | request/maintenance lane 分离实现 | maintenance 长跑时，submit response 仍能快速返回，且 stop_all 不会再把 runtime/job state 撞回错误状态 |
| P2.5 | lazy restore handoff 实现 + 测试 | single-target submit 的 lazy restore 语义保留，但恢复接管点不再位于 submit path |
| P3 | mailbox 增量 summary 机制 | mailbox backlog 增长不再线性拖慢 submit；summary 写入 ownership 与版本/CAS 规则见 `docs/ccbd-p3-p4-mailbox-cli-plan.md` |
| P4 | CLI 帮助文案、parser alias、render 收敛 | 主帮助只突出 `ask` / `doctor`；observer group 弱化与后续命令面收敛见 `docs/ccbd-p3-p4-mailbox-cli-plan.md` |
| P5 | 协议扩展设计或“不需要扩展”的结论 | 仅在结构优化后仍有必要时推进 |

### 16.3 模块改动表

| 目标 | 主要模块 | 预期改动 |
|---|---|---|
| light ping | `lib/ccbd/handlers/ping_runtime/handler.py` | 去掉默认 `health_monitor.check_all()`，改读 cached summary |
| compatible probe | `lib/cli/services/daemon.py` | mounted probe 只依赖 light ping，不触发 deep health |
| submit 去 mailbox refresh | `lib/message_bureau/facade_recording_submission.py` | `record_submission()` / `record_retry_attempt()` 不再同步 `refresh_mailbox()` |
| mailbox refresh 异步/增量 | `lib/mailbox_kernel/service_runtime/mailbox.py`, `lib/mailbox_kernel/store.py`, `lib/message_bureau/facade_state.py` | 设计增量 head/summary 或后台刷新机制 |
| submit maintenance 减负 | `lib/ccbd/socket_server_runtime/server.py`, `lib/ccbd/socket_server_runtime/loop.py` | submit 后同步 maintenance 从 double tick 收窄为 single tick |
| lazy restore handoff | `lib/ccbd/services/dispatcher_runtime/routing.py`, `lib/ccbd/services/runtime.py`, `lib/ccbd/services/dispatcher_runtime/lifecycle_start_runtime/*` | 保留 stopped agent lazy restore 语义，但把恢复从 submit path 后移 |
| shutdown / stop_all contract | `lib/ccbd/app_runtime/lifecycle.py`, `lib/ccbd/handlers/shutdown.py`, `lib/ccbd/handlers/stop_all.py`, `lib/ccbd/socket_server_runtime/protocol.py`, `lib/ccbd/socket_server_runtime/loop.py` | response-before-teardown，两阶段 stop contract |
| background maintenance fail-fast | `lib/ccbd/app_runtime/policy.py`, `lib/ccbd/supervisor_runtime/namespace.py`, `lib/ccbd/services/project_namespace_runtime/*` | 后台 namespace/tmux/recovery 走 non-blocking bounded probes |
| stop_all state gate | `lib/ccbd/app_runtime/lifecycle.py`, `lib/ccbd/handlers/stop_all.py`, `lib/ccbd/services/runtime.py`, `lib/ccbd/services/dispatcher.py` | `stop_all` finalize 后 runtime/job state 不回退，作为 lane 分离前置门禁 |
| maintenance lane 分离 | `lib/ccbd/socket_server_runtime/loop.py`, `lib/ccbd/app_runtime/lifecycle.py`, `lib/ccbd/services/dispatcher.py` | request worker 只做 request/response；maintenance worker 处理 tick/health/reconcile |
| command surface 收敛 | `lib/cli/router.py`, `lib/cli/parser_runtime/commands.py`, `lib/cli/phase2.py`, `lib/cli/render.py` | 主帮助、parser、dispatch、render 统一到 `ask/doctor/pend/repair` 结构 |
| diagnostics 子视图 | `lib/cli/services/doctor*`, `lib/cli/services/ps.py`, `lib/cli/services/logs.py` | `ps` / `logs` 收敛为 `doctor` 子视图或 alias |
| watch/inbox 降级 | `lib/cli/services/watch.py`, `lib/cli/services/inbox.py`, `lib/cli/services/pend.py` | `watch` / `inbox` 变成 `pend` 的模式或兼容 alias |
| recovery 分组 | `lib/cli/services/ack.py`, `lib/cli/services/retry.py`, `lib/cli/services/resubmit.py` | 降级为 `repair` 语义或 advanced recovery |

### 16.4 推荐施工顺序

这个顺序描述的是“从现在往后”的推荐动作，而不是已经完成阶段的历史回放。

1. 先把状态认定收口
   文档和任务拆解统一到：
   - `P2c` complete
   - `P2d` partial
   - `P2.5` partial

2. 然后完成 `P2.5`：lazy restore handoff
   从 submit path 拿掉 stopped/failed single-target 的同步
   `ensure_ready()`，把恢复接管点后移到 queued job start / recovery lane。

3. 同步做 `P2d` completion hardening
   不是重新设计双 worker，而是围绕已经存在的 request worker /
   maintenance worker 拆分继续做约束和回归：
   - request worker 保持 bounded request/response
   - maintenance worker 继续独占 post-request ticks / after-response actions
   - 新 maintenance/recovery 逻辑不得重新漂回 request path

4. 保持 focused 回归 watchlist
   尤其是：
   - provider-driven terminal progression
   - stop_all / restart restore 边界
   - runtime authority / start-flow attach focused suites

5. 只有在 `P2.5` 和 `P2d` 都稳定后，再评估是否需要新的 `P5`
   级协议复杂化
   也就是 token / idempotency，不抢先上。

### 16.5 验收清单

| 验收项 | 标准 |
|---|---|
| submit ack | 连续 ask 时，helper 忙不导致 submit timeout |
| mounted probe | `connect_mounted_daemon()` 不因 deep health 被拖慢 |
| maintenance | heartbeat/health/tick 长跑不阻塞 request response |
| mailbox | inbox 历史增长不线性拖慢 submit |
| CLI 心智 | 主帮助只突出 `ask` / `doctor`，`pend` 降级 |
| 恢复入口 | `ack/retry/resubmit` 不再和主命令平级宣传 |

## 17. Current Status

### 17.1 Completed

- `P0` complete:
  - control-plane timings
  - ping/doctor diagnostics
- `P1` complete:
  - light ping
  - submit path no longer refreshes mailbox synchronously
  - submit post-maintenance already reduced from double tick to single tick
- `P2a` mostly complete:
  - `shutdown` / `stop_all` response-before-teardown contract landed
  - after-response action plumbing landed
- `P2b` substantially complete:
  - background maintenance fast-probe timeout now propagates through namespace maintenance paths
  - focused backend/supervisor/project-namespace tests pass
- `P2c` complete:
  - `stop_all` finalize 后 runtime/job state 不回退
  - provider-driven terminal progression focused suites are stabilized
  - mount-attempt authority reset and `mount_superseded` semantics are landed
- `P2d` partially complete:
  - request worker / maintenance worker separation is landed
  - after-response actions are owned by maintenance lane
  - mutating requests already queue post-request maintenance instead of running
    full maintenance inline on the request worker
  - post-request maintenance dirty signals now coalesce instead of behaving
    like a counted work queue
  - maintenance pending state is now guarded by a socket-server lock, and
    after-response actions explicitly wake the maintenance lane
  - maintenance worker startup no longer depends on a periodic tick callback,
    so after-response finalizers always have a lane-owned executor
  - recovery mutators such as `retry` and `resubmit` now also queue the
    coalesced maintenance dirty signal after the response
- `P2.5` partially complete:
  - single-target submit no longer synchronously restores
    missing/stopped/failed runtimes inline before receipt
  - queued job start / recovery lane now takes over `ensure_ready()` for
    missing/stopped/failed single-target work
- `P3` complete:
  - mailbox summary authority / head-path migration / routine observer
    de-historyfication / summary CAS / diagnostics separation are landed
  - routine observer reads no longer silently heal or projection-fallback when
    mailbox summary authority is missing
- `P4` complete:
  - primary command surface is now centered on `ask` and `doctor`
  - converged weak observer entrypoint is `pend`
  - converged advanced recovery entrypoint is `repair`
  - legacy top-level observer/diagnostics/recovery aliases remain compatible
    but are no longer part of primary help

### 17.2 Remaining P2 Work

The remaining `P2` work is no longer broad architecture exploration. It is now
split into:

- `P2d completion`: tighten and validate the lane split now that the dual-worker
  structure exists
- `P2.5 completion`: finish the lazy restore handoff and validate the remaining
  edge cases

Most important remaining implementation gaps:

- lane separation has landed structurally, but the plan has not yet been
  rewritten around the new "already split, still needs hardening" state
- stopped/failed single-target restore now happens in the start/recovery lane,
  and missing-runtime handoff now follows the same path, but remaining edge
  cases still need wider regression coverage and final contract cleanup
- future maintenance/recovery work should be audited against the now-real
  request/maintenance split instead of being tracked as if lane separation had
  not started

### 17.3 Next P2 Work Focus

Prioritize these two follow-up tracks:

1. `P2d completion`
   - keep request worker bounded to request/response handling
   - keep maintenance worker ownership of queued post-request ticks and
     after-response actions
   - keep post-request maintenance as a coalescing dirty signal rather than a
     per-request counted maintenance backlog
   - keep maintenance pending state lock-protected so request and maintenance
     workers cannot clear each other's wakeup state
   - keep after-response finalizers runnable even when a server instance has no
     periodic tick callback
   - keep the mutating-op dirty-signal set aligned with request guard recovery
     mutators such as `retry` and `resubmit`
   - audit future maintenance additions so they do not silently drift back into
     request-path blocking
2. `P2.5 lazy restore handoff`
   - preserve current lazy restore semantics now that restore ownership has
     moved to the queued job start / recovery lane
   - widen regression coverage around missing/stopped/failed single-target
     recovery and any restore-state-missing edge cases

Regression suites that should stay in the watchlist while doing this work:

- `test_ccbd_socket_codex_protocol_turn_completes_via_tracker`
- `test_ccbd_socket_codex_protocol_turn_handles_interrupted_abort`
- `test_ccbd_socket_roundtrip_and_shutdown`
- `test_ccbd_stop_all_does_not_run_post_shutdown_heartbeat`
- `test_ccbd_stop_all_force_terminalizes_running_jobs_before_restart_restore`
- `test_ccbd_start_flow_writes_runtime_authority_via_rpc`

Current interpretation:

- if a test still assumes immediate inline state progression, migrate the test
  to wait for background convergence
- if a provider-driven terminal path still leaves jobs in `accepted`, treat it
  as a regression against the already-landed `P2c` boundary set
- if a stopped single-target path regresses back to submit-path
  `ensure_ready()`, treat it as a `P2.5` regression rather than a `P2c`
  blocker

### 17.4 Newly Landed P2c Boundaries

The current branch has now landed the missing authority reset:

- daemon-owned mount authority is attempt-scoped via
  `runtime.mount_attempt_id`
- mount success/failure finalize uses compare-and-swap semantics and records
  `mount_superseded` instead of writing stale failure back into authority
- daemon-owned provider-session attach during start-flow respects the same
  mount-attempt boundary, so stale mount work cannot overwrite a newer external
  attach during the attach phase
- app-maintenance no longer proactively creates a missing runtime unless
  persisted `start-policy` authority exists for the current project run
- externally attached actionable runtimes are no longer daemon-adopted into
  `provider-session` authority just to stamp daemon generation

These boundaries are what stabilized the focused Codex socket suites and the
repeated `tracker` / `turn_aborted` loops.

### 17.5 Concrete Next Steps

Next implementation order should now be:

1. keep the phase table and status tracking aligned around:
   - `P2c` complete
   - `P2d` partial
   - `P2.5` partial
   - `P3` complete
   - `P4` complete
2. use `docs/ccbd-lifecycle-test-plan.md` section 11 as the next verification
   driver for the `v6.0.29` follow-up branch
3. finish `P2d` / `P2.5` by proving the already-landed split and lazy restore
   handoff under stress, not by reintroducing submit-path restore work
4. keep the current focused socket/stop-all suites in the regression loop so
   lane ownership does not drift backward
5. extend real Linux / macOS / WSL verification before declaring the ask/ccbd
   stabilization branch release-ready

The concrete real-test matrix is now tracked in
`docs/ccbd-lifecycle-test-plan.md` section 11. That matrix is the authoritative
next-step checklist for proving that submit ack stability, provider-driven
terminal progression, mailbox observer weakening, macOS tmux behavior, and WSL
socket relocation all satisfy the design intent.
