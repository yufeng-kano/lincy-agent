<script setup lang="ts">
import { ref } from 'vue'
import { ArrowDown, ArrowUp, ChevronDown, ChevronRight } from 'lucide-vue-next'
import type { AgentUiEvent } from '@/api/client'
import { eventFlag, eventText, type TimelineRow } from '@/stores/agentEvents'

defineProps<{ rows: TimelineRow[] }>()

const expanded = ref<Set<string>>(new Set())

function toggle(key: string) {
  if (expanded.value.has(key)) expanded.value.delete(key)
  else expanded.value.add(key)
}

function formatTime(value: number): string {
  return new Date(value).toLocaleTimeString('en', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

function oneLine(text: string): string {
  return text.replace(/\s+/g, ' ').trim()
}

function isLongText(text: string): boolean {
  return text.length > 240 || text.split('\n').length > 3
}

function toolName(event: AgentUiEvent): string {
  return eventText(event, 'name') || 'tool'
}

function toolPending(row: TimelineRow): boolean {
  return row.event.type === 'tool_call' && row.result === null
}

function toolResult(row: TimelineRow): AgentUiEvent | null {
  return row.event.type === 'tool_result' ? row.event : row.result
}

function toolFailed(row: TimelineRow): boolean {
  const result = toolResult(row)
  return result !== null && eventFlag(result, 'failed')
}

function toolWarning(row: TimelineRow): boolean {
  const result = toolResult(row)
  return result !== null && eventFlag(result, 'warning')
}

function toolDotClass(row: TimelineRow): string {
  if (toolFailed(row)) return 'bg-[#EF4444]'
  if (toolWarning(row)) return 'bg-[#F59E0B]'
  if (toolPending(row)) return 'animate-pulse bg-[#111827]'
  return 'bg-[#D1D5DB]'
}

function toolBorderClass(row: TimelineRow): string {
  if (toolFailed(row)) return 'border-[#EF4444]'
  if (toolWarning(row)) return 'border-[#F59E0B]'
  return 'border-[#E5E7EB]'
}

function messageChannel(event: AgentUiEvent): string {
  return eventText(event, 'channel') || 'cli'
}

function messagePeer(event: AgentUiEvent): string {
  return eventText(event, event.type === 'inbound_message' ? 'sender' : 'recipient')
}

function separatorLabel(row: TimelineRow): string {
  const channel = messageChannel(row.event)
  if (row.event.type === 'processing_started') return `turn start - ${channel}`
  if (eventFlag(row.event, 'interrupted')) return `turn end - ${channel} - interrupted`
  return `turn end - ${channel}`
}
</script>

<template>
  <div class="space-y-2">
    <template v-for="row in rows" :key="row.key">
      <!-- Tool call / orphan tool result -->
      <div
        v-if="row.event.type === 'tool_call' || row.event.type === 'tool_result'"
        class="rounded-lg border bg-white shadow-[0_1px_2px_rgba(0,0,0,0.04)]"
        :class="toolBorderClass(row)"
      >
        <button
          type="button"
          class="flex w-full items-center gap-2 px-3 py-2 text-left"
          @click="toggle(row.key)"
        >
          <span class="h-1.5 w-1.5 shrink-0 rounded-full" :class="toolDotClass(row)" />
          <span class="shrink-0 font-mono text-xs text-[#111827]">{{ toolName(row.event) }}</span>
          <span class="min-w-0 flex-1 truncate text-xs text-[#6B7280]">
            {{ oneLine(eventText(row.event, 'summary')) }}
          </span>
          <span class="shrink-0 text-[11px] tabular-nums text-[#9CA3AF]">
            {{ formatTime(row.time) }}
          </span>
          <ChevronDown v-if="expanded.has(row.key)" class="h-3.5 w-3.5 shrink-0 text-[#9CA3AF]" />
          <ChevronRight v-else class="h-3.5 w-3.5 shrink-0 text-[#9CA3AF]" />
        </button>
        <div v-if="expanded.has(row.key)" class="space-y-2 border-t border-[#E5E7EB] px-3 py-2">
          <pre class="overflow-x-auto whitespace-pre-wrap break-words font-mono text-[11px] leading-5 text-[#111827]">{{ eventText(row.event, 'summary') }}</pre>
          <div v-if="row.streams.length > 0">
            <p class="mb-1 text-[11px] text-[#9CA3AF]">stream</p>
            <pre class="overflow-x-auto whitespace-pre-wrap break-words font-mono text-[11px] leading-5 text-[#6B7280]">{{ row.streams.join('\n') }}</pre>
          </div>
          <div v-if="row.result">
            <p class="mb-1 text-[11px] text-[#9CA3AF]">result</p>
            <pre class="overflow-x-auto whitespace-pre-wrap break-words font-mono text-[11px] leading-5 text-[#6B7280]">{{ eventText(row.result, 'summary') }}</pre>
          </div>
          <p v-else-if="toolPending(row)" class="text-[11px] text-[#9CA3AF]">running</p>
        </div>
      </div>

      <!-- Assistant inner monologue -->
      <div
        v-else-if="row.event.type === 'assistant_text'"
        class="rounded-lg border border-[#E5E7EB] bg-[#F9FAFB] px-3 py-2"
      >
        <div class="mb-1 flex items-center justify-between text-[11px] text-[#9CA3AF]">
          <span>inner monologue</span>
          <span class="tabular-nums">{{ formatTime(row.time) }}</span>
        </div>
        <p
          class="whitespace-pre-wrap break-words text-xs leading-5 text-[#6B7280]"
          :class="!expanded.has(row.key) && isLongText(eventText(row.event, 'content')) ? 'line-clamp-3' : ''"
        >{{ eventText(row.event, 'content') }}</p>
        <button
          v-if="isLongText(eventText(row.event, 'content'))"
          type="button"
          class="mt-1 text-[11px] text-[#6B7280] transition-colors hover:text-[#111827]"
          @click="toggle(row.key)"
        >
          {{ expanded.has(row.key) ? 'Show less' : 'Show more' }}
        </button>
      </div>

      <!-- Inbound / outbound message, any channel -->
      <div
        v-else-if="row.event.type === 'inbound_message' || row.event.type === 'outbound_message'"
        class="rounded-lg border border-[#E5E7EB] bg-white shadow-[0_1px_2px_rgba(0,0,0,0.04)]"
      >
        <button
          type="button"
          class="flex w-full items-center gap-2 px-3 py-2 text-left"
          @click="toggle(row.key)"
        >
          <ArrowDown
            v-if="row.event.type === 'inbound_message'"
            class="h-3.5 w-3.5 shrink-0 text-[#6B7280]"
          />
          <ArrowUp v-else class="h-3.5 w-3.5 shrink-0 text-[#6B7280]" />
          <span class="shrink-0 rounded border border-[#E5E7EB] px-1.5 py-0.5 font-mono text-[10px] text-[#6B7280]">
            {{ messageChannel(row.event) }}
          </span>
          <span v-if="messagePeer(row.event)" class="shrink-0 text-[11px] text-[#9CA3AF]">
            {{ messagePeer(row.event) }}
          </span>
          <span class="min-w-0 flex-1 truncate text-xs text-[#111827]">
            {{ oneLine(eventText(row.event, 'content')) }}
          </span>
          <span class="shrink-0 text-[11px] tabular-nums text-[#9CA3AF]">
            {{ formatTime(row.time) }}
          </span>
        </button>
        <div v-if="expanded.has(row.key)" class="border-t border-[#E5E7EB] px-3 py-2">
          <p class="whitespace-pre-wrap break-words text-xs leading-5 text-[#111827]">{{ eventText(row.event, 'content') }}</p>
        </div>
      </div>

      <!-- Turn separators -->
      <div
        v-else-if="row.event.type === 'processing_started' || row.event.type === 'processing_finished'"
        class="flex items-center gap-2 py-1"
      >
        <span class="h-px flex-1 bg-[#E5E7EB]" />
        <span class="text-[11px] text-[#9CA3AF]">{{ separatorLabel(row) }}</span>
        <span class="text-[11px] tabular-nums text-[#D1D5DB]">{{ formatTime(row.time) }}</span>
        <span class="h-px flex-1 bg-[#E5E7EB]" />
      </div>

      <!-- Resume separator -->
      <div v-else-if="row.event.type === 'resume_history'" class="flex items-center gap-2 py-1">
        <span class="h-px flex-1 bg-[#E5E7EB]" />
        <span class="text-[11px] text-[#9CA3AF]">{{ eventText(row.event, 'summary') }}</span>
        <span class="h-px flex-1 bg-[#E5E7EB]" />
      </div>

      <!-- Warning / error -->
      <div
        v-else-if="row.event.type === 'warning' || row.event.type === 'error'"
        class="rounded-lg border px-3 py-2 text-xs leading-5"
        :class="row.event.type === 'error'
          ? 'border-[#EF4444] text-[#EF4444]'
          : 'border-[#F59E0B] text-[#F59E0B]'"
      >
        <span class="whitespace-pre-wrap break-words">{{ eventText(row.event, 'message') }}</span>
      </div>

      <!-- Interrupt notice -->
      <div v-else-if="row.event.type === 'interrupt_state'" class="flex items-center gap-2 py-1">
        <span class="h-px flex-1 bg-[#E5E7EB]" />
        <span class="text-[11px] text-[#F59E0B]">
          interrupt {{ eventText(row.event, 'phase') }}
          <template v-if="eventText(row.event, 'message')"> - {{ eventText(row.event, 'message') }}</template>
        </span>
        <span class="h-px flex-1 bg-[#E5E7EB]" />
      </div>

      <!-- Unpaired stream line -->
      <div
        v-else-if="row.event.type === 'tool_stream'"
        class="px-3 font-mono text-[11px] leading-5 text-[#9CA3AF]"
      >
        {{ eventText(row.event, 'line') }}
      </div>

      <!-- Debug (only present when the Debug toggle is on) -->
      <div
        v-else-if="row.event.type === 'debug'"
        class="rounded border border-dashed border-[#E5E7EB] px-3 py-1.5 font-mono text-[11px] leading-5 text-[#9CA3AF]"
      >
        <span class="text-[#6B7280]">{{ eventText(row.event, 'label') }}</span>
        <span class="whitespace-pre-wrap break-words"> {{ eventText(row.event, 'message') }}</span>
      </div>
    </template>
  </div>
</template>
