<template>
  <div class="docs">
    <article class="docs-body">
      <h2>代理女仆 · 使用文档</h2>
      <p class="lede">
        主模型（大小姐）专注陪用户聊天，管家（子代理）在幕后调用工具执行任务。需要做事时通过
        <code>call_maid</code> 派给管家，管家执行完回报给大小姐转告用户。
      </p>

      <h3>工具 API（模型调用）</h3>
      <section class="card">
        <div class="card-head"><span class="badge tool">工具</span><code>call_maid</code></div>
        <p>调度管家执行任务。默认前台同步等待，短任务当场返回；超时自动转后台并返回句柄。</p>
        <table>
          <thead>
            <tr><th>参数</th><th>类型</th><th>必填</th><th>说明</th></tr>
          </thead>
          <tbody>
            <tr><td>request_text</td><td>string</td><td>必填</td><td>任务要求（自包含背景/约束/目标）</td></tr>
            <tr><td>agent_name</td><td>string</td><td>可选</td><td>目标管家名称，留空用默认</td></tr>
            <tr><td>resume_agent_id</td><td>string</td><td>可选</td><td>恢复已有 agent；running 时等价 steer</td></tr>
            <tr><td>run_in_background</td><td>bool</td><td>可选</td><td>true 立即转后台，默认前台等待</td></tr>
            <tr><td>tasks</td><td>array</td><td>可选</td><td>批量任务，最多 5 项，仅新建 agent</td></tr>
          </tbody>
        </table>
      </section>

      <section class="card">
        <div class="card-head"><span class="badge tool">工具</span><code>maid_task</code></div>
        <p>查询与控制后台任务，对齐 TaskOutput 语义。</p>
        <table>
          <thead>
            <tr><th>参数</th><th>类型</th><th>必填</th><th>说明</th></tr>
          </thead>
          <tbody>
            <tr><td>action</td><td>string</td><td>必填</td><td>status / result / stop / steer</td></tr>
            <tr><td>task_id</td><td>string</td><td>按动作</td><td>status/result/stop 时填</td></tr>
            <tr><td>agent_id</td><td>string</td><td>按动作</td><td>steer 必填；result 可选校验</td></tr>
            <tr><td>message</td><td>string</td><td>steer 必填</td><td>补充的要求文本</td></tr>
            <tr><td>block</td><td>bool</td><td>可选</td><td>result 是否阻塞，默认 true</td></tr>
            <tr><td>timeout_ms</td><td>int</td><td>可选</td><td>result 超时毫秒，默认 30000</td></tr>
          </tbody>
        </table>
      </section>

      <h3>用户命令</h3>
      <section class="card">
        <div class="card-head"><span class="badge cmd">命令</span><code>/maid status</code></div>
        <p>查看当前后台管家任务状态；批量任务展示子任务明细。</p>
      </section>
      <section class="card">
        <div class="card-head"><span class="badge cmd">命令</span><code>/maid stop</code></div>
        <p>请求停止当前后台管家任务；批量任务停止整批。</p>
      </section>

      <h3>执行流程</h3>
      <ol>
        <li>大小姐判断需要做事时调用 <code>call_maid</code>。</li>
        <li>短任务当场返回；超过前台阈值后转后台。</li>
        <li>后台任务结束后唤醒大小姐整理结果转告用户。</li>
        <li>期间可用 <code>maid_task</code> 查询或控制。</li>
      </ol>

      <h3>Agent 与 Run 模型</h3>
      <ul>
        <li>新 dispatch 创建新 agent；显式 <code>resume_agent_id</code> 才恢复身份。</li>
        <li>每 agent 同时最多一个活跃 run；每会话最多 5 个、全局 20 个。</li>
      </ul>

      <h3>控制台操作</h3>
      <ul>
        <li>选中会话后直接在下方输入即可派活：空闲时是 <b>resume</b>，运行中是 <b>steer</b>。</li>
        <li>停止 / 读取结果都在对应 <b>RUN 卡片</b>的头部，不在别处。</li>
        <li>
          每张 RUN 卡片常驻三个操作：<b>复制</b>（有结果复制结果，否则复制请求）、<b>回溯到这里</b>
          （丢弃本轮及之后的上下文，需点两次确认）、<b>Fork</b>（用同一条请求新建 Agent，不带上下文）。
        </li>
        <li>
          回溯不会删除任何记录：被折叠的 RUN 仍留在时间线上（虚线灰显、标「已回溯」），只是不再进入
          下次 resume 的上下文。
        </li>
        <li>快捷键：<code>Enter</code> 发送、<code>Shift+Enter</code> 换行、<code>/</code> 聚焦输入框、<code>Esc</code> 关闭弹层。</li>
        <li>滚动条不会被后台刷新拽走：往上翻看历史时新内容只累计成「↓ N 条新内容」，点了才回底部。</li>
      </ul>

      <h3>配置参考</h3>
      <section class="card">
        <div class="card-head"><span class="badge cfg">字段</span><code>插件配置</code></div>
        <table>
          <thead>
            <tr><th>配置项</th><th>默认</th><th>说明</th></tr>
          </thead>
          <tbody>
            <tr><td>default_agent_name</td><td>muiceagent</td><td>默认 SubAgent 名称</td></tr>
            <tr><td>allowed_agent_names</td><td>[muiceagent]</td><td>call_maid 可指定的白名单</td></tr>
            <tr><td>hide_native_tools</td><td>true</td><td>隐藏原生工具，只留 call_maid/maid_task</td></tr>
            <tr><td>include_raw_user_input</td><td>true</td><td>透传真实用户原话给管家</td></tr>
            <tr><td>foreground_timeout_seconds</td><td>50</td><td>前台等待阈值（秒）</td></tr>
            <tr><td>memory_agent_names</td><td>[]</td><td>启用持久记忆的 agent 列表</td></tr>
            <tr><td>max_active_per_umo</td><td>5</td><td>每会话活跃 run 上限</td></tr>
            <tr><td>max_active_global</td><td>20</td><td>全局活跃 run 上限</td></tr>
            <tr><td>retention_days</td><td>30</td><td>元数据保留天数</td></tr>
          </tbody>
        </table>
      </section>
    </article>
  </div>
