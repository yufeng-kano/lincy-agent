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

export interface ClaudeUsageWindow {
  utilization: number | null
  resets_at: string | null
}

export interface ClaudeScopedUsageWindow {
  label: string
  utilization: number | null
  resets_at: string | null
}

export interface ClaudeAccountInfo {
  email: string | null
  display_name: string | null
  plan_type: string | null
  rate_limit_tier: string | null
}

export interface ClaudeAccount {
  id: string
  source: string
  priority: number
  status: 'active' | 'standby' | 'benched' | 'unusable'
  error: string | null
  account: ClaudeAccountInfo | null
  usage: {
    five_hour: ClaudeUsageWindow | null
    seven_day: ClaudeUsageWindow | null
    seven_day_scoped?: ClaudeScopedUsageWindow[] | null
  } | null
  stale?: boolean
}

export interface ClaudeModel {
  id: string
  display_name: string | null
}

export interface ClaudeAccountsResponse {
  available: boolean
  accounts: ClaudeAccount[]
  models: ClaudeModel[]
  error: string | null
}

export async function fetchClaudeAccounts(refresh = false): Promise<ClaudeAccountsResponse> {
  const query = refresh ? '?refresh=true' : ''
  const res = await fetch(`${BASE}/api/claude-accounts${query}`)
  return res.json()
}

export async function promoteClaudeAccount(tokenId: string): Promise<{ ok: boolean }> {
  const res = await fetch(`${BASE}/api/claude-accounts/${tokenId}/promote`, { method: 'POST' })
  return responseJsonOrError(res)
}

export async function removeClaudeAccount(tokenId: string): Promise<{ ok: boolean }> {
  const res = await fetch(`${BASE}/api/claude-accounts/${tokenId}`, { method: 'DELETE' })
  return responseJsonOrError(res)
}

export interface ClaudeLoginBegin {
  login_id: string
  authorization_url: string
}

export async function beginClaudeLogin(): Promise<ClaudeLoginBegin> {
  const res = await fetch(`${BASE}/api/claude-accounts/login`, { method: 'POST' })
  return responseJsonOrError(res)
}

export async function completeClaudeLogin(
  loginId: string,
  code: string,
): Promise<{ ok: boolean; token_id: string }> {
  const res = await fetch(`${BASE}/api/claude-accounts/login/${loginId}/complete`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code }),
  })
  return responseJsonOrError(res)
}

export interface CodexUsageWindow {
  label: string
  utilization: number | null
  resets_at: string | null
}

export interface CodexAccountInfo {
  email: string | null
  plan_type: string | null
}

export interface CodexAccount {
  id: string
  source: string
  priority: number
  status: 'active' | 'standby' | 'benched' | 'unusable'
  error: string | null
  stale: boolean
  account: CodexAccountInfo | null
  usage: {
    windows: CodexUsageWindow[]
  } | null
}

export interface CodexModel {
  id: string
  display_name: string | null
}

export interface CodexAccountsResponse {
  available: boolean
  accounts: CodexAccount[]
  models: CodexModel[]
  error: string | null
}

export async function fetchCodexAccounts(refresh = false): Promise<CodexAccountsResponse> {
  const query = refresh ? '?refresh=true' : ''
  const res = await fetch(`${BASE}/api/codex-accounts${query}`)
  return res.json()
}

export async function promoteCodexAccount(tokenId: string): Promise<{ ok: boolean }> {
  const res = await fetch(`${BASE}/api/codex-accounts/${tokenId}/promote`, { method: 'POST' })
  return responseJsonOrError(res)
}

export async function removeCodexAccount(tokenId: string): Promise<{ ok: boolean }> {
  const res = await fetch(`${BASE}/api/codex-accounts/${tokenId}`, { method: 'DELETE' })
  return responseJsonOrError(res)
}

export interface CodexLoginBegin {
  login_id: string
  authorization_url: string
  listener_error?: string | null
}

export async function beginCodexLogin(): Promise<CodexLoginBegin> {
  const res = await fetch(`${BASE}/api/codex-accounts/login`, { method: 'POST' })
  return responseJsonOrError(res)
}

export interface CodexLoginStatus {
  status: 'pending' | 'completed' | 'expired'
  token_id: string | null
}

export async function fetchCodexLoginStatus(loginId: string): Promise<CodexLoginStatus> {
  const res = await fetch(`${BASE}/api/codex-accounts/login/${loginId}`)
  return responseJsonOrError(res)
}

export async function completeCodexLogin(
  loginId: string,
  value: string,
): Promise<{ ok: boolean; token_id: string }> {
  const res = await fetch(`${BASE}/api/codex-accounts/login/${loginId}/complete`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ value }),
  })
  return responseJsonOrError(res)
}
