/**
 * 本地开发用的假数据世界。仅在 `npm run dev` 且没有 AstrBot 桥时装载，
 * 生产构建会被摇掉（见 src/main.js 里的 import.meta.env.DEV 分支）。
 *
 * 数据形状严格对齐后端 console_* 路由的返回，方便用来验证滚动/焦点行为：
 * 历史足够长（能滚起来）、有一个持续追加 trace 的运行中 run。
 */

const UMO = "aiocqhttp:FriendMessage:10001";
const UMO_ALT = "aiocqhttp:GroupMessage:998877";

const LOREM_RESULT = `已经把 \`runtime_store\` 的锁粒度拆开了。

拆法：

- \`_meta_lock\` 只护 agent/run 的元数据读写
- \`_io_lock\` 只护 transcript 的 append
- 两把锁不嵌套，避免顺序反了死锁

改完之后并发 append 不再被元数据读阻塞。顺带补了三个回归用例：

\`\`\`python
async def test_concurrent_append(store):
    await asyncio.gather(*[store.append_message(aid, msg) for _ in range(32)])
    assert len(await store.load_transcript(aid)) == 32
\`\`\`

需要我把同样的拆分套到 notification_outbox 上吗？`;

function iso(offsetSeconds) {
  return new Date(Date.now() + offsetSeconds * 1000).toISOString();
}

function toolCall(index, callId, name, args) {
  return { index, kind: "tool_call", tool_call_id: callId, tool_name: name, arguments: args };
}

function toolResult(index, callId, name, message) {
  return { index, kind: "tool_result", tool_call_id: callId, tool_name: name, message };
}

function assistantNote(index, message) {
  return { index, kind: "assistant", message };
}

function buildFinishedRun(agentId, ordinal, baseOffset) {
  const taskId = `task_${agentId}_${ordinal}`;
  const entries = [
    assistantNote(0, `先看一遍 runtime_store 现在的锁用法，确认第 ${ordinal} 轮要动的范围。`),
    toolCall(1, `${taskId}_c1`, "read_file", { path: "runtime_store.py", start_line: 0 }),
    toolResult(
      2,
      `${taskId}_c1`,
      "read_file",
      Array.from({ length: 24 }, (_, i) => `${i + 1}| async with self._lock:  # line ${i + 1}`).join("\n"),
    ),
    toolCall(3, `${taskId}_c2`, "grep", { pattern: "async with self._lock", glob: "*.py" }),
    toolResult(
      4,
      `${taskId}_c2`,
      "grep",
      "runtime_store.py:118\nruntime_store.py:204\nruntime_store.py:377\nnotification_outbox.py:66",
    ),
    toolCall(5, `${taskId}_c3`, "edit_file", {
      path: "runtime_store.py",
      old: "self._lock = asyncio.Lock()",
      new: "self._meta_lock = asyncio.Lock()\nself._io_lock = asyncio.Lock()",
    }),
  ];

  if (ordinal % 3 === 0) {
    entries.push(
      toolResult(6, `${taskId}_c3`, "edit_file", "Error: 未找到匹配的文本，文件可能已被改动。"),
      toolCall(7, `${taskId}_c4`, "read_file", { path: "runtime_store.py", start_line: 280 }),
      toolResult(8, `${taskId}_c4`, "read_file", "280| self._lock = asyncio.Lock()  # 实际在这里"),
      toolCall(9, `${taskId}_c5`, "edit_file", { path: "runtime_store.py", start_line: 280 }),
      toolResult(10, `${taskId}_c5`, "edit_file", "已写入 1 处改动。"),
    );
  } else {
    entries.push(toolResult(6, `${taskId}_c3`, "edit_file", "已写入 2 处改动。"));
  }

  return {
    taskId,
    run: {
      task_id: taskId,
      agent_id: agentId,
      status: ordinal % 5 === 0 ? "error" : "completed",
      mode: "background",
      unified_msg_origin: UMO,
      request_text: `【大小姐请求】\n把 runtime_store 的锁粒度收一下，第 ${ordinal} 轮。\n\n【对方原话】\n锁太粗了，append 会被读阻塞。`,
      result: ordinal % 5 === 0 ? "" : LOREM_RESULT,
      error: ordinal % 5 === 0 ? "SubAgent 执行超时（>300s），已终止。" : "",
      created_at: iso(baseOffset),
      started_at: iso(baseOffset + 1),
      ended_at: iso(baseOffset + 74),
      updated_at: iso(baseOffset + 74),
      notification: { notification_id: `ntf_${taskId}`, delivered: ordinal % 4 !== 0 },
    },
    segment: {
      task_id: taskId,
      user_text: `【对方原话】\n锁太粗了，append 会被读阻塞，第 ${ordinal} 轮麻烦再收一下。`,
      mistress_text: `管家，把 runtime_store 的锁粒度拆一下（第 ${ordinal} 轮）。`,
      steers: ordinal % 4 === 0 ? ["顺便把 notification_outbox 也看一眼"] : [],
      tool_chain: { entries, messages: entries.map((e) => ({ role: e.kind, content: e.message ?? e.arguments })) },
    },
  };
}

