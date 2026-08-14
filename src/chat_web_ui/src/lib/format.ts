export function formatCost(cost: number | null | undefined): string {
  if (cost == null) return '-'
  return `$${cost.toFixed(4)}`
}

export function formatCostShort(cost: number | null | undefined): string {
  if (cost == null) return '-'
  if (cost < 0.01) return `$${cost.toFixed(4)}`
  return `$${cost.toFixed(2)}`
}

export function formatTokens(tokens: number): string {
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`
  if (tokens >= 1_000) return `${(tokens / 1_000).toFixed(0)}k`
  return tokens.toLocaleString()
}

export function formatPercent(value: number | null | undefined): string {
  if (value == null) return '-'
  return `${(value * 100).toFixed(1)}%`
}

export function formatCacheRate(value: number | null | undefined): string {
  if (value == null) return 'unavailable'
  return `${(value * 100).toFixed(1)}%`
}

export function formatCacheWriteTokens(
  measurable: boolean | null | undefined,
  tokens: number | null | undefined,
): string {
  if (!measurable) return '無法測量'
  return formatTokens(tokens ?? 0)
}

/** Wall-clock time of an ISO timestamp in the viewer's timezone, e.g. "14:23:07". */
export function formatClockTime(iso: string): string {
  return new Date(iso).toLocaleTimeString('en', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

/**
 * "provider:model" of the fallback that actually served a request, or null
 * when the primary served it / the record does not say (old sessions).
 */
export function formatServedByFallback(row: Record<string, unknown>): string | null {
  if (row.served_by_fallback !== true) return null
  const provider = (row.served_provider as string | null) || null
  const model = (row.served_model as string | null) || null
  if (!provider && !model) return 'fallback'
  return [provider, model].filter(Boolean).join(':')
}

export function formatLatency(ms: number): string {
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`
  return `${ms}ms`
}

type PricingSource = {
  source?: string | null
  stale?: boolean | null
  count?: number | null
}

function pricingSourceLabel(source: string | null | undefined): string {
  if (!source) return '-'
  if (source === 'local_override') return 'Local override'
  if (source === 'litellm_cache') return 'LiteLLM cache'
  if (source === 'litellm') return 'LiteLLM'
  return source
}

export function formatPricingSource(
  source: string | null | undefined,
  stale: boolean | null | undefined,
): string {
  if (!source) return '-'
  return stale ? `${pricingSourceLabel(source)} stale` : pricingSourceLabel(source)
}

export function formatPricingSources(sources: unknown): string {
  if (!Array.isArray(sources) || sources.length === 0) return 'Pricing unavailable'
  const labels = sources
    .filter((item): item is PricingSource => item && typeof item === 'object')
    .map((item) => formatPricingSource(item.source, item.stale))
  return Array.from(new Set(labels)).join(', ')
}

export function pricingSourceClass(stale: boolean | null | undefined): string {
  return stale ? 'text-[#B45309]' : 'text-[#6B7280]'
}
