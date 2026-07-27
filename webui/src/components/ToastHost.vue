<script setup>
import { toastState } from "@/composables/useToast";
</script>

<template>
  <transition name="toast">
    <div v-if="toastState.visible" class="toast" :class="`is-${toastState.tone}`" role="status">
      {{ toastState.message }}
    </div>
  </transition>
</template>

<style scoped>
.toast {
  position: fixed;
  bottom: 22px;
  left: 50%;
  transform: translateX(-50%);
  padding: 9px 16px;
  border-radius: 999px;
  background: var(--text-main);
  color: var(--bg-app);
  font-size: 12.5px;
  box-shadow: var(--shadow-float);
  max-width: 80vw;
  z-index: 2000;
  pointer-events: none;
}
.toast.is-error {
  background: var(--err);
  color: #fff;
}

.toast-enter-active,
.toast-leave-active {
  transition: opacity 0.18s ease, transform 0.18s ease;
}
.toast-enter-from,
.toast-leave-to {
  opacity: 0;
  transform: translate(-50%, 8px);
}
</style>