export function createWorld() {
  const agents = [];
  const runs = {};
  const transcripts = {};

  // 主会话：8 轮历史 + 1 个运行中的 run，历史足够长可以滚动。
  const mainId = "agent_main_0001";
  const mainRuns = [];
  const mainSegments = [];
  for (let ordinal = 1; ordinal <= 8; ordinal += 1) {
    const built = buildFinishedRun(mainId, ordinal, -3600 + ordinal * 300);
    mainRuns.push(built.run);
    mainSegments.push(built.segment);
  }

  const liveTaskId = `task_${mainId}_live`;
  const liveEntries = [
    assistantNote(0, "开始跑第 9 轮，先确认改动没有把测试打挂。"),
    toolCall(1, `${liveTaskId}_c1`, "run_tests", { path: "tests/", pattern: "test_runtime_store" }),
  ];
  const liveRun = {
    task_id: liveTaskId,
    agent_id: mainId,
    status: "running",
    mode: "background",
    unified_msg_origin: UMO,
    request_text: "【大小姐请求】\n跑一遍 runtime_store 的回归测试。",
    result: "",
    error: "",
    created_at: iso(-40),
    started_at: iso(-38),
    ended_at: "",
    updated_at: iso(-1),
    notification: null,
  };
  mainRuns.push(liveRun);
  mainSegments.push({
    task_id: liveTaskId,
    user_text: "【对方原话】\n刚才那个改动跑一下测试看看",
    mistress_text: "管家，跑一遍 runtime_store 的回归测试。",
    steers: [],
    tool_chain: { entries: liveEntries, messages: [] },
  });

  agents.push({
    agent_id: mainId,
    unified_msg_origin: UMO,
    agent_name: "butler",
    sender_id: "10001",
    title: "收窄 runtime_store 的锁粒度",
    created_at: iso(-3600),
    updated_at: iso(-1),
    active_task_id: liveTaskId,
    last_status: "running",
    last_task_id: liveTaskId,
    run_count: mainRuns.length,
    last_run_at: iso(-1),
    pending_notification: false,
  });
  runs[mainId] = mainRuns;
  transcripts[mainId] = { mistress_name: "沐雪", runs: mainSegments };

  // 两个已完成的短会话，用来测切换与筛选。
  for (const [index, title] of [
    "整理 CHANGELOG 1.4.1",
    "排查 SSE 断连",
    "给 toolset_adapter 补类型注解",
  ].entries()) {
    const agentId = `agent_side_000${index + 2}`;
    const built = buildFinishedRun(agentId, index + 1, -7200 + index * 600);
    agents.push({
      agent_id: agentId,
      unified_msg_origin: index === 2 ? UMO_ALT : UMO,
      agent_name: index === 1 ? "researcher" : "butler",
      sender_id: "10001",
      title,
      created_at: iso(-7200 + index * 600),
      updated_at: iso(-7100 + index * 600),
      active_task_id: "",
      last_status: built.run.status,
      last_task_id: built.taskId,
      run_count: 1,
      last_run_at: built.run.ended_at,
      pending_notification: !built.run.notification.delivered,
    });
    runs[agentId] = [built.run];
    transcripts[agentId] = { mistress_name: "沐雪", runs: [built.segment] };
  }

  return {
    umos: [UMO, UMO_ALT],
    agents,
    runs,
    transcripts,
    liveAgentId: mainId,
    liveTaskId,
    liveEntries,
    config: {
      default_agent_name: "butler",
      allowed_agent_names: ["butler", "researcher"],
      hide_native_tools: true,
      hide_transfer_tools: true,
      include_raw_user_input: true,
      log_raw_llm_io: false,
      foreground_timeout_seconds: 50,
      memory_agent_names: [],
      max_active_per_umo: 5,
      max_active_global: 20,
      retention_days: 30,
      dispatch_prompt_template: "你是管家，负责处理大小姐转交的执行任务…",
    },
    subagents: [{ name: "butler" }, { name: "researcher" }, { name: "archivist" }],
  };
}

/** 给运行中的 run 追加一步，模拟后端的 runtime_trace 推送。 */
export function advanceLiveRun(world) {
  const entries = world.liveEntries;
  const nextIndex = entries.length;
  const round = Math.floor(nextIndex / 2);
  const callId = `${world.liveTaskId}_c${round + 1}`;
  const last = entries[entries.length - 1];

  if (last && last.kind === "tool_call") {
    entries.push(
      toolResult(
        nextIndex,
        last.tool_call_id,
        last.tool_name,
        `collected 128 items\npassed 126\nfailed 2\n耗时 ${(round * 1.7).toFixed(1)}s`,
      ),
    );
  } else {
    const names = ["run_tests", "read_file", "grep", "edit_file", "write_file"];
    const name = names[round % names.length];
    entries.push(toolCall(nextIndex, callId, name, { path: `runtime_store.py`, round }));
  }

  const segment = world.transcripts[world.liveAgentId].runs.find(
    (item) => item.task_id === world.liveTaskId,
  );
  segment.tool_chain = { entries: [...entries], messages: [] };

  const run = world.runs[world.liveAgentId].find((item) => item.task_id === world.liveTaskId);
  run.updated_at = new Date().toISOString();

  return { entries: [...entries] };
}
