<script setup lang="ts">
import { useLiveStore } from '@/stores/live'
import { useWebSocketStore } from '@/stores/websocket'
import TokenBar from './TokenBar.vue'

const live = useLiveStore()
const ws = useWebSocketStore()
</script>

<template>
  <header class="flex items-center justify-between gap-3 px-4 py-3 border-b border-[#E5E7EB] md:px-6">
    <span class="text-sm font-semibold text-[#111827] md:hidden">Lincy</span>
    <div class="flex items-center gap-2 md:gap-4">
    <TokenBar
      v-if="live.active"
      :current="live.promptTokens"
      :soft-limit="live.softLimit"
      :hard-limit="live.hardLimit"
    />
    <div class="flex items-center gap-1.5">
      <span
        class="w-2 h-2 rounded-full"
        :class="ws.connected ? 'bg-[#22C55E]' : 'bg-[#D1D5DB]'"
      />
      <span class="text-xs text-[#6B7280]">{{ ws.connected ? 'Live' : 'Offline' }}</span>
    </div>
    </div>
  </header>
</template>
