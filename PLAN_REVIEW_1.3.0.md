# MaidAgent 1.3.0 — Plan Review 文档

> Claude Code 风格 Subagent Runtime 重构的实现审计文档
> 版本：1.3.0 ｜ 日期：2026-07-15 ｜ 仓库：`astrbot_plugin_maid_agent`

---

## 0. 文档目的

本文档供 planner review 使用，记录 1.3.0 重构的：
1. 目标与硬约束
2. 架构决策（每条附 Claude Code 源码依据 + AstrBot Core 边界）
3. 模块设计与公开 API
4. 关键实现细节（foreground 迁后台、batch 原子性、notification snapshot、resume/steer、递归禁止、memory）
5. 验证证据（测试矩阵 + 基线命令）
6. 已知风险与未实现项
7. 与 1.2.0 的兼容性边界

---

## 1. 目标与硬约束

### 1.1 目标
将 1.2.0 的「回复发送后统一后台执行」模型重构为 **foreground-first** runtime，对齐 Claude Code 的 `AgentTool` 语义：
- 短任务在当前 tool turn 同步返回结果
- 长任务超时后同一 runner 原地转后台
- 稳定 `agent_id`（跨 resume）+ 独立 `task_id`（每次执行）
- resume / steer / stop / result / batch 全套原语
- notification 投递对齐 Claude 的 opportunistic snapshot

### 1.2 硬约束（全程守住）
| 约束 | 状态 |
|---|---|
| 只修改 `astrbot_plugin_maid_agent`，不改 AstrBot Core | ✅ `git -C C:/astrbot/AstrBot status` 空 |
| 不接入 DevKit、不 fork/复制 DevKit | ✅ 未引入 |
| 不实现 worktree | ✅ 未实现 |
| 保留 1.2.0 未提交改动 | ✅ background_registry/batch_registry/session_store 保留兼容 |
| foreground 阈值 < Core 本地工具 60s 超时 | ✅ 锁定 50s，config 越界回退 |

---

## 2. 架构决策（附源码依据）

### 2.1 foreground 50s 迁后台（不新建 task / 不重启 runner）

**Claude Code 依据**：`AgentTool.tsx` foreground 自动迁后台阈值 120s，但迁移语义是「同一 runner 继续在后台跑，主线程不再等待」。

**AstrBot Core 边界**：
- `astr_agent_tool_exec.py:_execute_local` 用 `asyncio.wait_for(anext(wrapper), timeout=run_context.tool_call_timeout)` 包住每个本地工具，默认 60s（`internal.py: settings.get("tool_call_timeout", 60)`）。
- 因此 `call_maid` 作为本地工具，其整个执行被 Core 的 60s `wait_for` 包住。foreground 总预算 = min(我的外层 wait_for, Core 60s)。
- 选 50s 留 10s 余量，确保我的 `asyncio.wait_for` 先触发迁后台，而非 Core 先取消 `call_maid`。

**实现**（`runtime_orchestrator.py:_await_foreground`）：
```python
timeout = self.foreground_timeout_seconds  # 50
try:
    finalized = await asyncio.wait_for(asyncio.shield(execution_task), timeout=timeout)
except asyncio.TimeoutError:
    # execution_task 未被取消；同一 runner 继续在后台跑
    await self.store.update_run(agent_id, task_id, mode=MODE_BACKGROUND,
                                background_reason=BACKGROUND_REASON_TIMEOUT)
```
- 迁移后 `task_id` 不变，`mode` 从 foreground 改 background，`background_reason=timeout`。
- foreground 从 starting 起即占 capacity slot；迁后台不更换 slot，终态在 `finally` 释放。

**验证**：`test_foreground_timeout_migrates_to_background`（50ms 预算，runner 阻塞 → 迁后台 → release 后终态 completed）。

### 2.2 稳定 agent_id + 独立 task_id

**Claude Code 依据**：`runAgent.ts` 稳定 agentId，`resumeAgent.ts` 恢复时复用 agentId 但新建 run。

**实现**：
- `RuntimeStore.create_agent` 生成 32-hex `agent_id`，跨 resume 稳定。
- 每次 dispatch / resume 生成新 32-hex `task_id`。
- `AgentMeta.active_task_id` 记录当前 active run（每 agent 同时最多一个 active run）。
- resume 路由（`_dispatch_resume`）：
  - running agent → `steer`（`runner.follow_up`），不新建 task
  - terminal/interrupted → 新 `task_id`，始终 background

