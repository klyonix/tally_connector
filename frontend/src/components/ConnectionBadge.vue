<template>
  <button
    type="button"
    class="flex shrink-0 items-center gap-1.5 rounded-4 px-1.5 py-1 text-p-sm transition hover:bg-surface-gray-2"
    :title="title"
    :aria-label="label"
    @click="test"
  >
    <span class="size-1.5 shrink-0 rounded-full" :class="dot" />
    <!-- The dot carries the state on its own below a laptop; the word is the
         convenience, not the information. It still reaches a screen reader
         through `aria-label`, so nothing is lost by dropping it visually. -->
    <span class="hidden whitespace-nowrap text-ink-gray-7 xl:inline">{{ label }}</span>
  </button>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { appStore } from '@/stores/app'

/**
 * What we know about Tally, and a way to go and find out.
 *
 * "Not tested" is the honest resting state and it is what this shows until
 * somebody asks. Probing on mount would mean every page load waits on a TCP
 * timeout whenever the accounts PC is off, which is most evenings.
 */
const conn = computed(() => appStore.state.connection)

const label = computed(() => ({
  unknown: 'Tally: not tested',
  testing: 'Testing…',
  ok: 'Tally connected',
  down: 'Tally unreachable',
}[conn.value.state]))

const dot = computed(() => ({
  unknown: 'bg-surface-gray-4',
  testing: 'bg-surface-amber-3 animate-pulse',
  ok: 'bg-surface-green-3',
  down: 'bg-surface-red-3',
}[conn.value.state]))

const title = computed(() =>
  conn.value.message
    ? `${label.value} — ${conn.value.message}`
    : `${label.value}. Click to test the connection to Tally.`,
)

function test() {
  appStore.testConnection()
}
</script>
