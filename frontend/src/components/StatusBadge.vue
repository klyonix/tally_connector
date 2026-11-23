<template>
	<Badge :theme="theme" :label="label" variant="subtle" />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Badge } from 'frappe-ui'

/**
 * One place that decides what colour a status is.
 *
 * Declared as a table rather than derived from the string, because a status the
 * server adds tomorrow must render as grey - readable and plainly unrecognised
 * - instead of picking a colour by accident. An unfamiliar green is the one
 * thing a status chip must never be.
 */
const TONES: Record<string, string> = {
	Success: 'green',
	Completed: 'green',
	Partial: 'orange',
	Failed: 'red',
	Cancelled: 'gray',
	Running: 'blue',
	Pending: 'gray',
	Queued: 'gray',
}

const props = defineProps<{ status: string }>()
const theme = computed(() => TONES[props.status] || 'gray')
const label = computed(() => props.status || 'Unknown')
</script>
