# Changelog

## 2.0.69 - 2026-10-10

### 修复

- 工具超时、schema 与上下文压缩设置兼容新旧两种执行配置布局，最低 AstrBot 版本准确声明为 `4.27.3`。
- 删除会话与派发共用聊天锁；报告认领在取消或早退时可靠结算，重启只恢复未送达报告而不重跑旧任务。
- 控制台可保存完整配置；主背景过滤主代理 system 消息，并在子上下文被压缩后重新锚定。
- 压缩步不再重复发射历史事件；补跑轮失败时补充要求有明确终态；达到预算的收尾步清理等待注入的要求。
- 控制台 Fork 保留父会话的执行环境，孤儿轮次与提前失败的轮次也能拿到真实消息快照。
- 任务查询改用作用域索引，补充要求的主背景改为独立快照引用；保留期清理失效代次的旧任务、无用快照与空路由状态。

## 2.0.68 - 2026-10-08

### 新增

- 子代理自动继承主对话背景并保留自身工作记录。首次派发带入当前主对话，后续只追加主对话新增内容与本轮任务； 主历史被压缩或改写、无法增量对齐时，重新组织最新背景与已有工作记录。
- 任务与上下文生命周期分离。`task_id` 只标识工作身份与每轮结果，session 只承载工作上下文；  空闲窗口过期后继续旧任务仍能正确定位，但使用重新继承当前主对话的新上下文。
- 新增插件配置 `max_agent_steps`（默认 128）与 `session_idle_timeout_hours`（默认 5）。  执行预算由插件独立控制，空闲窗口整个主对话共用，运行期间暂停计时。

### 变更

- 五个模型工具重新定义契约：模型不再填写子代理名称或 session ID，统一使用 `default_agent_name`；  `maid_agent` 负责派发与继续已结束的工作，`maid_send_message` 只给运行中的任务补充要求，两者不互相降级或转换。
- 单一或已选定的默认工作上下文内，新任务沿用上下文而不新建 session；运行中再次普通派发直接报错，  不排队、不自动改成补充；并行新建由 `force_new` 或批量入口显式发起。批量执行期间不设置默认，  全部结束后存在多份记录时要求选择一次，此后普通派发沿用该默认。
- 运行消息、原始工具调用配对与多媒体改为完整持久化，不再从展示事件反推上下文；控制台 Fork 使用轮次快照。
- `new`/`reset` 成功后停止旧任务、取消未执行的补充与自动补跑，旧进度与旧报告不再进入新对话。

### 修复

- 子代理执行轮数不再读取 AstrBot 的 `max_agent_step` 并回退 30。原先该键已随框架迁移失效，  导致长任务在第 30 轮被拔掉工具强制收尾；现在按插件配置的预算执行，达到上限返回 `step_limit` 终态。
- 工具超时、schema 与上下文压缩设置改从当前 AstrBot 的 `agent_runner.config` 结构读取。

### 测试

- 新增任务路由、空闲窗口与上下文快照、执行预算三组测试，覆盖并行选择、补充未消费自动补跑、  重置隔离、压缩后展示连续性与超过 30 轮的真实执行。

## 2.0.67 - 2026-09-09

### 新增

- 控制台支持通过文件选择、拖拽和剪贴板粘贴上传任意类型附件；文件以会话隔离的随机 ID 保存，并可由管家引用。
- 新增 `maid_deliver_file`，让管家将生成的文件作为会话产物提供下载；聊天会话会尝试直接发送文件给用户。

### 修复

- 附件下载仅允许访问所属会话已引用的文件，并使用服务端保存的文件名、MIME 类型与大小元数据。
- **管家任务通知不再向主 Agent 的 system_prompt 注入硬编码英文指令**。转述要求改为写在本轮 `req.prompt` 的通知正文之前，随轮生效且不污染人格或会话历史。

## 2.0.65 - 2026-09-05

控制台界面：中文换 MiSans，浅色主题改纸质底色。

### 新增

- **中文字体改用 MiSans**。整包 4.6 MB 塞不进来——宿主对插件页资源不允许浏览器缓存，每次打开都要重下。改为按 unicode-range 切块，浏览器只取屏幕上真的出现过的字所在的块，实测首屏 161 KB。控制台显示的是 agent 输出、文本任意，所以补齐了字频划分没覆盖的一万五千多个码位（罕用字、繁体、古文），确保不会在句子中间掉回系统字体。
- **浅色主题改为纸质底色**。卡片、菜单、弹窗仍是纯白，浮在纸面上；侧栏的悬停与选中态一并换成同色系加深。深色主题不变。

### 优化

- 裁掉界面用不到的字体轴范围，每次打开少传 68 KB，字形渲染逐像素不变。

## 2.0.64 - 2026-09-05

控制台前端：打开变快，修好几处失效的界面，换上新字体。

### 性能

