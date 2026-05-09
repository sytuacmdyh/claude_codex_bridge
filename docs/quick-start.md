# CCB Quick Start

这份文档给你一条最短路径：安装 CCB、启动一个双 Agent 团队、发起一次委派、并在最后干净退出。

## 1. 环境准备

- Python `3.10+`
- `tmux`
- 至少一个可用的 Agent CLI（如 Codex CLI、Claude CLI、Gemini CLI）

建议先确认基础命令可用：

```bash
python3 --version
tmux -V
```

## 2. 安装 CCB

### Linux / macOS / WSL

```bash
git clone https://github.com/sytuacmdyh/claude_codex_bridge.git
cd claude_codex_bridge
./install.sh install
```

安装完成后，检查：

```bash
ccb --help
```

## 3. 初始化项目配置（最小可用）

在你的项目根目录创建 `.ccb/ccb.config`：

```text
cmd; writer:claude, reviewer:codex
```

含义：

- `cmd`：命令行 pane
- `writer` / `reviewer`：Agent 名称（也是 pane 标题）
- `claude` / `codex`：对应 provider
- `;`：左右分栏，`,`：上下堆叠

角色分配说明：

- 角色（如 `writer`、`reviewer`）的唯一权威来源是项目内 `.ccb/ccb.config`
- 评审规则文档中的角色表仅作示意，不作为真实配置来源

## 4. 启动 Agent 团队

在项目根目录执行：

```bash
ccb
```

首次在新项目执行 `ccb` 时，会自动在项目根写入 `AGENTS.md`（包含角色说明与评审 Rubrics）。

常用启动方式：

```bash
ccb -s    # 安全启动：保留 Agent 自身配置的权限策略
ccb -n    # 重建 .ccb（保留 ccb.config）后启动
```

## 5. 发起第一次委派

进入任一 Agent pane 后，可以显式委派任务：

```text
/ask reviewer 请审查 src/parser.ts 的边界条件，并给出风险清单
```

也可以自然语言触发隐式委派：

```text
让 reviewer 检查 parser 的边界情况，然后把问题汇总给我。
```

## 6. 推荐工作流（大型功能）

示例角色：`writer:claude`、`reviewer:codex`（可按你的 `.ccb/ccb.config` 调整）

### 阶段 A：计划评审循环

1. `writer` 制定实现计划（目标、范围、步骤、风险、验收标准）。
2. `reviewer` 对计划做 review，给出评分并提出修改意见。
3. `writer` 按意见修改计划并再次提交评审。
4. 重复 2-3，直到评分通过，再进入实现阶段。

### 阶段 B：代码评审循环

1. `writer` 按通过的计划开始实现。
2. `reviewer` 做 code review，给出评分并提出修改意见。
3. `writer` 按意见修改代码并再次提交评审。
4. 重复 2-3，直到评分通过。

## 7. 常用运行命令

```bash
ccb kill      # 停止当前项目后台运行态
ccb kill -f   # 强制清理（适合异常残留后重启）
ccb uninstall # 卸载
```

仓库方式安装的升级建议（使用你的仓库）：

```bash
git pull
./install.sh install
```

## 8. 常见问题（快速排查）

### 启动时报 tmux 相关错误

- 确认 `tmux -V` 正常
- 确认当前 shell 环境可以正常启动 tmux 会话

### Agent 没有按预期启动

- 检查 `.ccb/ccb.config` 是否有语法问题
- 先执行 `ccb kill -f`，再执行 `ccb -n` 重建运行态

### WSL 项目在挂载盘（`/mnt/...`）下

- 这是支持场景；CCB 会保留 `.ccb` 作为项目 authority
- 运行态可能自动迁移到本机 Linux state root，以提高 socket 稳定性

