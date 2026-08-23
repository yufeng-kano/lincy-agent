const BASE = ''

export async function fetchDashboard(from: string, to: string) {
  const res = await fetch(`${BASE}/api/dashboard?from=${from}&to=${to}`)
  return res.json()
}

export async function fetchSessions(from: string, to: string, limit = 20, offset = 0) {
  const res = await fetch(`${BASE}/api/sessions?from=${from}&to=${to}&limit=${limit}&offset=${offset}`)
  return res.json()
}

export async function fetchSessionDetail(id: string) {
  const res = await fetch(`${BASE}/api/sessions/${id}`)
  return res.json()
}

export async function fetchAllRequests(from: string, to: string, limit = 200, offset = 0) {
  const res = await fetch(`${BASE}/api/requests?from=${from}&to=${to}&limit=${limit}&offset=${offset}`)
  return res.json()
}

export async function fetchLiveStatus() {
  const res = await fetch(`${BASE}/api/live`)
  return res.json()
}

async function responseJsonOrError(res: Response) {
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    const message = typeof data.error === 'string' ? data.error : 'request failed'
    throw new Error(message)
  }
  return data
}

export interface ContextItem {
  key: string
  label: string
  tokens: number
}

export interface ContextSegment {
  key: string
  label: string
  tokens: number
  items: ContextItem[]
}

export interface ContextCompositionAvailable {
  available: true
  session_id: string
  turn_id: string | null
  request_id: string
  request_ts: string
  round: number | null
  reported_prompt_tokens: number | null
  calibrated: boolean
  total_tokens: number
  soft_max_prompt_tokens: number
  message_count: number
  tool_count: number
  segments: ContextSegment[]
}

export interface ContextCompositionUnavailable {
  available: false
  reason: string
}

export type ContextComposition = ContextCompositionAvailable | ContextCompositionUnavailable

/** Live-computed prompt breakdown of the latest brain request; never cached server-side. */
export async function fetchContextComposition(): Promise<ContextComposition> {
  const res = await fetch(`${BASE}/api/context/composition`)
  return responseJsonOrError(res)
}

export type AgentUiEventType =
  | 'inbound_message'
  | 'processing_started'
  | 'processing_finished'
  | 'assistant_text'
  | 'tool_call'
  | 'tool_result'
  | 'tool_stream'
  | 'warning'
  | 'error'
  | 'debug'
  | 'ctx_status'
  | 'resume_history'
  | 'outbound_message'
  | 'interrupt_state'

export interface AgentUiEvent {
  id: string
  seq: number
  ts: string
  type: AgentUiEventType
  /** Subagent label (worker-N / gui_task); null means the main brain lane. */
  agent: string | null
  data: Record<string, unknown>
}

export async function fetchAgentEvents(limit = 500): Promise<{ events: AgentUiEvent[] }> {
  const res = await fetch(`${BASE}/api/agent/events?limit=${limit}`)
  return responseJsonOrError(res)
}

/** Send channels the composer may attribute a message to; never includes web/system. */
export async function fetchChatChannels(): Promise<{ channels: string[] }> {
  const res = await fetch(`${BASE}/api/chat/channels`)
  return responseJsonOrError(res)
}

/** The inbound message comes back through the agent event stream, not this response. */
export async function sendChatMessage(
  content: string,
  channel: string,
): Promise<{ status: string; channel: string }> {
  const res = await fetch(`${BASE}/api/chat/messages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content, channel }),
  })
  return responseJsonOrError(res)
}

