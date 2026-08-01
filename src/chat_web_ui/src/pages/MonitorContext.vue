<script setup lang="ts">
import { computed, onMounted, onUnmounted } from 'vue'
import { RefreshCw } from 'lucide-vue-next'
import MonitorTabs from '@/components/dashboard/MonitorTabs.vue'
import ContextDonut from '@/components/dashboard/ContextDonut.vue'
import { Card, CardContent } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { formatTokens } from '@/lib/format'
import { useWebSocketStore } from '@/stores/websocket'
import {
  useContextCompositionStore,
  segmentColor,
  staticPrefixTokens,
  largestItem,
  cacheBreakpoints,
} from '@/stores/contextComposition'
import type { ContextCompositionAvailable, ContextSegment } from '@/api/client'

// requests.jsonl can be 10MB+ and is re-parsed on every call (never cached
// server-side); debounce rapid session_updated bursts from a fast tool loop
// instead of re-fetching on each one.
const REFRESH_DEBOUNCE_MS = 2000

const store = useContextCompositionStore()
const wsStore = useWebSocketStore()

const payload = computed<ContextCompositionAvailable | null>(() =>
  store.data && store.data.available ? store.data : null
)

const softLimitPct = computed(() => {
  if (!payload.value || !payload.value.soft_max_prompt_tokens) return 0
  return Math.round((payload.value.total_tokens / payload.value.soft_max_prompt_tokens) * 100)
})

const staticPrefixPct = computed(() => {
  if (!payload.value || !payload.value.total_tokens) return 0
  return Math.round((staticPrefixTokens(payload.value.segments) / payload.value.total_tokens) * 100)
})

const largest = computed(() => (payload.value ? largestItem(payload.value.segments) : null))

const largestPct = computed(() => {
  if (!payload.value || !largest.value || !payload.value.total_tokens) return 0
  return Math.round((largest.value.tokens / payload.value.total_tokens) * 100)
})

const turnNumber = computed(() => {
  const turnId = payload.value?.turn_id
  if (!turnId) return '-'
  const n = Number(turnId.replace('turn_', ''))
  return Number.isFinite(n) ? String(n) : turnId
})

function sharePct(tokens: number): string {
  const total = payload.value?.total_tokens || 1
  return ((tokens / total) * 100).toFixed(1)
}

interface BreakpointTick {
  key: string
  label: string
  pct: number
  tooltip: string
}

const breakpointTicks = computed<BreakpointTick[]>(() => {
  if (!payload.value) return []
  const total = payload.value.total_tokens || 1
  const bp = cacheBreakpoints(payload.value.segments)
  return [
    { key: 'bp1', label: 'BP1', pct: (bp.bp1 / total) * 100, tooltip: 'BP1: end of the system prompt. Set in ContextBuilder.build().' },
    { key: 'bp2', label: 'BP2', pct: (bp.bp2 / total) * 100, tooltip: 'BP2: end of the core-rules boot files. Set in ContextBuilder.build().' },
    { key: 'bp3', label: 'BP3', pct: (bp.bp3 / total) * 100, tooltip: 'BP3: end of conversation history, on the previous user turn.' },
    { key: 'bp4', label: 'BP4', pct: (bp.bp4 / total) * 100, tooltip: 'BP4: end of the current turn, before its tool loop. Re-applied by the responder before every LLM call.' },
  ]
})

interface SequenceSegment {
  key: string
  label: string
  tokens: number
  pct: number
  color: string
}

const sequenceSegments = computed<SequenceSegment[]>(() => {
  if (!payload.value) return []
  const total = payload.value.total_tokens || 1
  return payload.value.segments
    .filter((seg) => seg.tokens > 0)
    .map((seg: ContextSegment) => ({
      key: seg.key,
      label: seg.label,
      tokens: seg.tokens,
      pct: (seg.tokens / total) * 100,
      color: segmentColor(seg.key),
    }))
})

interface FileRow {
  key: string
  dir: string
  base: string
  tokens: number
  widthPct: number
  color: string
  segmentLabel: string
}

const fileRows = computed<FileRow[]>(() => {
  if (!payload.value) return []
  const rows: FileRow[] = []
  for (const seg of payload.value.segments) {
    for (const item of seg.items) {
      if (!item.key.includes('/')) continue
      const slash = item.key.lastIndexOf('/')
      rows.push({
        key: `${seg.key}:${item.key}`,
        dir: item.key.slice(0, slash + 1),
        base: item.key.slice(slash + 1),
        tokens: item.tokens,
        widthPct: 0,
        color: segmentColor(seg.key),
        segmentLabel: seg.label,
      })
    }
  }
  rows.sort((a, b) => b.tokens - a.tokens)
  const max = rows.length > 0 ? rows[0].tokens : 0
  for (const row of rows) row.widthPct = max > 0 ? (row.tokens / max) * 86 : 0
  return rows
})

let refreshTimer: number | undefined

