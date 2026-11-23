<template>
  <div>
    <PageHeader title="Sync History" subtitle="Every export and import this connector has run.">
      <template #actions>
        <Button variant="subtle" :loading="loading" @click="load">Refresh</Button>
      </template>
    </PageHeader>

    <ErrorMessage v-if="error" :message="error" class="mb-4" />

    <div class="mb-3 flex flex-wrap items-end gap-3">
      <div class="w-40">
        <FormControl type="select" label="Direction" v-model="kind" :options="KINDS" />
      </div>
      <div class="w-40">
        <FormControl type="select" label="Status" v-model="statusFilter" :options="STATUSES" />
      </div>
      <label class="flex items-center gap-2 pb-1.5 text-p-sm text-ink-gray-7">
        <input type="checkbox" v-model="thisCompanyOnly" class="rounded-4 border-outline-gray-3" />
        Only {{ app.company || 'this company' }}
      </label>
    </div>

    <div v-if="rows.length" class="overflow-x-auto rounded-4 border border-outline-gray-2">
      <table class="w-full min-w-[56rem] text-p-sm">
        <thead class="bg-surface-gray-1 text-left text-p-xs uppercase tracking-wide text-ink-gray-5">
          <tr>
            <th class="px-3 py-2 font-medium">Job</th>
            <th class="px-3 py-2 font-medium">Company</th>
            <th class="px-3 py-2 font-medium">Range</th>
            <th class="px-3 py-2 font-medium">Status</th>
            <th class="px-3 py-2 text-right font-medium">Records</th>
            <th class="px-3 py-2 text-right font-medium">Failed</th>
            <th class="px-3 py-2 text-right font-medium">Duration</th>
            <th class="px-3 py-2 text-right font-medium">Finished</th>
            <th class="px-3 py-2"></th>
          </tr>
        </thead>
        <tbody class="divide-y divide-outline-gray-1">
          <tr v-for="row in rows" :key="row.kind + row.name" class="hover:bg-surface-gray-1">
            <td class="px-3 py-2">
              <div class="flex items-center gap-1.5">
                <LucideUpload v-if="row.kind === 'export'" class="size-3.5 text-ink-gray-5" />
                <LucideDownload v-else class="size-3.5 text-ink-gray-5" />
                <span class="font-medium text-ink-gray-8">{{ row.title }}</span>
              </div>
              <span class="font-mono text-p-xs text-ink-gray-5">{{ row.name }}</span>
            </td>
            <td class="max-w-[12rem] truncate px-3 py-2 text-ink-gray-7">{{ row.company || '—' }}</td>
            <td class="whitespace-nowrap px-3 py-2 text-ink-gray-6">
              <template v-if="row.from_date">{{ row.from_date }} → {{ row.to_date }}</template>
              <span v-else class="text-ink-gray-4">—</span>
            </td>
            <td class="px-3 py-2"><StatusBadge :status="row.status" /></td>
            <td class="kx-nums px-3 py-2 text-right text-ink-gray-8">
              {{ formatNumber(row.records) }}
            </td>
            <td class="kx-nums px-3 py-2 text-right"
              :class="row.failed ? 'font-medium text-ink-red-6' : 'text-ink-gray-5'">
              {{ formatNumber(row.failed) }}
            </td>
            <td class="kx-nums whitespace-nowrap px-3 py-2 text-right text-ink-gray-6">
              {{ formatDuration(row.duration) }}
            </td>
            <td class="whitespace-nowrap px-3 py-2 text-right text-ink-gray-6">
              {{ formatWhen(row.finished_at || row.creation) }}
            </td>
            <td class="px-3 py-2 text-right">
              <Button size="sm" variant="ghost" @click="open(row)">Details</Button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <EmptyState
      v-else-if="!loading"
      title="No jobs match these filters"
      description="Change the direction or status above, or run an export."
    />

    <div v-if="rows.length >= limit" class="mt-3 flex justify-center">
      <Button variant="subtle" :loading="loading" @click="more">Load more</Button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Button, ErrorMessage, FormControl } from 'frappe-ui'
import LucideDownload from '~icons/lucide/download'
import LucideUpload from '~icons/lucide/upload'

import EmptyState from '@/components/EmptyState.vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { api, errorMessage, formatDuration, formatNumber, formatWhen } from '@/api'
import { appStore } from '@/stores/app'
import { jobsStore } from '@/stores/jobs'

const route = useRoute()
const app = appStore.state

const KINDS = [
  { label: 'All', value: 'all' },
  { label: 'Exports', value: 'export' },
  { label: 'Imports', value: 'import' },
]
const STATUSES = [
  { label: 'All', value: 'all' },
  { label: 'Success', value: 'Success' },
  { label: 'Partial', value: 'Partial' },
  { label: 'Failed', value: 'Failed' },
  { label: 'Running', value: 'Running' },
]

const rows = ref<any[]>([])
const kind = ref('all')
const statusFilter = ref('all')
const thisCompanyOnly = ref(false)
const limit = ref(50)
const loading = ref(false)
const error = ref('')

function open(row: any) {
  jobsStore.track(row.name, row.kind, true)
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    rows.value = await api('tally_connector.api.jobs.history', {
      kind: kind.value,
      status_filter: statusFilter.value,
      company: thisCompanyOnly.value ? app.company : null,
      limit: limit.value,
    })
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    loading.value = false
  }
}

function more() {
  limit.value += 50
  load()
}

onMounted(async () => {
  await load()
  // Arriving from a job panel's "View report": open that job straight away
  // rather than making somebody find it in the list they were just sent to.
  const wanted = route.query.job as string
  if (wanted) {
    const row = rows.value.find((r) => r.name === wanted)
    if (row) open(row)
  }
})

watch([kind, statusFilter, thisCompanyOnly], load)
</script>
