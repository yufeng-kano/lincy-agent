import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { fetchAgentEvents, type AgentUiEvent } from '@/api/client'

/** Read one payload field as text; payload shapes are typed per event on the backend. */
export function eventText(event: AgentUiEvent, field: string): string {
  const value = event.data[field]
  if (typeof value === 'string') return value
  if (value === null || value === undefined) return ''
  return String(value)
}

export function eventFlag(event: AgentUiEvent, field: string): boolean {
  return event.data[field] === true
}

function pairKey(event: AgentUiEvent): string {
  return `${event.agent ?? ''}\u0000${eventText(event, 'name')}`
}

/**
 * Match tool_result events back to their tool_call, FIFO per (agent, tool name).
 * Returns call seq -> result event; results without a pending call are left out.
 */
export function pairToolResults(events: AgentUiEvent[]): Map<number, AgentUiEvent> {
  const pending = new Map<string, number[]>()
  const paired = new Map<number, AgentUiEvent>()
  for (const event of events) {
    const key = pairKey(event)
    if (event.type === 'tool_call') {
      const queue = pending.get(key)
      if (queue) queue.push(event.seq)
      else pending.set(key, [event.seq])
    } else if (event.type === 'tool_result') {
      const callSeq = pending.get(key)?.shift()
      if (callSeq !== undefined) paired.set(callSeq, event)
    }
  }
  return paired
}

/** Subagent labels with at least one tool_call still waiting for its result. */
export function liveAgents(events: AgentUiEvent[]): Set<string> {
  const paired = pairToolResults(events)
  const live = new Set<string>()
  for (const event of events) {
    if (event.type !== 'tool_call' || !event.agent) continue
    if (!paired.has(event.seq)) live.add(event.agent)
  }
  return live
}

export interface TimelineRow {
  key: string
  time: number
  event: AgentUiEvent
  /** Result of a tool_call row, once it arrives. */
  result: AgentUiEvent | null
  /** tool_stream lines collected while this call was still open. */
  streams: string[]
}

function newRow(event: AgentUiEvent): TimelineRow {
  return {
    key: event.id,
    time: Date.parse(event.ts),
    event,
    result: null,
    streams: [],
  }
}

/**
 * Fold a lane's events into renderable rows: tool_result and tool_stream events
 * are attached to their open tool_call instead of taking a row of their own.
 */
export function buildAgentRows(events: AgentUiEvent[]): TimelineRow[] {
  const rows: TimelineRow[] = []
  const openByPair = new Map<string, TimelineRow[]>()
  const openByAgent = new Map<string, TimelineRow[]>()

  for (const event of events) {
    const agentKey = event.agent ?? ''
    if (event.type === 'tool_result') {
      const row = openByPair.get(pairKey(event))?.shift()
      if (row) {
        row.result = event
        const openRows = openByAgent.get(agentKey)
        const index = openRows ? openRows.indexOf(row) : -1
        if (openRows && index >= 0) openRows.splice(index, 1)
        continue
      }
      // Orphan result: tool calls stay hidden when ui.show_tool_use is off.
      rows.push(newRow(event))
      continue
    }

    if (event.type === 'tool_stream') {
      const openRows = openByAgent.get(agentKey)
      const target = openRows && openRows.length > 0 ? openRows[openRows.length - 1] : null
      if (target) {
        target.streams.push(eventText(event, 'line'))
        continue
      }
      rows.push(newRow(event))
      continue
    }

    const row = newRow(event)
    rows.push(row)
    if (event.type === 'tool_call') {
      const key = pairKey(event)
      const pairQueue = openByPair.get(key)
      if (pairQueue) pairQueue.push(row)
      else openByPair.set(key, [row])
      const agentQueue = openByAgent.get(agentKey)
      if (agentQueue) agentQueue.push(row)
      else openByAgent.set(agentKey, [row])
    }
  }
  return rows
}

/** Keep the browser side bounded; the backend log is per lincy run anyway. */
const MAX_EVENTS = 3000

export interface AgentLane {
  label: string
  firstSeq: number
  lastSeq: number
  live: boolean
}

export const useAgentEventsStore = defineStore('agentEvents', () => {
  const events = ref<AgentUiEvent[]>([])
  const loading = ref(false)
  const error = ref('')

  const brainEvents = computed(() => events.value.filter((event) => event.agent === null))

  // Same rule as the TUI: the newest processing_* event decides whether a turn is open.
  const busy = computed(() => {
    for (let i = events.value.length - 1; i >= 0; i -= 1) {
      const type = events.value[i].type
      if (type === 'processing_started') return true
      if (type === 'processing_finished') return false
    }
    return false
  })

  const agents = computed<AgentLane[]>(() => {
    const live = liveAgents(events.value)
    const lanes = new Map<string, AgentLane>()
    for (const event of events.value) {
      const label = event.agent
      if (!label) continue
      const lane = lanes.get(label)
      if (!lane) {
        lanes.set(label, { label, firstSeq: event.seq, lastSeq: event.seq, live: live.has(label) })
      } else if (event.seq > lane.lastSeq) {
        lane.lastSeq = event.seq
      }
    }
    return [...lanes.values()]
  })

  const latestCtxStatus = computed(() => {
    for (let i = events.value.length - 1; i >= 0; i -= 1) {
      const event = events.value[i]
      if (event.type === 'ctx_status') return String(event.data.text ?? '')
    }
    return ''
  })

  function eventsFor(label: string): AgentUiEvent[] {
    return events.value.filter((event) => event.agent === label)
  }

  function addEvent(event: AgentUiEvent) {
    if (events.value.some((existing) => existing.id === event.id)) return
    // Events almost always arrive in order, so insert from the tail.
    const next = [...events.value]
    let index = next.length
    while (index > 0 && next[index - 1].seq > event.seq) index -= 1
    next.splice(index, 0, event)
    events.value = next.length > MAX_EVENTS ? next.slice(next.length - MAX_EVENTS) : next
  }

  function update(msg: Record<string, unknown>) {
    if (msg.type !== 'agent_event') return
    const event = msg.event as AgentUiEvent | undefined
    if (!event?.id) return
    addEvent(event)
  }

  async function load(limit = 500) {
    loading.value = true
    error.value = ''
    try {
      const data = await fetchAgentEvents(limit)
      events.value = (data.events || []).slice(-MAX_EVENTS)
    } catch (err) {
      error.value = err instanceof Error ? err.message : 'failed to load agent events'
    } finally {
      loading.value = false
    }
  }

  return {
    events,
    loading,
    error,
    agents,
    brainEvents,
    busy,
    latestCtxStatus,
    eventsFor,
    addEvent,
    update,
    load,
  }
})
