<template>
  <div class="flex flex-col gap-2">
    <div class="flex items-baseline justify-between gap-3">
      <p class="truncate text-base-medium text-ink-gray-8">
        {{ headline }}
      </p>
      <span class="kx-nums shrink-0 text-base-semibold tabular-nums text-ink-gray-9">
        {{ job.progress }}%
      </span>
    </div>

    <Progress :value="job.progress" size="md" class="kx-progress" :class="barClass" />

    <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-p-sm text-ink-gray-6">
      <span>{{ job.stage }}</span>
      <!-- The counts, only where there are counts. A stage with nothing to
           count says so by not claiming a denominator it does not have. -->
      <span v-if="job.total > 0" class="kx-nums">
        {{ formatNumber(job.processed) }} / {{ formatNumber(job.total) }} processed
      </span>
      <span v-if="job.warnings" class="text-ink-amber-6">
        {{ job.warnings }} {{ job.warnings === 1 ? 'warning' : 'warnings' }}
      </span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Progress } from 'frappe-ui'
import { formatNumber } from '@/api'
import type { Job } from '@/stores/jobs'

const props = defineProps<{ job: Job }>()

const headline = computed(() => {
  const verb = props.job.kind === 'export' ? 'Exporting to Tally' : 'Importing from Tally'
  if (props.job.status === 'Failed') return props.job.kind === 'export' ? 'Export failed' : 'Import failed'
  if (!props.job.running) return props.job.kind === 'export' ? 'Export complete' : 'Import complete'
  return verb
})

const barClass = computed(() => {
  if (props.job.status === 'Failed') return 'kx-progress--failed'
  if (!props.job.running) return 'kx-progress--done'
  return ''
})
</script>
