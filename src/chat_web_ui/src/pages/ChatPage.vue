<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { ArrowDown, Send } from 'lucide-vue-next'
import AgentTimeline from '@/components/agent/AgentTimeline.vue'
import { useChatStore } from '@/stores/chat'
import { useWebSocketStore } from '@/stores/websocket'
import type { AgentUiEvent } from '@/api/client'
import {
  buildAgentRows,
  eventText,
  pairToolResults,
  useAgentEventsStore,
  type TimelineRow,
} from '@/stores/agentEvents'

const BRAIN_TAB = 'brain'
/** Distance from the bottom that still counts as "following the tail". */
const STICK_THRESHOLD_PX = 80

const chat = useChatStore()
const agentStore = useAgentEventsStore()
const ws = useWebSocketStore()

const activeTab = ref(BRAIN_TAB)
const showDebug = ref(false)
const draft = ref('')
const scrollEl = ref<HTMLElement | null>(null)
const stick = ref(true)
const hasNew = ref(false)

function visible(events: AgentUiEvent[]): AgentUiEvent[] {
  return events.filter((event) => {
    if (event.type === 'ctx_status') return false
    // Turn chrome and debug-only rows: only shown when Debug is on.
    if (event.type === 'debug') return showDebug.value
    if (event.type === 'processing_started') return showDebug.value
    if (event.type === 'processing_finished') return showDebug.value
    if (event.type === 'resume_history') return showDebug.value
    return true
  })
}

const rows = computed<TimelineRow[]>(() => {
  if (activeTab.value === BRAIN_TAB) {
    return buildAgentRows(visible(agentStore.brainEvents))
  }
  return buildAgentRows(visible(agentStore.eventsFor(activeTab.value)))
})

const activeLane = computed(() =>
  agentStore.agents.find((lane) => lane.label === activeTab.value) ?? null
)

const liveLanes = computed(() =>
  agentStore.agents
    .filter((lane) => lane.live)
    .map((lane) => ({ label: lane.label, tool: runningTool(lane.label) }))
)

function runningTool(label: string): string {
  const events = agentStore.eventsFor(label)
  const paired = pairToolResults(events)
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const event = events[i]
    if (event.type === 'tool_call' && !paired.has(event.seq)) return eventText(event, 'name')
  }
  return ''
}

const statusLabel = computed(() => {
  if (chat.error) return 'Error'
  return agentStore.busy ? 'Processing' : 'Ready'
})

const statusClass = computed(() => {
  if (chat.error) return 'bg-[#EF4444]'
  return agentStore.busy ? 'animate-pulse bg-[#111827]' : 'bg-[#22C55E]'
})

function atBottom(): boolean {
  const el = scrollEl.value
  if (!el) return true
  return el.scrollHeight - el.scrollTop - el.clientHeight <= STICK_THRESHOLD_PX
}

function scrollToBottom() {
  const el = scrollEl.value
  if (el) el.scrollTop = el.scrollHeight
}

function onScroll() {
  stick.value = atBottom()
  if (stick.value) hasNew.value = false
}

async function jumpToLatest() {
  stick.value = true
  hasNew.value = false
  await nextTick()
  scrollToBottom()
}

async function selectTab(tab: string) {
  activeTab.value = tab
  await jumpToLatest()
}

async function submit() {
  const ok = await chat.send(draft.value)
  if (!ok) return
  draft.value = ''
  await jumpToLatest()
}

onMounted(async () => {
  await Promise.all([chat.loadChannels(), agentStore.load()])
  await nextTick()
  scrollToBottom()
})

// Row count alone misses updates that only fill in an existing row (a tool result
// folding into its call), so track the newest event instead.
const timelineSignal = computed(() => {
  const events = agentStore.events
  return events.length > 0 ? events[events.length - 1].seq : 0
})

watch(timelineSignal, async () => {
  await nextTick()
  if (stick.value) scrollToBottom()
  else hasNew.value = true
})
</script>

