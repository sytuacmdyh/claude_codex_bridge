# CCBD 生命周期稳定性测试方案

## 1. 文档定位

这份文档是 `docs/ccbd-lifecycle-stability-plan.md` 的独立测试执行方案。

它不重新定义架构 authority，而是将生命周期稳定性设计转化为可执行测试计划、阶段门禁、长稳验证和 CI 接入要求。

若架构设计与本测试方案冲突，以以下文档为准：

- `docs/ccbd-startup-supervision-contract.md`
- `docs/ccbd-lifecycle-stability-plan.md`

## 2. 测试目标

测试计划必须证明两件事：

1. project lifecycle authority 是唯一且稳定的
2. provider helper ownership 是有界且可回收的

因此测试不能只验证“功能能跑通”，还必须验证：

- 同项目不存在双 keeper / 双 backend generation
- `mounted` 不会早于 readiness 发布
- 冷启动事务预算不会被误实现为“每次都固定等待到 timeout”
- 显式 `kill` 永远能收口到 `unmounted`
- helper/bridge 数量对 slot 数有上界，不会随 ask 次数无界增长
- 旧 generation 与 orphan residue 不会破坏当前 authority

## 3. 单元测试计划

### 3.1 核心覆盖

必须覆盖：

- lifecycle phase 转移
- keeper 持锁与并发等待
- readiness 前后 mounted 语义
- startup stage / progress / deadline 语义
- startup transaction timeout 与 generic RPC timeout 分层
- generation fence 拒绝旧实例写 authority
- helper manifest 序列化与回收策略

### 3.2 细分主题

建议新增或收口的测试主题：

- `lifecycle.json` 初始创建、读写、迁移、schema 校验
- `desired_state` 与 `phase` 的合法转移矩阵
- `startup_id` / `generation` 不匹配时的拒绝逻辑
- `startup_transaction_timeout_s` 作为 ceiling 时的提前返回逻辑
- `startup_progress_stall_timeout_s` 对卡死阶段的提前失败逻辑
- old generation 对 `lease`、socket cleanup、namespace cleanup 的写保护
- helper manifest 与 runtime generation 的一致性校验
- 普通 runtime state write 试图改写 authority 字段时必须被拒绝
- helper manifest writer 不得回退到 `binding_generation`
- orphan sweeper 的选择器只命中项目内 stale helper group

### 3.3 建议文件组织

- `test/test_v2_lifecycle_models.py`
- `test/test_v2_lifecycle_store.py`
- `test/test_v2_lifecycle_transactions.py`
- `test/test_v2_socket_ownership.py`
- `test/test_v2_helper_groups.py`

## 4. 集成测试计划

### 4.1 必测场景

必须覆盖：

- 同项目双终端并发 `ccb` / `ccb ask`
- `kill` 与 `start` / `ask` 并发
- 冷启动 `ask` 与交互式 `ccb` 的 readiness 分层
- lifecycle migration from old lease-only project
- config drift 导致的 orderly restart
- stop transaction 中 socket 不可连但 pid 活着

### 4.2 集成测试标准

集成测试应以“真实状态文件 + 受控进程假体 + 真实 tmux socket 或等价后端”为标准，不允许仅靠 monkeypatch 模拟所有关键时序。

### 4.3 重点用例

- 同项目两个 CLI 几乎同时发起 `ccb`，最终只产生一个 keeper 和一个 mounted generation
- 同项目两个 CLI 几乎同时发起 `ccb ask`，第二个请求必须等待已存在的 `starting` 事务
- 冷启动事务耗时超过旧 5 秒预算但小于 `startup_transaction_timeout_s` 时，`ccb ask` 仍能成功提交
- 已 mounted 热路径下的 `ccb ask` / `ping` 不得接近 `startup_transaction_timeout_s`
- namespace/UI readiness 故意延迟时，`ccb ask` 仍可在 control-plane mounted 后成功；交互式 `ccb` 才继续等待 attach
- `phase=starting` 时 child 启动失败，系统稳定落到 `phase=failed`
- `phase=mounted` 时 config 改动，keeper 通过 orderly restart 完成 generation 切换
- socket 不可连但 pid 仍存活时，普通 `ccb kill` 能经 keeper 收口，而不是被 degraded 拒绝
- `ccb kill` 与新的 `ccb ask` 并发时，新的 ask 只能等待停机结果，不得触发第二条启动链
- 从旧项目状态启动：
  - 只有 `lease.json`
  - 没有 `lifecycle.json`
  - 没有 `helper.json`
  都能进入确定性迁移路径

