<script setup lang="ts">
import { computed, ref } from 'vue'
import type { ContextSegment } from '@/api/client'
import { formatTokens } from '@/lib/format'
import { CONTEXT_PALETTE } from '@/stores/contextComposition'

const props = defineProps<{
  segments: ContextSegment[]
  total: number
}>()

// The donut merges history + current_turn into one "Conversation" slice
// (the sequence bar downstream keeps them separate); everything else maps
// 1:1 onto a segment, in prompt order, using the palette colors in order.
const STATIC_PREFIX_KEYS = ['tool_definitions', 'system_prompt', 'boot_core_rules', 'boot_tool_files', 'pinned_context']

interface SliceItem {
  key: string
  label: string
  tokens: number
}

interface Slice {
  key: string
  label: string
  tokens: number
  color: string
  items: SliceItem[]
}

const slices = computed<Slice[]>(() => {
  const byKey = new Map(props.segments.map((seg) => [seg.key, seg]))
  const result: Slice[] = []
  STATIC_PREFIX_KEYS.forEach((key, index) => {
    const seg = byKey.get(key)
    if (seg && seg.tokens > 0) {
      result.push({
        key,
        label: seg.label,
        tokens: seg.tokens,
        color: CONTEXT_PALETTE[index],
        items: seg.items,
      })
    }
  })
  const history = byKey.get('history')
  const currentTurn = byKey.get('current_turn')
  const conversationTokens = (history?.tokens ?? 0) + (currentTurn?.tokens ?? 0)
  if (conversationTokens > 0) {
    const items: SliceItem[] = []
    if (history && history.tokens > 0) {
      items.push({ key: 'history', label: 'History', tokens: history.tokens })
    }
    if (currentTurn) items.push(...currentTurn.items)
    result.push({
      key: 'conversation',
      label: 'Conversation',
      tokens: conversationTokens,
      color: CONTEXT_PALETTE[5],
      items,
    })
  }
  return result
})

// In-slice label color: computed from the fill's actual relative luminance
// rather than a fixed light/dark split, since the palette is categorical
// (hue-driven), not an ordinal ramp where "index >= 3" meant "dark half".
// Threshold is calibrated to this exact six-color palette: aqua (#1baf7a,
// L=0.323) and magenta (#e87ba4, L=0.340) read light enough on-screen to
// need #111827, even though both sit under the textbook WCAG 0.4 cutoff;
// 0.3 is the value that reproduces the validated white/dark split for all
// six swatches (blue 0.188, orange 0.278, green 0.162 stay white; yellow
// 0.435 joins aqua/magenta on dark).
const LABEL_LUMINANCE_THRESHOLD = 0.3

function relativeLuminance(hex: string): number {
  const r = parseInt(hex.slice(1, 3), 16) / 255
  const g = parseInt(hex.slice(3, 5), 16) / 255
  const b = parseInt(hex.slice(5, 7), 16) / 255
  const channel = (c: number) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4)
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
}

function labelColorFor(hex: string): string {
  return relativeLuminance(hex) >= LABEL_LUMINANCE_THRESHOLD ? '#111827' : 'white'
}

// --- geometry (kept local to this component) ---

const CX = 100
const CY = 100
const OUTER_R = 88
const INNER_R = 54
const OUTER_ARC_R = 96

/** Point on a circle of radius r, angleDeg measured clockwise from 12 o'clock. */
function arcPoint(angleDeg: number, r: number): [number, number] {
  const rad = (angleDeg * Math.PI) / 180
  return [CX + r * Math.sin(rad), CY - r * Math.cos(rad)]
}

interface SliceGeometry {
  path: string
  labelX: number
  labelY: number
  spanDeg: number
  percent: number
}

const geometry = computed<SliceGeometry[]>(() => {
  const total = props.total || 1
  let cursor = 0
  return slices.value.map((slice) => {
    const t0 = cursor / total
    cursor += slice.tokens
    const t1 = cursor / total
    const a0 = t0 * 360
    const a1 = t1 * 360
    const largeArc = a1 - a0 > 180 ? 1 : 0
    const [x1, y1] = arcPoint(a0, OUTER_R)
    const [x2, y2] = arcPoint(a1, OUTER_R)
    const [x3, y3] = arcPoint(a1, INNER_R)
    const [x4, y4] = arcPoint(a0, INNER_R)
    const path = [
      `M ${x1} ${y1}`,
      `A ${OUTER_R} ${OUTER_R} 0 ${largeArc} 1 ${x2} ${y2}`,
      `L ${x3} ${y3}`,
      `A ${INNER_R} ${INNER_R} 0 ${largeArc} 0 ${x4} ${y4}`,
      'Z',
    ].join(' ')
    const midDeg = (a0 + a1) / 2
    const [lx, ly] = arcPoint(midDeg, (OUTER_R + INNER_R) / 2)
    return { path, labelX: lx, labelY: ly, spanDeg: a1 - a0, percent: (slice.tokens / total) * 100 }
  })
})

const staticPrefixFraction = computed(() => {
  const total = props.total || 1
  const staticTokens = slices.value
    .filter((slice) => slice.key !== 'conversation')
    .reduce((sum, slice) => sum + slice.tokens, 0)
  return staticTokens / total
})

const outerArcPath = computed(() => {
  const fraction = staticPrefixFraction.value
  if (fraction <= 0) return ''
  // Clamp below 360deg: a true full circle degenerates the arc command
  // (identical start/end points), and the whole prompt is never 100% static.
  const angle = Math.min(fraction, 0.9995) * 360
  const largeArc = angle > 180 ? 1 : 0
  const [x1, y1] = arcPoint(0, OUTER_ARC_R)
  const [x2, y2] = arcPoint(angle, OUTER_ARC_R)
  return `M ${x1} ${y1} A ${OUTER_ARC_R} ${OUTER_ARC_R} 0 ${largeArc} 1 ${x2} ${y2}`
})

