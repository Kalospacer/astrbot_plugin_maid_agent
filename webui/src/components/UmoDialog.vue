<script setup>
import { nextTick, ref, watch } from "vue";

const props = defineProps({
  open: { type: Boolean, default: false },
  umos: { type: Array, default: () => [] },
  selected: { type: String, default: "" },
});

const emit = defineEmits(["close", "select"]);

const input = ref(null);
const draft = ref("");

watch(
  () => props.open,
  async (open) => {
    if (!open) return;
    draft.value = "";
    await nextTick();
    input.value?.focus();
  },
);

function submit() {
  const value = draft.value.trim();
  if (!value) return;
  emit("select", value);
}
</script>

<template>
  <div v-if="open" class="overlay" @click.self="emit('close')">
    <div class="dialog" role="dialog" aria-modal="true" aria-label="选择 UMO">
      <header class="dialog-head">
        <h2>选择用户 (UMO)</h2>
        <button class="icon-btn" type="button" aria-label="关闭" @click="emit('close')">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>
      </header>

      <div class="dialog-body">
        <form @submit.prevent="submit">
          <input
            ref="input"
            v-model="draft"
            type="text"
            placeholder="输入新的 UMO 并回车…"
            autocomplete="off"
          />
        </form>

        <p v-if="!umos.length" class="empty-state">没有历史来源。手动输入一个 AstrBot UMO。</p>
        <ul v-else class="umo-list">
          <li v-for="umo in umos" :key="umo">
            <button
              class="umo-item"
              :class="{ 'is-active': umo === selected }"
              type="button"
              @click="emit('select', umo)"
            >
              <span class="umo-avatar">{{ umo.slice(0, 2).toUpperCase() }}</span>
              <span class="umo-text">{{ umo }}</span>
            </button>
          </li>
        </ul>
      </div>
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
  width: 380px;
  max-width: 92vw;
  max-height: 80vh;
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
  padding: 14px 16px 16px;
  overflow-y: auto;
}
.dialog-body input {
  width: 100%;
  padding: 8px 10px;
  margin-bottom: 12px;
  background: var(--bg-sunken);
  border: 1px solid var(--line);
  border-radius: var(--radius-item);
  font-size: 13px;
}
.dialog-body input:focus {
  border-color: var(--accent);
}

.umo-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.umo-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 8px 10px;
  border-radius: var(--radius-item);
  text-align: left;
}
.umo-item:hover {
  background: var(--bg-sunken);
}
.umo-item.is-active {
  background: var(--accent-soft);
}
.umo-avatar {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: var(--accent);
  color: #fff;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 600;
  flex-shrink: 0;
}
.umo-text {
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
