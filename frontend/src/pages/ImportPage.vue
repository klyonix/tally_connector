<template>
	<div>
		<PageHeader
			title="Import from Tally"
			subtitle="Upload a Tally master or transaction XML file. Creates customers, suppliers and items, and writes bank clearance dates back."
		/>

		<ErrorMessage v-if="error" :message="error" class="mb-4" />

		<section
			class="rounded-4 border-2 border-dashed p-6 text-center transition"
			:class="dragging ? 'border-brand-400 bg-brand-50' : 'border-outline-gray-2'"
			@dragover.prevent="dragging = true"
			@dragleave.prevent="dragging = false"
			@drop.prevent="onDrop"
		>
			<LucideFileUp class="mx-auto size-7 text-ink-gray-4" />
			<p class="mt-2 text-base-medium text-ink-gray-8">Drop a Tally XML file here</p>
			<p class="mt-0.5 text-p-sm text-ink-gray-5">or choose one from your computer</p>
			<div class="mt-3 flex items-center justify-center gap-2">
				<!-- `hidden`, not `sr-only`. An `sr-only` input is exposed to assistive
             technology as an unlabelled file control in the tab order, which is
             the wrong statement twice for a control a button opens. -->
				<input
					ref="fileInput"
					type="file"
					accept=".xml,text/xml,application/xml"
					hidden
					@change="onPick"
				/>
				<Button variant="subtle" @click="fileInput?.click()">Choose file</Button>
			</div>
			<p v-if="file" class="mt-3 truncate font-mono text-p-xs text-ink-gray-6">
				{{ file.name }} · {{ (file.size / 1024).toFixed(0) }} KB
			</p>
		</section>

		<section v-if="file" class="mt-4 flex flex-wrap items-center gap-2">
			<Button :loading="uploading || validating" @click="validateFile">Validate file</Button>
			<Button
				variant="solid"
				theme="gray"
				:disabled="!checked || checked.success === false || checked.empty"
				:loading="starting"
				@click="startImport"
			>
				Start import
			</Button>
			<Button variant="ghost" @click="reset">Clear</Button>
		</section>

		<!-- Validate before anything is written. The old screen's only answer to a
         file Tally would never have produced was a stack trace, after it had
         already created half the customers in it. -->
		<section v-if="checked" class="mt-4">
			<div
				v-if="checked.success === false"
				class="rounded-4 border border-outline-red-3 bg-surface-red-1 p-3 text-p-sm text-ink-red-8"
			>
				<p class="font-medium">This file cannot be imported</p>
				<p class="mt-1">{{ checked.error }}</p>
			</div>
			<div
				v-else-if="checked.empty"
				class="rounded-4 border border-outline-amber-3 bg-surface-amber-1 p-3 text-p-sm text-ink-amber-8"
			>
				<p class="font-medium">This file parses but holds nothing to import</p>
				<p class="mt-1">
					No TALLYMESSAGE objects were found. Export it again from Tally with data
					selected.
				</p>
			</div>
			<div v-else class="rounded-4 border border-outline-gray-2 p-4">
				<div class="flex flex-wrap items-center justify-between gap-3">
					<p class="text-base-medium text-ink-gray-8">
						{{ formatNumber(checked.objects) }} objects ready to import
					</p>
					<Badge
						v-if="checked.report_name"
						theme="gray"
						variant="subtle"
						:label="checked.report_name"
					/>
				</div>
				<ul class="mt-3 flex flex-wrap gap-2">
					<li
						v-for="kind in checked.kinds"
						:key="kind.tag"
						class="rounded-4 border border-outline-gray-2 px-2 py-1 text-p-xs text-ink-gray-7"
					>
						<span class="font-medium">{{ kind.tag }}</span>
						<span class="kx-nums ml-1.5 text-ink-gray-5">{{
							formatNumber(kind.count)
						}}</span>
					</li>
				</ul>
			</div>
		</section>

		<section v-if="job" class="mt-5 rounded-4 border border-outline-gray-2 p-4">
			<JobProgress :job="job" />
			<div class="mt-3 flex flex-wrap gap-2">
				<Button size="sm" variant="subtle" @click="jobsStore.focus(job)"
					>View details</Button
				>
				<Button
					v-if="!job.running"
					size="sm"
					variant="ghost"
					@click="$router.push({ name: 'History' })"
				>
					Sync history
				</Button>
			</div>
		</section>

		<section class="mt-6">
			<h2 class="mb-2 text-base-semibold text-ink-gray-8">Recent imports</h2>
			<div
				v-if="recent.length"
				class="overflow-hidden rounded-4 border border-outline-gray-2"
			>
				<table class="w-full text-p-sm">
					<thead
						class="bg-surface-gray-1 text-left text-p-xs uppercase tracking-wide text-ink-gray-5"
					>
						<tr>
							<th class="px-3 py-2 font-medium">File</th>
							<th class="px-3 py-2 font-medium">Status</th>
							<th class="px-3 py-2 text-right font-medium">Records</th>
							<th class="px-3 py-2 text-right font-medium">When</th>
						</tr>
					</thead>
					<tbody class="divide-y divide-outline-gray-1">
						<tr
							v-for="row in recent"
							:key="row.name"
							class="cursor-pointer hover:bg-surface-gray-1"
							@click="jobsStore.track(row.name, 'import', true)"
						>
							<td class="truncate px-3 py-2 text-ink-gray-8">
								{{ row.title || row.name }}
							</td>
							<td class="px-3 py-2"><StatusBadge :status="row.status" /></td>
							<td class="kx-nums px-3 py-2 text-right text-ink-gray-7">
								{{ formatNumber(row.records) }}
							</td>
							<td class="px-3 py-2 text-right text-ink-gray-6">
								{{ formatWhen(row.finished_at || row.creation) }}
							</td>
						</tr>
					</tbody>
				</table>
			</div>
			<EmptyState
				v-else
				title="Nothing has been imported yet"
				description="Drop a Tally XML file above to bring masters and bank clearance dates back into ERPNext."
			/>
		</section>
	</div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Badge, Button, ErrorMessage, FileUploadHandler, toast } from 'frappe-ui'
