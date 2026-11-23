<template>
  <div class="overflow-x-auto rounded-4 border border-outline-gray-2">
    <table class="w-full min-w-[42rem] text-p-sm">
      <thead class="bg-surface-gray-1 text-left text-p-xs uppercase tracking-wide text-ink-gray-5">
        <tr>
          <th class="px-3 py-2 font-medium">Data type</th>
          <th class="px-3 py-2 text-right font-medium">Records</th>
          <th class="px-3 py-2 font-medium">Status</th>
          <th class="px-3 py-2 font-medium">Last export</th>
          <th class="px-3 py-2 text-right font-medium">Actions</th>
        </tr>
      </thead>
      <tbody class="divide-y divide-outline-gray-1">
        <tr v-for="row in rows" :key="row.key" class="align-middle hover:bg-surface-gray-1">
          <td class="px-3 py-2">
            <router-link
              :to="{ name: 'Area', params: { area: row.key } }"
              class="font-medium text-ink-gray-8 hover:text-brand-700"
            >
              {{ row.label }}
            </router-link>
            <p class="text-p-xs text-ink-gray-5">{{ row.description }}</p>
          </td>
          <td class="kx-nums whitespace-nowrap px-3 py-2 text-right text-ink-gray-8">
            {{ formatNumber(row.count) }}
            <!-- Said once, here: a unit has no posting date, so the range above
                 does not apply to it. Without this the same table would appear
                 to be reporting two different things in one column. -->
            <span v-if="!row.dated" class="ml-1 text-p-xs text-ink-gray-4">all</span>
          </td>
          <td class="whitespace-nowrap px-3 py-2">
            <Badge v-if="!row.included" theme="gray" variant="subtle" label="Excluded" />
            <StatusBadge v-else-if="row.last" :status="row.last.status" />
            <span v-else class="text-ink-gray-5">Not exported</span>
          </td>
          <td class="whitespace-nowrap px-3 py-2 text-ink-gray-6">
            <template v-if="row.last">
              {{ formatWhen(row.last.at) }}
              <span v-if="row.last.whole_run" class="text-p-xs text-ink-gray-4">(full export)</span>
            </template>
            <span v-else class="text-ink-gray-4">—</span>
          </td>
          <td class="whitespace-nowrap px-3 py-2 text-right">
            <div class="flex justify-end gap-1.5">
              <Button
                size="sm"
                variant="subtle"
                :loading="busy === row.key + ':xml'"
                @click="$emit('download', row)"
              >
                XML
              </Button>
              <Button
                size="sm"
                variant="subtle"
                :loading="busy === row.key + ':push'"
                @click="$emit('push', row)"
              >
                Push
              </Button>
            </div>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup lang="ts">
import { Badge, Button } from 'frappe-ui'
import StatusBadge from './StatusBadge.vue'
import { formatNumber, formatWhen } from '@/api'

defineProps<{ rows: any[]; busy?: string }>()
defineEmits(['push', 'download'])
</script>