- **控制台产物从 3,465 KB 降到 1,358 KB（-61%，gzip 566 → 330 KB）**。产物里原有 34 个 TextMate 语法、2.52 MB，占 76.1%，形态是顶层 `Object.freeze(JSON.parse(…))`——React 挂载前主线程要先同步 parse 2.52 MB。根因是传递依赖：`ruby` 自己只有 52 KB，闭包却经 haml → html → javascript/css 拉进 cpp/java/glsl 共 20 个模块 1961 KB。而宿主 60s asset_token 约束强制 `inlineDynamicImports`，「按需加载语法」的 `import()` 一个字节都没省。语法表收敛到 13 门，首屏同步 parse 降到 0.51 MB（-80%）。
- shiki 预热挪进 `requestIdleCallback`；启动时两条 SSE 订阅由串行改并发，省一整个桥往返。

### 修复

- **轮次导航轨此前完全失效**。折叠器原地增长节点数组、引用恒定，`memo` 永久命中缓存，首次挂载不足 2 轮返回空后就再也不出现；且活动轮用的锚点属性全项目无人写入。
- **后台会话的时间与排序一直冻着**：事件只在当前会话通知界面。
- **新会话标题一直显示「新会话」**：标题投影在容器尚不存在时被整个跳过。
- **模型选择器可能让整个控制台白屏**：`session.models` 返回缺字段的响应时，渲染期直接抛错。
- 消息尾部的时间与用量 pill 没有对齐（缺 `box-sizing`，28px 的盒实际渲染成 40px）。
- 会话列表无法用键盘操作（行是带点击事件的 `div`，没有焦点与按键处理）；弹窗补焦点陷阱与关闭还焦。

### 新增

- **每轮用时面板**：尾部「用时」可点开查看本轮总用时、输出速度（TPS）、首 token 用时（TTFT）。
- **工作状态行显示计时与上下文占用**，计时锚点取 turn/start 事件时刻，切走再切回不会归零。
- **界面字体**：侧栏字样改用 Outfit（与 AstrBot 本体 logo 同源），正文改用 Anthropic Sans，思考块用其真 Italic 字形，中文走苹方 / 微软雅黑。

## 2.0.63 - 2026-09-04

### 修复

- **续派同一个管家时，第二轮的汇报没人转达**。`resume_agent_id` 复用上一轮跑完的会话，派发处的 `update_meta` 清了 `notified` 和 `deliveryStatus`，却漏了 `deliveryClaimed`。于是第二轮跑完，`_on_turn_terminal` 过得了 `notified` 那道门，却在 `claim_delivery()` 上被上一轮留下的 `deliveryClaimed=True` 判成重复转述，通知被静静跳过——任务其实早就完成了，大小姐要等用户主动问才去读。现在三个投递字段一起归零。

## 2.0.62 - 2026-09-04

破坏性改动：删掉前台/后台执行模式，派发一律非阻塞，管家干活时会自己在聊天里说话。

起因是前台模式把「用真实聊天 event」和「阻塞等结果」绑在了同一个开关上。阻塞那半边会把主 agent 的 run 一直挂在工具调用里，AstrBot 于是把用户随后发的消息当成 follow-up 吞进 tool result（`[SYSTEM NOTICE] User sent follow-up messages...`），用户没法在管家干活的同时继续聊天。这两件事本来正交，这个版本把它们拆开。

### 破坏性

- **删除 `dispatch_session_mode` 配置项**。聊天派发不再有执行环境之分。旧配置里残留这个键会被加载时忽略，不影响启动。
- **`maid_agent` 删除 `run_in_background` 参数**（含 `tasks[]` 里那一份）。派发永远立即返回 `{agent_id, task_id, dispatch_id}` 句柄。
- **`maid_task_output` 删除 `block` / `timeout_ms` 参数**。它曾经能阻塞最长 600 秒，是第二条把主 agent 钉死的路径；现在只读一眼，运行中返回进度，跑完返回终态。
- 两个工具的 description 重写为显式契约：`maid_agent` 只入队、毫秒返回、收到后必须停止调工具并在本轮回话；`maid_task_output` 是一次性快照而非等待原语。返回值另加 `next` 字段，在决策点直接给出下一步。不写清楚的话主模型会自己轮询 `maid_task_output` 把 run 吊着——阻塞的单位是整个 agent run，不是单次工具调用。
- 会话元数据与 host 帧不再携带 `executionMode` / `backgroundReason` / `foregroundLease`，前端类型同步移除。

### 新增