// --- hover / focus / tooltip ---

const hoveredKey = ref<string | null>(null)

function isDimmed(key: string): boolean {
  return hoveredKey.value !== null && hoveredKey.value !== key
}

const tooltip = ref<{ x: number; y: number; slice: Slice } | null>(null)

function showTooltip(slice: Slice, evt: MouseEvent | FocusEvent) {
  hoveredKey.value = slice.key
  if (evt instanceof MouseEvent) {
    tooltip.value = { x: evt.clientX + 14, y: evt.clientY + 14, slice }
    return
  }
  const rect = (evt.currentTarget as HTMLElement).getBoundingClientRect()
  tooltip.value = { x: rect.right + 10, y: rect.top, slice }
}

function moveTooltip(evt: MouseEvent) {
  if (!tooltip.value) return
  tooltip.value = { ...tooltip.value, x: evt.clientX + 14, y: evt.clientY + 14 }
}

function hideTooltip() {
  hoveredKey.value = null
  tooltip.value = null
}
</script>

<template>
  <div class="flex flex-col lg:flex-row gap-8 items-center lg:items-start">
    <div class="flex flex-col items-center shrink-0">
      <div class="relative">
        <svg viewBox="0 0 200 200" class="h-56 w-56">
          <path
            v-for="(slice, i) in slices"
            :key="slice.key"
            :d="geometry[i].path"
            :fill="slice.color"
            stroke="white"
            stroke-width="2"
            tabindex="0"
            role="img"
            class="cursor-pointer outline-none transition-opacity duration-150"
            :style="{ opacity: isDimmed(slice.key) ? 0.35 : 1 }"
            :aria-label="`${slice.label}: ${formatTokens(slice.tokens)} tokens (${geometry[i].percent.toFixed(1)}%)`"
            @mouseenter="showTooltip(slice, $event)"
            @mousemove="moveTooltip"
            @mouseleave="hideTooltip"
            @focus="showTooltip(slice, $event)"
            @blur="hideTooltip"
          />
          <path
            v-if="outerArcPath"
            :d="outerArcPath"
            fill="none"
            stroke="#E5E7EB"
            stroke-width="3"
            stroke-linecap="round"
          />
          <text
            v-for="(slice, i) in slices"
            v-show="geometry[i].spanDeg >= 28"
            :key="`label-${slice.key}`"
            :x="geometry[i].labelX"
            :y="geometry[i].labelY"
            text-anchor="middle"
            dominant-baseline="central"
            :fill="labelColorFor(slice.color)"
            class="pointer-events-none select-none text-[11px] tabular-nums transition-opacity duration-150"
            :style="{ opacity: isDimmed(slice.key) ? 0.35 : 1 }"
          >{{ Math.round(geometry[i].percent) }}%</text>
        </svg>
        <div class="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
          <span class="normal-nums text-[28px] font-semibold leading-none text-[#111827]">{{ formatTokens(total) }}</span>
          <span class="mt-1.5 text-[11px] text-[#6B7280]">prompt tokens</span>
        </div>
      </div>
      <p class="mt-3 max-w-[15rem] text-center text-[11px] text-[#6B7280]">
        Outer arc marks the cache-safe static prefix (~{{ Math.round(staticPrefixFraction * 100) }}%) — the prompt-cache hit region.
      </p>
    </div>

    <div class="min-w-0 flex-1 space-y-1.5">
      <div
        v-for="slice in slices"
        :key="`legend-${slice.key}`"
        class="flex items-center gap-2 rounded py-1 transition-opacity duration-150"
        :style="{ opacity: isDimmed(slice.key) ? 0.35 : 1 }"
        @mouseenter="showTooltip(slice, $event)"
        @mousemove="moveTooltip"
        @mouseleave="hideTooltip"
      >
        <span class="h-2.5 w-2.5 shrink-0 rounded-sm" :style="{ backgroundColor: slice.color }" />
        <span class="min-w-0 flex-1 truncate text-xs text-[#111827]">{{ slice.label }}</span>
        <span class="text-xs tabular-nums text-[#111827]">{{ formatTokens(slice.tokens) }}</span>
        <span class="w-12 text-right text-xs tabular-nums text-[#6B7280]">
          {{ ((slice.tokens / (total || 1)) * 100).toFixed(1) }}%
        </span>
      </div>
    </div>

    <Teleport to="body">
      <div
        v-if="tooltip"
        class="pointer-events-none fixed z-50 rounded border border-[#E5E7EB] bg-white px-3 py-2 text-xs shadow-[0_1px_2px_rgba(0,0,0,0.08)]"
        :style="{ left: tooltip.x + 'px', top: tooltip.y + 'px' }"
      >
        <div class="mb-1 font-medium text-[#111827]">{{ tooltip.slice.label }}</div>
        <div v-if="tooltip.slice.items.length === 0" class="text-[#6B7280]">
          {{ formatTokens(tooltip.slice.tokens) }} tokens
        </div>
        <div v-for="item in tooltip.slice.items" :key="item.key" class="flex items-center gap-4">
          <span class="max-w-[12rem] truncate text-[#6B7280]">{{ item.label }}</span>
          <span class="ml-auto tabular-nums text-[#111827]">{{ formatTokens(item.tokens) }}</span>
        </div>
      </div>
    </Teleport>
  </div>
</template>
