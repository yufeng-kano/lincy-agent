<script setup lang="ts">
import { onMounted } from 'vue'
import { useDashboardStore } from '@/stores/dashboard'
import MonitorTabs from '@/components/dashboard/MonitorTabs.vue'
import TimeRangeSelector from '@/components/dashboard/TimeRangeSelector.vue'
import SummaryCards from '@/components/dashboard/SummaryCards.vue'
import DailyCostChart from '@/components/dashboard/DailyCostChart.vue'
import DailyCacheChart from '@/components/dashboard/DailyCacheChart.vue'
import SessionsTable from '@/components/dashboard/SessionsTable.vue'

const store = useDashboardStore()

onMounted(() => {
  store.refresh()
})
</script>

<template>
  <div>
    <MonitorTabs />
    <div class="space-y-6">
      <TimeRangeSelector />
      <SummaryCards />
      <div class="grid grid-cols-1 xl:grid-cols-2 gap-4">
        <DailyCostChart />
        <DailyCacheChart />
      </div>
      <SessionsTable />
    </div>
  </div>
</template>
