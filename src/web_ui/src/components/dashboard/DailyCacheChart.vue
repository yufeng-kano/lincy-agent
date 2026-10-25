<script setup lang="ts">
import { computed, ref, watch, onMounted } from 'vue'
import { Line } from 'vue-chartjs'
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Tooltip,
  Legend,
} from 'chart.js'
import type { ChartOptions, TooltipItem } from 'chart.js'
import { useDashboardStore } from '@/stores/dashboard'
import { fetchAllRequests } from '@/api/client'

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Tooltip, Legend)

const BRAIN = '#111827'
const SUBAGENT = '#9CA3AF'
const MINUTES_PER_DAY = 24 * 60

// Brain requests more than this far apart are not one continuous run; a line
// bridging the overnight quiet hours would read as a slow decline.
const LINE_GAP_MINUTES = 60

interface RequestPoint {
  x: number // minutes since local midnight
  y: number | null // read cache rate in percent; null breaks the line
  client: string
  round: number | null
  promptTokens: number
  cacheRead: number
  cacheWrite: number
}

const store = useDashboardStore()
const brainPoints = ref<RequestPoint[]>([])
const subagentPoints = ref<RequestPoint[]>([])

async function loadRequests() {
  if (store.range !== 'today') {
    brainPoints.value = []
    subagentPoints.value = []
    return
  }
  const now = new Date()
  const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
  // The API returns newest first; 1000 is its page cap and covers a busy day.
  const data = await fetchAllRequests(today, today, 1000)
  const reqs = (data.requests || []) as Record<string, unknown>[]
  const brain: RequestPoint[] = []
  const subagents: RequestPoint[] = []
  for (const r of reqs) {
    const rate = r.read_cache_rate as number | null
    if (rate == null) continue
    const d = new Date(r.ts as string)
    const client = (r.client_label as string | null) ?? 'unknown'
    const point: RequestPoint = {
      x: d.getHours() * 60 + d.getMinutes() + d.getSeconds() / 60,
      y: rate * 100,
      client,
      round: (r.round as number | null) ?? null,
      promptTokens: (r.prompt_tokens as number) ?? 0,
      cacheRead: (r.cache_read_tokens as number) ?? 0,
      cacheWrite: (r.cache_write_tokens as number) ?? 0,
    }
    ;(client === 'brain' ? brain : subagents).push(point)
  }
  // Chart.js draws lines in data order, so time must run left to right.
  brain.sort((a, b) => a.x - b.x)
  subagents.sort((a, b) => a.x - b.x)
  brainPoints.value = breakLongGaps(brain)
  subagentPoints.value = subagents
}

function breakLongGaps(points: RequestPoint[]): RequestPoint[] {
  const out: RequestPoint[] = []
  for (const [i, p] of points.entries()) {
    const prev = points[i - 1]
    if (prev && p.x - prev.x > LINE_GAP_MINUTES) {
      out.push({ ...prev, x: (prev.x + p.x) / 2, y: null })
    }
    out.push(p)
  }
  return out
}

onMounted(loadRequests)
watch(() => store.range, loadRequests)
watch(() => store.summary, loadRequests)

const isToday = computed(() => store.range === 'today')

