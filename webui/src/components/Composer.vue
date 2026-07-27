<script setup>
import { computed, nextTick, ref } from "vue";

import { uploadFile } from "@/api/bridge";
import { toast } from "@/composables/useToast";
import { formatFileSize } from "@/utils/format";

const props = defineProps({
  agentNames: { type: Array, default: () => [] },
  dispatchAgent: { type: String, default: "" },
  mode: { type: String, default: "dispatch" }, // dispatch | resume | steer
  busy: { type: Boolean, default: false },
});

const emit = defineEmits(["submit", "update:dispatchAgent"]);

const MAX_ATTACHMENTS = 5;
const MAX_BYTES = 10 * 1024 * 1024;
const ALLOWED_TYPES = new Set(["image/jpeg", "image/png", "image/webp", "image/gif"]);

const textarea = ref(null);
const fileInput = ref(null);
const text = ref("");
const attachments = ref([]);
const sending = ref(false);

const PLACEHOLDER = {
  dispatch: "有什么可以帮忙的？",
  resume: "继续给这位管家派活…",
  steer: "追加要求，立即注入正在运行的 Run…",
};
const placeholder = computed(() => PLACEHOLDER[props.mode] || PLACEHOLDER.dispatch);

const canSend = computed(
  () => !sending.value && !props.busy && (text.value.trim().length > 0 || attachments.value.length > 0),
);

function focus() {
  textarea.value?.focus();
}
defineExpose({ focus });

function resize() {
  const el = textarea.value;
  if (!el) return;
  el.style.height = "auto";
  el.style.height = `${Math.min(el.scrollHeight, 220)}px`;
}

/* ------------------------------------------------------------- 附件 */

async function uploadOne(attachment) {
  try {
    const result = await uploadFile("console/upload", attachment.file);
    if (!result?.path) throw new Error("上传接口没有返回图片路径。");
    attachment.path = result.path;
    attachment.size = result.size ?? attachment.size;
    attachment.status = "ready";
  } catch (err) {
    attachment.status = "error";
    attachment.error = err.message || "图片上传失败";
  }
  return attachment;
}

function addFiles(fileList) {
  const files = Array.from(fileList || []);
  let skipped = 0;
  for (const file of files) {
    if (attachments.value.length >= MAX_ATTACHMENTS) {
      skipped += 1;
      continue;
    }
    if (!ALLOWED_TYPES.has(file.type) || file.size <= 0 || file.size > MAX_BYTES) {
      skipped += 1;
      continue;
    }
    const attachment = {
      id: `attachment_${Date.now()}_${Math.random().toString(16).slice(2)}`,
      file,
      name: file.name || "image",
      size: file.size,
      previewUrl: URL.createObjectURL(file),
      path: "",
      status: "uploading",
      error: "",
      uploadPromise: null,
    };
    attachment.uploadPromise = uploadOne(attachment);
    attachments.value.push(attachment);
  }
  if (skipped) {
    toast("部分图片未添加：仅支持 JPEG/PNG/WEBP/GIF，最多 5 张且每张不超过 10 MB", "error");
  }
}

function removeAttachment(id) {
  const index = attachments.value.findIndex((item) => item.id === id);
  if (index < 0) return;
  const [removed] = attachments.value.splice(index, 1);
  if (removed.previewUrl) URL.revokeObjectURL(removed.previewUrl);
}

function clearAttachments() {
  for (const item of attachments.value) {
    if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
  }
  attachments.value = [];
  if (fileInput.value) fileInput.value.value = "";
}

async function readyPaths() {
  await Promise.all(attachments.value.map((item) => item.uploadPromise).filter(Boolean));
  const failed = attachments.value.find((item) => item.status === "error");
  if (failed) throw new Error(`${failed.name}：${failed.error || "上传失败"}`);
  return attachments.value.filter((item) => item.status === "ready" && item.path).map((item) => item.path);
}

function onPaste(event) {
  const files = Array.from(event.clipboardData?.files || []);
  if (!files.length) return;
  event.preventDefault();
  addFiles(files);
}

function onDrop(event) {
  const files = Array.from(event.dataTransfer?.files || []);
  if (!files.length) return;
  event.preventDefault();
  addFiles(files);
}

/* ------------------------------------------------------------- 发送 */

async function submit() {
  if (!canSend.value) return;
  sending.value = true;
  try {
    const imagePaths = await readyPaths();
    emit("submit", { text: text.value.trim(), imagePaths, done: reset });
  } catch (err) {
    toast(err.message || "发送失败", "error");
    sending.value = false;
  }
}

/** 由父层在派发成功后调用：清空输入并恢复可用。 */
async function reset(ok = true) {
  sending.value = false;
  if (!ok) return;
  text.value = "";
  clearAttachments();
  await nextTick();
  resize();
}

function onKeydown(event) {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    submit();
  }
}
</script>