**验证**：`test_resume_running_routes_to_steer`、`test_resume_terminal_creates_new_task_background`。

### 2.3 batch 原子容量拒绝

**Claude Code 依据**：`toolOrchestration.ts` concurrency-safe tools 并发，但容量不足时整体拒绝。

**实现**（`dispatch_batch`）：
```python
async with self._lock:
    try:
        await self._reserve_capacity_unlocked(umo, len(requests))  # 原子检查
    except CapacityExceededError as exc:
        raise BatchCapacityError(str(exc)) from exc
    for req in requests:
        agent = await self.store.create_agent(...)  # 全部创建后才释放锁
        run = await self.store.create_run(...)
        created.append((agent, run))
```
- 容量不足 → `BatchCapacityError`，**不创建任何 agent/task/transcript**（锁内全部完成或全部不创建）。
- 最多 5 项（`call_maid` 层校验 + orchestrator 层校验）。
- batch 不允许 resume；每项尊重 `run_in_background`，foreground children 并发等待并分别 inline 完成或 50 秒迁后台。

**验证**：`test_batch_atomic_capacity_rejection`（3 项 vs cap=2 → 拒绝，`list_agent_ids` 为空）。

### 2.4 notification outbox（snapshot 语义 + best-effort 去重）

**Claude Code 依据**：
- `messageQueueManager.ts` / `query.ts`：notification 各自入队，query snapshot 当前所有可消费通知一起作为 attachments。
- `LocalAgentTask.tsx`：terminal notification 格式 + `notified` 去重。

**实现**（`notification_outbox.py`）：
- 终态（completed/failed/stopped）在 `RuntimeStore.finalize_run` 里**原子**生成 `notification_id` 并写入 pending notification（同一 `os.replace` 写 run.json）。
- `queue_delivery(umo)`：
  - 若该 UMO 已有投递在进行 → 标记 `_pending_redelivery`，立即返回（snapshot 语义：新完成进下次）
  - 否则启动 `_deliver_pass`：持 UMO 锁，`list_pending_notifications` snapshot 当前所有 pending，逐个调 notifier
- **无定时重试**。仅在三种触发时重新处理：
  1. 插件重启（`on_restart` 扫描所有 pending）
  2. 新用户消息（`note_user_message`）
  3. `maid_task(result)` 成功读终态（认领当前 notification，并触发同 UMO 其余 pending 的重试）
- 去重：`_already_delivered` 扫描 conversation history 的 `MAID_NOTIFICATION_ID_META_KEY` 标记。**best-effort，不承诺 exactly-once**。

**唤醒链**（`main.py:_notify_main_agent`）用**公开** Core API 重建，不调私有 `_wake_main_agent_for_background_result`：
```python
cron_event = CronMessageEvent(context=ctx, session=session, message=merged_summary, extras=extras)
result = await build_main_agent(event=cron_event, plugin_context=ctx, config=config, req=req)
async for _ in result.agent_runner.step_until_done(30): pass
await persist_agent_history(ctx.conversation_manager, event=cron_event, req=result.provider_request, summary_note=summary)
```

**验证**：`test_first_delivery_claims_pending`、`test_snapshot_merges_all_pending`、`test_new_completion_during_delivery_picked_up_next`、`test_best_effort_dedupe_via_history`、`test_no_periodic_retry_only_on_triggers`、`test_result_claim_skips_delivery`。

### 2.5 transcript append-only + resume 重建

**Claude Code 依据**：`resumeAgent.ts` 过滤 unresolved tool uses；sidechain transcript append-only。

**实现**（`runtime_store.py`）：
- `transcript.jsonl` 只追加（`_append_jsonl`），不重写。
- `_read_jsonl` 容忍损坏尾部：遇到第一个 `json.JSONDecodeError` 即截断后续（append-only 不变量保证后续不可靠）。
- `rebuild_contexts_for_resume`：
  1. 跳过 `_control` 记录
  2. 追踪 assistant 的 `tool_calls` id，tool 结果到达时 discard
  3. 若尾部 assistant 的 tool_calls 仍 unresolved → 截断该 assistant 消息（避免 LLM 收到无结果的 tool_call）