<template>
  <div class="mx-auto flex h-[calc(100vh-132px)] w-full max-w-4xl flex-col md:h-[calc(100vh-98px)]">
    <div class="flex flex-wrap items-center justify-between gap-2 border-b border-[#E5E7EB] pb-3">
      <h1 class="text-base font-semibold text-[#111827]">Agent</h1>
      <div class="flex items-center gap-3 text-xs text-[#6B7280]">
        <span
          v-if="agentStore.latestCtxStatus"
          class="rounded border border-[#E5E7EB] bg-[#F9FAFB] px-2 py-0.5 font-mono text-[11px] tabular-nums text-[#6B7280]"
        >
          {{ agentStore.latestCtxStatus }}
        </span>
        <button
          type="button"
          class="rounded border px-2 py-0.5 text-[11px] transition-colors"
          :class="showDebug
            ? 'border-[#111827] text-[#111827]'
            : 'border-[#E5E7EB] text-[#9CA3AF] hover:text-[#6B7280]'"
          @click="showDebug = !showDebug"
        >
          Debug
        </button>
        <span class="flex items-center gap-2">
          <span class="h-2 w-2 rounded-full" :class="statusClass" />
          <span>{{ statusLabel }}</span>
        </span>
      </div>
    </div>

    <div class="flex gap-4 overflow-x-auto border-b border-[#E5E7EB]">
      <button
        type="button"
        class="-mb-px shrink-0 border-b-2 pb-2 pt-2 text-sm transition-colors"
        :class="activeTab === BRAIN_TAB
          ? 'border-[#111827] font-medium text-[#111827]'
          : 'border-transparent text-[#6B7280] hover:text-[#111827]'"
        @click="selectTab(BRAIN_TAB)"
      >
        Brain
      </button>
      <button
        v-for="lane in agentStore.agents"
        :key="lane.label"
        type="button"
        class="-mb-px flex shrink-0 items-center gap-1.5 border-b-2 pb-2 pt-2 text-sm transition-colors"
        :class="activeTab === lane.label
          ? 'border-[#111827] font-medium text-[#111827]'
          : 'border-transparent text-[#6B7280] hover:text-[#111827]'"
        @click="selectTab(lane.label)"
      >
        <span
          class="h-1.5 w-1.5 rounded-full"
          :class="lane.live ? 'animate-pulse bg-[#22C55E]' : 'bg-[#D1D5DB]'"
        />
        <span class="font-mono text-xs">{{ lane.label }}</span>
      </button>
    </div>

    <div v-if="activeTab !== BRAIN_TAB" class="flex items-center gap-2 pt-3 text-xs text-[#6B7280]">
      <span class="font-mono text-[#111827]">{{ activeTab }}</span>
      <span>{{ activeLane?.live ? 'running' : 'idle' }}</span>
    </div>

    <div class="relative min-h-0 flex-1">
      <div ref="scrollEl" class="h-full overflow-y-auto py-4 pr-1" @scroll="onScroll">
        <div v-if="agentStore.loading" class="py-10 text-center text-sm text-[#9CA3AF]">
          Loading
        </div>
        <div
          v-else-if="rows.length === 0"
          class="flex h-full items-center justify-center px-6 text-center text-sm text-[#D1D5DB]"
        >
          <span v-if="!ws.connected">
            No activity. The chat-cli process does not look like it is running.
          </span>
          <span v-else>No activity yet</span>
        </div>
        <AgentTimeline v-else :rows="rows" />
      </div>

      <button
        v-if="hasNew"
        type="button"
        class="absolute bottom-3 left-1/2 flex -translate-x-1/2 items-center gap-1.5 rounded-full border border-[#E5E7EB] bg-white px-3 py-1.5 text-xs text-[#6B7280] shadow-[0_1px_2px_rgba(0,0,0,0.04)] transition-colors hover:text-[#111827]"
        @click="jumpToLatest"
      >
        <ArrowDown class="h-3.5 w-3.5" />
        Jump to latest
      </button>
    </div>

    <div v-if="liveLanes.length > 0" class="flex flex-wrap gap-2 pb-2">
      <button
        v-for="lane in liveLanes"
        :key="`live-${lane.label}`"
        type="button"
        class="flex items-center gap-1.5 rounded-full border border-[#E5E7EB] bg-[#F9FAFB] px-2.5 py-1 text-[11px] text-[#6B7280] transition-colors hover:text-[#111827]"
        @click="selectTab(lane.label)"
      >
        <span class="h-1.5 w-1.5 animate-pulse rounded-full bg-[#22C55E]" />
        <span class="font-mono">{{ lane.label }}</span>
        <span>running<template v-if="lane.tool"> - {{ lane.tool }}</template></span>
      </button>
    </div>

    <div class="shrink-0 border-t border-[#E5E7EB] pt-3">
      <p v-if="chat.error" class="mb-2 text-xs text-[#EF4444]">{{ chat.error }}</p>
      <form class="flex items-end gap-2" @submit.prevent="submit">
        <select
          :value="chat.channel"
          title="Send as channel"
          class="h-11 shrink-0 rounded-md border border-[#D1D5DB] bg-white px-2 font-mono text-xs text-[#111827] outline-none transition-colors focus:border-[#111827] disabled:cursor-not-allowed disabled:text-[#9CA3AF]"
          :disabled="chat.sending"
          @change="chat.selectChannel(($event.target as HTMLSelectElement).value)"
        >
          <option v-for="name in chat.channels" :key="name" :value="name">{{ name }}</option>
        </select>
        <textarea
          v-model="draft"
          rows="1"
          class="max-h-32 min-h-11 flex-1 resize-none rounded-md border border-[#D1D5DB] px-3 py-2 text-sm leading-6 text-[#111827] outline-none transition-colors placeholder:text-[#9CA3AF] focus:border-[#111827]"
          placeholder="Message Lincy"
          :disabled="chat.sending"
          @keydown.enter.exact.prevent="submit"
        />
        <button
          type="submit"
          title="Send"
          class="flex h-11 w-11 shrink-0 items-center justify-center rounded-md bg-[#111827] text-white transition-colors hover:bg-black disabled:cursor-not-allowed disabled:bg-[#D1D5DB]"
          :disabled="chat.sending || !draft.trim()"
        >
          <Send class="h-4 w-4" />
        </button>
      </form>
    </div>
  </div>
</template>
