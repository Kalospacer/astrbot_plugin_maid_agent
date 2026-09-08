# 控制台文件链路调查与方案设计

> 调查范围:插件 WebUI(控制台)的**输入侧文件链路**(剪贴板粘贴、+ 号选择器、"上传并引用到对话")与**输出侧文件链路**(管家产物如何回传给用户)。
> 结论分三部分:现状与根因(§1–2)、宿主能力盘点(§3)、改造方案(§4–§7)。
> 所有代码引用均基于当前工作区(`branch: hoplite/siphnos-3ffc02ab`,插件版本 2.0.66)与 AstrBot v4.28.0 / v4.24.5 / v4.26.0 官方源码核对。

---

## 0. 摘要

| # | 问题 | 根因(一句话) | 方案方向 |
| --- | --- | --- | --- |
| 1 | 输入框不能直接粘贴剪贴板图片 | `Composer.tsx` 的 `<textarea>` 没有任何 `onPaste` 处理,剪贴板里的文件被静默丢弃 | 前端补 `onPaste`(顺带补 `onDrop`),文件走统一附件管线 |
| 2 | 左下角 + 号只支持图片 | 前端 `accept` 与过滤逻辑写死 `image/*`,后端 `session.prompt` 只认 `text/image` 两种 part,`save_attachment` 只收 4 种图片魔数 | 前端放开类型 + 新增 multipart 上传通道(`bridge.upload`)+ `session.prompt` 新增 `file` part,文件以"服务器路径 + 元数据"注入提示词,管家用文件工具读取 |
| 3 | 管家缺少发文件给用户的接口 | 管家把产物写进服务器本地 workspace;`_speak` 只发文本;控制台会话 `deliverable=False`,AstrBot 原生 `astrbot_download_file` 的 `send` 被静默丢弃;控制台没有任何产物块与下载入口 | 给管家注册 `maid_deliver_file` 工具:复制产物进会话附件区 + 追加产物事件(控制台渲染下载 chip,走 `bridge.download` + 新 GET 二进制路由);聊天会话同时经 `MessageChain` 的 `File/Image` 组件真实投递 |

**版本门槛(重要)**:宿主的页面桥 `upload`/`download` 能力自 **AstrBot v4.24.5** 起存在;官方插件 Web API 封装 `astrbot.api.web`(`request.files()` / `file_response`)自 **v4.26.0** 起存在。本插件 `metadata.yaml` 声明 `astrbot_version: >=4.20.0` 已过时,落地本方案需提升门槛或做能力探测降级(§7.3)。

---

## 1. 现状:输入侧(用户 → 管家)

### 1.1 前端输入框(`webui/src/components/Composer.tsx`)

当前附件链路的全部事实:

- `<textarea>`(L118)只有 `onChange` / `onKeyDown`,**没有 `onPaste`**。往 textarea 粘贴图片时,浏览器只把剪贴板里的文本部分(通常为空)写入,文件部分被丢弃——用户看到的就是"贴不进去"。整个 `webui/src` 里没有任何 `onPaste`/`clipboardData` 处理(`rg onPaste webui/src` 为空)。
- + 号按钮(L144–152,`aria-label="附加图片"`)触发的隐藏 `<input type="file">`(L157–158)带 `accept="image/png,image/jpeg,image/webp,image/gif"`,文件选择器里非图片不可选。
- 即使绕过 accept(如拖进来的 File),`onPickImages`(L75–76)也会 `if (!file.type.startsWith("image/")) continue;` 直接丢弃。
- 附件上限 5 个(`slice(0, 5)`,L75/L84),每个文件经 `fileToBase64`(L216)读成 base64,暂存在 `PendingImage[]`,发送时展开成 `PromptContentPart`。

### 1.2 前端类型与发送通道

- `webui/src/types.ts` L151:`PromptContentPart = { type: "text" } | { type: "image"; mediaType; data; name? }`——**协议层面只有文本和图片两种 part,没有文件**。
- `webui/src/store/app.ts` L400:`sendPrompt` 把 parts 打进 `session.prompt` 的 JSON-RPC payload(图片以 base64 内嵌在 JSON 里,大图会显著膨胀请求体)。
- `webui/src/api/bridge.ts` L12–13:桥接口**已经声明了** `upload(endpoint, file)` 与 `download(endpoint, params, filename)`,但全项目零调用——能力就摆在那里,只是没用。

### 1.3 后端 RPC 与存储