import LucideFileUp from '~icons/lucide/file-up'

import EmptyState from '@/components/EmptyState.vue'
import JobProgress from '@/components/JobProgress.vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { api, errorMessage, formatNumber, formatWhen } from '@/api'
import { appStore } from '@/stores/app'
import { jobsStore } from '@/stores/jobs'

const app = appStore.state
const fileInput = ref<HTMLInputElement | null>(null)
const file = ref<File | null>(null)
const uploaded = ref<string>('')
const checked = ref<any>(null)
const recent = ref<any[]>([])

const dragging = ref(false)
const uploading = ref(false)
const validating = ref(false)
const starting = ref(false)
const error = ref('')
const jobName = ref('')

const job = computed(() => (jobName.value ? jobsStore.get(jobName.value, 'import') : null))

function reset() {
	file.value = null
	uploaded.value = ''
	checked.value = null
	error.value = ''
	if (fileInput.value) fileInput.value.value = ''
}

function accept(f: File | undefined | null) {
	if (!f) return
	if (!/\.xml$/i.test(f.name)) {
		error.value = 'That is not an XML file. Tally exports masters and vouchers as .xml.'
		return
	}
	error.value = ''
	checked.value = null
	uploaded.value = ''
	file.value = f
}

function onDrop(e: DragEvent) {
	dragging.value = false
	accept(e.dataTransfer?.files?.[0])
}

function onPick(e: Event) {
	accept((e.target as HTMLInputElement).files?.[0])
}

/**
 * Upload once, then act on the stored file.
 *
 * The old page read the whole file into a string and posted it as a JSON
 * argument, which for a Tally export of any size is a request nothing survives.
 * Frappe's own upload endpoint streams it, and both validate and import then
 * work from a `File` the server already holds.
 */
async function upload() {
	if (uploaded.value) return uploaded.value
	if (!file.value) return ''
	uploading.value = true
	try {
		const handler = new FileUploadHandler()
		const doc = await handler.upload(file.value, { private: true, folder: 'Home/Attachments' })
		uploaded.value = doc.file_url
		return uploaded.value
	} finally {
		uploading.value = false
	}
}

async function validateFile() {
	error.value = ''
	validating.value = true
	try {
		const url = await upload()
		if (!url) return
		checked.value = await api('tally_connector.api.import_data.validate_file', {
			file_url: url,
		})
	} catch (e) {
		error.value = errorMessage(e)
	} finally {
		validating.value = false
	}
}

async function startImport() {
	error.value = ''
	starting.value = true
	try {
		const url = await upload()
		const r = await api('tally_connector.api.import_data.start_import', {
			file_url: url,
			company: app.company,
		})
		if (!r.success) {
			error.value = r.error || 'The import did not start.'
			return
		}
		jobName.value = r.log
		await jobsStore.track(r.log, 'import', false)
		toast.info('Import running. This continues in the background - you can leave this page.')
	} catch (e) {
		error.value = errorMessage(e)
	} finally {
		starting.value = false
	}
}

async function loadRecent() {
	try {
		recent.value = await api('tally_connector.api.jobs.history', { kind: 'import', limit: 8 })
	} catch (e) {
		error.value = errorMessage(e)
	}
}

onMounted(loadRecent)
</script>
