# Phase 2 实施与验证交接

## 基线与交付状态

实施位置是 `/workspace/Marlow`，基线为用户指定的远端 `main@37d58cdb`；
Phase 1 提交为 `84df86ea`。开始时工作树干净。本机
`/Users/stellanlee/project/Marlow` 未挂载，未读取或修改其中的未提交内容。
变更通过 `refactor/phase-2-codex-runtime` 分支提交审查，目标分支为 `main`；
验证结果对应本次 Phase 2 实施，未合并到主分支。

已实现单一 Codex app-server 执行路径，实际删除 Native 推理实现。
回归测试仍有基线已有失败，因此不宣称全套测试全绿或已完成生产验收。
下文分别记录真实二进制、协议替身、业务回归和未验证范围。
架构决策及逐项迁移清单见 [phase-2-codex-runtime.md](phase-2-codex-runtime.md)。

## 实现与删除

| 能力 | 当前归属与主要文件 | 迁移验证 |
| --- | --- | --- |
| CLI、SDK、Gateway、Webhook AI、AI Cron、delegation | `run_agent.py` 薄 facade → `agent/transports/codex_app_server_session.py` → Codex provider | SDK/CLI/Gateway/Cron 回归；协议事件与实际 smoke |
| 配置、进程环境和 discovery | `agent/codex_environment.py`、`agent/transports/codex_app_server.py` | 临时个人 HOME 标记、有效 config/skills/account、实际 sandbox 子进程 |
| 模型驱动业务任务、插件 LLM、MCP sampling | `agent/codex_tasks.py`；同一 facade，不建立第二个推理客户端 | scope、取消、预算、idle sampling 拒绝 |
| Memory、Honcho、历史 | `agent/codex_memory.py`、`agent/memory_recall.py`、`agent/business_memory.py`；保留原业务存储 | 召回、冲突过滤、provider egress、raw history、Memory lifecycle |
| Work Experience、Decision Memory | 保留现有 service/store；每 turn 绑定可信身份和 egress | experience 测试及越权边界 |
| 工具与业务审批 | `agent/codex_bridge.py`、薄 stdlib MCP proxy、原业务 guard | 单次审批、拒绝/取消、伪造参数、越权、重复请求 |
| shell、文件及本地图片 | 唯一 Marlow 执行入口 → 同一 app-server `command/exec`，固定 named sandbox | 实际二进制拒绝个人文件、软链接和只读项目写；原子写与 V4 patch |
| 浏览器、Gateway 媒体发送 | 原浏览器工具；`agent/codex_file_scope.py` 限定披露范围并冻结上传内容 | file:// 与表达式拒绝、跨会话/认证文件拒绝、异步替换测试 |
| Cron | 纯脚本保持确定性；AI 任务捕获 owner/workspace/project scope | owner、profile、delivery、任务 lease；模型不能新增 script/no_agent 旁路 |
| Thread/Turn、故障与恢复 | `agent/codex_state.py` 持久映射/审计/跨进程 lease；`marlow_cli/runtime.py` 人工核实入口 | 跨用户/群组拒绝、恢复、task 冲突、取消/EOF/超时/不重放 |
| 压缩、重试和标题 | Codex 原生 compact 与请求重试；标题确定性派生 | compact 审计；无 Marlow 压缩或辅助标题调用 |
| Gateway 图片、静态贴纸、浏览器截图 | 原生图像输入/图像工具结果，交给当前 Codex turn | 实际 MCP 图像转换、本地 sandbox 读图、贴纸零辅助调用 |

删除的实现包括 Native init/runtime helpers/conversation loop/tool executor、
Chat Completions 与旧 Codex Responses transport、auxiliary client、
provider router 与 `providers/`、`plugins/model-providers/`、context engine、
Marlow context/trajectory compressor、fallback CLI、mini SWE runner 和仅服务这些路径的测试。
移除了直接 `openai` 运行依赖并更新 `uv.lock`；新增有上界的 `wcwidth` 声明，
修复原有 Markdown 显示路径缺少依赖声明的问题。历史定价/用量读取及 Memory provider
抽象继续存在，它们不进行 Marlow 推理请求。

删除明细见 [phase-2-deletions.json](phase-2-deletions.json)。混合业务测试保留并迁移；
没有为了得到绿色结果删除基线失败的 Gateway、文件或浏览器业务测试。
发行包单独检查旧模块路径和依赖，不将源码删除当成打包已完成的证据。

## 版本与实际协议