- `harness/api.py` `session_prompt`(L349):只处理 `type == "text"` 与 `type == "image"`(L365);图片经 `store.save_attachment` 落盘后转成 `image_block`。
- `harness/store.py`:
  - `IMAGE_MEDIA_EXT`(L26)是闭合的 4 种图片类型表;`save_attachment`(L199–203)遇到表外 mediaType 直接 `raise ValueError("不支持的图片类型: …")`。
  - 附件实体是 `attachments/<sessionId>/<attachmentId>.<ext>`(L276 起),`attachmentId` 受 `ATTACHMENT_ID_RE` 约束,文件名(`name`)只存进 ref,不落盘。
  - `load_attachment`(L250)按扩展名反查 mediaType,查不到按 `application/octet-stream`。
- `harness/api.py` `session_attachment`(L396):下载附件的唯一现存入口,但有三个硬限制:
  1. `referenced` 检查(L400)只认 **`user/message` 事件里引用过的 image 块**——管家产生的文件永远不满足;
  2. 以 base64 JSON 返回,不是二进制流,大文件低效且无法触发浏览器下载;
  3. 语义是"给前端做缩略图"(前端 `loadAttachmentImage` 包成 `data:` URL),不是文件交付。

### 1.4 管家如何"看到"文件(`harness/drivers.py` + `maid_dispatcher.py`)

- `_execute_turn` 中 `image_paths_for_message`(drivers.py L1160)把最后一条 `user/message` 里的 image 块解析成本地路径,经 `attachment_paths_for_prompt` 校验存在性后,作为 `image_urls` 传给 `_build_runner`(L571)。
- `maid_dispatcher.py` `_build_runner` 把它塞进 `ProviderRequest(prompt=…, image_urls=…)`。AstrBot v4.28 的 `ProviderRequest`(`astrbot/core/provider/entities.py` L91–116)只有 `image_urls` / `audio_urls` / `extra_user_content_parts`——**没有任意文件字段**,多模态文件输入不由 runner 原生承载。
- 聊天侧派发(`main.py` `_dispatch_chat_task` L583 起)同样只快照 `Image` 组件(`events_shim.py` `image_paths_from_event` L44);聊天里用户发的 `File` 组件被无视。

### 1.5 小结:输入侧的三道闸

```
剪贴板图片 ──✂ 无 onPaste─────────────────────────── ✗
+ 号选文件 ──✂ accept=image/* + image/* 过滤──────── ✗
协议/后端  ──✂ PromptContentPart 无 file + 图片魔数表 ✗
```

三道闸都在,所以"支持所有文件类型"需要前端交互、传输通道、协议、存储、模型摄取五层一起动。

---

## 2. 现状:输出侧(管家 → 用户)

### 2.1 管家的文件写到哪

管家的文件工具来自 AstrBot computer-use 工具集(`toolset_adapter.py` `build_child_toolset` 复现官方 handoff 选择规则):

- **local 运行时**:`ExecuteShellTool / LocalPythonTool / FileReadTool / FileWriteTool / FileEditTool / GrepTool`(`toolset_adapter.py` `_LOCAL_TOOL_CLASS_NAMES`)。相对路径在"主 workspace"下解析(AstrBot v4.28 `computer_tools/fs.py` L33:"In local runtime, relative paths are resolved under the primary workspace"),即 `data/workspaces/<normalized_umo>/` 一类的**服务器本地目录**。
- **sandbox 运行时**:额外有 `astrbot_upload_file` / `astrbot_download_file`(v4.28 `fs.py` L804/L870)。

用户说的"agent 只会把文件存到用户无法访问的服务器本地目录"完全属实:这些目录没有任何 HTTP 出口,插件也没有把产物登记进会话事件的机制。

### 2.2 管家的消息出口只有文本

- `drivers.py` `_speak`(L877):`MessageChain().message(f"{speaker}: {text}")`——**纯文本**。管家即使想发文件也没有表达手段。
- 控制台会话:`_execute_turn` L530 `self._voice_sink = child_event if meta.get("sourceKind") == "chat" else None`——dashboard 会话没有 voice sink;`events_shim.py` `MaidAgentEvent.deliverable`(L127)对 `DASHBOARD_UMO` 返回 False,`send()`(L130–138)直接 return。

### 2.3 AstrBot 原生下载工具为什么救不了场

AstrBot v4.28 的 `astrbot_download_file`(sandbox 专属工具)确实有 `also_send_to_user` 参数:把文件从 sandbox 拉到宿主临时目录后,用 `Image.fromFileSystem(local_path)` 或 `File(name=…, file=local_path)` 组 `MessageChain` 发给用户(v4.28 `fs.py` L919–929)。但对本插件的三重限制:

