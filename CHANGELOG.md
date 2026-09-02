# Changelog

## 2.0.4 - 2026-09-02

控制台前端（webui）性能与设计重写：修复流式期间整棵组件树随每个 token 重渲染、每个 token 全量重折会话历史的两个渲染放大问题；视觉层对齐 Kimi 设计语言。构建保持单文件形态——AstrBot 插件页宿主以 60s JWT asset_token 签名下发资产、URL 改写基于正则，拆包会导致 chunk 401 白屏，故入口仍为零相对导入的单文件。

### 修复

- **流式输出全树重渲染**：旧版 `useApp` 把全局 version 当作 `useSyncExternalStore` 的 snapshot，任何一次状态变更（流式时每秒数十帧 SSE）都会重渲染所有订阅组件。重写为选择器记忆化订阅（缓存 version + 选中值，Object.is 相等则不重渲染），配合 store 侧的 `session.stamp` / `sessionsStamp` 变更戳解决原地 mutate 与引用比较的冲突；`emit()` 改为微任务合批。流式期间只有数据真正变化的组件重渲染。
- **会话折叠 O(n)/token**：`ChatView` 每个 token 都对全部历史事件重新 `foldConversation`。重写为常驻的 `ConversationFolder` 增量折叠器，只处理新增事件；节点改为替换式更新并配合 `React.memo`，未变化节点零重渲染；检测到历史前置加载或事件收缩时自动退化为全量重折。

### 变更

- **设计对齐 Kimi 设计语言**：用户气泡由品牌蓝染色改为中性灰；发送按钮由品牌蓝改为深色主按钮；markdown 字重从 700/600 收敛为 400/500 两档并去除中文伪斜体；圆角归入 6/8/10/12/16 标尺；统计/详情数字启用 `tabular-nums`；hero 标题 26→22px/500；token 层去除 light/dark 重复的静态色板（design-platform.css 缩减约三分之一）。
- **验证脚本**：新增 `webui/scripts/`（preview 资源校验、chunk 完整性校验、jsdom 运行时冒烟测试，覆盖 hero→发送→流式→回合完成全流程）。
- MarkdownText / ReadBlock 改为 `React.lazy` 包裹：在单文件产物中等同即时解析，无加载语义变化，仅保留代码边界的 Suspense 兜底。
- metadata.yaml / pyproject.toml / webui/package.json / `__version__` 升至 2.0.4。

### 不变

- 构建产物形态不变：单 `console.js` + 单 `console.css`，无相对导入，与插件页 asset_token 改写模型兼容（拆包在宿主 60s token 与正则改写下行不通，已验证并记录于 vite.config.ts 注释）。
- 控制台 RPC 协议、SSE 帧格式、插件页集成（bridge-sdk 注入）不变，后端零改动。
- `call_maid` / `maid_task` 的工具签名与语义不变，`/maid status`、`/maid stop` 命令不变。
- 已有会话数据无需迁移，不修改 AstrBot Core 任何文件。

## 2.0.2 - 2026-08-24

修复并发能力退化：`call_maid` 每次新 dispatch 不再复用旧会话，batch 从串行改并发执行。

### 修复

- **subagent 永远复用同一对话**：2.0.0 引入的 `_chat_session_for` 会按 `(umo, agent_name)` 扫描复用已有会话，导致所有后续任务追加进同一会话、上下文不断积累污染。现在不带 `resume_session_id` 时无条件创建新会话，恢复每次 dispatch 独立 agent 的设计。
- **batch dispatch 串行退化**：`call_maid(tasks=[...])` 从 `for item: await` 串行等待改为 `asyncio.gather` 并发执行。5 项 batch 总耗时从 sum(单项) 降回 max(单项)。
- **README 参数名不一致**：README 写 `resume_agent_id`，代码用 `resume_session_id`，模型按 README 调用时参数静默失效。已统一为 `resume_session_id`。

### 变更

- `call_maid` batch 路径新增批量容量预检：超额时整批拒绝且不创建任何会话，避免 `asyncio.gather` 并发派发时逐项检查全部通过后超限。
- `_dispatch_chat_task` 新增 `skip_capacity_check` 参数：batch 路径预检已完成时跳过逐项检查，单次路径保持原有行为。
- metadata.yaml / pyproject.toml / `__version__` / `__init__.py` 升至 2.0.2。