唯一支持版本：`codex-cli 0.159.2`。实际执行文件为本环境的
`/opt/codex/bin/codex`，启动和登录均检查精确版本；其他版本明确失败。
已经生成并核对该二进制的实验 JSON 协议 schema 和 config schema。

实际二进制拒绝 schema 中残留的旧 `approval_policy="untrusted"`，因此使用
`on-request`；initialize 开启实验能力后使用 named permissions `marlow`。
`thread/start` 和**每个** `turn/start` 都传 `environments=[]`，移除模型目录中的
native shell/apply_patch 等执行工具。实际 Responses 请求工具目录已验证，
授权 Marlow MCP `skills_list` 仍正常调用。

原生 PreToolUse hooks 对某些错误会 fail open，故不将其作为业务权限边界。
MCP 的 native approval 委托给 Marlow host guard；host 每次按具体操作、actor、
session、turn 校验。审批超时、取消、拒绝或缺失渠道均不代表同意。
旧 request_routing/权限环境映射及 delegation 自动同意代码已删除。

## Profile 环境与认证

每个 Profile 使用稳定私有目录：

```text
<profile>/runtime/home/             # 受控 HOME 与 XDG
<profile>/runtime/codex/            # CODEX_HOME、config.toml、file auth、native state
<profile>/runtime/codex-runs.sqlite3 # Marlow 运行映射和审计
<profile>/workspace/                # 默认工作区
<profile>/workspace/conversations/<scope-hash>/
<profile>/.delivery-outbox/         # 已授权媒体的冻结上传副本
```

配置和 runtime 路径不允许软链接；不复制个人配置、skills 或登录态。
文件凭据后端是强制项，不使用系统 keychain。受控 subprocess env 从允许列表构建，
不继承个人 HOME、XDG、Shell startup 或 Codex/OpenAI 配置覆盖；login shell 禁用。
provider env_key 不进入 shell 子进程环境，工具/历史输出经过密钥脱敏。
Codex 自带 hooks/plugins/skills 和 Phase 1 退役能力禁用；明确 profile skills、
业务插件和授权工具通过 Marlow 提供。读取项目不自动授权执行项目扩展。
有效配置和发现结果冲突时明确失败，不绕过系统或组织强制策略。

安装匹配版本后，在选定 Profile 执行：

```bash
MARLOW_HOME=/absolute/marlow-profile marlow login
MARLOW_HOME=/absolute/marlow-profile marlow status
```

这使用 Codex 自身登录流程和该 Profile 的 file credential store。
没有自动读取个人 `~/.codex`。认证缺失返回 auth-required 和 Profile 登录指引，
没有旧 Runtime fallback 或无限重启。

## 自定义 Provider 与项目配置

Marlow YAML 只管理运行边界和业务设置。例如：

```yaml
runtime:
  codex:
    binary: /absolute/path/to/codex
    turn_timeout: 600
    max_concurrent: 4
    pass_env: [CUSTOM_MODEL_KEY]
    projects:
      - path: /absolute/authorized-project
        writable: true
    project: /absolute/authorized-project
    skills: []
    execution_network: false
terminal:
  backend: local
```

`projects` 是 allowlist，只有选定项目/明确任务授权才进入该任务的 grants；
`project` 选择默认项目。交互命令可从 allowlist 中选择当前授权 cwd。
Gateway 会话默认使用独立会话工作区；共享线程保持共享 workspace，业务 actor 按次绑定。
显式授权的项目不会成为 app-server discovery cwd。不要将 Profile 或其父目录授予项目。

模型配置仅位于 `<profile>/runtime/codex/config.toml`：

```toml
model = "your-supported-model"
model_provider = "custom"

[model_providers.custom]
name = "My Responses provider"
base_url = "https://provider.example/v1"
wire_api = "responses"
env_key = "CUSTOM_MODEL_KEY"
requires_openai_auth = false
request_max_retries = 2
stream_max_retries = 1
```

以父进程环境显式提供 `CUSTOM_MODEL_KEY`，并在 `pass_env` 中允许该变量。
CLI 的选定 Profile `.env` 加载仍用于既有业务密钥；直接使用 Python SDK 的调用方
应显式设置环境。配置密钥不得放进用户提示。

这里只支持该版本 Codex 的 Responses 协议和工具调用；“OpenAI-compatible”或
Chat Completions 兼容标签不构成支持证明。原生配置保留允许的模型字段，
不会每次启动覆写 TOML。权限/扩展等 host-controlled 字段不能覆盖强制边界。

## 历史、恢复和用量

原始历史、Memory 与业务数据库保留。旧 Native 历史创建新 Codex Thread 时以明确标记的
参考文本导入；不会伪造旧 Thread 或重放工具调用。相同会话通过 lease 协调，
不同会话受 Profile 并发上限约束；同一 task 的跨 session 执行也使用稳定 lease。