1. **只在 sandbox 运行时暴露**(`_SANDBOX_RUNTIME_TOOL_CONFIG`),local 运行时(多数自部署用户的默认)没有这对工具;
2. **member 权限被 `check_admin_permission` 拒绝**(`computer_use_require_admin=True` 时);
3. **对 dashboard 会话必然静默失败**——工具内部调 `context.context.event.send(...)`,而 `MaidAgentEvent.send` 对 DASHBOARD_UMO 直接丢弃。控制台任务里管家调用它,会收到"成功"的假象,用户什么都收不到。

### 2.4 控制台侧没有任何产物概念

- `contracts.py` 的内容块词表(text/reasoning/image/tool-call/tool-result)没有文件块;`KNOWN_EVENT_TYPES` 没有产物事件。
- `webui/src/store/conversation.ts` 折叠器的 switch(L270–497:turn/start、step、turn/end、user/message、assistant/chunk、assistant/message、tool/call、tool/result、maid/delivery)没有产物行。
- `ChatView.tsx` 只渲染用户消息里的图片附件(`UserImages` L385,走 `session.attachment` RPC);助手消息正文只渲染 text/reasoning。
- `main.py` `_register_web_apis`(L218)只注册了 4 条路由,全是 POST(JSON RPC)与 SSE,**没有 GET 二进制路由**——而浏览器下载必须走 GET。

---

## 3. 宿主能力盘点(AstrBot 侧,已对源码核实)

### 3.1 页面桥 `window.AstrBotPluginPage`

来源:AstrBot `astrbot/dashboard/plugin_page_bridge.js`(iframe 内注入)+ `dashboard/src/views/PluginPagePage.vue`(父窗口处理)。**本插件控制台正运行在这套桥上**(`bridge.ts` 的 `apiPost`/`subscribeSSE` 即此)。

| 桥方法 | 父窗口实际行为 | 对后端的意义 |
| --- | --- | --- |
| `upload(endpoint, file)` | 把 `File` 读成 ArrayBuffer postMessage 给父窗口;父窗口组 `FormData`,`formData.append("file", file, fileName)` 后 **POST** 到 `/api/v1/plugins/extensions/<插件名>/<endpoint>`,timeout 60s,不限 body 大小 | 后端收到一个 **multipart POST,文件字段名固定为 `file`**,文件名/类型保留 |
| `download(endpoint, params, filename)` | 父窗口 **GET** 同前缀路径,`params` 作为 query,`responseType: "blob"`;拿到 blob 后建 `<a download>` 触发浏览器下载;`filename` 缺省时解析响应的 `Content-Disposition` | 后端收到一个 **GET + query**,返回任意二进制即触发保存 |
| `apiGet/apiPost` | 同前缀的 GET/POST,响应解包 `response.data?.data ?? response.data` | 现有 JSON RPC 通道,已在使用 |
| `subscribeSSE` | 父窗口发起 SSE 并把帧 postMessage 回 iframe | 现有 events.mux/host,已在使用 |

关键细节:

- 前缀路径 `/api/v1/plugins/extensions/{plugin_path}` 在 AstrBot v4.28 由 `astrbot/dashboard/api/plugins.py` L379–421 提供,支持 GET/POST/PUT/PATCH/DELETE,并且**带 `require_plugin_scope` 鉴权**(dashboard 用户 JWT)——插件路由不需要自己做认证,只需校验业务参数。
- 路由匹配:`api/plugins.py` `_match_registered_web_api` 把插件 `register_web_api` 注册的 route(支持 `<path:x>` 占位符)转成正则对 `plugin_path` 全匹配;**query string 不参与匹配**。
- iframe `sandbox="allow-scripts allow-forms allow-downloads"`(PluginPagePage.vue L651),且下载动作本身由父窗口执行,iframe 沙箱不构成阻碍。
- **版本边界**:`files:upload`/`files:download` 在 v4.23.6 及以前不存在,自 **v4.24.5**(2026-05-13)起可用。本插件声明的 `>=4.20.0` 覆盖了没有这套桥的版本。

### 3.2 插件 Web API 的调用形态(与现有 Quart 风格兼容)

本插件 `main.py` 直接 `from quart import jsonify, make_response, request`,而 AstrBot v4.28 的 dashboard 已是 FastAPI(`astrbot/dashboard/server.py`)。两者靠 `astrbot/dashboard/asgi_runtime.py` 的 **Quart 兼容层**接通:

