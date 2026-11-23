<template>
	<div>
		<!-- The identity sits behind the heading, where the page is quietest. -->
		<div class="relative mb-4 overflow-hidden rounded-4 border border-outline-gray-2 px-5 py-6">
			<BrandBackdrop />
			<div class="relative">
				<PageHeader title="Overview" :subtitle="subtitle" class="!pb-0">
					<template #actions>
						<Button variant="subtle" :loading="loading" @click="load">Refresh</Button>
						<Button
							variant="solid"
							theme="gray"
							:disabled="!app.syncEnabled"
							@click="$router.push({ name: 'Export' })"
						>
							Export to Tally
						</Button>
					</template>
				</PageHeader>
			</div>
		</div>

		<ErrorMessage v-if="error" :message="error" class="mb-4" />

		<!-- Off is a state somebody chose, so it is stated rather than left to be
         discovered when a button refuses. -->
		<div
			v-if="!app.syncEnabled"
			class="mb-4 rounded-4 border border-outline-amber-3 bg-surface-amber-1 p-3 text-p-sm text-ink-amber-8"
		>
			<b>Sync is off.</b> Nothing will be exported or imported until it is turned back on with
			the switch at the top.
		</div>

		<!-- A job that stopped reporting. Kept out of the running count, because a
         header saying "2 Syncs Running · 0%" about work nobody is doing is the
         dashboard telling a story. -->
		<section v-if="stalled.length" class="mb-5">
			<div
				v-for="job in stalled"
				:key="job.name"
				class="mb-2 flex flex-wrap items-center justify-between gap-3 rounded-4 border border-outline-amber-3 bg-surface-amber-1 p-3"
			>
				<div class="min-w-0">
					<p class="text-p-sm-medium text-ink-amber-8">
						{{ job.kind === 'export' ? 'Export' : 'Import' }} {{ job.name }} stopped
						reporting
					</p>
					<p class="text-p-xs text-ink-amber-8 opacity-80">
						Last seen at {{ job.stage }} · {{ formatWhen(job.last_seen) }}. Its worker
						is probably not running.
					</p>
				</div>
				<div class="flex shrink-0 gap-2">
					<Button size="sm" variant="subtle" @click="jobs.focus(job)">Details</Button>
					<Button
						size="sm"
						variant="solid"
						theme="gray"
						:loading="cancelling === job.name"
						@click="cancelJob(job)"
					>
						Cancel
					</Button>
				</div>
			</div>
		</section>

		<!-- A running job leads the page, because if one is running it is the only
         thing anybody opened this page to see. -->
		<section
			v-if="running.length"
			class="mb-5 rounded-4 border border-brand-200 bg-brand-50 p-4"
		>
			<div v-for="job in running" :key="job.name" class="flex flex-col gap-3">
				<JobProgress :job="job" />
				<div>
					<Button size="sm" variant="subtle" @click="jobs.focus(job)"
						>View details</Button
					>
				</div>
			</div>
		</section>

		<section class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
			<div
				v-for="tile in tiles"
				:key="tile.label"
				class="rounded-4 border border-outline-gray-2 p-3"
			>
				<p class="text-p-xs uppercase tracking-wide text-ink-gray-5">{{ tile.label }}</p>
				<p class="mt-1 truncate text-base-semibold text-ink-gray-9" :title="tile.value">
					{{ tile.value }}
				</p>
				<p v-if="tile.hint" class="mt-0.5 truncate text-p-xs text-ink-gray-5">
					{{ tile.hint }}
				</p>
			</div>
		</section>

		<section class="mt-5 grid gap-5 lg:grid-cols-[minmax(0,1fr)_20rem]">
			<div>
				<h2 class="mb-2 text-base-semibold text-ink-gray-8">Recent activity</h2>
				<div
					v-if="data?.recent?.length"
					class="overflow-hidden rounded-4 border border-outline-gray-2"
				>
					<table class="w-full text-p-sm">
						<thead
							class="bg-surface-gray-1 text-left text-p-xs uppercase tracking-wide text-ink-gray-5"
						>
							<tr>
								<th class="px-3 py-2 font-medium">Job</th>
								<th class="px-3 py-2 font-medium">Status</th>
								<th class="px-3 py-2 text-right font-medium">Records</th>
								<th class="px-3 py-2 text-right font-medium">When</th>
							</tr>
						</thead>
						<tbody class="divide-y divide-outline-gray-1">
							<tr
								v-for="row in data.recent"
								:key="row.name"
								class="cursor-pointer hover:bg-surface-gray-1"
								@click="open(row)"
							>
								<td class="px-3 py-2">
									<span class="font-medium text-ink-gray-8">{{ row.title }}</span>
									<span class="ml-1.5 font-mono text-p-xs text-ink-gray-5">{{
										row.name
									}}</span>
								</td>
								<td class="px-3 py-2"><StatusBadge :status="row.status" /></td>
								<td class="kx-nums px-3 py-2 text-right text-ink-gray-7">
									{{ formatNumber(row.records) }}
								</td>
								<td class="px-3 py-2 text-right text-ink-gray-6">
									{{ formatWhen(row.at) }}
								</td>
							</tr>
						</tbody>
					</table>
				</div>
				<EmptyState
					v-else-if="!loading"
					title="Nothing has been sent to Tally yet"
					description="Choose a date range and run an export. The masters go first, then the vouchers."
				>
					<template #action>
						<Button
							variant="solid"
							theme="gray"
							@click="$router.push({ name: 'Export' })"
						>
							Export to Tally
						</Button>
					</template>
				</EmptyState>
			</div>

			<div>
				<h2 class="mb-2 text-base-semibold text-ink-gray-8">Quick actions</h2>
				<div class="flex flex-col gap-2">
					<button
						v-for="action in actions"
						:key="action.label"
						type="button"
						class="flex items-start gap-3 rounded-4 border border-outline-gray-2 p-3 text-left transition hover:border-brand-300 hover:bg-brand-50"
						@click="action.run"
					>
						<component
							:is="action.icon"
							class="mt-0.5 size-4 shrink-0 text-brand-600"
						/>
						<span class="min-w-0">
							<span class="block text-p-sm-medium text-ink-gray-8">{{
								action.label
							}}</span>
							<span class="block text-p-xs text-ink-gray-5">{{ action.hint }}</span>
						</span>
					</button>
				</div>
			</div>
		</section>
	</div>