### 4.4 真实 CLI 生命周期 smoke 集

以下用例是固定的真实 `ccb` 黑盒生命周期基线，必须通过 `ccb` 入口拉起项目，而不是只调用后台 service：

- `test/test_v2_phase2_entrypoint.py::test_ccb_v2_project_lifecycle`
- `test/test_v2_phase2_entrypoint.py::test_ccb_ping_ccbd_recovers_from_stale_mount_and_bumps_generation`
- `test/test_v2_phase2_entrypoint.py::test_ccb_long_running_job_keeps_heartbeat_and_doctor_healthy`
- `test/test_v2_phase2_entrypoint.py::test_ccb_fake_provider_recovers_running_execution_after_ccbd_restart`
- `test/test_v2_ccbd_start_matrix.py::test_ccb_start_restarts_dead_daemon_on_subsequent_start`

统一执行入口：

- `python -m pytest -q -m ccb_lifecycle_smoke test/test_v2_phase2_entrypoint.py test/test_v2_ccbd_start_matrix.py`

CI 接入要求：

- GitHub macOS cross-platform workflow 必须运行这组 smoke，用于覆盖真实 tmux / namespace / socket / attach 时序
- Linux 运行同一 marker 作为基线对照，避免只在 macOS 上单点发现回归

### 4.5 建议文件组织

- `test/test_v2_project_lifecycle_integration.py`
- `test/test_v2_kill_lifecycle_integration.py`
- `test/test_v2_lifecycle_migration.py`

## 5. 黑盒压力测试计划

### 5.1 必测场景

必须覆盖：

- 多项目并行运行，各自 keeper/ccbd 独立
- 长时间 ask/pend/watch 循环下无 bridge 数量持续增长
- crash 注入后 orphan sweep 是否收口
- 单项目大量 agent 并发执行时 keeper 不发生重启风暴

### 5.2 观测重点

黑盒压力测试的重点不是精确业务回复，而是 lifecycle 与资源边界：

- `keeper` 数量
- `ccbd` generation 数量
- helper process group 数量
- socket inode 切换是否单调有界
- ask 次数增加时 helper 数是否稳定

### 5.3 压测脚本建议

- 多项目并发压测
  - 10 至 30 个项目并行 `ccb` / `ccb ask`
  - 验证互不接管、互不清理、互不复用 keeper
- 单项目高频 ask 压测
  - 1 个项目、多个 agent、循环 ask 1000 次以上
  - 观察 helper group 数量是否稳定
- kill/restart 抖动压测
  - 反复执行 `ccb ask`、`ccb kill`、`ccb`
  - 验证不会出现 stuck `starting` 或双 mounted generation

### 5.4 建议输出指标

- `keeper_count_per_project`
- `mounted_generation_count_per_project`
- `helper_group_count_per_agent`
- `orphan_helper_count_per_project`
- `lifecycle_phase_duration_seconds`

## 6. 故障注入测试计划

### 6.1 基本要求

必须显式构造：

- bind 前 socket 路径残留
- 旧 generation 延迟退出
- keeper config check timeout
- helper leader 死亡但子进程存活
- namespace destroy 与 helper cleanup 交叉发生

每个故障注入场景都必须定义：

- 注入点
- 预期 lifecycle phase
- 预期 cleanup 结果
- 不允许发生的副作用

### 6.2 注入矩阵

1. `starting` 前注入
- stale socket path 存在
- 旧 keeper state 残留
- 旧 `lease.json` 指向已死 pid

2. `starting` 中注入
- child 在 bind 前退出
- child 在 bind 后、readiness 前退出
- child readiness ping 超时
- child `startup_stage` 长时间不推进

3. `mounted` 中注入
- socket accept 阻塞或超时
- backend heartbeat 正常但 socket 不可连
- config-check ping timeout
- namespace UI readiness 延迟，但 control-plane 仍可服务 `ask`

4. `stopping` 中注入
- stop_all 卡住
- provider helper group 无响应
- namespace destroy 成功但 helper group 仍存活

5. helper 侧注入
- helper leader 死亡，child 留存
- child 脱离 parent 成孤儿
- helper manifest 丢失但 pgid 仍存活

### 6.3 每个场景必须验证

- lifecycle 是否落到正确 phase
- 当前 authoritative generation 是否仍唯一
- 是否产生 stale helper / stale socket / stale lease residue