- `_call_plugin_extension`(api/plugins.py L204 起)把 FastAPI `Request` 包成 `PluginRequest`,再经 `call_request_view` → `bind_quart_request_context`(asgi_runtime.py L567):用原始 body+headers 给插件 handler 建一个 Quart `test_request_context`。因此插件 handler 里的 `request.get_json()`、**`request.files`(multipart)**、`request.args`(query)都可用。
- 响应侧 `_coerce_view_result` / `_quart_response_to_starlette`(L475 起):Quart Response 原样转 Starlette,**headers 逐对保留**——意味着 `Content-Disposition` 能活着到达父窗口的下载解析。

同时 AstrBot v4.26.0+ 提供了官方封装 `astrbot.api.web`(本插件尚未使用):

- `request` 代理:`request.query` / `await request.form()` / **`await request.files()`** → `PluginMultiDict[PluginUploadFile]`;`PluginUploadFile` 有 `filename` / `content_type` / `await save(path)` / `await read()`。
- 响应构造器:`json_response` / `error_response` / **`file_response(path, filename=…, content_type=…)`**(Starlette `FileResponse`,自动带 `Content-Disposition: attachment`)/ `stream_response`。

**建议**:新路由直接用 `astrbot.api.web`(v4.26.0+ 才有),或继续用 Quart 风格(与现有 `web_rpc` 一致、门槛更低)。两者在 v4.28 上都被同一套兼容层支持。

### 3.3 聊天侧发文件(AstrBot `MessageChain`)

`astrbot_download_file` 的实现(v4.28 `fs.py` L919–929)证明平台消息链支持文件投递:

```python
Image.fromFileSystem(local_path)          # 图片
File(name=name, file=local_path)          # 任意文件
await event.send(MessageChain(chain=[component]))
```

本插件的 `MaidAgentEvent.send` 走 `context.send_message(umo, message)` 按真实平台适配器投递(CHANGELOG 2.0.62),因此**聊天会话里管家发文件的平台通路是现成的**,缺的只是插件侧的工具与触发。

### 3.4 权限边界(决定上传文件的存放位置)

AstrBot v4.28 `computer_tools/fs.py` 头部审计(L15–25):member + local 时,管家 read/grep 只允许 `data/skills`、插件 skills、**当前 workspace**、`/tmp/.astrbot`(system tmp)与 temp 目录;write/edit 限当前 workspace 与临时目录。**插件数据目录 `data/plugin_data/astrbot_plugin_maid_agent/…` 不在 member 的读白名单里**。这直接影响 §4.2 的存储布局决策。

---

## 4. 方案设计 A:输入框支持粘贴与任意类型上传

### 4.1 交互层(纯前端,独立可先行)

`Composer.tsx` 改造:

1. **`onPaste`**(挂 `<textarea>`):读 `e.clipboardData.items`,`kind === "file"` 的项取 `getAsFile()`;命中任何文件时 `e.preventDefault()`(阻止把文件名/空白插进正文),文件进入与 + 号相同的附件暂存;纯文本粘贴不拦截。
2. **`onDrop` / `onDragOver`**(挂输入卡片):拖拽文件入框即附加,行为同粘贴(成本极低,顺手补齐)。
3. **+ 号放开类型**:去掉 `accept`,或改为 `accept="image/*,.pdf,.doc*,.txt,…"` 之外的 `"*/*"`;`onPickImages` 更名 `onPickFiles`,删除 `image/*` 过滤;aria-label/Tooltip 从"附加图片"改为"附加文件"。
4. **附件 chip 分型**:`PendingImage` 泛化为 `{ kind: "image" | "file"; name; mediaType; size; preview?; }`;图片保留缩略图,非图片渲染扩展名图标 + 名称 + 人类可读大小;移除按 5 个的总数上限,改为"图片 ≤5 + 文件 ≤N + 单文件 ≤ 上限"的明确规则并在超限时报错(`setError`)。
5. **降级路径**:老宿主桥没有 `upload` 方法时(`typeof bridge.upload !== "function"`),图片仍走现有 base64 part 通道,非图片文件给出"当前 AstrBot 版本不支持文件上传(需 ≥4.24.5)"的明确报错,而不是静默丢弃。

### 4.2 传输与存储层(后端)

**新路由 `POST /astrbot_plugin_maid_agent/api/upload`**(`main.py` `_register_web_apis` 追加;handler 读 multipart 字段 `file`,query 带 `sessionId`):