中断、进程退出和不确定写操作进入 `needs_verification`，后续任务不自动重跑。
MCP `tools/call` 在失去响应/401/session-expired 后不重试；只读 discovery 可有限重试。
先核实真实副作用，再执行明确的操作确认：

```bash
MARLOW_HOME=/absolute/marlow-profile marlow runtime status
MARLOW_HOME=/absolute/marlow-profile marlow runtime acknowledge \
  --session SESSION --thread THREAD --turn TURN --checked
```

没有 turn ID 时按状态输出使用 `--turn unknown`。确认保存旧审计，不执行任务。
`/retry` 是用户明确请求的新 turn，保存此前历史并要求核实副作用；待核实状态仍阻止重跑。
`/undo` 在该协议版本不支持，明确返回说明。`/compress` 使用 native compact。

没有输入、到期 AI job 或获准触发时不创建模型 turn。标题本地派生，静态贴纸和截图
不产生辅助推理。MCP sampling 默认关闭，只在当前授权工具操作内按预算触发，
绑定父任务，并随超时/取消停止。默认并发 4、工具操作预算 90、turn 超时 600 秒；
可配置但均有上界。Codex 负责有限 provider 请求重试，Marlow 不叠加整个任务重试。

审计包含 trigger、Profile/scope、task/session、thread/turn、开始/结束、结果、
错误和实际可得的 usage。未收到 usage 时记录 unknown；模型调用次数/成本不伪装成
精确数据。兼容 SDK 字段 `api_calls/session_api_calls` 计数的是 Marlow 发起的
Codex turn，不能当成 Codex 内部 provider HTTP 请求数；Gateway 使用 `Codex turns` 标签。
兼容 SDK 的 `max_tokens` 也不代表该协议存在硬 Token 限额。

## 测试结果与证据

所有测试通过仓库 `scripts/run_tests.sh`，每文件独立子进程、清空凭据环境，使用临时
Profiles，不发送真实业务消息、不调用付费模型。默认全套不包含 integration/e2e/docker
目录；跳过或未收集的外部 E2E 不算通过。

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 干净基线 main | `scripts/run_tests.sh -j 12`（独立 baseline worktree） | 805 文件；14,747 通过、104 失败、95 跳过；839.8 秒 |
| 最终全量 | `scripts/run_tests.sh -j 10` | **663 文件；13,021 通过、97 失败、90 跳过；314.5 秒** |
| runtime/协议/审批/视觉/delegation 最终定向 | 下方定向命令 | **14 文件；300 通过、0 失败、7 跳过** |
| 最后诊断和强化隔离 | `scripts/run_tests.sh tests/gateway/test_usage_command.py tests/runtime/test_codex_smoke.py tests/runtime/test_business_boundary.py` | **16 通过、0 失败** |
| Memory/Experience/Decision 专项 | `scripts/run_tests.sh tests/agent/experience tests/agent/test_memory_recall_query.py tests/agent/test_memory_recall_merge.py tests/agent/test_memory_cards.py tests/agent/test_memory_card_conflicts.py tests/runtime -j 6` | 当时 294 通过、0 失败；最终完整套件也覆盖保留的业务测试 |
| SDK 专项 | `scripts/run_tests.sh tests/runtime tests/run_agent/test_run_agent.py tests/run_agent/test_session_reset_fix.py -j 4` | 当时 38 通过、0 失败；最终完整套件包含 SDK 文件 |
| 语法及静态 | `python -m compileall -q agent marlow_cli gateway tools run_agent.py tests`；变更 Python 文件 `ruff check --select E9,F63,F7,F82`；`git diff --check` | 通过 |
| 原生 TOML 显式编辑 | 临时 TOML 中修改带缩进/引号的根 model，验证 provider 表及其字段保留 | 通过 |
| 发行包 | `python -m build --wheel --no-isolation`；zip 内容和 METADATA 检查 | 331 个条目，退役模块 0，直接 OpenAI 依赖 0，adapter 存在；16 个保留 SKILL 资源 |

最终定向命令：

```bash
scripts/run_tests.sh tests/runtime \
  tests/agent/transports/test_codex_app_server_session.py \
  tests/tools/test_delegate.py tests/tools/test_mcp_oauth_cold_load_expiry.py \
  tests/tools/test_mcp_oauth_bidirectional.py tests/tools/test_browser_console.py \
  tests/tools/test_browser_content_none_guard.py tests/tools/test_browser_secret_exfil.py \
  tests/tools/test_vision_tools.py tests/tools/test_vision_native_fast_path.py
```