## 7. 阶段门禁

### 7.1 Phase 1 门禁

- 单元测试全部通过
- 同项目并发启动不会产生双 keeper / 双 backend generation
- `lifecycle.json` 迁移覆盖旧 lease-only 项目
- 冷启动等待逻辑未通过放大全局 RPC timeout 实现

### 7.2 Phase 2 门禁

- readiness gate 生效，`mounted` 不会早发
- `startup_transaction_timeout_s` 被验证为最大预算上限而非固定等待
- `ccb ask` 与交互式 `ccb` 已验证等待不同 readiness 层
- 普通 `ccb kill` 覆盖 degraded/socket_unreachable 场景
- socket ownership 测试覆盖 inode fence

### 7.3 Phase 3 门禁

- helper manifest + process group cleanup 覆盖所有长期 helper backend
- ask 高频循环下 helper 数量稳定
- 无新增 project-owned `PPID=1` helper 泄漏
- helper ownership 写路径只依赖 canonical `runtime_generation`

### 7.4 Phase 4 门禁

- `doctor` / diagnostics 输出包含 lifecycle、socket、runtime、helper 指标
- orphan sweeper 可回收 stale helper residue
- fault bundle 足以解释最近一次 failed/stopping 卡点

## 8. 长稳与回归

### 8.1 soak tests

除一次性 CI 外，必须增加长稳测试与回归基线。

建议至少包含：

- 30 分钟单项目 soak
  - ask / pend / watch / kill / restart 循环
- 2 小时多项目 soak
  - 多个项目同时运行，观察 keeper 与 helper 数量是否稳定
- 崩溃恢复 soak
  - 周期性注入 backend crash，再观察是否出现 helper 累积

### 8.2 回归基线指标

- 单项目 keeper 最大数量恒为 1
- 单项目 mounted generation 最大数量恒为 1
- 单 agent 长期 helper group 最大数量恒为 1
- orphan helper 数在 crash 后可回落到 0 或明确受控上限
- `ccb kill` 的收口时间在有界阈值内

## 9. 手工验证清单

在自动化之外，仍需保留最小手工验证清单，用于发版前确认用户可见行为。

建议手工清单：

- 新项目首次 `ccb`
- 已有项目 `ccb` 恢复历史
- `ccb ask agentX` 正常提交与回复
- `ccb kill` 后当前项目不再保留 mounted backend
- `ccb kill -f` 可清理异常状态
- 多项目同时运行互不影响
- 观察系统进程，确认无持续增长的 provider helper/bridge

手工验证应记录：

- 项目路径
- phase 变化
- keeper pid / ccbd pid
- helper group 数量前后变化
- 是否出现 orphan residue

## 10. CI 与观测接入

### 10.1 CI 分组

CI 不应只执行功能性 pytest，还应输出关键生命周期指标。

建议接入：

- lifecycle 测试分组
- helper ownership 测试分组
- 压力测试 smoke 分组
- 故障注入 smoke 分组

### 10.2 失败时上传内容

CI 失败时应优先上传：

- `lifecycle.json`
- `lease.json`
- startup/shutdown report
- lifecycle journal
- helper manifests
- recent stderr/stdout logs

## 11. 6.0.29 后真实验证矩阵

### 11.1 定位

本节是 `v6.0.29` 之后 ccbd / ask / mailbox observer 稳定化工作的真实验证方案。

它不是新的架构阶段，而是用于证明以下已经落地或部分落地的目标确实成立：

- `P2c` mount authority / stop_all state gate 已阻断 stale finalize 回写
- `P2d` request / maintenance lane 分离不会让 maintenance 卡住 submit ack
- `P2.5` lazy restore handoff 不再把 missing / stopped / failed runtime 恢复压回 submit path
- `P3` mailbox summary read model 让 routine observer 脱离 history scan 热路径
- `P4` CLI surface 弱化 observer，避免 `pend` / `watch` / `queue` 被误读为 ask 终态 authority

### 11.2 验证层级

真实验证按 7 层执行。越往后越接近用户真实环境，失败优先记录到 `docs/ccbd-manual-test-issue-log.md`，再决定是否补自动化。