1. 校验 session 存在(`store.exists`),文件非空、大小 ≤ 配置上限(建议默认 25 MB,`_conf_schema.json` 增键 `max_upload_mb`);
2. **存储泛化**(`harness/store.py`):
   - `IMAGE_MEDIA_EXT` 之外新增 `ATTACHMENT_EXT_BY_TYPE` / 按文件名扩展名 + `mimetypes` 推断;`sniff_image_media_type` 保留(粘贴的截图常没有可靠扩展名);
   - `save_attachment` 拆出 `save_attachment_bytes(session_id, raw, media_type, name)`:任意类型可存,落盘名保持 `<attachmentId>.<ext>`,`name`(原始文件名)写入 ref;图片行为不变(base64 入口继续兼容);
   - `load_attachment` 已按"表外 → octet-stream"兜底,基本无需动;
3. 返回 `{ attachment: { attachmentId, name, mediaType, byteLength } }`,前端把它暂存在附件 chip 上。

**存储位置与 member 读权限的取舍**(§3.4):

- 方案一(推荐):规范副本仍存插件 `attachments/<sid>/`(受会话删除/retention 统一清理);**同时把文件硬链/复制到该会话管家的 workspace**(local 运行时 `data/workspaces/<normalized_umo>/maid-files/<name>`),提示词里引用 workspace 副本路径。这样 member+local 也可读,且"上传的文件出现在管家工作区"语义自然。
- 方案二(最简):只存插件附件区,提示词引用其绝对路径。admin 或 `computer_use_require_admin=False` 的部署可用;member 受限部署下管家读不到,需在工具返回里明确报错提示。
- 文档建议按方案一实现,workspace 副本在会话删除时随附件一起清理(记录映射即可,workspace 目录本身归 AstrBot 管,不主动递归删)。

### 4.3 协议层(`session.prompt`)

- `PromptContentPart` 新增 `{ type: "file"; attachment: { attachmentId; name; mediaType; byteLength } }`(与 image part 的 `{type;mediaType;data}` 并列;也可以直接复用 `{type:"file", attachmentId}` 更瘦,前端已在 upload 响应里拿到完整 ref)。
- `harness/api.py` `session_prompt` 新增分支:校验 attachment 存在(防伪造 id),追加一个**文件引用块**进 user 消息 content。
- **模型摄取方式**(受 §1.4 约束,`ProviderRequest` 无文件字段):
  - 图片:维持现状(`image_urls`);
  - 任意文件:在派发 prompt 文本里注入一段附录,例如:

    ```
    [用户随消息附带了文件]
    - /abs/path/to/workspace/maid-files/report.pdf (report.pdf, application/pdf, 1.2 MB)
    你可以用文件读取工具查看它们。
    ```

    同时事件里的文件块保证控制台渲染、`session.attachment`/新下载路由可回放。管家用 `astrbot_read_file_tool` / shell 处理,二进制(如 xlsx/zip)由管家自行用 Python/shell 处理——这正是"上传并引用到对话"的语义:文件成为对话上下文的一部分,而不是塞进模型上下文。
- steer 模式(`session.prompt` 的 `driver.steer(text)` 分支)同样把文件附录拼进 steer 文本。

### 4.4 控制台渲染

- `ChatView.tsx` `ChatNodeView` 的 user 分支:除 `images` 外再过滤 `type === "file"` 块,渲染 `FileChips`(图标 + 名称 + 大小 + 下载按钮,复用 §5.3 的下载通道,让用户也能把自己传过的文件取回);
- `webui/dev/mock-bridge.ts` 与 `scripts/smoke-render.mjs` 的 `session.prompt` mock 增补 file part 分支。

---

## 5. 方案设计 B:管家产物回传(下载接口)

### 5.1 新工具 `maid_deliver_file`(注册给管家)

在 `toolset_adapter.py` `build_child_toolset` 里,**在 `_sanitize_child_toolset` 之后**追加(注意:`_RECURSION_TOOL_NAMES = MAID_TOOL_NAMES` 会剥离 `maid_*` 控制面工具,所以必须在 sanitize 之后 add,且命名避免撞 `MAID_TOOL_NAMES`;或单独命名如 `maid_deliver_file` 并显式加入白名单逻辑):

```
maid_deliver_file(path: str, name: str = "", remark: str = "") -> str
```

行为(实现为 `FunctionTool` 子类,持有 driver/registry 引用——`build_toolset` 每 turn 调用(drivers.py L543),闭包可捕获当前 `session_id`):