**验证**：`test_jsonl_append_and_corrupt_tail_truncation`、`test_rebuild_contexts_truncates_unresolved_tool_calls`、`test_rebuild_contexts_keeps_paired_tool_calls`。

### 2.6 重启 reconcile（静默 interrupted）

**Claude Code 依据**：Claude 重启不自动重放 sidechain。

**实现**（`RuntimeStore.reconcile_on_restart`）：
- 扫描所有 agent 的 `active_task_id`
- 若 run 状态 ∈ {starting, running} → 改 `interrupted`，清 `active_task_id`
- **静默**：不自动重放、不主动通知
- console 层同步收敛（`console_store_reconcile_runtime` 写 interrupted 审计事件）

**验证**：`test_reconcile_on_restart_collapses_running_to_interrupted`。

### 2.7 child toolset 递归禁止 + memory

**Claude Code 依据**：`agentMemory.ts` / `loadAgentsDir.ts` 自动补 Read/Write/Edit；memory scope 隔离。

**实现**（`toolset_adapter.py`）：
- `build_child_toolset` 复现 `FunctionToolExecutor._build_handoff_toolset`：
  - `handoff.agent.tools=None` → 全部工具（除 handoff）+ runtime computer tools
  - list → 按名解析 registered + runtime
  - `[]` → 空工具
- `_sanitize_child_toolset` 移除 `call_maid`/`maid_task`/`transfer_to_*`（禁止递归）
- `memory_agent_names` 命中 → 自动补 `FileReadTool`/`FileWriteTool`/`FileEditTool`（走 `tool_mgr.get_builtin_tool`，保留原权限检查）
- `load_memory_index_inline`：最多 200 行 / 25000 bytes，超限截断 + 提示拆分 topic 文件
- `get_memory_dir(umo, agent_name)`：UMO+agent_name 隔离，**不随 retention 删除**

**不实现** Claude 的 memory snapshot（项目模板同步），因为 AstrBot 无通用 project 概念。

**验证**：`test_recursion_and_handoff_detection`、`test_sanitize_strips_control_plane_and_handoffs`、`test_memory_dir_isolated_by_umo_and_agent`、`test_load_memory_index_inline_truncates_over_line_cap`、`test_build_child_strips_recursion`。

### 2.8 child event 隔离

**实现**（`_isolate_child_event`）：
- 用 `_DashboardMaidEvent` 构造 child event，复制原 event 的 `unified_msg_origin`/`sender_id`/`role`/`group`/`platform`
- 独立的 `extras`/`result`/`stop`/`tempfiles` 状态
- **不伪造 admin**：`role` 继承原 event

### 2.9 并发容量

**实现**：
- 每 UMO 最多 `max_active_per_umo=5`，全局最多 `max_active_global=20`
- `_reserve_capacity_unlocked` 在锁内检查 global + umo
- 无 FIFO 队列，超限立即 `CapacityExceededError`
- foreground/background 都占 active slot；迁后台沿用同一 slot，终态统一释放

**验证**：`test_per_umo_capacity_limit`。

---

## 3. 模块设计与公开 API

### 3.1 `runtime_store.py` — 持久化层
```
<data_dir>/agents/<agent_id>/
  agent.json          # AgentMeta（原子替换）
  transcript.jsonl    # append-only
  runs/<task_id>.json # RunMeta + PendingNotification（原子替换）
  outputs/<task_id>.txt
```
**关键 API**：
- `create_agent(*, unified_msg_origin, agent_name, sender_id) -> AgentMeta`
- `create_run(run: RunMeta) -> RunMeta`
- `finalize_run(agent_id, task_id, *, status, result, error, output_file) -> RunMeta` — 原子写终态 + pending notification
- `claim_notification(agent_id, task_id)` — 标记已投递
- `rebuild_contexts_for_resume(agent_id) -> list[dict]` — 过滤损坏 + 未配对 tool calls
- `reconcile_on_restart() -> list[RunMeta]` — starting/running → interrupted
- `prune_inactive(retention_days)` — 30天清理（不删 memory/旧 sessions）
- `list_pending_notifications(umo)` — snapshot

### 3.2 `runtime_orchestrator.py` — 状态机 + 并发
**状态机**：`starting → running → completed | failed | stopped`；重启遗留 → `interrupted`
**模式**：`foreground` | `background`（运行模式，非终态）