| 层级 | 目标 | 必跑内容 | 通过标准 |
|---|---|---|---|
| A. Baseline | 确认当前分支没有基础回归 | `pytest -q`、focused socket / stop_all / mailbox / CLI suites、`bash test/system_comm_matrix.sh`、`archi --diff .` | 单测全绿；comm matrix 全绿；架构风险无新增 blocker |
| B. Fastpath stress | 验证 submit ack 不受 maintenance / mailbox / restore 拖累 | 高频 `ask`、大 mailbox history、helper busy、missing/stopped/failed runtime submit | submit receipt bounded；无 accepted stuck；无 inline `ensure_ready()` 回退 |
| C. Lifecycle recovery | 验证 kill / crash / restart / authority 收口 | ask 中 kill、ccbd crash、socket 删除、tmux pane 死亡、stop_all 后 restart | 单 backend generation；runtime/job 不回退；queued jobs 收敛到 terminal 或可恢复状态 |
| D. Communication matrix | 验证跨 provider 通讯稳定 | codex / claude / gemini / opencode 间 ask、broadcast、reply、interrupted abort、tracker terminal path | reply 可达；provider-driven terminal progression 不停在 `accepted` |
| E. Observer/repair | 验证 P3/P4 命令面目的达成 | `pend` / `watch` / `inbox` / `queue` / `doctor` / `repair ack|retry|resubmit` | observer 只输出 non-authoritative snapshot；summary missing/error 显式 degraded |
| F. macOS real | 验证真实 tmux/socket/namespace 时序 | macOS 上跑 A-D 的 smoke 子集，重点 cold start、attach、kill、provider terminal | 与 Linux 同语义；不新增 macOS 专属等待 hack |
| G. WSL real | 验证 native Linux path 与 `/mnt/<drive>` mounted drive | WSL home 项目、WSL mounted Windows drive 项目、long path socket relocation、doctor relocation 字段 | socket 放置符合 WSL 方案；doctor 可解释 relocation；ask/kill/comm matrix 可用 |

### 11.3 Baseline 命令

每次进入真实测试前先固定 baseline：

```bash
pytest -q
pytest -q test/test_v2_ccbd_socket.py test/test_ccbd_socket_server_loop.py test/test_v2_message_bureau_dispatcher_integration.py -k 'tracker or interrupted_abort or stop_all or roundtrip or start_flow'
pytest -q test/test_message_bureau_control_queue.py test/test_v2_mailbox_kernel_service.py test/test_v2_cli_router.py test/test_v2_cli_render.py -k 'summary or queue or inbox or pend or repair'
bash test/system_comm_matrix.sh
archi --diff .
```

Baseline 失败时不进入 macOS / WSL 真实环境测试，先归因：

- 若 failure 是 old test contract 仍假设 inline state progression，应改为等待 background convergence
- 若 provider-driven terminal path 留在 `accepted`，按 P2c/P2d/P2.5 stabilization bug 处理
- 若 observer summary missing/error 被渲染成 ready reply，按 P3/P4 regression 处理

### 11.4 Fastpath stress 场景

必须新增或手工执行一个 `system_fastpath_stress` 类脚本，覆盖以下场景：

1. 热路径高频 ask
- 单项目、2 至 4 个 configured agents
- 连续提交 100 至 300 个 ask
- 每次只要求 receipt bounded，不要求同步等待 provider 完成

2. 大 mailbox history
- 人工或脚本生成至少 5k 至 20k 条 mailbox events
- 再执行 `ask`、`pend`、`queue`、`inbox`
- 验证 routine observer 不触发 history projection fallback，submit latency 不随 history 线性增长

3. maintenance busy
- 让 heartbeat / recovery / mailbox refresh 有可观测耗时
- 并发执行 submit / ping / doctor
- 验证 request worker response 不等待 maintenance 长跑结束

4. lazy restore handoff
- 构造 missing runtime、stopped runtime、failed runtime
- 对单 agent submit
- 验证 submit path 只完成 receipt，runtime recovery 由 queued job start / recovery lane 接管

建议输出指标：

- `submit_receipt_latency_p50_ms`
- `submit_receipt_latency_p95_ms`
- `submit_receipt_latency_max_ms`
- `accepted_job_count_after_convergence`
- `maintenance_pending_tick_max`
- `mailbox_summary_status_counts`
- `runtime_restore_owner_path`

### 11.5 Lifecycle / recovery 真实场景

以下场景应尽量用真实 `ccb` 命令、真实 project `.ccb`、真实 tmux session 执行：