1. 校验:`os.path.isfile`、大小 ≤ 上限(复用 `max_upload_mb` 或独立 `max_artifact_mb`,建议默认 100 MB)、拒绝目录;
2. 复制进 `attachments/<sessionId>/`(**与用户上传同库**,`attachmentId` 规则一致;`save_attachment_bytes` 直接入库,name 取参数或原文件名);
3. **追加产物事件**:`maid/artifact`(加入 `contracts.py` `KNOWN_EVENT_TYPES`),data = `{ attachmentId, name, mediaType, byteLength, sourcePath, remark }`,经 `driver._emit` 发布到 mux 流,事件落 `events.jsonl`(非 surface 事件,不进模型上下文,`history_page` 会原样带回,刷新/翻页不丢);
4. **聊天会话双投递**:`sourceKind == "chat"` 时,用 `child_event.send(MessageChain(chain=[Image.fromFileSystem(path) | File(name, file=path)]))` 把文件直接发进聊天(对齐 `astrbot_download_file` 的组件选择逻辑:v4.28 `fs.py` 用 `_IMAGE_FILE_SUFFIXES` 判定图片/文件);dashboard 会话跳过(产物事件就是投递);
5. 返回给模型的文本:`已交付文件 <name>(attachmentId=…),用户可在控制台下载` / 聊天侧 `已发送给用户`,失败返回可读错误。

> 备注:sandbox 运行时下管家产出的文件在 sandbox 里,`path` 是 sandbox 路径。工具需先经 booter `download_file` 拉到宿主(参照 `astrbot_download_file` 的 `sb.download_file(remote_path, local_path)`),再走上述 1–4。local 运行时 `path` 直接是宿主路径。

### 5.2 控制台渲染(产物 chip)

- `conversation.ts` 折叠器新增 `case "maid/artifact"`:push 一个 `ArtifactNode { kind: "artifact"; key; seq; attachmentId; name; mediaType; byteLength; time; turn }`(不计入 `contentRevision`,归入当前 turn 的过程区即可);
- `ChatView.tsx` 渲染 ArtifactNode:图标(按扩展名)+ 名称 + 大小 + **下载按钮**;
- `types.ts` 增补 `ArtifactNode` / `FileAttachmentRef`。

### 5.3 下载通道(新 GET 路由)

**新路由 `GET /astrbot_plugin_maid_agent/api/file?sessionId=…&attachmentId=…`**(`main.py` 注册 `["GET"]`;query 不参与路由匹配,§3.1):

1. 校验 `sessionId` 存在、`attachmentId` 合法(`ATTACHMENT_ID_RE`)且文件确实位于 `attachments/<sessionId>/` 之下(store 返回解析后的 `Path`,天然防穿越);
2. 授权语义:该路由经 dashboard 代理自带登录鉴权(§3.1)。业务上可复用并放宽 `session_attachment` 的 `referenced` 检查——改为"attachmentId 属于该会话目录"(产物事件与用户上传都已登记,目录归属即授权);如需更严,可要求产物事件/文件块确实引用过该 id;
3. 响应:优先 `astrbot.api.web.file_response(path, filename=name, content_type=mediaType)`(v4.26.0+);或 Quart 风格 `make_response((raw, 200, {"Content-Type": …, "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"}))`——兼容层保证 headers 存活(§3.2);
4. 前端 `app.ts` 新增:

   ```ts
   export async function downloadAttachment(sessionId: string, ref: FileAttachmentRef) {
     await bridgeDownload("api/file", { sessionId, attachmentId: ref.attachmentId }, ref.name);
   }
   ```

   `bridge.download(endpoint, params, filename)` 显式传文件名,不依赖 Content-Disposition 解析(父窗口对 `filename*=` UTF-8 的解析已实现,双保险)。

### 5.4 与 AstrBot 原生工具的关系

`maid_deliver_file` 与 `astrbot_download_file` 并存不冲突:

- sandbox + admin 下原生工具可用且体验完整(拉出 + 直发聊天),保留;
- `maid_deliver_file` 补齐 local 运行时、member 权限、以及 **dashboard 会话**(原生工具在该场景静默失败,§2.3)——这正是本次要修的洞;
- 可选:在管家 system prompt(`build_system_prompt`)里加一句"产物请用 maid_deliver_file 交付",避免模型在 dashboard 会话里误用原生工具。

---

## 6. 接口契约汇总(落地后)

