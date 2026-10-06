<script setup lang="ts">
import { useRoute } from 'vue-router'

const route = useRoute()

const tabs = [
  { name: 'Overview', path: '/monitor' },
  { name: 'Requests', path: '/monitor/requests' },
  { name: 'Context', path: '/monitor/context' },
]
</script>

<template>
  <div class="flex gap-4 border-b border-[#E5E7EB] mb-6">
    <router-link
      v-for="tab in tabs"
      :key="tab.path"
      :to="tab.path"
      class="pb-2 text-sm transition-colors border-b-2 -mb-px"
      :class="route.path === tab.path
        ? 'text-[#111827] font-medium border-[#111827]'
        : 'text-[#6B7280] border-transparent hover:text-[#111827]'"
    >
      {{ tab.name }}
    </router-link>
  </div>
</template>