- `ccb ask` 已提交但 provider 未 terminal 时执行 `ccb kill`
- 删除 project socket 后执行 `ccb ping ccbd`、`ccb ask`、`ccb kill`
- kill ccbd pid 后执行 `ccb ask`，确认 keeper / lifecycle 进入确定性恢复
- kill agent pane 后等待 supervision remount，再提交 ask
- stop_all 后立即 restart restore，验证 runtime/job 不从 stopped/terminal 回退到 idle/running
- 同项目两个终端同时 cold start，确认只出现一个 authoritative generation
- config 改动后 orderly restart，确认 configured agents 与 runtime authority 对齐

每个场景记录：

- `lifecycle.json`
- `lease.json`
- `runtime.json`
- `supervision-events.jsonl`
- `doctor` 输出
- 失败时的 tmux pane snapshot

### 11.6 Communication matrix 扩展

`test/system_comm_matrix.sh` 继续作为真实通讯基线，但后续扩展应覆盖：

- codex → claude / gemini / opencode
- claude → codex / gemini / opencode
- broadcast `ask all`
- interrupted abort 后下一轮 ask 仍可完成
- tracker completion 与 mailbox reply 同时存在时，terminal authority 不被 observer 覆盖
- provider busy 时 reply delivery bounded wait 生效，不能无限等待 pane prompt ready

验收标准：

- 所有发出的 ask 都有 receipt
- terminal job 最终能被 `ask wait` 或 tracker path 观察到
- observer 输出可以缺省或 degraded，但不得伪造 terminal success
- 失败可由 `doctor` / logs / supervision events 定位到 provider、runtime、mailbox 或 lifecycle 层

### 11.7 macOS 验证要求

macOS 验证的目标不是重新证明所有 Linux 单测，而是覆盖真实平台差异：

- BSD `sed` / `awk` / `ps` 差异不破坏测试脚本
- tmux socket / pane title / session discovery 行为与 Linux 语义一致
- provider pane prompt ready 判断不会因终端渲染差异无限等待
- `ccb kill` 能收口到无 project-owned backend / helper residue

macOS 最小必跑：

```bash
pytest -q -m ccb_lifecycle_smoke test/test_v2_phase2_entrypoint.py test/test_v2_ccbd_start_matrix.py
bash test/system_comm_matrix.sh
pytest -q test/test_v2_ccbd_socket.py -k 'tracker or interrupted_abort or roundtrip'
```

### 11.8 WSL 验证要求

WSL 验证分两类项目路径：

- Linux native path，例如 `~/tmp/ccb-wsl-native`
- Windows mounted drive path，例如 `/mnt/c/tmp/ccb-wsl-mounted`

每类都要验证：

- `ccb` cold start
- `ccb ask <agent>` receipt
- `ccb ask --wait <agent>` terminal
- `ccb ping ccbd`
- `ccb doctor`
- `ccb kill`
- `bash test/system_comm_matrix.sh` 的 smoke 子集

mounted drive 项目还必须验证：

- socket / ccbd runtime storage relocation 发生在 Linux-local state root
- logical `.ccb` 呈现仍指向项目 anchor
- `doctor` 能显示 relocation reason，且区分 WSL mounted drive 与 ordinary long path fallback
- Unix shell 文件不因 CRLF 破坏执行

Windows PowerShell bootstrap 独立验证，不与本节 Linux/WSL ccbd runtime 测试混在同一轮中。

### 11.9 Soak 验证

真实 soak 分三档：

| 档位 | 时长 | 场景 | 通过标准 |
|---|---:|---|---|
| S1 | 30 分钟 | 单项目 ask / wait / pend / doctor / kill / restart 循环 | 无 stuck starting；无 helper 持续增长；kill 有界收口 |
| S2 | 2 小时 | 多项目并行 ask / provider terminal / doctor | 项目间 keeper / ccbd / socket / runtime authority 不串扰 |
| S3 | 过夜 | 周期性 crash 注入 + recovery + communication smoke | orphan residue 有界；下一轮 ask 可恢复；diagnostics 可解释失败 |

Soak 失败不直接归类为 flaky。必须先保留 artifacts，再判断是：

- real lifecycle authority bug
- provider terminal detection bug
- test harness timing assumption
- platform-specific shell/tmux compatibility issue

### 11.10 出口门禁

只有满足以下条件，才能把 `v6.0.29` 后 ccbd / ask 稳定化视为达到设计目标：