### 不变

- `call_maid` / `maid_task` 的工具签名与语义不变，`/maid status`、`/maid stop` 命令不变。
- 控制台路径（`session.prompt`）不受影响，用户选定的会话仍复用同一 driver 串行消费。
- 已有会话数据无需迁移，不修改 AstrBot Core 任何文件。

## 2.0.1 - 2026-08-21

修掉「任务被外部因素打断后，控制台一直显示正在工作」的问题。三层防护，前后端一起上：进程崩溃/断电留下的未闭合 turn 会在重启后自动补终态；运行中的 turn 挂了超时看门狗兜底强制结束；前端工作标识改为以宿主上报的运行态为准，不再被历史数据误导。

### 新增

- **单 turn 看门狗（`max_turn_seconds`，默认 1800 秒，0 = 关闭）**：一个 turn 连续运行超过该秒数会被强制取消，终态标记为 interrupted。防止网络断连、工具调用卡死等外部因素导致任务永远占着「运行中」，控制台永久转圈。

### 修复

- **中断遗留的孤儿 turn 导致永久「正在工作…」**：进程被硬杀/崩溃/断电时，事件日志里会留下没有对应 `turn/end` 的 `turn/start`，重启后控制台依据历史事件永远显示工作中。现在新建会话驱动时会自动检测并补写一条 `turn/end{interrupted}` 终态（幂等，不会重复补写、不会误关正常闭合的会话）。
- **取消路径 turn/end 不落盘**：`turn/end` 的发射位置在 try/finally 之外，`asyncio.CancelledError` 路径（含看门狗取消）会跳过。现在 pump 循环显式捕获取消事件并补写终态，且看门狗取消后 pump 任务继续存活、可正常处理后续消息。
- **前端工作中标识以宿主为准**：控制台「正在工作…」改为「日志里有开着的 turn 且宿主上报运行中」同时满足才显示（原来是或关系），历史里的孤儿 turn 不再单独点亮工作指示。

### 变更

- `harness/drivers.py` 的 astrbot 运行时导入加了离线兜底（缺 astrbot 时回落 `_log.py` 门面与空基类），harness 层现在可以在无 AstrBot 的环境下直接跑测试。
- 新增 `tests/test_orphan_turn_and_watchdog.py`：孤儿 turn 自愈（含幂等与误关回归）、看门狗取消后状态复位与 pump 存活。
- metadata.yaml / pyproject.toml / webui/package.json / `__version__` 升至 2.0.1。

### 不变

- `call_maid` / `maid_task` 的工具签名与语义不变，`/maid status`、`/maid stop` 命令不变。
- 已有会话数据无需迁移，不修改 AstrBot Core 任何文件。

## 2.0.0 - 2026-08-17

控制台和后端整体重写。控制台从「只能看运行轨迹」变成一个真正能用的聊天页面：直接在里面给管家派任务、看流式回复、看工具怎么一步步执行，跟平常用的 AI 聊天软件一个形态。

### 新增

- **聊天式控制台**：左侧会话列表（搜索、重命名、删除、Fork），中间对话流，右侧详情栏。详情栏显示会话信息、耗时与 token 汇总、原始事件流，不用时可以收起。
- **新任务引导页**：没会话时输入框居中放在页面中间，上方可以选要派给哪个管家；发出第一句话后输入框自动沉到底部，进入正常对话。
- **流式输出**：管家的回复逐字出现，思考过程折叠成一行（点开可看全文），工具调用是独立卡片，能看到执行的命令、参数和结果。
- **每步 token 消耗**：每轮回复下方有一行小字显示这一步用了多少输入/输出 token；右侧详情栏里有整个会话的汇总。
- **图片附件**：输入框左侧 + 号可以带图（最多 5 张），随任务一起发给管家。
- **消息来源切换**：侧栏底部可以切换控制台会话归属于哪个聊天来源，也可以自己填一个（比如某个 QQ 群），新建任务的结果会推到那边去。
- **设置弹窗**：插件配置直接在控制台里改，打开时自动读取当前值；可以切浅色/深色主题，刷新后记住选择。

### 变更

