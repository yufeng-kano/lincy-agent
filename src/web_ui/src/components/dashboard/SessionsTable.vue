<script setup lang="ts">
import { useRouter } from 'vue-router'
import { useDashboardStore } from '@/stores/dashboard'
import { formatCacheRate, formatCacheWriteTokens, formatCostShort } from '@/lib/format'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'

const store = useDashboardStore()
const router = useRouter()

function formatTime(iso: string): string {
  const d = new Date(iso)
  return `${(d.getMonth() + 1).toString().padStart(2, '0')}/${d.getDate().toString().padStart(2, '0')} ${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`
}

function goToSession(s: Record<string, unknown>) {
  router.push(`/monitor/${s.session_id}`)
}
</script>

<template>
  <div class="border border-[#E5E7EB] rounded-lg shadow-[0_1px_2px_rgba(0,0,0,0.04)]">
    <div class="px-4 py-3 text-sm font-medium text-[#111827]">Sessions</div>
    <Table>
      <TableHeader>
        <TableRow class="text-xs text-[#6B7280]">
          <TableHead class="w-32">Time</TableHead>
          <TableHead class="w-16">Status</TableHead>
          <TableHead class="w-16 text-right">Turns</TableHead>
          <TableHead class="w-20 text-right">Cost</TableHead>
          <TableHead class="w-24 text-right">Read Cache</TableHead>
          <TableHead class="w-24 text-right">Write Cache</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        <TableRow
          v-for="s in store.sessions"
          :key="(s as Record<string, unknown>).session_id as string"
          class="cursor-pointer hover:bg-[#F9FAFB] transition-colors"
          @click="goToSession(s as Record<string, unknown>)"
        >
          <TableCell class="text-sm text-[#111827] tabular-nums">
            {{ formatTime((s as Record<string, unknown>).created_at as string) }}
          </TableCell>
          <TableCell>
            <Badge
              variant="secondary"
              class="text-[10px] px-1.5 py-0"
              :class="(s as Record<string, unknown>).status === 'active' ? 'bg-[#ECFDF5] text-[#065F46]' : ''"
            >
              {{ (s as Record<string, unknown>).status }}
            </Badge>
          </TableCell>
          <TableCell class="text-right text-sm tabular-nums">
            {{ (s as Record<string, unknown>).turn_count }}
          </TableCell>
          <TableCell class="text-right text-sm tabular-nums">
            {{ formatCostShort((s as Record<string, unknown>).total_cost as number) }}
          </TableCell>
          <TableCell class="text-right text-sm tabular-nums">
            {{ formatCacheRate((s as Record<string, unknown>).read_cache_rate as number | null) }}
          </TableCell>
          <TableCell class="text-right text-sm tabular-nums">
            {{ formatCacheWriteTokens(
              (s as Record<string, unknown>).write_cache_measurable as boolean | null,
              (s as Record<string, unknown>).total_cache_write as number | null,
            ) }}
          </TableCell>
        </TableRow>
      </TableBody>
    </Table>
  </div>
</template>
