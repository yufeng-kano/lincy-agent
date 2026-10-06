<script setup lang="ts">
import { ref, computed } from 'vue'
import { formatTokens } from '@/lib/format'

const props = defineProps<{
  current: number
  softLimit: number
  hardLimit: number
}>()

const expanded = ref(false)

const ceiling = computed(() => expanded.value ? props.hardLimit : props.softLimit)
const fillPct = computed(() => Math.min((props.current / ceiling.value) * 100, 100))
const softUsage = computed(() => props.current / props.softLimit)

const softPct = computed(() =>
  expanded.value ? (props.softLimit / props.hardLimit) * 100 : 100
)

const fillColor = computed(() => {
  if (softUsage.value > 1) return '#EF4444'
  if (softUsage.value > 0.85) return '#F59E0B'
  if (softUsage.value > 0.7) return '#111827'
  return '#D1D5DB'
})

const tooltipText = computed(() =>
  `Current: ${props.current.toLocaleString()} | Soft: ${props.softLimit.toLocaleString()} | Max: ${props.hardLimit.toLocaleString()}`
)
</script>

<template>
  <div
    class="flex flex-col gap-1 cursor-pointer select-none"
    :title="tooltipText"
    @click="expanded = !expanded"
  >
    <div class="flex items-baseline justify-between">
      <span class="text-xs text-[#6B7280] tabular-nums">
        tok {{ formatTokens(current) }} / {{ formatTokens(ceiling) }}
      </span>
      <span class="text-xs tabular-nums" :style="{ color: fillColor }">
        {{ (softUsage * 100).toFixed(1) }}%
      </span>
    </div>
    <div class="relative h-1 bg-[#F3F4F6] rounded-sm w-48">
      <div
        class="absolute inset-y-0 left-0 rounded-sm transition-all duration-300"
        :style="{ width: fillPct + '%', backgroundColor: fillColor }"
      />
      <div
        v-if="expanded"
        class="absolute inset-y-0 w-px bg-[#9CA3AF]"
        :style="{ left: softPct + '%' }"
      />
    </div>
  </div>
</template>