- **存储格式更换（Breaking）**：会话数据从 `agents/<agent_id>/` 换成 `sessions/<sessionId>/`——每次会话是一个 `events.jsonl`（按顺序逐条记录会话里发生的每件事）加 `meta.json` 元数据，图片附件放在 `attachments/`。重开页面或重启 AstrBot 后按记录顺序恢复，消息不丢。旧数据首次启动时自动迁移，原目录保留不删。
- **Console 前端从 Vue 换成 React**：源码在 `webui/`，构建产物照常提交在 `pages/console/`，页面地址不变，AstrBot 侧仍是零依赖静态托管。
- **公式字体不再打进插件包**：数学公式用的 KaTeX 字体改为启动时从 AstrBot 面板自带的资源里复制复用，插件体积小了一截；万一没找到就用系统字体渲染公式，不影响其他功能。
- metadata.yaml / pyproject.toml / webui/package.json / `__version__` 升至 2.0.0。

### 不变

- `call_maid` / `maid_task` 的工具签名与语义不变，`/maid status`、`/maid stop` 命令不变。
- 不修改 AstrBot Core 任何文件。

## 1.5.1 - 2026-08-03

### 新增

- **Agent Fork（`console/actions/fork`）**：可将当前 Agent 经 rewind 折叠后的有效 transcript 复制到新 Agent（`RuntimeOrchestrator.fork_agent` + `RuntimeStore.clone_transcript`），不立即创建 Run，等待从副本继续。Run 卡片与侧栏接入 Fork 操作，新增 `useTimedConfirm` 组合式函数统一管理需二次确认的动作，移除未使用的 `readRunResult`。

### 修复

- **未处理的 Promise rejection**：`sync.start` 以 `void` 操作符包裹，避免未捕获的 Promise rejection 触发前端告警。

### 变更

- metadata.yaml / pyproject.toml / webui/package.json / `__version__` 升至 1.5.1。

## 1.5.0 - 2026-07-27

Console 前端从无构建的静态三件套重写为 Vue 3 + Vite 单页应用，修掉「停在某个焦点上会被自动刷新拽到底部」这类整页重绘导致的可用性问题；Run 操作按 Claude Code 语义重排为 复制 / 回溯 / Fork，并为「回溯」补上 append-only 的后端实现。

### 新增

- **Run 回溯（rewind）**：Run 卡片新增「回溯到这里」，把 Agent 退回该 Run 开始之前的状态——该 Run 及其之后的所有 Run 退出上下文，下次 resume 从更早的状态继续。实现是纯追加的：`rewind_to_run` 只往 `transcript.jsonl` 追加一条 `{"kind":"rewind","task_id":T}` 标记，折叠发生在读取时（`fold_rewound_records`），磁盘上一条记录都不删。因此被丢弃的 Run 仍可审计、回溯本身也能靠再追加一条标记撤销。被折叠的 Run 继续留在时间线上（虚线灰显 + 「已回溯」标签），但不再进入 `rebuild_contexts_for_resume`。新增 `POST console/actions/rewind`；Agent 有活跃 Run 时拒绝（409）。
- **Vue 3 + Vite 前端工程**：源码位于 `webui/`，`npm run build` 产出到 `pages/console/`（构建产物入库，AstrBot 侧仍是零依赖静态托管）。`webui/dev/` 提供 mock bridge 与 fixtures，可在 AstrBot 之外直接 `npm run dev` 开发。
- **Agent 任务标题**：新建 Agent 时先用请求文本生成本地兜底标题（`build_fallback_title`），随后 best-effort 调 LLM 生成简洁标题并经 SSE `runtime_title` 推送替换；LLM 不可用时保留兜底标题。标题持久化在 `agent.json` 的 `title` 字段。
- **粘性滚动**：时间线只在用户本来就贴着底部时才自动跟随；上滑查看历史时新内容不再抢走视口，改为右下角「↓ N 条新内容」提示，点击才回到底部。

### 修复

