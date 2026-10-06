import { ref } from 'vue'
import { defineStore } from 'pinia'
import { fetchContextComposition, type ContextComposition, type ContextSegment } from '@/api/client'

// Validated six-color categorical palette (CVD-checked on #FFFFFF, including
// the donut's wrap-around green/blue adjacency), in prompt order (front to
// back of the prompt). Shared by ContextDonut.vue and the sequence/table
// rows in MonitorContext.vue so a segment's color never drifts between them.
// This is the one sanctioned exception to the zero-color rule (data
// encoding, not chrome): see docs/dev/web-dashboard.md's visual system.
export const CONTEXT_PALETTE = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300'] as const

// history and current_turn share the same "Conversation" green: the donut
// merges them into one slice, and the sequence bar keeps them separate but
// visually grouped as "the part of the prompt that grows every turn".
const SEGMENT_COLOR: Record<string, string> = {
  tool_definitions: CONTEXT_PALETTE[0],
  system_prompt: CONTEXT_PALETTE[1],
  boot_core_rules: CONTEXT_PALETTE[2],
  boot_tool_files: CONTEXT_PALETTE[3],
  pinned_context: CONTEXT_PALETTE[4],
  history: CONTEXT_PALETTE[5],
  current_turn: CONTEXT_PALETTE[5],
}

export function segmentColor(key: string): string {
  return SEGMENT_COLOR[key] ?? CONTEXT_PALETTE[0]
}

// Segments that make up the cache-safe static prefix: identical every turn,
// per src/lincy/context/builder.py's build() (system prompt through pinned
// context are all assembled before any conversation history).
export const STATIC_PREFIX_KEYS = [
  'tool_definitions',
  'system_prompt',
  'boot_core_rules',
  'boot_tool_files',
  'pinned_context',
]

export function staticPrefixTokens(segments: ContextSegment[]): number {
  return segments
    .filter((seg) => STATIC_PREFIX_KEYS.includes(seg.key))
    .reduce((sum, seg) => sum + seg.tokens, 0)
}

export interface LargestItem {
  segmentLabel: string
  itemLabel: string
  tokens: number
}

/** Largest single item across all segments, for the "Largest single source" stat. */
export function largestItem(segments: ContextSegment[]): LargestItem | null {
  let best: LargestItem | null = null
  for (const seg of segments) {
    for (const item of seg.items) {
      if (!best || item.tokens > best.tokens) {
        best = { segmentLabel: seg.label, itemLabel: item.label, tokens: item.tokens }
      }
    }
  }
  return best
}

export interface CacheBreakpoints {
  bp1: number
  bp2: number
  bp3: number
  bp4: number
}

/**
 * Cumulative token offsets for the four cache breakpoints the brain prompt
 * carries: BP1/BP2 are set in ContextBuilder.build() (end of system prompt,
 * end of core-rules boot files); BP3 sits on the previous user turn (end of
 * history); BP4 is re-applied by the responder before every LLM call (end of
 * the current turn, before its tool loop).
 */
export function cacheBreakpoints(segments: ContextSegment[]): CacheBreakpoints {
  let cumulative = 0
  let bp1 = 0
  let bp2 = 0
  let bp3 = 0
  let bp4 = 0
  for (const seg of segments) {
    if (seg.key === 'current_turn') {
      // Position right before the current turn starts is "end of history",
      // whether or not a history segment actually contributed any tokens.
      bp3 = cumulative
      const toolLoop = seg.items.find((item) => item.key === 'tool_loop')
      bp4 = cumulative + (seg.tokens - (toolLoop?.tokens ?? 0))
      cumulative += seg.tokens
      continue
    }
    cumulative += seg.tokens
    if (seg.key === 'system_prompt') bp1 = cumulative
    if (seg.key === 'boot_core_rules') bp2 = cumulative
  }
  return { bp1, bp2, bp3, bp4 }
}

export const useContextCompositionStore = defineStore('contextComposition', () => {
  const data = ref<ContextComposition | null>(null)
  const loading = ref(false)
  const error = ref('')

  async function load() {
    loading.value = true
    error.value = ''
    try {
      data.value = await fetchContextComposition()
    } catch (err) {
      error.value = err instanceof Error ? err.message : 'failed to load context composition'
    } finally {
      loading.value = false
    }
  }

  return { data, loading, error, load }
})
