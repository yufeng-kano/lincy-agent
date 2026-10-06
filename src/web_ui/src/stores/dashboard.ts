import { ref, computed } from 'vue'
import { defineStore } from 'pinia'
import { fetchDashboard, fetchSessions } from '@/api/client'

export type DateRange = 'today' | '7d' | '30d' | 'month' | 'custom'

function formatDate(d: Date): string {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

function getRange(range: DateRange, selectedMonth: string, customFrom?: string, customTo?: string) {
  const today = new Date()
  const to = formatDate(today)
  if (range === 'today') return { from: to, to }
  if (range === '7d') {
    const d = new Date(today)
    d.setDate(d.getDate() - 6)
    return { from: formatDate(d), to }
  }
  if (range === '30d') {
    const d = new Date(today)
    d.setDate(d.getDate() - 29)
    return { from: formatDate(d), to }
  }
  if (range === 'month' && selectedMonth) {
    // selectedMonth is "YYYY-MM"
    const [y, m] = selectedMonth.split('-').map(Number)
    const first = new Date(y, m - 1, 1)
    const last = new Date(y, m, 0) // last day of month
    return { from: formatDate(first), to: formatDate(last) }
  }
  return { from: customFrom || to, to: customTo || to }
}

export const useDashboardStore = defineStore('dashboard', () => {
  const range = ref<DateRange>('today')
  const selectedMonth = ref('')
  const customFrom = ref('')
  const customTo = ref('')
  const summary = ref<Record<string, unknown> | null>(null)
  const sessions = ref<Record<string, unknown>[]>([])
  const totalSessions = ref(0)
  const loading = ref(false)

  const dateRange = computed(() => getRange(range.value, selectedMonth.value, customFrom.value, customTo.value))

  async function refresh() {
    loading.value = true
    try {
      const { from, to } = dateRange.value
      const [dashData, sessData] = await Promise.all([
        fetchDashboard(from, to),
        fetchSessions(from, to, 50),
      ])
      summary.value = dashData
      sessions.value = sessData.sessions || []
      totalSessions.value = sessData.total || 0
    } finally {
      loading.value = false
    }
  }

  function setRange(r: DateRange, from?: string, to?: string) {
    range.value = r
    if (from) customFrom.value = from
    if (to) customTo.value = to
    refresh()
  }

  function setMonth(month: string) {
    selectedMonth.value = month
    range.value = 'month'
    refresh()
  }

  return { range, selectedMonth, customFrom, customTo, summary, sessions, totalSessions, loading, refresh, setRange, setMonth }
})