- **管家会在聊天里说话**：干活过程中每一段正文（没有正文时用推理）以 `管家名: ...` **即时**发到聊天。判据是这一步带不带 `tool_calls`：带的是过程播报、立刻发；不带的是终答、留给大小姐转述，避免同一份结论出现两遍。不能拿「后面还有没有下一段」当判据——那会把第一句话一直押到下一段出现，实测能拖到整个任务结束才冒出来。
- 合成事件的 `send` 直接走 `Context.send_message`，不调基类。基类那套是给平台绑定的真实事件用的，我们只满足一半契约；它眼下只上报一条 metric，不值得拿它去换「基类改实现就整条静默丢失」的风险（`_speak` 会把异常吞成 warning）。
- **派发时快照真实 event**：图片复制进会话附件区，发送者、昵称、群、self_id、平台身份与能力位一并带走。管家不再持有主流程的 event，因此主流程结束清理临时文件不再影响它。合成事件的 `send` 改为按 UMO 走平台适配器真实投递。
- **两个投递开关，默认都关**：`show_maid_speech`（正文投递）与 `show_maid_tool_status`（函数调用状态投递，对齐 AstrBot 的「输出函数调用状态」，格式 `管家名: 🔨 调用工具: xxx`）。默认状态下聊天里只有大小姐的最终转述。工具状态在工具刚开始执行时就发出，是模型不输出正文时唯一的实时进度信号；代价是长任务一步一条。
- **`/maid status` 与 `maid_task_output` 有内容了**：管家名、agent 短 ID、已跑秒数、第几步、当前/刚用完的工具**及其入参与输出**、最近一段正文（没有正文时退回推理）。只给工具名没有信息量——女仆大量时间待在只发工具调用、不说话的步里。数据全部从事件日志尾部现读，不额外记账。工具入参用 `json.loads(strict=False)` 解析：模型常把裸换行写进字符串，严格模式会让整块参数退化成 JSON 噪音，而那一坨正是要看的代码本身。

### 修复

- **大小姐的转述整段丢失**：`_notify_main_agent` 跑完 `step_until_done` 只把文本拼进 history 存盘，投递押在模型愿不愿意调 `send_message_to_user` 上（该指令自 1.3.0 `b26ea87` 留下）。模型直接把转述写成正文时这段话就石沉大海——任务跑完聊天里什么都不出现。现在模型正常作答即可，没调工具就由插件按 UMO 自己投递；system prompt 也不再往工具上引导。
- **管家 `send_message_to_user` 的输出进黑洞**：合成事件的 `send` 只往一个没人读的 list 里 append，管家主动发的每条消息都被静默丢弃。现在真实投递。
- **`fg_timeout` 读错配置键**：`_main_tool_call_timeout` 读的是 `provider_settings.tool_call_timeout`，而 Core 读的是 `agent_runner.config.misc.tool_call_timeout`，前者不存在，于是永远取写死的 120。随前台等待一并删除。
- **被 Core 掐断时结果静默丢失**：前台等待只接 `asyncio.TimeoutError`，Core 超时取消抛的是 `CancelledError`，接不住就跳过了补 `notify=True` 的那段，会话保持 `notify=False` / `deliveryStatus=skipped`，管家跑完 `_on_turn_terminal` 直接 return，结果烂在日志里。随前台等待一并删除。
- **通知回灌不排队**：`_notify_main_agent` 绕开 pipeline 自起主 agent，既不拿 session lock 也不注册 runner。改成非阻塞后每个任务都以它收尾，会插进用户正在进行的一轮，两个 runner 抢写同一份 conversation history。现在包在 `session_lock_manager.acquire_lock(umo)` 里；同时「这份汇报归谁转达」改成 `deliveryClaimed` 原子认领（在 `log.lock` 里读改写），因为通知回灌和 `maid_task_output` 各自持有的外层锁不是同一把。
- **retention 清理每小时报错**：`running` 是 set，却按 dict 调 `running.pop(sid, None)`，`TypeError` 被外层 `except` 吞成一条"清理失败"。该变量本就没用，删掉。

### 不变

- 五工具 API 的其余签名、`/maid stop`、控制台 RPC 协议、SSE 帧格式、插件页集成均不变。
- 控制台派发仍在隔离环境里跑，只是不再叫"background 模式"，也不会往聊天里发消息。
- 并发上限语义不变（`max_active_per_umo` / `max_active_global`），前台租约删除后它是唯一的并发闸门。
- 已有会话数据无需迁移，不修改 AstrBot Core 任何文件。

## 2.0.5 — 2026-09-03

Breaking release. The legacy two-tool control plane and all migration, alias, timeout-switch and automatic-repair paths were removed. Default-Agent fallback returns as an explicit `default_agent_name` setting.

- Added the Claude-style `maid_agent`, `maid_send_message`, `maid_list_agents`, `maid_task_output`, and `maid_task_stop` tools.
- `hide_native_tools=true` now exposes only those five tools; its default and disabled behavior are unchanged.
- Added strict `dispatch_session_mode` with stable `foreground` real-event execution and `background` isolated harness execution.
- Added one foreground lease per UMO, deterministic batch allocation, lifecycle release, delivery tracing, and Console runtime metadata.
- Console configuration now honors schema options, reports strict settings errors, requires a selected Agent, and labels its tasks as isolated sandboxes.
- Loading existing settings is now tolerant: removed legacy keys are ignored and invalid stored values fall back to defaults with a warning, so a pre-2.0.5 config cannot block plugin startup; saving stays strict and persists a normalized settings object.
- Restored `default_agent_name`: an unmatched or disallowed `subagent_type` falls back to it instead of failing; resolution errors now list the allowed agents.
- When no subagent is configured at all, the plugin now provisions one automatically — named after `default_agent_name`, all tools, current-chat provider, persona shared with the dispatch prompt template — instead of leaving dispatch with nothing to resolve.