function formatMinutes(value: number): string {
  const h = Math.floor(value / 60)
  const m = Math.round(value % 60)
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`
}

const chartData = computed(() => {
  if (isToday.value) {
    return {
      datasets: [
        {
          label: 'Brain',
          data: brainPoints.value,
          borderColor: BRAIN,
          borderWidth: 1.5,
          pointRadius: 3,
          pointBackgroundColor: BRAIN,
          tension: 0,
          showLine: true,
          spanGaps: false,
        },
        {
          label: 'Sub-agents',
          data: subagentPoints.value,
          borderColor: SUBAGENT,
          borderWidth: 0,
          pointRadius: 3,
          pointBackgroundColor: SUBAGENT,
          // Consecutive sub-agent requests belong to different workers, so a
          // connecting line would imply a continuity that does not exist.
          showLine: false,
        },
      ],
    }
  }

  const days = ((store.summary as Record<string, unknown>)?.daily_costs as
    { date: string; read_cache_rate: number | null; brain_read_cache_rate: number | null }[]) || []
  const pct = (v: number | null | undefined) => (v == null ? null : v * 100)
  return {
    labels: days.map((d) => d.date.slice(5)),
    datasets: [
      {
        label: 'Brain',
        data: days.map((d) => pct(d.brain_read_cache_rate)),
        borderColor: BRAIN,
        borderWidth: 1.5,
        pointRadius: 3,
        pointBackgroundColor: BRAIN,
        tension: 0,
        spanGaps: true,
      },
      {
        label: 'All clients',
        data: days.map((d) => pct(d.read_cache_rate)),
        borderColor: SUBAGENT,
        borderWidth: 1.5,
        pointRadius: 3,
        pointBackgroundColor: SUBAGENT,
        tension: 0,
        spanGaps: true,
      },
    ],
  }
})

const legend = {
  display: true,
  position: 'top' as const,
  align: 'end' as const,
  labels: {
    color: '#6B7280',
    boxWidth: 8,
    boxHeight: 8,
    usePointStyle: true,
    pointStyle: 'circle',
    font: { size: 11 },
  },
}

const yScale = {
  min: 0,
  max: 100,
  grid: { color: '#F3F4F6' },
  ticks: {
    color: '#6B7280',
    font: { size: 11 },
    stepSize: 20,
    callback: (v: number | string) => `${v}%`,
  },
}

const todayOptions: ChartOptions<'line'> = {
  responsive: true,
  maintainAspectRatio: false,
  animation: false,
  interaction: { mode: 'nearest', intersect: true },
  plugins: {
    legend,
    tooltip: {
      displayColors: false,
      callbacks: {
        title: (items: TooltipItem<'line'>[]) => {
          const p = items[0]?.raw as RequestPoint | undefined
          if (!p || p.y == null) return ''
          const round = p.round == null ? '' : ` round ${p.round}`
          return `${formatMinutes(p.x)} ${p.client}${round}`
        },
        label: (item: TooltipItem<'line'>) => {
          const p = item.raw as RequestPoint
          if (p.y == null) return []
          return [
            `read ${p.y.toFixed(1)}%`,
            `prompt ${p.promptTokens.toLocaleString()}`,
            `cache read ${p.cacheRead.toLocaleString()}`,
            `cache write ${p.cacheWrite.toLocaleString()}`,
          ]
        },
      },
    },
  },
  scales: {
    x: {
      type: 'linear',
      min: 0,
      max: MINUTES_PER_DAY,
      grid: { display: false },
      ticks: {
        color: '#6B7280',
        font: { size: 11 },
        stepSize: 180,
        callback: (v: number | string) => formatMinutes(Number(v)),
      },
    },
    y: yScale,
  },
}

const dailyOptions: ChartOptions<'line'> = {
  responsive: true,
  maintainAspectRatio: false,
  animation: false,
  plugins: {
    legend,
    tooltip: {
      callbacks: {
        label: (item: TooltipItem<'line'>) =>
          `${item.dataset.label}: ${item.parsed.y == null ? 'n/a' : `${item.parsed.y.toFixed(1)}%`}`,
      },
    },
  },
  scales: {
    x: {
      grid: { display: false },
      ticks: { color: '#6B7280', font: { size: 11 } },
    },
    y: yScale,
  },
}

const chartOptions = computed(() => (isToday.value ? todayOptions : dailyOptions))
</script>

<template>
  <div class="border border-[#E5E7EB] rounded-lg p-4 shadow-[0_1px_2px_rgba(0,0,0,0.04)]">
    <div class="text-sm font-medium text-[#111827] mb-3">
      {{ isToday ? 'Request Read Cache Rate' : 'Daily Read Cache Rate' }}
    </div>
    <div class="h-48">
      <Line :data="chartData" :options="chartOptions" />
    </div>
  </div>
</template>