**关键 API**：
- `dispatch_single(*, event, request: DispatchRequest, runner_payload) -> DispatchOutcome`
- `dispatch_batch(*, event, requests, runner_payload) -> BatchOutcome`
- `steer(*, agent_id, message_text, sender_id) -> str`
- `stop(*, agent_id, sender_id, source) -> DispatchOutcome`
- `get_result(*, task_id, block, timeout_ms, event) -> DispatchOutcome` — block 对齐 Claude（默认 30s，max 600s），额外返回 `query_status=success|timeout|not_ready`
- `register_steer_handler(agent_id, handler)` — runner_factory 创建 runner 后注册

**异常**：`CapacityExceededError`、`BatchCapacityError`、`RunNotFoundError`、`AgentBusyError`

**runner_factory 契约**：`Callable[[RunMeta, AstrMessageEvent, dict], Awaitable[_ChildRunner]]`，`_ChildRunner.run() -> str`

### 3.3 `notification_outbox.py` — 通知投递
- `set_notifier(async (PendingNotification) -> NotifierResult)`
- `set_history_scanner(async (umo) -> list[dict])` — best-effort 去重
- `queue_delivery(umo)` — snapshot 语义
- `note_user_message(umo)` / `note_result_claimed(agent_id, task_id)` / `on_restart()`

### 3.4 `toolset_adapter.py` — child 工具集 + memory
- `build_child_toolset(context, *, handoff, umo, agent_name, memory_agent_names) -> ToolSet|None`
- `load_memory_index_inline(umo, agent_name) -> str|None`
- `get_memory_dir(umo, agent_name) -> Path`

### 3.5 `main.py` — 接入层
**`call_maid` 新接口**：
```python
async def call_maid(self, event, request_text="", agent_name="",
                    resume_agent_id="", run_in_background=False,
                    tasks=None, action="") -> str  # 返回 JSON
```
- foreground 完成 → 同 turn 返回 `outcome.result`
- 超时/显式后台 → 返回 `background_reason` 句柄
- batch → `tasks=[...]` 最多 5 项
- 旧 `action=dispatch/steer/stop/done` → 兼容转换 + 弃用 warning；`done` 变 no-op（保留 sender 校验）

**`maid_task` 新工具**：
```python
async def maid_task(self, event, action, task_id="", agent_id="",
                    message="", block=True, timeout_ms=30000) -> str
```
- `status` 非阻塞 / `result` 阻塞（认领 notification）/ `stop` / `steer`

**`_make_child_runner`**：用 `maid_dispatcher._build_runner` 手动构建 runner（拿到 `runner.follow_up` 句柄注册 steer），`runner.step_until_done` 执行，每步 `runtime_store.append_message` 追加 transcript。

**`_notify_main_agent`**：公开 API 重建唤醒链（CronMessageEvent + build_main_agent + persist_agent_history）。

---

## 4. 验证证据

### 4.1 测试矩阵（92 passed）
| 测试文件 | 数量 | 覆盖 |
|---|---|---|
| test_runtime_store.py | 9 | JSONL 追加/损坏尾部/未配对 tool call 截断/reconcile/retention/finalize 原子 notification |
| test_runtime_orchestrator.py | 21 | foreground 同 runner 迁后台、batch 并发/竞态、容量占用/释放、权限、shutdown interrupted |
| test_notification_outbox.py | 7 | snapshot 合并/去重/无定时重试/重启、新消息、result 三种触发 |
| test_toolset_adapter.py | 11 | 递归移除/memory 隔离/超限截断/build_child_toolset |
| test_call_maid_130.py | 22 | call_maid/maid_task、嵌套 Schema、child event 隔离、transcript 增量追加 |
| test_console_store_130.py | 4 | agent_id/run_mode 字段/list_agents/get_agent_runs |
| test_dispatch_unified_and_timeout.py | 12 | 1.2.0 回归（保留兼容） |
| test_agent_id_provenance.py | 6 | 1.2.0 session 溯源回归 |

### 4.2 基线命令（可复现）
```bash
VENV="C:/astrbot/AstrBot/.venv/Scripts/python.exe"
# ruff
RUFF_CACHE_DIR="C:/tmp/ruff_cache_maid" "$VENV" -m ruff check astrbot_plugin_maid_agent --config astrbot_plugin_maid_agent/pyproject.toml
# → All checks passed!

# pytest
TMPDIR="C:/tmp/maid_pytest_tmp" "$VENV" -m pytest astrbot_plugin_maid_agent/tests -q
# → 92 passed

# Core 未碰
git -C C:/astrbot/AstrBot status --short  # 空
```

