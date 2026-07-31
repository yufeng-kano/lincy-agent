import { ref } from 'vue'
import { defineStore } from 'pinia'
import { fetchChatChannels, sendChatMessage } from '@/api/client'

const CHANNEL_STORAGE_KEY = 'lincy.agent.send-channel'
const DEFAULT_CHANNEL = 'cli'

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

/** Composer state: which channel an outgoing message is attributed to, and sending. */
export const useChatStore = defineStore('chat', () => {
  const channels = ref<string[]>([DEFAULT_CHANNEL])
  const channel = ref(readStoredChannel())
  const sending = ref(false)
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
    } catch { /* control API down: fall back to cli only */ }
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

  return {
    channels,
    channel,
    sending,
    error,
    selectChannel,
    loadChannels,
    send,
  }
})
