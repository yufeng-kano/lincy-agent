<script setup lang="ts">
import { computed } from 'vue'
import { useDashboardStore } from '@/stores/dashboard'
import { formatCacheRate, formatCacheWriteTokens, formatCostShort, formatPricingSources } from '@/lib/format'
import { Card, CardContent } from '@/components/ui/card'

const store = useDashboardStore()

const cards = computed(() => {
  const s = store.summary as Record<string, unknown> | null
  if (!s) return []
  return [
    {
      label: 'Total Cost',
      value: formatCostShort(s.total_cost as number | null),
      detail: formatPricingSources(s.pricing_sources),
    },
    { label: 'Turns', value: String((s.total_turns as number | undefined) ?? 0) },
    { label: 'Sessions', value: String((s.total_sessions as number | undefined) ?? 0) },
    { label: 'Read Cache Rate', value: formatCacheRate(s.read_cache_rate as number | null) },
    {
      label: 'Write Cache',
      value: formatCacheWriteTokens(
        s.write_cache_measurable as boolean | null,
        s.total_cache_write as number | null,
      ),
    },
  ]
})
</script>

<template>
  <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-4">
    <Card v-for="c in cards" :key="c.label" class="border-[#E5E7EB] shadow-[0_1px_2px_rgba(0,0,0,0.04)]">
      <CardContent class="pt-4 pb-4">
        <div class="text-2xl font-semibold text-[#111827] tabular-nums">{{ c.value }}</div>
        <div class="text-xs text-[#6B7280] mt-1">{{ c.label }}</div>
        <div v-if="c.detail" class="mt-1 truncate text-[10px] text-[#6B7280]" :title="c.detail">
          {{ c.detail }}
        </div>
      </CardContent>
    </Card>
  </div>
</template>