<template>
  <form class="composer" @submit.prevent="submit" @drop="onDrop" @dragover.prevent>
    <div v-if="attachments.length" class="attachments">
      <div
        v-for="item in attachments"
        :key="item.id"
        class="attachment"
        :class="{ 'is-error': item.status === 'error' }"
        :title="item.error || item.name"
      >
        <img :src="item.previewUrl" alt="" />
        <span class="attachment-meta">
          <strong>{{ item.name }}</strong>
          <small>
            {{
              item.status === "uploading"
                ? "上传中"
                : item.status === "error"
                  ? "上传失败"
                  : formatFileSize(item.size)
            }}
          </small>
        </span>
        <button
          class="attachment-remove"
          type="button"
          :aria-label="`移除 ${item.name}`"
          @click="removeAttachment(item.id)"
        >
          ×
        </button>
      </div>
    </div>

    <textarea
      ref="textarea"
      v-model="text"
      rows="1"
      :placeholder="placeholder"
      autocomplete="off"
      @input="resize"
      @keydown="onKeydown"
      @paste="onPaste"
    />

    <div class="composer-bar">
      <div class="composer-left">
        <input
          ref="fileInput"
          type="file"
          accept="image/jpeg,image/png,image/webp,image/gif"
          multiple
          hidden
          @change="
            addFiles($event.target.files);
            $event.target.value = '';
          "
        />
        <button
          class="icon-btn attach-btn"
          type="button"
          aria-label="添加图片"
          title="添加图片（最多 5 张，每张 10 MB；也可直接粘贴或拖入）"
          @click="fileInput?.click()"
        >
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
          </svg>
        </button>
        <span v-if="mode === 'steer'" class="mode-hint">steer 到运行中的 Run</span>
        <span v-else-if="mode === 'resume'" class="mode-hint">resume 当前 Agent</span>
      </div>

      <div class="composer-right">
        <label class="agent-select" :class="{ 'is-hidden': mode !== 'dispatch' }">
          <span class="sr-only">选择执行本次任务的管家</span>
          <select
            :value="dispatchAgent"
            :disabled="mode !== 'dispatch'"
            @change="emit('update:dispatchAgent', $event.target.value)"
          >
            <option v-for="name in agentNames" :key="name" :value="name">{{ name }}</option>
          </select>
        </label>
        <button class="send-btn" type="submit" :disabled="!canSend" aria-label="发送">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
            <line x1="12" y1="19" x2="12" y2="5" /><polyline points="5 12 12 5 19 12" />
          </svg>
        </button>
      </div>
    </div>
  </form>
</template>

<style scoped>
.composer {
  display: flex;
  flex-direction: column;
  background: var(--bg-raised);
  border: 1px solid var(--line-strong);
  border-radius: var(--radius-card);
  padding: 10px 12px 8px;
  box-shadow: var(--shadow-float);
  transition: border-color 0.15s ease;
}
.composer:focus-within {
  border-color: var(--accent);
}

.attachments {
  display: flex;
  gap: 8px;
  overflow-x: auto;
  padding-bottom: 8px;
}
.attachment {
  position: relative;
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 160px;
  max-width: 210px;
  padding: 5px 26px 5px 5px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--bg-sunken);
}
.attachment.is-error {
  border-color: var(--err);
}
.attachment img {
  width: 34px;
  height: 34px;
  flex: 0 0 34px;
  border-radius: 6px;
  object-fit: cover;
  background: var(--line);
}
.attachment-meta {
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.attachment-meta strong {
  font-size: 11.5px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.attachment-meta small {
  font-size: 10px;
  color: var(--text-muted);
}
.attachment.is-error .attachment-meta small {
  color: var(--err);
}
.attachment-remove {
  position: absolute;
  top: 3px;
  right: 4px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  color: var(--text-muted);
  font-size: 15px;
  line-height: 16px;
}
.attachment-remove:hover {
  color: var(--text-main);
  background: var(--line);
}

textarea {
  background: transparent;
  font-size: var(--size-body);
  line-height: 1.6;
  min-height: 22px;
  max-height: 220px;
  padding: 0;
  margin: 2px 0 6px;
  overflow-y: auto;
}
textarea::placeholder {
  color: var(--text-dim);
}

.composer-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.composer-left,
.composer-right {
  display: flex;
  align-items: center;
  gap: 8px;
}
.attach-btn {
  width: 26px;
  height: 26px;
  border: 1px solid var(--line-strong);
  border-radius: 50%;
}
.mode-hint {
  font-size: var(--size-meta);
  color: var(--accent);
}

.agent-select.is-hidden {
  display: none;
}
.agent-select select {
  appearance: none;
  padding: 4px 22px 4px 9px;
  border-radius: var(--radius-item);
  border: 1px solid transparent;
  background: transparent;
  color: var(--text-muted);
  font-size: 12.5px;
  font-weight: 500;
  max-width: 170px;
  cursor: pointer;
  background-image: linear-gradient(45deg, transparent 50%, currentColor 50%),
    linear-gradient(135deg, currentColor 50%, transparent 50%);
  background-position: right 10px center, right 6px center;
  background-size: 4px 4px, 4px 4px;
  background-repeat: no-repeat;
}
.agent-select select:hover {
  background-color: var(--bg-sunken);
  color: var(--text-main);
}

.send-btn {
  width: 30px;
  height: 30px;
  border-radius: var(--radius-item);
  background: var(--accent);
  color: #ffffff;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.send-btn:hover:not(:disabled) {
  background: var(--accent-hover);
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}
</style>
