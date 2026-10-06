import { ref } from 'vue'
import { defineStore } from 'pinia'
import { fetchLiveStatus } from '@/api/client'

export const useLiveStore = defineStore('live', () => {
  const active = ref(false)
  const sessionId = ref('')
  const promptTokens = ref(0)
  const softLimit = ref(128_000)
  const hardLimit = ref(200_000)

  async function refresh() {
    const data = await fetchLiveStatus()
    active.value = data.active ?? false
    sessionId.value = data.session_id ?? ''
    promptTokens.value = data.prompt_tokens ?? 0
    softLimit.value = data.soft_limit ?? 128_000
    hardLimit.value = data.hard_limit ?? 200_000
  }

  function update(msg: Record<string, unknown>) {
    if (msg.type === 'live_token_update') {
      active.value = true
      sessionId.value = (msg.session_id as string) ?? ''
      promptTokens.value = (msg.prompt_tokens as number) ?? 0
      softLimit.value = (msg.soft_limit as number) ?? 128_000
      hardLimit.value = (msg.hard_limit as number) ?? 200_000
    }
  }

  return { active, sessionId, promptTokens, softLimit, hardLimit, refresh, update }
})
