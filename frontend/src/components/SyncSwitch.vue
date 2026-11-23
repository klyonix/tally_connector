<template>
	<button
		type="button"
		role="switch"
		:aria-checked="on"
		class="flex shrink-0 items-center gap-2 rounded-4 px-1.5 py-1 transition hover:bg-surface-gray-2"
		:title="title"
		:disabled="busy"
		@click="toggle"
	>
		<span
			class="relative inline-flex h-[16px] w-[28px] shrink-0 items-center rounded-full transition-colors"
			:class="on ? 'bg-brand-500' : 'bg-surface-gray-4'"
		>
			<span
				class="inline-block size-3 rounded-full bg-white shadow transition-transform"
				:class="on ? 'translate-x-[13px]' : 'translate-x-[3px]'"
			/>
		</span>
		<span
			class="hidden whitespace-nowrap text-p-sm lg:inline"
			:class="on ? 'text-ink-gray-7' : 'font-medium text-ink-amber-8'"
		>
			{{ on ? 'Sync on' : 'Sync off' }}
		</span>
	</button>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { toast } from 'frappe-ui'
import { api, errorMessage } from '@/api'
import { appStore } from '@/stores/app'

/**
 * The master switch, at the top of every page.
 *
 * It stops the connector rather than hiding it: with this off the server
 * refuses every export and import, so a scheduled run and a stray API call are
 * stopped by the same rule the buttons obey. Putting it here is the point -
 * somebody watching a sync go wrong should not have to find a settings page to
 * stop it.
 */
const busy = ref(false)
const on = computed(() => appStore.state.syncEnabled)

const title = computed(() =>
	on.value
		? 'Sync is on. Click to stop every export and import.'
		: 'Sync is off. Nothing will be exported or imported until this is on.'
)

async function toggle() {
	busy.value = true
	const next = !on.value
	try {
		const r = await api('tally_connector.api.settings.set_sync_enabled', {
			enabled: next ? 1 : 0,
		})
		appStore.state.syncEnabled = !!r.sync_enabled
		toast[next ? 'success' : 'warning'](
			next ? 'Sync is on.' : 'Sync is off. Nothing will be exported or imported.'
		)
	} catch (e) {
		toast.error(errorMessage(e))
	} finally {
		busy.value = false
	}
}
</script>