- **插件在 Python 3.12 下 import 失败**：`main.py` 把 `AstrMessageEvent` 放在 `TYPE_CHECKING` 块里，而 AstrBot 的 `CommandFilter` 会对 handler 跑 `inspect.signature(..., eval_str=True)`（`astrbot/core/star/filter/command.py:68`），配合模块顶部的 `from __future__ import annotations` 直接 `NameError`，导致整个插件加载失败、`/maid status` 与 `/maid stop` 全部不可用。改为运行时别名 `AstrMessageEvent = CoreAstrMessageEvent`。
- **焦点与滚动位置被轮询刷新打断**：旧前端每次同步都 `innerHTML` 全量重建对话流，展开的 trace 折叠回去、输入焦点丢失、视口被拽到底部。Vue 的 keyed 渲染只更新变化节点，上述状态不再受同步影响。
- **Console 一直停在“正在连接 AstrBot…”**：初始数据刷新完成后不再等待 SSE bridge 订阅请求返回。订阅成功时同步状态切换为 `live`；请求无响应或连接失败时继续使用已经启动的 5 秒轮询，不再阻塞 `booting` 收尾和整个界面。

### 变更

- **Run 操作按 Claude Code 语义重排**：常驻 复制 / 回溯到这里 / Fork 三个操作。「复制」有结果时复制结果、否则复制请求；「Fork」通过 `console/actions/fork` 复制当前 Agent 经 rewind 折叠后的有效 transcript 到新 Agent，不立即创建 Run，等待用户从副本继续；「停止」只在运行中出现，「读取结果」按钮已移除。
  - 原 `console/actions/rerun` 只会用同一条请求新建空 Agent，并不具备 Fork 的上下文复制语义；前端不再调用该入口。
  - 「回溯」需连点两次确认（与侧栏删除 Agent 同一约定），运行中禁用。
- `console/actions/rerun` 路由与请求/响应格式保持不变，仅前端展示改称 Fork。
- transcript 与导出接口的每个 run 段新增 `rewound` 布尔字段。
- metadata.yaml / pyproject.toml / webui/package.json / `__version__` 升至 1.5.0。

### 不变

- 不修改 AstrBot Core 任何文件。
- `call_maid` / `maid_task` 的工具签名与语义不变。
- 已有 `transcript.jsonl` 无需迁移：不含 rewind 标记的历史记录折叠后与原样一致。

## 1.4.1 - 2026-07-19

Console 对话流补齐第三方角色「大小姐（主人格）」：主模型派给女仆的请求此前混入用户气泡并被前端过滤丢弃，现改为独立角色气泡，时间线呈现 用户 → 大小姐 → 女仆 三方对话。

### 新增

- **Console「大小姐」角色气泡**：女仆运行轨迹在用户气泡与女仆气泡之间新增大小姐气泡（左对齐、accent 描边浅底），承载主模型派给女仆的 `【大小姐请求】`。气泡名称取全局默认人格名（`provider_settings.default_personality`），未命名（`default` 占位）或不可用时省略名称标签；头像统一使用 AstrBot 星标 `✦`。
- **transcript 结构化派活字段**：女仆首条 `user` 消息在保留完整 `content` 的同时，附带 `user_input`（对方原话）与 `mistress_request`（大小姐请求）两个结构化真源字段；`_build_agent_transcript_payload` 据此产出干净拆分的 `user_text` 与新增 `mistress_text`，transcript 与导出接口额外返回 `mistress_name`，前端无需解析拼接串。
- **测试**：覆盖结构化字段拆分、旧 transcript 的 `【…】` 标记兜底解析、仅大小姐请求（`user_text` 为空、不重复显示）、默认人格名解析与回退。

### 修复

- **大小姐派活指令不再被吞**：此前 `dispatch_prompt` 把 `【对方原话】` 与 `【大小姐请求】` 拼进女仆首条 user 文本，前端 `displayUserText` 只取首块（对方原话）导致大小姐请求被整段丢弃；现拆为独立 `mistress_text` 字段单独成泡，不再丢失。

### 变更

- **`user_text` 语义收敛**：`user_text` 现仅含人类原话（`【对方原话】`），不再夹带大小姐请求。`_build_agent_transcript_payload` 优先读结构化字段，缺失时对旧 transcript 按 `【…】` 标记兜底解析（历史记录的大小姐块可能带模板尾巴，属可接受降级）；context 访问收敛在路由层，纯 payload builder 保持无副作用可测。
- 抽出 `USER_INPUT_BLOCK_LABEL` / `MISTRESS_REQUEST_BLOCK_LABEL` 常量，dispatch 拼接与兜底解析共用同一份，消除魔法串重复。
- metadata.yaml / pyproject.toml / `__version__` 升至 1.4.1（一并修正 pyproject.toml 遗留的 1.3.0）。