| 通道 | 方法/路由 | 载荷 | 说明 |
| --- | --- | --- | --- |
| 上传 | 桥 `upload("api/upload", file)` → `POST …/api/upload?sessionId=…` | multipart,字段 `file` | 返回 `{attachment: FileAttachmentRef}`;大小/类型校验 |
| 引用 | RPC `session.prompt` | part 新增 `{type:"file", attachment}` | 后端展开为文件块 + prompt 附录 |
| 产物 | 工具 `maid_deliver_file(path, name?, remark?)` | — | 复制入库 + `maid/artifact` 事件(+聊天 MessageChain 投递) |
| 下载 | 桥 `download("api/file", {sessionId, attachmentId}, name)` → `GET …/api/file` | query | 二进制流 + Content-Disposition |
| 缩略图 | RPC `session.attachment`(现状保留) | JSON base64 | 仅图片预览;`referenced` 检查可同步放宽 |

事件/块新增:

- 内容块:`{"type":"file","attachment":{"attachmentId","name","mediaType","byteLength"}}`(user 消息);
- 事件:`maid/artifact`,`data={"attachmentId","name","mediaType","byteLength","sourcePath","remark"}`,非 surface、非 ignorable。

---

## 7. 实施清单

### 7.1 后端(Python)

| 文件 | 改动 |
| --- | --- |
| `harness/store.py` | `save_attachment_bytes`(任意类型);扩展名↔MIME 泛化;`save_attachment_from_path` 泛化;artifacts 与 uploads 同库 |
| `harness/api.py` | `session_prompt` 增 `file` part 分支 + prompt 附录;`session_attachment` referenced 放宽(可选) |
| `main.py` | 注册 `POST api/upload`、`GET api/file`;两个 handler(multipart / 二进制响应);上传大小上限 |
| `harness/contracts.py` | `file_block()`;`maid/artifact` 进 `KNOWN_EVENT_TYPES` |
| `toolset_adapter.py` | `maid_deliver_file` FunctionTool(sanitize 后注入;sandbox 先拉宿主) |
| `harness/drivers.py` | `emit_artifact()`(driver 上,走 `_emit`);`build_toolset` 传入 driver/session |
| `config.py` / `_conf_schema.json` | `max_upload_mb`(默认 25)、可选 `max_artifact_mb` |
| `metadata.yaml` | `astrbot_version` 提至 `>=4.24.5`(若采用 `astrbot.api.web` 则 `>=4.26.0`)或保留门槛+前端能力探测 |

### 7.2 前端(webui)

| 文件 | 改动 |
| --- | --- |
| `types.ts` | `FileAttachmentRef`、`file` part、`maid/artifact` 事件类型 |
| `api/bridge.ts` | 导出 `uploadFile` / `downloadFile` 封装 + 能力探测 |
| `store/app.ts` | `uploadAttachment`、`downloadAttachment`;`sendPrompt` 组装 file part |
| `components/Composer.tsx` | `onPaste`/`onDrop`/accept 放开/附件 chip 分型/降级报错(§4.1) |
| `components/ChatView.tsx` | 用户文件 chips;ArtifactNode 渲染 + 下载按钮 |
| `store/conversation.ts` | `case "maid/artifact"` → ArtifactNode |
| `dev/mock-bridge.ts`、`scripts/smoke-render.mjs` | mock upload/download 与 file part |

### 7.3 兼容与降级策略

- 老版 AstrBot(<4.24.5):桥无 `upload`/`download` → 前端禁用文件入口并提示版本要求;图片粘贴→base64 通道仍可用(纯前端+现有 RPC);
- 旧前端构建(已分发的 `pages/console` 产物)对新后端:新 part 类型不出现,行为完全不变(后端对未知 part 维持现状忽略或报 `bad-request`);
- `astrbot.api.web` 仅 v4.26.0+:若要兼容 4.24/4.25,新路由用 Quart 风格实现(兼容层两条路都通,§3.2)。

### 7.4 测试计划

- `tests/`(现有 fake-astrbot 桩模式):store 泛化存取(类型/扩展/防穿越)、`session_prompt` file part、upload/file 路由 handler(模拟 multipart 与 query)、`maid_deliver_file` 工具(成功/超限/不存在/sandbox 拉取桩)、retention 删除会话时附件与 workspace 副本清理;
- 前端:`npm run build`(`tsc --noEmit`)、`smoke-render.mjs` 增补 file part 与 artifact 事件;
- 人工验证矩阵:粘贴截图 / 粘贴非图片 / 拖拽 / + 号选 pdf;管家 `maid_deliver_file` 产物在控制台 chip 可下载、聊天会话收到文件;老宿主(4.23.x)降级提示。

