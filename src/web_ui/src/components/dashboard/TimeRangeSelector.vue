<script setup lang="ts">
import { ref, computed } from 'vue'
import { useDashboardStore, type DateRange } from '@/stores/dashboard'

const store = useDashboardStore()
const showMonthPicker = ref(false)

const ranges: { label: string; value: DateRange }[] = [
  { label: 'Today', value: 'today' },
  { label: '7D', value: '7d' },
  { label: '30D', value: '30d' },
]

const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

const now = new Date()
const pickerYear = ref(now.getFullYear())

const currentMonth = computed(() => {
  if (store.range !== 'month' || !store.selectedMonth) return ''
  return store.selectedMonth
})

const monthLabel = computed(() => {
  if (!currentMonth.value) return ''
  const [y, m] = currentMonth.value.split('-')
  return `${months[parseInt(m) - 1]} ${y}`
})

function selectMonth(monthIndex: number) {
  const m = `${pickerYear.value}-${String(monthIndex + 1).padStart(2, '0')}`
  store.setMonth(m)
  showMonthPicker.value = false
}

function isSelected(monthIndex: number): boolean {
  if (!currentMonth.value) return false
  const m = `${pickerYear.value}-${String(monthIndex + 1).padStart(2, '0')}`
  return m === currentMonth.value
}

function isFuture(monthIndex: number): boolean {
  if (pickerYear.value > now.getFullYear()) return true
  if (pickerYear.value === now.getFullYear() && monthIndex > now.getMonth()) return true
  return false
}
</script>

<template>
  <div class="flex items-center gap-1 relative">
    <button
      v-for="r in ranges"
      :key="r.value"
      class="px-3 py-1 text-sm rounded transition-colors"
      :class="store.range === r.value
        ? 'bg-[#111827] text-white'
        : 'text-[#6B7280] hover:bg-[#F3F4F6]'"
      @click="store.setRange(r.value); showMonthPicker = false"
    >
      {{ r.label }}
    </button>

    <!-- Month button -->
    <button
      class="px-3 py-1 text-sm rounded transition-colors"
      :class="store.range === 'month'
        ? 'bg-[#111827] text-white'
        : 'text-[#6B7280] hover:bg-[#F3F4F6]'"
      @click="showMonthPicker = !showMonthPicker"
    >
      {{ store.range === 'month' ? monthLabel : 'Month' }}
    </button>

    <!-- Month picker dropdown -->
    <div
      v-if="showMonthPicker"
      class="absolute top-full left-0 mt-1 bg-white border border-[#E5E7EB] rounded-lg shadow-lg p-3 z-50 w-64"
    >
      <!-- Year nav -->
      <div class="flex items-center justify-between mb-2">
        <button
          class="p-1 text-[#6B7280] hover:text-[#111827] text-sm"
          @click="pickerYear--"
        >
          &larr;
        </button>
        <span class="text-sm font-medium text-[#111827]">{{ pickerYear }}</span>
        <button
          class="p-1 text-sm"
          :class="pickerYear >= now.getFullYear() ? 'text-[#D1D5DB] cursor-default' : 'text-[#6B7280] hover:text-[#111827]'"
          :disabled="pickerYear >= now.getFullYear()"
          @click="pickerYear++"
        >
          &rarr;
        </button>
      </div>
      <!-- Month grid -->
      <div class="grid grid-cols-3 gap-1">
        <button
          v-for="(m, i) in months"
          :key="i"
          class="px-2 py-1.5 text-xs rounded transition-colors"
          :class="[
            isSelected(i) ? 'bg-[#111827] text-white' : '',
            isFuture(i) ? 'text-[#D1D5DB] cursor-default' : isSelected(i) ? '' : 'text-[#6B7280] hover:bg-[#F3F4F6]',
          ]"
          :disabled="isFuture(i)"
          @click="!isFuture(i) && selectMonth(i)"
        >
          {{ m }}
        </button>
      </div>
    </div>
  </div>

  <!-- Click outside to close -->
  <div
    v-if="showMonthPicker"
    class="fixed inset-0 z-40"
    @click="showMonthPicker = false"
  />
</template>