97 个最终失败用例 ID 均存在于基线的 104 个失败中，新增失败用例 ID 为 0。
这是用例 ID 比对，不是对每个根因完全相同的证明；删除的 Native 测试也不算修复的缺陷。
失败涉及既有 Gateway session/dispatch/topic/status、文件工具、浏览器启动、MCP OAuth
及 subprocess runner 等 23 个文件。完整用例 ID、计数、日志路径和打包检查保存在
[phase-2-validation-results.json](phase-2-validation-results.json)。

中间轮次曾有 Native 接口契约测试失败和两项 OAuth 状态测试外网 discovery 超时。
已迁移 Native 专用契约并将该两项状态测试限定为确定性替身；专门的 metadata discovery
测试继续使用已有 HTTP fixture。最终结果以本表为准。最终全量后仅补强隔离标记、
用量标签与 TOML 编辑验证；相应定向检查均通过。


真实隔离证据来自 `tests/runtime/`：临时“个人 HOME”有 config、AGENTS、skills、
MCP/hook/Shell 唯一标记；实际 config/read、skills/list、account/read 和进程
环境不继承这些来源。明确授权的 Profile skill 作为正向对照出现在真实 MCP 结果中。
实际 `command/exec` 验证受控 HOME、个人 marker 读取失败、软链接逃逸失败和只读项目
写入失败。两 Profile 使用独立 runtime/env/MCP 进程，关闭 A 不影响 B。
constructor/idle、取消启动和贴纸下载没有创建模型 turn；sampling idle 请求被拒绝。

真实 Codex + 本地 Responses fixture 是协议/适配验证，不代表远端模型的视觉、推理质量、
工具选择或付费 provider 已通过生产验证。工具结果与用量来自真实 Codex 进程，模型响应
由 loopback HTTP fixture 提供，报告不将两者混称。

## 独立审查

按要求使用独立只读审查者，未让其修改工作树。审查发现并修复：

- 非本地 backend 静默在 host 执行；现在明确拒绝。
- 取消启动/通知 TurnID 归属、丢失写入状态、失败工具被错误标记完成；补状态和中断边界。
- Profile 插件相对导入、MCP 缓存、技能路径串用；改为可信 Profile 上下文。
- stateless ownership、跨 session 的相同 task 竞争；稳定 lease 与待核实状态阻断重放。
- sampling 无父归因/取消/预算；绑定当前真实工具调用及父任务。
- 媒体认证文件/跨会话泄露与异步替换；可信披露范围和 nofollow 冻结副本。
- browser file:// 和 console 表达式旁路；保留受控 HTTP/HTTPS 导航和日志读取。
- Cron profile/script/recipient/workdir 扩权；捕获 owner scope 并审批跨目标投递。
- Gateway background 媒体 scope、delegate workspace 继承、undo/retry 历史分歧。
- 最终清理辅助视觉死分支及无人交互 delegation 自动同意路径。

独立复核此前 99 项通过，包括真实二进制 + loopback provider；后续独立视觉复核
发现新增“零调用”测试误读不存在日志，已修正为断言日志不存在。
最终边界复核未报告新的可复现 P0/P1；这不等同于完整生产安全证明。

## 升级影响、限制与未验证项

旧 YAML `model/providers/auxiliary/fallback/compression/context` 等推理配置保留在磁盘，
但启动时给迁移错误；需要操作人先备份并移动模型设置，不能静默扩大权限。
旧 delegation 自动同意配置不再批准危险操作。现有历史/Memory/自定义内容不清理，
没有恢复 Phase 1 退役 skills 或生成/语音/Computer Use/REPL/TUI 等能力。

已知功能限制：固定 Codex 0.159.2；仅 local sandbox，Docker/SSH backend 明确不支持；
`terminal(background=True)` 不支持，使用受控 Cron/delegation；浏览器 console 任意 JS
不支持；`/undo` 不支持。这些是明确的升级影响；当前代码不会悄悄改用旧引擎。

仅执行 Linux 实测。macOS 路径/Seatbelt/keychain 行为没有在真实 macOS 上验证。
没有真实远端付费模型 E2E、渠道真实收发、外部 Honcho 服务或实际浏览器会话 E2E。
宿主业务插件、外部 MCP 和浏览器工具是可信 host 集成，依赖各自业务 guard，
不处在 `command/exec` 的 OS sandbox 内；不要宣传成全进程隔离。
全量测试的基线失败仍应在后续工作中解决；本交接保留并列出它们。