### 4.3 版本一致性
- `metadata.yaml`: `version: "1.3.0"`
- `pyproject.toml`: `version = "1.3.0"`
- `__init__.py`: `__version__ = "1.3.0"`

---

## 5. 已知风险与未实现项

### 5.1 已知风险
| 风险 | 说明 | 缓解 |
|---|---|---|
| foreground 50s 与 Core 60s 的余量 | 若 Core 未来调低 tool_call_timeout，50s 可能不再安全 | config 越界回退到 50；`_safe_int_setting` 限制 1-55 |
| `_make_child_runner` 用 `_build_runner` 私有函数 | 依赖 maid_dispatcher 内部函数 | 已在 toolset_adapter 复现 handoff 工具选择；_build_runner 是本插件自己的函数，非 Core 私有 |
| notification exactly-once | 公开 API 唤醒链仍只能 best-effort 去重 | history 写入 notification IDs；文档不宣称 exactly-once |

### 5.2 明确未实现（按计划）
- **worktree**：不实现（硬约束）
- **DevKit 接入**：不接入（硬约束）
- **memory snapshot**：不实现 Claude 的项目模板同步（AstrBot 无通用 project 概念）
- **定时重试 notification**：不实现（对齐 Claude，仅三种触发）
- **FIFO 队列**：不实现（超限立即拒绝）

---

## 6. 与 1.2.0 的兼容性

| 1.2.0 项 | 1.3.0 处理 |
|---|---|
| `call_maid(action=dispatch/steer/stop/done)` | 保留兼容转换 + 弃用 warning；`done` 变 no-op（保留 sender 校验） |
| `sessions/*.json` | 保留，1.3.0 runtime 使用独立 `agents/` 目录 |
| `background_registry` / `batch_registry` | 保留兼容（1.2.0 路径仍工作） |
| `session_enabled` / `session_timeout_minutes` / `dispatch_auto_background_*` | 从 schema 移除，但 `load_maid_mode_config` 容忍旧配置存在不报错 |
| console SQLite 审计 | 保留，新增 agent_id/run_mode 字段存入 meta_json（向后兼容） |

---

## 7. 文件变更清单

**新增**：
- `runtime_store.py`、`runtime_orchestrator.py`、`notification_outbox.py`、`toolset_adapter.py`
- `tests/test_runtime_store.py`、`test_runtime_orchestrator.py`、`test_notification_outbox.py`、`test_toolset_adapter.py`、`test_call_maid_130.py`、`test_console_store_130.py`

**改动**：
- `main.py`（call_maid/maid_task 重写 + runtime 接入 + console 新 API）
- `config.py`（5 个新字段 + dispatch_prompt_template）
- `console_store.py`（agent_id/run_mode 字段 + list_agents/get_agent_runs）
- `constants.py`（MAID_TASK_TOOL_NAME / MAID_NOTIFICATION_ID_META_KEY）
- `_conf_schema.json`（5 个新配置项）
- `metadata.yaml` / `pyproject.toml` / `__init__.py`（1.3.0）
- `CHANGELOG.md` / `README.md`

**统计**：16 files changed, ~1851 insertions, ~377 deletions

---

## 8. Review 检查清单

- [x] foreground 50s 迁后台是同一 runner 原地转后台，不新建 task/不重启 runner
- [x] call_maid/maid_task 权限校验完整（sender 归属），无伪造 admin
- [x] batch 原子容量拒绝不创建任何 agent/task/transcript
- [x] notification 无定时重试，三种触发正确，snapshot 合并正确，best-effort 去重
- [x] child event 隔离（独立 extras/result/stop/tempfiles，保留 sender/role/group/platform/UMO）
- [x] 递归禁止（child toolset 无 call_maid/maid_task/transfer_to_*）
- [x] resume：running→steer（follow_up），terminal/interrupted→新 task_id 后台
- [x] transcript append-only + 损坏尾部过滤 + 未配对 tool call 截断
- [x] retention 30 天不删 memory 和旧 sessions
- [x] 未修改 AstrBot Core 任何文件
- [x] 旧 action 兼容 + 弃用提示
- [x] 重启遗留 starting/running 静默转 interrupted，不自动重放/通知