## 1.4.0 - 2026-07-16

彻底移除 1.2 遗留引擎与兼容层，Console 前端重构为 Claude Desktop 风格。

### 移除（Breaking）

- **`call_maid` 的 `action` 参数**：1.2 的 `action=dispatch/steer/stop/done` 兼容路径整体删除。请使用 `request_text`/`resume_agent_id`/`run_in_background`/`tasks` 与 `maid_task(status/result/stop/steer)`。
- **1.2 后台执行引擎**：`background_registry.py`、`batch_registry.py`、`session_store.py` 及 main.py 中全部"回复发送后统一后台执行 + 追答回灌"链路（约 2000 行，自 1.3.0 起已不可达的死代码）。旧 `sessions/*.json` 数据保留在磁盘但不再读写。
- **弃用配置项**：`session_enabled`、`session_timeout_minutes`、`dispatch_auto_background_enabled`、`dispatch_auto_background_seconds` 不再读取。
- **`{maid_full_reply_block}` 占位符**：`dispatch_prompt_template` 中出现时按未知占位符处理，整体回退默认模板并告警。

### 变更

- **Console 前端重构（仿 Claude Desktop）**：暖色双主题（跟随 dashboard 亮/暗）、可折叠侧栏、居中对话流、空状态时段问候 + 居中 composer、执行轨迹改为随主题的"思考块"卡片、Inspector 改为按需滑出的详情面板。数据流（bridge SDK + SSE + 轮询）不变。
- `/maid status`、`/maid stop` 仅面向 1.3+ runtime run。

## 1.3.0 - 2026-07-15

Claude Code 风格 subagent runtime 重构：foreground-first 调度，稳定 agent_id + 独立 task_id，支持 resume/steer/stop/result 与批量并发。

### 新增

- **foreground-first runtime**：`call_maid` 默认在前台同步等待管家执行（最多 `foreground_timeout_seconds=50` 秒），短任务在同一 tool turn 直接返回结果；超时后同一 runner 原地转后台继续执行（不新建 task、不重启 runner），返回 `background_reason=timeout` 的结构化句柄。
- **稳定 agent_id + 独立 task_id**：新 dispatch 永远创建新 agent；显式 `resume_agent_id` 才恢复。running agent 的 resume 作为 steer（`runner.follow_up`），terminal/interrupted 的 resume 创建新 task_id 始终后台执行。
- **`maid_task` 工具**：对齐 Claude TaskOutput 语义，支持 `status/result/stop/steer`。`result` 默认阻塞 30 秒（最大 600 秒），成功读终态时认领 pending notification 避免重复唤醒；查询额外返回 `query_status=success|timeout|not_ready`。
- **批量 dispatch**：`call_maid(tasks=[...])` 最多 5 项，支持每项独立 foreground/background；foreground children 并发等待。原子预留容量，不足时整批拒绝且不创建 agent/task/transcript，结果保持输入顺序。
- **notification outbox**：completed/failed/stopped 生成稳定 `notification_id`，终态与 pending notification 同一原子 metadata 更新。首次完成立即唤醒，无定时重试；仅在插件重启、新用户消息或 `maid_task(result)` 时重新处理。按 UMO 加锁 + Claude snapshot 语义（开始投递合并当时所有 pending，处理期间新完成进下次）。用公开 `CronMessageEvent`/`build_main_agent`/`persist_agent_history` 重建唤醒链，不调 Core 私有方法。best-effort 去重，不承诺 exactly-once。
- **runtime 持久化层**：`agents/<agent_id>/{agent.json,transcript.jsonl,runs/<task_id>.json,outputs/<task_id>.txt}`，与旧 `sessions/*.json` 完全隔离。transcript append-only，resume 时过滤损坏尾部与未配对 tool calls。30 天无活动清理（不删 memory 与旧 sessions）。
- **child toolset adapter**：插件内复现 AstrBot handoff 工具选择规则（不依赖 `_build_handoff_toolset` 私有方法）。child 移除 `call_maid`/`maid_task`/所有 `transfer_to_*` 禁止递归。`memory_agent_names` opt-in 的 agent 自动补 AstrBot 原生 Read/Write/Edit，memory 以 UMO+agent_name 隔离，MEMORY.md 最多内联 200 行/25000 bytes。
- **并发容量**：每 UMO 最多 5 个 active runs，全局最多 20；无队列，超限立即拒绝。
- **console agent/run 层级**：前端按 agent → run 展示 runtime 状态、foreground/background、interrupted、notification 与 ID，并提供 resume/steer/stop/result；SQLite 继续作为审计记录而非状态真源。
- **runtime Console 操作**：Agent/Run 行悬停或选中后显示停止、读取结果和删除操作。删除采用二次确认，仅允许无活跃 Run、无待投递通知的终态 Agent，并同步清理 transcript/runs/outputs 与 SQLite 审计记录。
- **runtime 调用链恢复**：修复 1.3 Agent/Run 在运行中和完成后均只显示“0 次工具调用”的回归。工具开始/结束使用独立 SSE 实时推送，历史与断线刷新从 append-only transcript 按 task 边界恢复；左栏隐藏已有 runtime 对应项的重复 SQLite 审计副本。
- **Console 实时时长**：活跃 Run 改用浏览器本地时钟每秒更新，终态固定使用 `started_at → ended_at`；不再用仅在状态变化时刷新的 `updated_at` 充当运行计时器，避免持续显示 `0s`。
- **配置**：新增 `foreground_timeout_seconds`/`memory_agent_names`/`max_active_per_umo`/`max_active_global`/`retention_days`。`dispatch_prompt_template` 改为不依赖 `{maid_full_reply_block}` 的自包含模板。
- **测试**：覆盖同 runner foreground 迁后台、foreground 容量占用与释放、batch 原子拒绝、UMO/sender 权限、shutdown interrupted、notification 单次 snapshot 合并、JSONL 尾部过滤、child event 隔离、嵌套工具 Schema 和 memory。

