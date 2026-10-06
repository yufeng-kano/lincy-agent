<script setup lang="ts">
import { computed } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'isomorphic-dompurify'

marked.setOptions({ breaks: true, gfm: true })

const props = withDefaults(
  defineProps<{
    content: string
    /** dark = inbound (white text on near-black); light = outbound */
    variant?: 'light' | 'dark'
  }>(),
  { variant: 'light' },
)

const html = computed(() => {
  const raw = marked.parse(props.content ?? '', { async: false }) as string
  return DOMPurify.sanitize(raw)
})
</script>

<template>
  <div
    class="md-content break-words text-sm leading-6"
    :class="variant === 'dark' ? 'md-content--dark' : 'md-content--light'"
    v-html="html"
  />
</template>

<style scoped>
.md-content :deep(p) {
  margin: 0;
}

.md-content :deep(p + p) {
  margin-top: 0.5em;
}

.md-content :deep(ul),
.md-content :deep(ol) {
  margin: 0.35em 0;
  padding-left: 1.25em;
}

.md-content :deep(li) {
  margin: 0.15em 0;
}

.md-content :deep(li > p) {
  margin: 0;
}

.md-content :deep(strong) {
  font-weight: 600;
}

.md-content :deep(em) {
  font-style: italic;
}

.md-content :deep(a) {
  text-decoration: underline;
  text-underline-offset: 2px;
}

.md-content :deep(code) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 0.85em;
  padding: 0.1em 0.35em;
  border-radius: 0.25rem;
}

.md-content :deep(pre) {
  margin: 0.5em 0;
  padding: 0.6em 0.75em;
  border-radius: 0.5rem;
  overflow-x: auto;
  font-size: 0.8em;
  line-height: 1.5;
}

.md-content :deep(pre code) {
  padding: 0;
  background: transparent;
  border-radius: 0;
  font-size: inherit;
}

.md-content :deep(blockquote) {
  margin: 0.5em 0;
  padding-left: 0.75em;
  border-left: 2px solid currentColor;
  opacity: 0.85;
}

.md-content :deep(hr) {
  margin: 0.75em 0;
  border: none;
  border-top: 1px solid currentColor;
  opacity: 0.25;
}

.md-content--light {
  color: #111827;
}

.md-content--light :deep(a) {
  color: #111827;
}

.md-content--light :deep(code) {
  background: #F3F4F6;
  color: #111827;
}

.md-content--light :deep(pre) {
  background: #F3F4F6;
  color: #111827;
}

.md-content--light :deep(blockquote) {
  color: #6B7280;
  border-left-color: #D1D5DB;
}

.md-content--dark {
  color: #ffffff;
}

.md-content--dark :deep(a) {
  color: #E5E7EB;
}

.md-content--dark :deep(code) {
  background: rgba(255, 255, 255, 0.12);
  color: #F9FAFB;
}

.md-content--dark :deep(pre) {
  background: rgba(255, 255, 255, 0.1);
  color: #F9FAFB;
}

.md-content--dark :deep(blockquote) {
  color: #D1D5DB;
  border-left-color: rgba(255, 255, 255, 0.35);
}
</style>