</template>

<style scoped>
.docs {
  flex: 1;
  overflow-y: auto;
  padding: 22px 24px 48px;
}
.docs-body {
  max-width: 720px;
  margin: 0 auto;
}
h2 {
  font-size: 22px;
  font-weight: 600;
  margin: 0 0 6px;
}
h3 {
  font-size: 14.5px;
  font-weight: 600;
  margin: 26px 0 10px;
  padding-top: 16px;
  border-top: 1px solid var(--line);
}
h3:first-of-type {
  border-top: none;
  padding-top: 0;
}
.lede {
  font-size: 13.5px;
  color: var(--text-muted);
  line-height: 1.75;
  margin: 0 0 8px;
}
p,
ol,
ul {
  font-size: 13.5px;
  color: var(--text-muted);
  line-height: 1.75;
  margin-bottom: 10px;
}
ol,
ul {
  padding-left: 20px;
}
li {
  margin-bottom: 5px;
}
li::marker {
  color: var(--accent);
}
b {
  color: var(--text-main);
  font-weight: 600;
}
code {
  font-family: var(--font-mono);
  font-size: 0.88em;
  background: var(--accent-soft);
  color: var(--accent-hover);
  padding: 1px 5px;
  border-radius: 4px;
}

.card {
  background: var(--bg-raised);
  border: 1px solid var(--line);
  border-radius: var(--radius-card);
  padding: 14px 16px;
  margin: 12px 0;
}
.card-head {
  display: flex;
  align-items: center;
  gap: 9px;
  margin-bottom: 8px;
}
.card-head code {
  font-size: 13.5px;
  color: var(--text-main);
  background: none;
  padding: 0;
}
.badge {
  font-family: var(--font-mono);
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.04em;
  padding: 2px 7px;
  border-radius: 5px;
  text-transform: uppercase;
}
.badge.tool {
  background: var(--accent);
  color: #fff;
}
.badge.cmd {
  background: var(--accent-soft);
  color: var(--accent-hover);
  border: 1px solid var(--accent);
}
.badge.cfg {
  background: var(--bg-code);
  color: var(--text-muted);
  border: 1px solid var(--line);
}
.card table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;
  margin-top: 6px;
}
.card th {
  text-align: left;
  color: var(--text-main);
  font-weight: 600;
  font-size: 12px;
  padding: 5px 8px;
  border-bottom: 1px solid var(--line);
}
.card td {
  padding: 6px 8px;
  border-bottom: 1px solid var(--line);
  color: var(--text-muted);
  vertical-align: top;
}
.card tr:last-child td {
  border-bottom: none;
}
.card td:first-child {
  font-family: var(--font-mono);
  color: var(--accent-hover);
  white-space: nowrap;
}
</style>
