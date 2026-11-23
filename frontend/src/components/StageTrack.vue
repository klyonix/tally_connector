<template>
	<ol class="flex flex-col gap-0.5">
		<li
			v-for="(stage, i) in stages"
			:key="stage"
			class="flex items-center gap-2 rounded-4 px-2 py-1 text-p-sm"
			:class="rowClass(i)"
		>
			<span class="flex size-4 shrink-0 items-center justify-center">
				<LucideCheck v-if="ran(stage, i)" class="size-3.5 text-ink-green-6" />
				<LoadingIndicator v-else-if="i === currentIndex && running" class="size-3" />
				<LucideX v-else-if="i === currentIndex && failed" class="size-3.5 text-ink-red-6" />
				<span v-else class="size-1.5 rounded-full bg-surface-gray-4" />
			</span>
			<span class="truncate">{{ stage }}</span>
		</li>
	</ol>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { LoadingIndicator } from 'frappe-ui'

/**
 * Where a job is, in the sequence the server declares.
 *
 * The stage list comes from the payload rather than being written here, so a
 * stage added on the server appears without a second edit - and a stage that is
 * never reached is drawn as pending rather than skipped, which is the
 * distinction somebody reading a stuck job actually needs.
 */
const props = defineProps<{
	stages: string[]
	current: string
	running: boolean
	failed?: boolean
	/** The stages this run actually entered, from the server. */
	reached?: string[]
}>()

const currentIndex = computed(() => {
	const i = props.stages.indexOf(props.current)
	// A stage the client does not know is drawn as "not started" rather than as
	// complete. Guessing forwards would tick off work that never happened.
	return i === -1 ? 0 : i
})

/**
 * Did this stage actually run?
 *
 * Answered from what the worker recorded, not from position. Ticking every
 * stage before the current one meant a finished download-only export showed a
 * green tick beside "Sending to Tally" - a stage that never ran, on a job that
 * never pushed. A progress display that claims work is worse than one that
 * leaves a gap.
 *
 * A log written before the server recorded this has no list, and there the old
 * behaviour is the best available answer - so it falls back rather than
 * showing a completed job with nothing ticked.
 */
function ran(stage: string, i: number) {
	if (props.reached?.length) return props.reached.includes(stage) && stage !== props.current
	return i < currentIndex.value
}

function rowClass(i: number) {
	if (i === currentIndex.value) return 'bg-brand-50 font-medium text-ink-gray-9'
	if (ran(props.stages[i], i)) return 'text-ink-gray-6'
	// Never reached. Dimmer than a completed stage and with no tick, so a stage
	// that was skipped reads differently from one that was done.
	return 'text-ink-gray-4'
}
</script>
