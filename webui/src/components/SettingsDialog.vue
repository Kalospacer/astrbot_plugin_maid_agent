<script setup>
import { reactive, ref, watch } from "vue";

import { toastError } from "@/composables/useToast";

const props = defineProps({
  open: { type: Boolean, default: false },
  config: { type: Object, default: null },
});

const emit = defineEmits(["close", "save"]);

const saving = ref(false);

/**
 * 表单绑的是**打开弹窗那一刻的本地副本**，不是 store。
 * 1.4.1 每次刷新都用最新配置回填 DOM，正在编辑的字段会被覆写。
 */
const form = reactive({
  default_agent_name: "",
  allowed_agent_names: "",
  hide_native_tools: false,
  hide_transfer_tools: false,
  include_raw_user_input: false,
  log_raw_llm_io: false,
  foreground_timeout_seconds: 50,
  memory_agent_names: "",
  max_active_per_umo: 5,
  max_active_global: 20,
  retention_days: 30,
  dispatch_prompt_template: "",
});

watch(
  () => props.open,
  (open) => {
    if (!open) return;
    const config = props.config || {};
    form.default_agent_name = config.default_agent_name || "";
    form.allowed_agent_names = (config.allowed_agent_names || []).join(", ");
    form.hide_native_tools = Boolean(config.hide_native_tools);
    form.hide_transfer_tools = Boolean(config.hide_transfer_tools);
    form.include_raw_user_input = Boolean(config.include_raw_user_input);
    form.log_raw_llm_io = Boolean(config.log_raw_llm_io);
    form.foreground_timeout_seconds = config.foreground_timeout_seconds ?? 50;
    form.memory_agent_names = (config.memory_agent_names || []).join(", ");
    form.max_active_per_umo = config.max_active_per_umo ?? 5;
    form.max_active_global = config.max_active_global ?? 20;
    form.retention_days = config.retention_days ?? 30;
    form.dispatch_prompt_template = config.dispatch_prompt_template || "";
  },
  { immediate: true },
);

function splitNames(value) {
  return String(value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

async function submit() {
  saving.value = true;
  try {
    await emit("save", {
      default_agent_name: form.default_agent_name.trim(),
      allowed_agent_names: splitNames(form.allowed_agent_names),
      hide_native_tools: form.hide_native_tools,
      hide_transfer_tools: form.hide_transfer_tools,
      include_raw_user_input: form.include_raw_user_input,
      log_raw_llm_io: form.log_raw_llm_io,
      foreground_timeout_seconds: Math.min(55, Math.max(1, Number(form.foreground_timeout_seconds) || 50)),
      memory_agent_names: splitNames(form.memory_agent_names),
      max_active_per_umo: Math.max(1, Number(form.max_active_per_umo) || 5),
      max_active_global: Math.max(1, Number(form.max_active_global) || 20),
      retention_days: Math.max(1, Number(form.retention_days) || 30),
      dispatch_prompt_template: form.dispatch_prompt_template,
    });
  } catch (err) {
    toastError(err, "保存失败");
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <div v-if="open" class="overlay" @click.self="emit('close')">
    <div class="dialog" role="dialog" aria-modal="true" aria-label="全局配置">
      <header class="dialog-head">
        <h2>全局配置</h2>
        <button class="icon-btn" type="button" aria-label="关闭" @click="emit('close')">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>
      </header>

      <form class="dialog-body" @submit.prevent="submit">
        <label>
          <span>默认 Agent</span>
          <input v-model="form.default_agent_name" autocomplete="off" />
        </label>
        <label>
          <span>允许 Agent（逗号分隔）</span>
          <input v-model="form.allowed_agent_names" autocomplete="off" />
        </label>

        <div class="toggles">
          <label><input v-model="form.hide_native_tools" type="checkbox" />隐藏原生工具</label>
          <label><input v-model="form.hide_transfer_tools" type="checkbox" />隐藏 transfer</label>
          <label><input v-model="form.include_raw_user_input" type="checkbox" />附带用户原话</label>
          <label><input v-model="form.log_raw_llm_io" type="checkbox" />记录 LLM 原文</label>
        </div>

        <div class="grid-2">
          <label>
            <span>Foreground 超时（秒）</span>
            <input v-model.number="form.foreground_timeout_seconds" type="number" min="1" max="55" step="1" />
          </label>
          <label>
            <span>轨迹保留天数</span>
            <input v-model.number="form.retention_days" type="number" min="1" step="1" />
          </label>
          <label>
            <span>每 UMO 最大活跃 Run</span>
            <input v-model.number="form.max_active_per_umo" type="number" min="1" step="1" />
          </label>
          <label>
            <span>全局最大活跃 Run</span>
            <input v-model.number="form.max_active_global" type="number" min="1" step="1" />
          </label>
        </div>

        <label>
          <span>启用 Memory 的 Agent（逗号分隔）</span>
          <input v-model="form.memory_agent_names" autocomplete="off" />
        </label>
        <label>
          <span>调度提示模板</span>
          <textarea v-model="form.dispatch_prompt_template" rows="8" />
        </label>

        <footer class="dialog-foot">
          <button class="btn-ghost" type="button" @click="emit('close')">取消</button>
          <button class="btn-primary" type="submit" :disabled="saving">
            {{ saving ? "保存中…" : "保存全局配置" }}
          </button>
        </footer>
      </form>
    </div>
  </div>
</template>

<style scoped>
.overlay {
  position: fixed;
  inset: 0;
  background: rgba(20, 22, 24, 0.45);
  backdrop-filter: blur(2px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}
.dialog {
  width: 520px;
  max-width: 92vw;
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  background: var(--bg-raised);
  border: 1px solid var(--line);
  border-radius: var(--radius-card);
  box-shadow: var(--shadow-modal);
}
.dialog-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 16px;
  border-bottom: 1px solid var(--line);
}
.dialog-head h2 {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
}
.dialog-body {
  padding: 16px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.dialog-body label span {
  display: block;
  font-size: 12.5px;
  font-weight: 500;
  margin-bottom: 5px;
}
.dialog-body input:not([type="checkbox"]),
.dialog-body textarea {
  width: 100%;
  background: var(--bg-sunken);
  border: 1px solid var(--line);
  border-radius: var(--radius-item);
  padding: 8px 10px;
  font-size: 13px;
}
.dialog-body textarea {
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.6;
  resize: vertical;
}
.dialog-body input:focus,
.dialog-body textarea:focus {
  border-color: var(--accent);
}
.dialog-body input[type="checkbox"] {
  accent-color: var(--accent);
  width: auto;
}
.toggles {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
.toggles label {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 13px;
  cursor: pointer;
}
.grid-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.dialog-foot {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 6px;
  padding-top: 12px;
  border-top: 1px solid var(--line);
}
.btn-ghost {
  padding: 8px 14px;
  border: 1px solid var(--line-strong);
  border-radius: var(--radius-item);
  color: var(--text-muted);
  font-size: 13px;
}
.btn-ghost:hover {
  color: var(--text-main);
  background: var(--bg-sunken);
}
.btn-primary {
  padding: 8px 16px;
  border-radius: var(--radius-item);
  background: var(--accent);
  color: #fff;
  font-size: 13px;
  font-weight: 500;
}
.btn-primary:hover:not(:disabled) {
  background: var(--accent-hover);
}
</style>
