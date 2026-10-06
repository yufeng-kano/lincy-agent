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
  Filler,
} from 'chart.js'
import { useDashboardStore } from '@/stores/dashboard'
import { fetchAllRequests } from '@/api/client'

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Tooltip, Filler)

const store = useDashboardStore()
const requestData = ref<{ time: string; rate: number | null }[]>([])

async function loadRequests() {
  if (store.range !== 'today') {
    requestData.value = []
    return
  }
  const now = new Date()
  const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
  const data = await fetchAllRequests(today, today, 500)
  const reqs = (data.requests || []) as Record<string, unknown>[]
  requestData.value = reqs
    .map((r) => {
      const ts = r.ts as string
      const d = new Date(ts)
      const time = d.toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit', hour12: false })
      const rate = r.read_cache_rate as number | null
      return { time, rate: rate == null ? null : rate * 100 }
    })
}

onMounted(loadRequests)
watch(() => store.range, loadRequests)
watch(() => store.summary, loadRequests)

const isToday = computed(() => store.range === 'today')

const chartData = computed(() => {
  if (isToday.value) {
    return {
      labels: requestData.value.map((d) => d.time),
      datasets: [
        {
          data: requestData.value.map((d) => d.rate),
          borderColor: '#111827',
          backgroundColor: 'rgba(17, 24, 39, 0.04)',
          borderWidth: 1.5,
          pointRadius: 3,
          pointBackgroundColor: '#111827',
          tension: 0.3,
          fill: true,
        },
      ],
    }
  }

  const costs = (store.summary as Record<string, unknown>)?.daily_costs as
    { date: string; read_cache_rate: number | null }[] || []
  return {
    labels: costs.map((d) => d.date.slice(5)),
    datasets: [
      {
        data: costs.map((d) => (d.read_cache_rate == null ? null : d.read_cache_rate * 100)),
        borderColor: '#111827',
        backgroundColor: 'rgba(17, 24, 39, 0.04)',
        borderWidth: 1.5,
        pointRadius: 3,
        pointBackgroundColor: '#111827',
        tension: 0.3,
        fill: true,
      },
    ],
  }
})

const chartOptions = {
  responsive: true,
  maintainAspectRatio: false,
  plugins: {
    legend: { display: false },
    tooltip: {
      callbacks: {
        label: (ctx: { parsed: { y: number | null } }) => `${(ctx.parsed.y ?? 0).toFixed(1)}%`,
      },
    },
  },
  scales: {
    x: {
      grid: { display: false },
      ticks: { color: '#6B7280', font: { size: 11 }, maxRotation: 45 },
    },
    y: {
      min: 0,
      max: 100,
      grid: { color: '#F3F4F6' },
      ticks: {
        color: '#6B7280',
        font: { size: 11 },
        callback: (v: number | string) => `${v}%`,
      },
    },
  },
}
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