---

## 8. 风险与开放问题

1. **member + local 的读权限**(§4.2 方案一 vs 二):workspace 副本方案会让上传文件落到 AstrBot workspace 目录,与插件附件区形成两份;需接受"规范副本 + 可读副本"的一致性成本(上传后文件不可变,复制一次即可,风险低)。
2. **大文件**:桥上传 timeout 60s,几十 MB 在慢网络可能超时;`max_upload_mb` 默认值与报错文案要明确。下载走父窗口 blob,无超时问题。
3. **沙箱运行时**:产物路径在 sandbox 内,`maid_deliver_file` 依赖 booter 拉取,与 `astrbot_download_file` 的 `sb.download_file` 行为对齐;失败要给模型可读错误让它重试或换路径。
4. **文件名安全**:`name` 全程作为元数据传递,落盘名始终是 `<attachmentId>.<ext>`;下载时 `filename*=UTF-8''…` 编码,避免 header 注入与乱码。
5. **`maid/artifact` 与折叠器**:新增 node 类型要过 memo/引用稳定约定(conversation.ts 头注释),避免重蹈 2.0.64 轮次轨失效的覆辙(原地修改不重渲染)。
6. **是否把图片也迁到 upload 通道**:统一通道更干净且消除 base64 JSON 膨胀,但会多一次桥往返;可作为后续优化,不影响本期兼容。
7. **聊天侧用户发文件的摄取**(`events_shim.py` `image_paths_from_event` 只认 Image 组件):与本方案同构(快照 File 组件 → 附件区 → prompt 附录),建议作为独立小改动跟进,不在本期 WebUI 范围内。

---

## 9. 证据索引

本仓库(行号基于当前工作区):

- `webui/src/components/Composer.tsx` L75–84(图片过滤与上限)、L118(textarea 无 onPaste)、L144–158(+ 号与 accept)
- `webui/src/types.ts` L151(PromptContentPart)
- `webui/src/store/app.ts` L400(session.prompt)、L432(loadAttachmentImage)
- `webui/src/api/bridge.ts` L12–13(桥已声明 upload/download)
- `harness/api.py` L349–395(session_prompt)、L396–415(session_attachment)
- `harness/store.py` L26(IMAGE_MEDIA_EXT)、L199–227(save_attachment*)、L250–293(load/paths)
- `harness/drivers.py` L530(voice sink 仅 chat)、L571(image_urls)、L877(_speak 纯文本)、L1160(image_paths_for_message)
- `harness/events_shim.py` L44(只快照 Image)、L127–138(dashboard 不可投递)
- `maid_dispatcher.py` `_build_runner`(ProviderRequest 仅 image_urls)
- `toolset_adapter.py`(child toolset 组装与 sanitize)
- `main.py` L218–227(仅 POST/SSE 路由)
- `webui/src/store/conversation.ts` L270–497(折叠器 case 表)

AstrBot 官方源码(核实日期 2026-09-08):

- `astrbot/dashboard/plugin_page_bridge.js`(v4.24.5+ / v4.28.0):`upload`/`download`/`apiGet`/`apiPost`/`subscribeSSE` 定义
- `dashboard/src/views/PluginPagePage.vue` L382–455(api:get/api:post/files:upload/files:download 的父窗口实现)、L651(iframe sandbox allow-downloads)
- `astrbot/dashboard/api/plugins.py`(v4.28.0)L379–421(extensions 代理路由与 require_plugin_scope)、L204–232(_call_plugin_extension)
- `astrbot/dashboard/asgi_runtime.py`(v4.28.0)L475–614(Quart 兼容层:响应转换保 headers、test_request_context 绑定)
- `astrbot/api/web.py`(v4.26.0+):`PluginUploadFile` / `request` 代理 / `file_response` 等
- `astrbot/core/tools/computer_tools/fs.py`(v4.28.0)L1–29(权限审计)、L804–950(`astrbot_upload_file` / `astrbot_download_file` 及 `File`/`Image.fromFileSystem` 用法)
- `astrbot/core/provider/entities.py`(v4.28.0)L91–116(ProviderRequest 无通用文件字段)
- 版本时间线:bridge `files:upload/download` 自 v4.24.5(2026-05-13);`astrbot.api.web` 自 v4.26.0;v4.23.6(2026-04-27)及更早无此能力;当前最新 v4.28.0(2026-09-07)
