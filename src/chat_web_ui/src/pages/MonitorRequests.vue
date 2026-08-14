<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { fetchAllRequests } from '@/api/client'
import { useDashboardStore } from '@/stores/dashboard'
import { useWebSocketStore } from '@/stores/websocket'
import { formatCacheRate, formatCacheWriteTokens, formatCost, formatCostShort, formatTokens, formatLatency, formatPricingSource, formatServedByFallback, pricingSourceClass } from '@/lib/format'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import MonitorTabs from '@/components/dashboard/MonitorTabs.vue'
import TimeRangeSelector from '@/components/dashboard/TimeRangeSelector.vue'

const dashStore = useDashboardStore()
const wsStore = useWebSocketStore()

const requests = ref<Record<string, unknown>[]>([])
const total = ref(0)
const loading = ref(false)

function formatTime(iso: string): string {
  const d = new Date(iso)
  return d.toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
}

interface SessionGroup {
  session_id: string
  session_label: string
  requests: Record<string, unknown>[]
  total_cost: number
}

const grouped = computed(() => {
  const groups: SessionGroup[] = []
  let current: SessionGroup | null = null
  for (const r of requests.value) {
    const sid = r.session_id as string
    if (!current || current.session_id !== sid) {
      current = { session_id: sid, session_label: r.session_label as string, requests: [], total_cost: 0 }
      groups.push(current)
    }
    current.requests.push(r)
    if (typeof r.cost === 'number') current.total_cost += r.cost
  }
  return groups
})

function _localDate(d: Date): string {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

const dateRange = computed(() => {
  const today = new Date()
  const to = _localDate(today)
  if (dashStore.range === 'today') return { from: to, to }
  if (dashStore.range === '7d') {
    const d = new Date(today)
    d.setDate(d.getDate() - 6)
    return { from: _localDate(d), to }
  }
  if (dashStore.range === '30d') {
    const d = new Date(today)
    d.setDate(d.getDate() - 29)
    return { from: _localDate(d), to }
  }
  return { from: dashStore.customFrom || to, to: dashStore.customTo || to }
})

async function load() {
  loading.value = true
  try {
    const { from, to } = dateRange.value
    const data = await fetchAllRequests(from, to, 500)
    requests.value = data.requests || []
    total.value = data.total || 0
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  load()
  wsStore.onMessage((msg) => {
    if (msg.type === 'session_updated' || msg.type === 'session_created') {
      load()
    }
  })
})

watch(() => dashStore.range, load)
watch(() => dashStore.customFrom, load)
watch(() => dashStore.customTo, load)
</script>

<template>
  <div>
    <MonitorTabs />
    <div class="space-y-6">
    <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <TimeRangeSelector />
      <span class="text-xs text-[#6B7280] tabular-nums sm:text-right">{{ total }} requests</span>
    </div>

    <div
      v-for="group in grouped"
      :key="group.session_id"
      class="border border-[#E5E7EB] rounded-lg shadow-[0_1px_2px_rgba(0,0,0,0.04)]"
    >
      <!-- Session header -->
      <div class="flex items-center justify-between px-4 py-2.5 border-b border-[#F3F4F6]">
        <router-link
          :to="`/monitor/${group.session_id}`"
          class="text-sm font-medium text-[#111827] hover:underline"
        >
          Session {{ group.session_label }}
        </router-link>
        <div class="flex items-center gap-4 text-xs text-[#6B7280] tabular-nums">
          <span>{{ group.requests.length }} requests</span>
          <span>{{ formatCostShort(group.total_cost) }}</span>
        </div>
      </div>

      <Table>
        <TableHeader>
          <TableRow class="text-xs text-[#6B7280]">
            <TableHead class="w-20">Time</TableHead>
            <TableHead class="w-12">Round</TableHead>
            <TableHead class="w-16">Client</TableHead>
            <TableHead class="w-36">Model</TableHead>
            <TableHead class="w-16 text-right">Prompt</TableHead>
            <TableHead class="w-16 text-right">Output</TableHead>
            <TableHead class="w-20 text-right">Read Cache</TableHead>
            <TableHead class="w-24 text-right">Write Cache</TableHead>
            <TableHead class="w-16 text-right">Latency</TableHead>
            <TableHead class="w-24 text-right">Pricing</TableHead>
            <TableHead class="w-20 text-right">Cost</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          <TableRow
            v-for="(r, idx) in group.requests"
            :key="idx"
            class="hover:bg-[#F9FAFB] transition-colors"
          >
            <TableCell class="text-xs text-[#6B7280] tabular-nums">
              {{ formatTime(r.ts as string) }}
            </TableCell>
            <TableCell class="text-xs text-[#6B7280] tabular-nums">
              r{{ r.round }}
            </TableCell>
            <TableCell class="text-xs text-[#6B7280] truncate max-w-[64px]">
              {{ (r.client_label as string) || 'brain' }}
            </TableCell>
            <TableCell class="text-xs text-[#111827] max-w-[144px]">
              <div class="truncate">{{ r.model }}</div>
              <div
                v-if="formatServedByFallback(r)"
                class="truncate text-[10px] text-[#B45309]"
                :title="`Served by fallback ${formatServedByFallback(r)}`"
              >
                &rarr; {{ formatServedByFallback(r) }}
              </div>
            </TableCell>
            <TableCell class="text-xs text-right tabular-nums">
              {{ formatTokens((r.prompt_tokens as number) || 0) }}
            </TableCell>
            <TableCell class="text-xs text-right tabular-nums">
              {{ formatTokens((r.completion_tokens as number) || 0) }}
            </TableCell>
            <TableCell class="text-xs text-right tabular-nums">
              {{ formatCacheRate((r.read_cache_rate as number | null) ?? null) }}
            </TableCell>
            <TableCell class="text-xs text-right tabular-nums">
              {{ formatCacheWriteTokens(
                (r.write_cache_measurable as boolean | null) ?? null,
                (r.cache_write_tokens as number | null) ?? null,
              ) }}
            </TableCell>
            <TableCell class="text-xs text-right tabular-nums">
              {{ formatLatency((r.latency_ms as number) || 0) }}
            </TableCell>
            <TableCell
              class="text-xs text-right"
              :class="pricingSourceClass(r.pricing_stale as boolean | null)"
              :title="(r.pricing_source_url as string) || undefined"
            >
              {{ formatPricingSource(
                r.pricing_source as string | null,
                r.pricing_stale as boolean | null,
              ) }}
            </TableCell>
            <TableCell class="text-xs text-right tabular-nums text-[#111827]">
              {{ formatCost(r.cost as number) }}
            </TableCell>
          </TableRow>
        </TableBody>
      </Table>
    </div>
    </div>
  </div>
</template>
