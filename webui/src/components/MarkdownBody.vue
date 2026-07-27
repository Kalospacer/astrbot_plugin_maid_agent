<script setup>
import { onMounted, onUpdated, ref } from "vue";

import { renderMarkdown, sanitizeMarkdownRoot } from "@/utils/markdown";

const props = defineProps({
  text: { type: String, default: "" },
});

const root = ref(null);

function sanitize() {
  sanitizeMarkdownRoot(root.value);
}

onMounted(sanitize);
onUpdated(sanitize);
</script>

<template>
  <div ref="root" class="markdown-body" v-html="renderMarkdown(props.text)" />
</template>