function scheduleRefresh() {
  if (refreshTimer !== undefined) window.clearTimeout(refreshTimer)
  refreshTimer = window.setTimeout(() => {
    refreshTimer = undefined
    store.load()
  }, REFRESH_DEBOUNCE_MS)
}

onMounted(() => {
  store.load()
  wsStore.onMessage((msg) => {
    if (msg.type === 'session_updated') scheduleRefresh()
  })
})

onUnmounted(() => {
  if (refreshTimer !== undefined) window.clearTimeout(refreshTimer)
})
</script>

<template>
  <div>
    <MonitorTabs />

    <div v-if="store.error" class="mb-4 text-xs text-[#EF4444]">{{ store.error }}</div>

    <div v-if="store.loading && !store.data" class="text-sm text-[#6B7280]">Loading...</div>

    <div v-else-if="store.data && !store.data.available" class="rounded-lg border border-[#E5E7EB] py-16 text-center">
      <p class="text-sm text-[#6B7280]">{{ store.data.reason }}</p>
      <p class="mt-2 text-xs text-[#9CA3AF]">Start chat-cli and send a message to produce a brain request.</p>
    </div>

    <div v-else-if="payload" class="space-y-6">
      <!-- 1. Header -->
      <div class="space-y-3">
        <div class="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <Card class="border-[#E5E7EB] shadow-[0_1px_2px_rgba(0,0,0,0.04)]">
            <CardContent class="pt-4 pb-4">
              <div class="text-2xl font-semibold tabular-nums text-[#111827]">{{ formatTokens(payload.total_tokens) }}</div>
              <div class="mt-1 text-xs text-[#6B7280]">Prompt tokens</div>
              <div class="mt-1 text-[10px] tabular-nums text-[#6B7280]">{{ softLimitPct }}% of soft limit</div>
            </CardContent>
          </Card>
          <Card class="border-[#E5E7EB] shadow-[0_1px_2px_rgba(0,0,0,0.04)]">
            <CardContent class="pt-4 pb-4">
              <div class="text-2xl font-semibold tabular-nums text-[#111827]">{{ staticPrefixPct }}%</div>
              <div class="mt-1 text-xs text-[#6B7280]">Cache-safe static prefix</div>
              <div class="mt-1 text-[10px] text-[#6B7280]">unchanged across turns</div>
            </CardContent>
          </Card>
          <Card class="border-[#E5E7EB] shadow-[0_1px_2px_rgba(0,0,0,0.04)]">
            <CardContent class="pt-4 pb-4">
              <div class="truncate text-2xl font-semibold tabular-nums text-[#111827]" :title="largest?.itemLabel">
                {{ largest ? formatTokens(largest.tokens) : '-' }}
              </div>
              <div class="mt-1 text-xs text-[#6B7280]">Largest single source</div>
              <div class="mt-1 truncate text-[10px] text-[#6B7280]" :title="largest?.itemLabel">
                {{ largest ? `${largest.itemLabel} · ${largestPct}%` : '-' }}
              </div>
            </CardContent>
          </Card>
        </div>
        <div class="flex items-center justify-end gap-3">
          <span class="text-xs tabular-nums text-[#6B7280]">
            session {{ payload.session_id }} · turn {{ turnNumber }} · round {{ payload.round ?? '-' }}
          </span>
          <button
            type="button"
            class="flex items-center gap-1 rounded border border-[#E5E7EB] px-2 py-1 text-[11px] text-[#6B7280] hover:border-[#D1D5DB] hover:text-[#111827] disabled:opacity-50"
            :disabled="store.loading"
            title="Refresh now"
            @click="store.load()"
          >
            <RefreshCw class="h-3 w-3" :class="store.loading ? 'animate-spin' : ''" />
            Refresh
          </button>
        </div>
      </div>

      <!-- 2. Donut -->
      <Card class="border-[#E5E7EB] shadow-[0_1px_2px_rgba(0,0,0,0.04)]">
        <CardContent class="pt-4 pb-4">
          <ContextDonut :segments="payload.segments" :total="payload.total_tokens" />
        </CardContent>
      </Card>

      <!-- 3. Sequence -->
      <Card class="border-[#E5E7EB] shadow-[0_1px_2px_rgba(0,0,0,0.04)]">
        <CardContent class="pt-4 pb-4">
          <div class="mb-1 text-sm font-medium text-[#111827]">Sequence</div>
          <div class="relative h-11">
            <div
              v-for="bp in breakpointTicks"
              :key="`tick-${bp.key}`"
              class="absolute inset-y-0 w-px bg-[#D1D5DB]"
              :style="{ left: bp.pct + '%' }"
            />
            <span
              v-for="(bp, i) in breakpointTicks"
              :key="`label-${bp.key}`"
              class="absolute -translate-x-1/2 whitespace-nowrap bg-white px-1 text-[10px] tabular-nums text-[#6B7280]"
              :class="i % 2 === 0 ? 'top-0' : 'top-[18px]'"
              :style="{ left: bp.pct + '%' }"
              :title="bp.tooltip"
            >{{ bp.label }}</span>
          </div>
          <div class="flex h-[26px] gap-[2px] overflow-hidden rounded-md bg-[#F3F4F6]">
            <div
              v-for="seg in sequenceSegments"
              :key="seg.key"
              :style="{ width: seg.pct + '%', backgroundColor: seg.color }"
              :title="`${seg.label}: ${formatTokens(seg.tokens)} tokens`"
            />
          </div>
          <div class="mt-2 flex items-center justify-between text-[10px] text-[#6B7280]">
            <span>Static prefix (identical every turn)</span>
            <span>Conversation grows per turn</span>
          </div>
        </CardContent>
      </Card>

      <!-- 4. Files -->
      <Card v-if="fileRows.length" class="border-[#E5E7EB] shadow-[0_1px_2px_rgba(0,0,0,0.04)]">
        <CardContent class="pt-4 pb-4">
          <div class="mb-3 text-sm font-medium text-[#111827]">Files</div>
          <div class="space-y-2">
            <div
              v-for="row in fileRows"
              :key="row.key"
              class="flex items-center gap-3"
              :title="`${row.dir}${row.base} · ${row.segmentLabel}`"
            >
              <span class="w-56 shrink-0 truncate text-xs">
                <span class="text-[#6B7280]">{{ row.dir }}</span><span class="text-[#111827]">{{ row.base }}</span>
              </span>
              <div class="h-3.5 min-w-0 flex-1">
                <div class="h-3.5 rounded-r-[4px]" :style="{ width: row.widthPct + '%', backgroundColor: row.color }" />
              </div>
              <span class="w-16 shrink-0 text-right text-xs tabular-nums text-[#111827]">{{ formatTokens(row.tokens) }}</span>
            </div>
          </div>
        </CardContent>
      </Card>

      <!-- 5. Table -->
      <Card class="border-[#E5E7EB] shadow-[0_1px_2px_rgba(0,0,0,0.04)]">
        <CardContent class="pt-4 pb-4">
          <div class="mb-3 text-sm font-medium text-[#111827]">Breakdown</div>
          <div class="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow class="text-xs text-[#6B7280] hover:bg-transparent">
                  <TableHead>Content</TableHead>
                  <TableHead class="text-right">Tokens</TableHead>
                  <TableHead class="text-right">Share</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                <template v-for="seg in payload.segments" :key="seg.key">
                  <TableRow class="hover:bg-[#F9FAFB]">
                    <TableCell class="text-xs font-semibold text-[#111827]">
                      <span class="inline-flex items-center gap-2">
                        <span class="h-2 w-2 rounded-sm" :style="{ backgroundColor: segmentColor(seg.key) }" />
                        {{ seg.label }}
                      </span>
                    </TableCell>
                    <TableCell class="text-right text-xs font-semibold tabular-nums text-[#111827]">
                      {{ formatTokens(seg.tokens) }}
                    </TableCell>
                    <TableCell class="text-right text-xs font-semibold tabular-nums text-[#111827]">
                      {{ sharePct(seg.tokens) }}%
                    </TableCell>
                  </TableRow>
                  <TableRow v-for="item in seg.items" :key="`${seg.key}:${item.key}`" class="hover:bg-[#F9FAFB]">
                    <TableCell class="pl-6 text-xs text-[#6B7280]">{{ item.label }}</TableCell>
                    <TableCell class="text-right text-xs tabular-nums text-[#6B7280]">{{ formatTokens(item.tokens) }}</TableCell>
                    <TableCell class="text-right text-xs tabular-nums text-[#6B7280]">{{ sharePct(item.tokens) }}%</TableCell>
                  </TableRow>
                </template>
                <TableRow class="border-t-2 border-[#E5E7EB] hover:bg-transparent">
                  <TableCell class="text-xs font-semibold text-[#111827]">Total</TableCell>
                  <TableCell class="text-right text-xs font-semibold tabular-nums text-[#111827]">
                    {{ formatTokens(payload.total_tokens) }}
                  </TableCell>
                  <TableCell class="text-right text-xs font-semibold tabular-nums text-[#111827]">100.0%</TableCell>
                </TableRow>
              </TableBody>
            </Table>
          </div>
        </CardContent>
      </Card>

      <!-- 6. Footnote -->
      <div class="text-[11px] leading-relaxed text-[#6B7280]">
        <p>
          Data source: the latest brain request in requests.jsonl, with its total calibrated against turns.jsonl's
          max_prompt_tokens; the CJK char-rate is solved from that total, so per-item figures are estimates within a
          few percent.
        </p>
        <p v-if="!payload.calibrated" class="mt-1">Uncalibrated estimate (no completed turn record)</p>
      </div>
    </div>
  </div>
</template>