### 变更

- `call_maid` 新接口：`request_text`/`agent_name`/`resume_agent_id`/`run_in_background`/`tasks`/`action`。旧 `action=dispatch/steer/stop/done` 保留兼容转换并输出弃用提示；`done` 变为无状态 no-op。
- 主模型工具集现在同时暴露 `call_maid` 与 `maid_task`。
- 重启遗留的 `starting/running` run 静默转为 `interrupted`，不自动重放、不主动通知。
- 兼容 1.2 用户配置中遗留的 `{maid_full_reply_block}`：1.3 runtime 将其映射为空并记录弃用警告；未知或损坏的模板占位符自动回退默认模板，不再导致 run 立即 failed。
- runtime 控制操作改为默认拒绝缺少 event/UMO/sender 的调用；Console 仅在三个明确调用点使用 `trusted_internal=True`，不再通过 `source=dashboard` 隐式绕过权限。
- 已处于任意终态的 run 不再重复 finalize 或重新生成 notification；child computer-tool roster 增加与当前 Core 的契约测试，legacy toolset 过滤不再原地修改输入对象。
- runtime/outbox 并发测试改用 started/idle 事件和条件等待，移除固定 20–200ms wall-clock 等待及未使用的 factory helper。
- metadata.yaml / pyproject.toml / `__version__` 升至 1.3.0。

### 不变

- 不修改 AstrBot Core 任何文件。
- 旧 `sessions/*.json` 与 `background_registry`/`batch_registry` 保留兼容，1.3.0 runtime 与之隔离。

## 1.2.0 - 2026-07-14