- Linux baseline 全绿
- `system_comm_matrix.sh` 全绿
- focused tracker / interrupted abort / stop_all / lazy restore suites repeated loop 稳定
- fastpath stress 中 submit receipt latency 不随 mailbox history 或 maintenance busy 线性退化
- missing / stopped / failed runtime single-target ask 不在 submit path 同步 restore
- `pend` / `watch` / `inbox` / `queue` 不被任何测试或文案当作 ask terminal authority
- macOS smoke 通过，且没有新增 macOS-only 语义分支
- WSL native path 与 mounted drive smoke 通过，socket relocation 可被 doctor 解释
- 至少 S1 soak 通过；发版前建议 S2 通过

### 11.11 Linux 实测记录

2026-05-09 Linux 本机验证已完成以下项目：

- `pytest -q`
  - 结果：`1709 passed in 132.75s`
  - 结论：当前分支单元 / 集成 baseline 全绿
- `bash test/system_comm_matrix.sh`
  - 结果：`ALL TESTS PASSED`
  - 结论：真实 tmux + stub provider 通讯矩阵通过，覆盖 codex / claude / gemini 混合与跨项目隔离
- `CCB_LINUX_SOAK_SECONDS=90 CCB_LINUX_SOAK_KILL_EVERY=2 CCB_LINUX_SOAK_STUB_DELAY=0.2 CCB_LINUX_SOAK_ASK_WAIT_TIMEOUT_S=90 bash test/system_linux_soak.sh`
  - 结果：8 轮 ask/wait/pend/doctor，4 次 kill/restart，全部通过
  - 指标：p50 `200ms`，p95 `244ms`，max `244ms`
  - artifacts：`/home/bfly/yunwei/test_ccb_linux_soak_20260509183038-1091008/project`
- `CCB_LINUX_SOAK_SECONDS=300 CCB_LINUX_SOAK_KILL_EVERY=3 CCB_LINUX_SOAK_STUB_DELAY=0.2 CCB_LINUX_SOAK_ASK_WAIT_TIMEOUT_S=90 bash test/system_linux_soak.sh`
  - 结果：23 轮 ask/wait/pend/doctor，7 次 kill/restart，全部通过
  - 指标：p50 `204ms`，p95 `247ms`，max `270ms`
  - artifacts：`/home/bfly/yunwei/test_ccb_linux_soak_20260509183857-1727628/project`
- `bash test/system_fastpath_stress.sh`
  - 结果：60 次连续 ask、`queue all`、`pend --queue all`、首/中/尾样本 `ask wait`、`doctor`、`kill`、`unmounted` 全部通过
  - 指标：submit p50 `197ms`，p95 `227ms`，max `239ms`
  - artifacts：`/home/bfly/yunwei/test_ccb_fastpath_20260509185600-3149944/project`

本轮 Linux 验证结论：

- submit fastpath 达到目标：60 次真实提交的 p95 receipt 低于 `1500ms` 阈值，且未随 provider 串行执行队列线性退化
- `ask wait` 可观察到首/中/尾样本全部 terminal，未发现 provider-driven job 卡在 `accepted`
- `pend --queue` 仅作为 weak observer 使用，未参与 terminal authority 判定
- kill/restart soak 中未发现 stuck `starting`、双 backend generation 或 kill 不收口
- 仍未完成 macOS 与 WSL 真实环境验证；Linux 结论不能替代平台兼容结论

2026-05-09 同机补充跨平台前置自动化：

- 当前运行环境：Ubuntu 22.04.5 LTS，kernel `6.8.0-90-generic`，非 WSL，非 macOS
- `pytest -q -m ccb_lifecycle_smoke test/test_v2_phase2_entrypoint.py test/test_v2_ccbd_start_matrix.py`
  - 结果：`5 passed`
- `pytest -q test/test_v2_storage_paths.py test/test_wsl_path_utils.py test/test_opencode_paths_runtime.py test/test_project_id.py -k "wsl or mounted or relocation or socket or path or storage"`
  - 结果：`14 passed`
- `pytest -q test/test_codex_launcher_session_paths.py test/test_claude_session_pathing.py test/test_claude_resolver_pathing.py test/test_stability_regressions.py -k "wsl or windows or path or session or workdir or workspace"`
  - 结果：`14 passed`

补充结论：

- 可模拟的 WSL path / socket / storage relocation / provider session path 相关自动化通过
- 这不等价于真实 WSL mounted-drive 验证，因为当前机器没有 `/mnt/<drive>` drvfs 环境
- 这不等价于 macOS 验证，因为 BSD userland、macOS tmux、Keychain/provider 环境差异没有被真实覆盖
