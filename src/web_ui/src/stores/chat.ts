import { ref } from 'vue'
import { defineStore } from 'pinia'
import {
  fetchChatChannels,
  postAgentAction,
  postAgentReload,
  sendChatMessage,
  type AgentActionPath,
} from '@/api/client'

const CHANNEL_STORAGE_KEY = 'lincy.agent.send-channel'
const DEFAULT_CHANNEL = 'cli'

/** Header actions on the Agent page; 'reload' always reloads everything. */
export type AgentAction = AgentActionPath | 'reload'

function readStoredChannel(): string {
  try {
    return localStorage.getItem(CHANNEL_STORAGE_KEY) || DEFAULT_CHANNEL
  } catch {
    return DEFAULT_CHANNEL
  }
}

function storeChannel(channel: string) {
  try {
    localStorage.setItem(CHANNEL_STORAGE_KEY, channel)
  } catch { /* private mode: selection just does not persist */ }
}

/** Composer state: which channel an outgoing message is attributed to, sending, and header actions. */
export const useChatStore = defineStore('chat', () => {
  const channels = ref<string[]>([DEFAULT_CHANNEL])
  const channel = ref(readStoredChannel())
  const sending = ref(false)
  const acting = ref(false)
  const error = ref('')

  function selectChannel(next: string) {
    if (!channels.value.includes(next)) return
    channel.value = next
    storeChannel(next)
  }

  async function loadChannels() {
    let available: string[] = []
    try {
      const data = await fetchChatChannels()
      available = Array.isArray(data.channels) ? data.channels : []
    } catch { /* lincy not reachable: fall back to cli only */ }
    if (available.length === 0) available = [DEFAULT_CHANNEL]
    channels.value = available
    if (!available.includes(channel.value)) {
      channel.value = available.includes(DEFAULT_CHANNEL) ? DEFAULT_CHANNEL : available[0]
    }
  }

  async function send(content: string): Promise<boolean> {
    const text = content.trim()
    if (!text) return false
    sending.value = true
    error.value = ''
    try {
      await sendChatMessage(text, channel.value)
      return true
    } catch (err) {
      error.value = err instanceof Error ? err.message : 'failed to send message'
      return false
    } finally {
      sending.value = false
    }
  }

  /** No optimistic update: the effect shows up through the agent event stream. */
  async function runAction(action: AgentAction): Promise<void> {
    acting.value = true
    error.value = ''
    try {
      if (action === 'reload') await postAgentReload('all')
      else await postAgentAction(action)
    } catch (err) {
      error.value = err instanceof Error ? err.message : 'request failed'
    } finally {
      acting.value = false
    }
  }

  return {
    channels,
    channel,
    sending,
    acting,
    error,
    selectChannel,
    loadChannels,
    send,
    runAction,
  }
})
