<script setup lang="ts">
import { useRoute } from 'vue-router'

const route = useRoute()

interface NavItem {
  name: string
  path: string
  enabled: boolean
  label?: string
}

const navItems: NavItem[] = [
  { name: 'Monitor', path: '/monitor', enabled: true },
  { name: 'Proxy', path: '/proxy', enabled: true },
  { name: 'Agent', path: '/chat', enabled: true },
  { name: 'Settings', path: '/settings', enabled: false },
]
</script>

<template>
  <aside class="hidden md:flex w-[200px] shrink-0 border-r border-[#E5E7EB] flex-col">
    <div class="px-5 py-6">
      <span class="text-lg font-semibold text-[#111827] tracking-tight">Lincy</span>
    </div>
    <nav class="flex-1 px-3 space-y-0.5">
      <template v-for="item in navItems" :key="item.path">
        <router-link
          v-if="item.enabled"
          :to="item.path"
          class="flex items-center px-3 py-2 rounded text-sm transition-colors"
          :class="route.path.startsWith(item.path)
            ? 'text-[#111827] font-semibold bg-[#F3F4F6]'
            : 'text-[#6B7280] hover:text-[#111827] hover:bg-[#F9FAFB]'"
        >
          {{ item.name }}
        </router-link>
        <div
          v-else
          class="flex items-center gap-2 px-3 py-2 text-sm text-[#D1D5DB] cursor-default"
        >
          {{ item.name }}
          <span v-if="item.label" class="text-[10px]">{{ item.label }}</span>
        </div>
      </template>
    </nav>
  </aside>
  <nav
    class="fixed bottom-0 left-0 right-0 z-20 border-t border-[#E5E7EB] bg-white px-2 py-2 md:hidden"
  >
    <div class="grid grid-cols-4 gap-1">
      <template v-for="item in navItems" :key="`mobile-${item.path}`">
        <router-link
          v-if="item.enabled"
          :to="item.path"
          class="flex items-center justify-center rounded px-2 py-2 text-xs transition-colors"
          :class="route.path.startsWith(item.path)
            ? 'text-[#111827] font-semibold bg-[#F3F4F6]'
            : 'text-[#6B7280]'"
        >
          {{ item.name }}
        </router-link>
        <div
          v-else
          class="flex items-center justify-center rounded px-2 py-2 text-xs text-[#D1D5DB] cursor-default"
        >
          {{ item.name }}
        </div>
      </template>
    </div>
  </nav>
</template>