</template>

<script setup lang="ts">
import { computed, onMounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Button, ErrorMessage } from 'frappe-ui'
import LucideDownload from '~icons/lucide/download'
import LucideFileCode from '~icons/lucide/file-code'
import LucideShieldCheck from '~icons/lucide/shield-check'
import LucideUpload from '~icons/lucide/upload'

import BrandBackdrop from '@/components/BrandBackdrop.vue'
import EmptyState from '@/components/EmptyState.vue'
import JobProgress from '@/components/JobProgress.vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { api, errorMessage, formatDuration, formatNumber, formatWhen } from '@/api'
import { appStore } from '@/stores/app'
import { jobsStore } from '@/stores/jobs'
import { ref } from 'vue'

const router = useRouter()
const app = appStore.state
const jobs = jobsStore
const running = jobsStore.running
const stalled = jobsStore.stalled
const cancelling = ref('')

const data = ref<any>(null)
const loading = ref(false)
const error = ref('')

const subtitle = computed(() =>
	data.value ? `Connected books: ${data.value.company}` : 'ERPNext to Tally Prime 4.x'
)

const tiles = computed(() => {
	const d = data.value
	if (!d) return []
	const s = d.summary || {}
	return [
		{
			label: 'Tally company',
			value: d.tally_company || 'Not set',
			hint: d.host,
		},
		{
			label: 'Last sync',
			value: d.last_export ? formatWhen(d.last_export.at) : 'Never',
			hint: d.last_export
				? `${formatNumber(d.last_export.records)} records in ${formatDuration(
						d.last_export.duration
				  )}`
				: 'No export has finished yet',
		},
		{
			label: 'Successful',
			value: String(s.success ?? 0),
			hint: s.partial ? `${s.partial} partial` : 'Last 100 jobs',
		},
		{
			label: 'Failed',
			value: String(s.failed ?? 0),
			hint: d.auto_sync ? `Auto sync: ${d.auto_sync_interval}` : 'Auto sync off',
		},
	]
})

const actions = [
	{
		label: 'Export to Tally',
		hint: 'Masters, then transactions',
		icon: LucideUpload,
		run: () => router.push({ name: 'Export' }),
	},
	{
		label: 'Import from Tally',
		hint: 'Upload a Tally XML file',
		icon: LucideDownload,
		run: () => router.push({ name: 'Import' }),
	},
	{
		label: 'Validate data',
		hint: 'Check before Tally refuses it',
		icon: LucideShieldCheck,
		run: () => router.push({ name: 'Validation' }),
	},
	{
		label: 'Download XML',
		hint: 'Generate the two files without pushing',
		icon: LucideFileCode,
		run: () => router.push({ name: 'Export', query: { download: '1' } }),
	},
]

async function cancelJob(job: any) {
	cancelling.value = job.name
	try {
		await jobsStore.cancel(job)
	} catch (e) {
		error.value = errorMessage(e)
	} finally {
		cancelling.value = ''
	}
}

function open(row: any) {
	jobsStore.track(row.name, row.kind, true)
}

async function load() {
	if (!app.company) return
	loading.value = true
	error.value = ''
	try {
		data.value = await api('tally_connector.api.dashboard.overview', { company: app.company })
	} catch (e) {
		error.value = errorMessage(e)
	} finally {
		loading.value = false
	}
}

onMounted(load)
// The company lives in the header, so every page has to answer to it changing.
watch(() => app.company, load)
// A job finishing changes what this page says, and the store already knows.
watch(() => jobsStore.finished.value.length, load)
</script>