- 为 task、batch 与 session 增加 `agent_id` / `parent_message_id` 溯源；后台结果回灌优先使用 agent 锚点定位，文本匹配仅作为历史兼容兜底。
- 将 chat single、chat batch 与 dashboard dispatch 收敛到统一 launcher，并将 UMO 活跃任务检查与登记改为原子操作，消除并发派发竞态。
- 新增可配置的慢任务后台提示；达到阈值只发送 `auto_background` 事件与聊天提示，不改变任务状态，`stop/steer` 继续可用。
- 参考 Claude Code sidechain transcript，在每个完整 agent step 后做 best-effort session checkpoint，异常或重载时尽量保留已完成步骤。
- 插件启动时自动把失去内存 runner 的 `queued/running/stopping` 控制台任务收敛为 `stopped`，并记录 `interrupted` 审计事件。
- 管家 toolset 强制移除 `call_maid` 控制面工具，避免 subagent 递归登记无法实际启动的幽灵任务。
- 群聊中的 `stop/steer/done` 增加任务与 session 所有者校验，避免其他成员控制或结束不属于自己的后台上下文。
- 将显式 stop 后、runner 未生成最终响应的异常路径统一收敛为 `stopped`，避免误报任务失败。
- 新增 18 项回归测试，覆盖溯源、Provider 元数据隔离、历史兼容、统一分流、并发占用、慢任务提示、取消收尾、停止异常收敛、群聊所有权、重启恢复与递归工具隔离。

## 1.1.5 - 2026-07-06

- 修复 AstrBot 插件页 iframe 禁用原生弹窗时，会话列表置顶、重命名、删除按钮点击后无有效反馈的问题。
- 将重命名改为列表内联编辑，将删除改为二次点击确认，并为置顶/取消置顶补充 toast 状态反馈。

## 1.1.4 - 2026-07-06

- 新增插件内置 WebUI 控制台：提供 UMO 隔离的会话列表、GPT 风格任务续接输入、右侧 Inspector、历史导出、会话删除/重命名/置顶与全局配置编辑入口。
- 修复 WebUI bridge 初始化与 SSE 订阅路径，前端现在正确等待 `AstrBotPluginPage.ready()`，订阅 `console/stream`，并在 SSE 不可用时使用轮询兜底同步，避免页面卡在“离线/加载中”或发送时报 `apiPost` 未定义。
- 修复自动刷新抢占阅读位置的问题：轮询刷新会保留聊天滚动位置，只有用户已在底部时才自动跟随新输出；同时保留用户手动展开/收起的思考过程状态，避免刷新后自动关闭。
- 恢复并增强完整工具调用链监控：子 agent 执行时持续保存 runner messages 快照，Inspector 新增“调用链”页签，按顺序展示 assistant 输出、工具调用、工具返回和原始 messages。
- 修复 Console 续接运行中任务时被 sender 校验误拦截的问题，Dashboard/Console 创建的后台任务现在允许同一 UMO 继续补充要求。
- 移除 WebUI 中危险的“清空历史”按钮，导出历史保留；停止、结束 Session、重跑等操作改为真实后端动作接口。
- 将 WebUI 任务更新/删除接口改为 POST 路由，适配 AstrBot 插件页 bridge 仅支持 GET/POST 的限制。

## 1.1.3 - 2026-06-07

- 修复流式输出关闭后，后台追答请求（`_request_maid_follow_up`）中因携带结构化 tool 消息历史且未提供 `tools` 定义，导致部分严格的 API 平台（如 Minimax、DeepSeek）返回空输出或报错 `EmptyModelOutputError` 的兼容性问题。此修复通过将追答历史简化为标准文本对话实现 100% 服务端兼容性。
- 修复在流式输出开启时，由于 AstrBot 核心 early-return 导致 `after_message_sent` 钩子失效，后台任务无法被正常唤起的问题。现在通过拦截并包裹 `result.async_stream`，在流式传输结束后自动触发后台调度，实现与流式模式的无缝融合。

## 1.1.2 - 2026-04-29

- 仅在插件侧修复 `call_maid` 工具历史兼容问题，将 `ThinkPart` / `TextPart` 等 Pydantic 内容块递归转为 OpenAI 兼容的普通 dict，避免 AstrBot core 在 `_finally_convert_payload` 中对对象调用 `.get()` 崩溃。
- 为 Kimi / Moonshot thinking 模式补齐 assistant tool-call 历史的 `reasoning_content`，支持从 `LLMResponse.reasoning_content`、`raw_completion.message.reasoning(_content)` 与 `reasoning_details` 兜底提取。
- 安装窄作用 OpenAI provider 兼容补丁，将 Kimi / Moonshot 返回在 `message.reasoning` 的 thinking 内容映射到 `LLMResponse.reasoning_content`，使 core 同轮 tool-loop 二次请求能自动携带 `ThinkPart`。
- 在主模型请求清洗阶段修复旧历史中缺失 `reasoning_content` 的 assistant tool-call 消息，避免后续请求触发 `thinking is enabled but reasoning_content is missing`。
- 修复 core 同轮二次请求前的临时 `ToolCallsResult`，为 assistant tool-call 内容补入空 thinking 块，使 OpenAI payload 转换阶段能生成 `reasoning_content`，同时保留正常工具返回文本。
- 后台追答和主会话工具历史写入时保留主模型 thinking 内容与签名，确保后续轮次能正确回放工具调用上下文。

