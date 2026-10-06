<script setup lang="ts">
import { computed } from 'vue'
import { Bar } from 'vue-chartjs'
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  BarElement,
  Tooltip,
} from 'chart.js'
import { useDashboardStore } from '@/stores/dashboard'

ChartJS.register(CategoryScale, LinearScale, BarElement, Tooltip)

const store = useDashboardStore()

const chartData = computed(() => {
  const costs = (store.summary as Record<string, unknown>)?.daily_costs as { date: string; cost: number }[] || []
  return {
    labels: costs.map((d) => d.date.slice(5)),
    datasets: [
      {
        data: costs.map((d) => d.cost),
        backgroundColor: '#111827',
        borderRadius: 2,
        maxBarThickness: 32,
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
        label: (ctx: { parsed: { y: number | null } }) => `$${(ctx.parsed.y ?? 0).toFixed(4)}`,
      },
    },
  },
  scales: {
    x: {
      grid: { display: false },
      ticks: { color: '#6B7280', font: { size: 11 } },
    },
    y: {
      grid: { color: '#F3F4F6' },
      ticks: {
        color: '#6B7280',
        font: { size: 11 },
        callback: (v: number | string) => `$${v}`,
      },
    },
  },
}
</script>

<template>
  <div class="border border-[#E5E7EB] rounded-lg p-4 shadow-[0_1px_2px_rgba(0,0,0,0.04)]">
    <div class="text-sm font-medium text-[#111827] mb-3">Daily Cost</div>
    <div class="h-48">
      <Bar :data="chartData" :options="chartOptions" />
    </div>
  </div>
</template>
