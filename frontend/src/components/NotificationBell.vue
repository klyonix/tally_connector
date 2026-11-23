<template>
	<!--
    `Popover`, not `Dropdown`. frappe-ui's Dropdown renders `options` rows and
    has no body slot at all, so a `#body` on it renders nothing and the panel
    never opens - which is exactly what happened, silently, until it was
    clicked. Popover is the component for arbitrary content.
  -->
	<Popover side="bottom" align="end" :offset="8" bare @open="load">
		<template #trigger>
			<button
				type="button"
				class="relative flex shrink-0 items-center rounded-4 p-1.5 transition hover:bg-surface-gray-2"
				:aria-label="unread ? `${unread} things need attention` : 'Notifications'"
			>
				<LucideBell class="size-4 text-ink-gray-6" />
				<!-- Only failures, warnings and stalls light it. A bell that rings for
             every successful export is a bell people stop looking at. -->
				<span
					v-if="unread"
					class="absolute -right-0.5 -top-0.5 grid min-w-[15px] place-items-center rounded-full bg-surface-red-3 px-1 text-[10px] font-semibold leading-[15px] text-white"
					>{{ unread > 9 ? '9+' : unread }}</span
				>
			</button>
		</template>
		<template #default="{ close }">
			<div
				class="w-[22rem] max-w-[90vw] rounded-6 border border-outline-gray-2 bg-surface-base p-1 shadow-2xl"
			>
				<div class="flex items-center justify-between px-2 py-1.5">
					<span class="text-p-xs-medium uppercase tracking-wide text-ink-gray-5">
						Notifications
					</span>
					<button
						v-if="unread"
						type="button"
						class="text-p-xs text-brand-700 hover:underline"
						@click="markAllRead"
					>
						Mark all read
					</button>
				</div>

				<ul v-if="items.length" class="max-h-[22rem] overflow-y-auto">
					<li v-for="item in items" :key="item.kind + item.name">
						<button
							type="button"
							class="flex w-full items-start gap-2.5 rounded-4 px-2 py-2 text-left transition hover:bg-surface-gray-2"
							@click="pick(item, close)"
						>
							<span
								class="mt-1.5 size-1.5 shrink-0 rounded-full"
								:class="dot(item.tone)"
							/>
							<span class="min-w-0 flex-1">
								<span class="flex items-baseline justify-between gap-2">
									<span class="truncate text-p-sm-medium text-ink-gray-8">
										{{ item.title }}
										<span class="font-normal text-ink-gray-5">
											· {{ item.kind === 'export' ? 'export' : 'import' }}
										</span>
									</span>
									<span class="shrink-0 text-p-xs text-ink-gray-5">{{
										formatWhen(item.at)
									}}</span>
								</span>
								<span class="mt-0.5 block truncate text-p-xs text-ink-gray-6">{{
									item.text
								}}</span>
							</span>
						</button>
					</li>
				</ul>
				<p v-else class="px-2 py-6 text-center text-p-sm text-ink-gray-5">
					Nothing yet. Exports and imports show up here.
				</p>
			</div>
		</template>
	</Popover>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Popover } from 'frappe-ui'
import LucideBell from '~icons/lucide/bell'
import { api, errorMessage, formatWhen } from '@/api'
import { jobsStore } from '@/stores/jobs'

/**
 * What has happened that somebody would want to know about.
 *
 * Read state is a timestamp in this browser rather than a row per user on the
 * server: the whole of what has to be remembered is "when did I last look", and
 * storing that server-side would be a write on every glance.
 */
const SEEN_KEY = 'tally-connector:notifications-seen'

const items = ref<any[]>([])
const unread = ref(0)

function seen(): string {
	try {
		return localStorage.getItem(SEEN_KEY) || ''
	} catch {
		// A browser with site data blocked still shows notifications; it simply
		// treats everything as unread, which is the safe direction.
		return ''
	}
}

async function load() {
	try {
		const r = await api('tally_connector.api.jobs.notifications', { since: seen() || null })
		items.value = r.items || []
		unread.value = r.unread || 0
	} catch (e) {
		// A bell that throws must not take the header with it.
		console.warn('[tally-connector] notifications unavailable', errorMessage(e))
	}
}

function markAllRead() {
	try {
		localStorage.setItem(SEEN_KEY, new Date().toISOString().replace('T', ' ').slice(0, 19))
	} catch {
		/* nothing to remember it with; the count simply stays */
	}
	unread.value = 0
}

function dot(tone: string) {
	return (
		{
			error: 'bg-surface-red-3',
			warning: 'bg-surface-amber-3',
			success: 'bg-surface-green-3',
			info: 'bg-surface-blue-3',
		}[tone] || 'bg-surface-gray-4'
	)
}

/** Close the panel, then open the job it names. */
function pick(item: any, close: () => void) {
	close()
	openJob(item)
}

function openJob(item: any) {
	jobsStore.track(item.name, item.kind, true)
}

onMounted(load)
// A job finishing is the thing worth telling somebody about, so the bell
// refreshes when one does rather than only on a timer.
watch(() => jobsStore.finished.value.length, load)
</script>