## 1.1.1 - 2026-03-19

- 移除主模型请求阶段对 `tool` / `tool_calls` 历史的清洗，避免已发生工具调用被下一轮对话灾难性遗忘。
- 为 `call_maid` 的 `dispatch / steer / stop / done` 全动作补齐主会话结构化历史记录，格式对齐 AstrBot 原版 `assistant(tool_calls)` + `tool(result)` 消息对。
- 在后台结果真正回到大小姐时，额外补写 `call_maid` 的结果记录，确保后续轮次能感知“大小姐确实调用过管家且已收到结果”。
- 统一 `call_maid` 工具记录的消息构造逻辑，集中处理参数、`tool_call_id` 与 thinking 兼容字段，避免多处手工拼装格式漂移。
- 修复 OpenAI / Responses 在 thinking 开启时对 assistant tool-call 消息缺失 `reasoning_content` 的 400 报错。

## 1.1.0 - 2026-03-18

- 将主模型调度入口从 XML 协议迁移为原生 `call_maid` Function Call，`dispatch` 改为“登记后后台执行”而非阻塞当前主链路。
- 新增 `hide_transfer_tools` 配置开关；当 `hide_native_tools=false` 时可独立隐藏 AstrBot 原生 `transfer_to_*` 工具。
- 修复后台回灌消息未写入主对话历史的问题，避免任务完成后下一轮对话仍误判为“管家仍在执行中”。
- 移除主模型额外提示词注入，改由 `call_maid` 工具自身的描述承担动作说明。

## 1.0.1 - 2026-03-17

- 新增 `hide_native_tools` 配置开关，可按需保留或隐藏大小姐可见的 AstrBot 原生工具。
- 支持单轮回复中解析多个 `<call_maid ...>` 标签，并以 batch 方式并发调度多个管家子任务。
- 为 batch 子任务补齐独立 session、统一汇总回灌、批量 `/maid status`、整批 `/maid stop` 与 batch steering 拒绝语义。
- 修复 `prompt_injector.py` 日志导入，统一改为 `from astrbot.api import logger`。
- 修复单任务在 `stopped / error` 终态下的 session 收尾，避免脏上下文污染下次复用。
- 为 `background_registry` 与 `batch_registry` 增加完成态清理，避免后台任务与 batch 记录长期堆积。
- 调整 batch 停止链路，改为按 batch runner 显式 `request_stop()`，避免共享 event 注册表导致的停止遗漏。
- 重新通过 `ruff format`、`ruff check --fix`、`py_compile`、`compileall` 与 IDE 诊断验证。

## 1.0.0 - 2026-03-15

- 初始化 `代理女仆` 插件，提供“大小姐 + 管家”双代理模式。
- 主模型默认禁用原生工具，仅通过 XML 协议块调用后台管家 subagent。
- 新增基于 `<call_maid ...>` 的协议解析与用户可见输出清洗。
- 支持单 active 管家 session 持久化、超时失效与跨轮复用。
- 新增后台任务注册表，支持查询运行状态、停止任务与对运行中的管家进行 steering。
- 新增 `/maid status`、`/maid stop` 控制入口，以及主模型可见的 `status / stop / steer` 控制标签。
- 子 agent 运行链路对接 AstrBot 的 active runner / stop 机制，并补齐上下文压缩与 context summary 所需的上下文预算参数。
- 增加子 agent 上下文预算、估算 token、实际 token usage 等调试日志。
- 新增“服侍模式”，可在用户未继续发言时由大小姐按协议主动追加多轮回复。
- 提供可配置的主模型协议提示模板、管家调度提示模板、服侍模式续发提示词与最大续发轮数。
