<template>
  <!-- Drawn only while something is running. A persistent chip that usually says
       "0 running" is furniture; one that appears is information. -->
  <button
    v-if="headline"
    type="button"
    class="flex shrink-0 items-center gap-1.5 rounded-4 border border-brand-200 bg-brand-50 py-1 pl-1.5 pr-2 text-p-sm-medium text-brand-700 transition hover:bg-brand-100"
    :title="title"
    @click="open"
  >
    <LoadingIndicator class="size-3 shrink-0" />
    <!-- The words go before the number does. Below a laptop the percentage is
         the part worth keeping, and a label squeezed to "1 Sync Runn…" beside a
         breadcrumb is what made these two collide in the first place. -->
    <span class="hidden whitespace-nowrap lg:inline">
      {{ headline.count }} {{ headline.count === 1 ? 'Sync' : 'Syncs' }} Running
    </span>
    <span class="hidden text-brand-300 lg:inline" aria-hidden="true">·</span>
    <span class="kx-nums tabular-nums">{{ headline.progress }}%</span>
  </button>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { LoadingIndicator } from 'frappe-ui'
import { jobsStore } from '@/stores/jobs'

const headline = jobsStore.headline

// What the label says when the label is not drawn, so the narrow state loses
// nothing a pointer cannot recover.
const title = computed(() =>
  headline.value
    ? `${headline.value.count} running · ${headline.value.job.stage} · ${headline.value.progress}%`
    : '',
)

function open() {
  if (headline.value) jobsStore.focus(headline.value.job)
}
</script>
