<template>
	<div>
		<PageHeader
			title="Validation & Errors"
			subtitle="What Tally would refuse, before Tally is the one telling you."
		>
			<template #actions>
				<Button variant="subtle" :loading="probing" @click="probeTally">Ask Tally</Button>
				<Button variant="solid" theme="gray" :loading="checking" @click="run"
					>Run validation</Button
				>
			</template>
		</PageHeader>

		<ErrorMessage v-if="error" :message="error" class="mb-4" />

		<section class="mb-4 rounded-4 border border-outline-gray-2 p-4">
			<div class="flex flex-wrap items-end gap-3">
				<div class="w-[9.5rem]">
					<FormControl type="date" label="From" v-model="app.fromDate" />
				</div>
				<div class="w-[9.5rem]">
					<FormControl type="date" label="To" v-model="app.toDate" />
				</div>
			</div>
			<p class="mt-2.5 text-p-xs text-ink-gray-5">
				Books: <b class="text-ink-gray-7">{{ app.company || 'no company chosen' }}</b
				>. This generates the export, checks it and throws the files away. Nothing is
				written to Tally and nothing is attached to a log.
			</p>
		</section>

		<!-- What the destination Tally is, where anybody has asked it. Two of the
         checks below need it, and they are skipped rather than guessed when
         Tally is not switched on. -->
		<section v-if="target" class="mb-4 rounded-4 border border-outline-gray-2 p-4">
			<div class="flex flex-wrap items-center gap-x-6 gap-y-2 text-p-sm">
				<div>
					<p class="text-p-xs uppercase tracking-wide text-ink-gray-5">Destination</p>
					<p class="font-medium text-ink-gray-8">{{ target.company || 'Not set' }}</p>
				</div>
				<div>
					<p class="text-p-xs uppercase tracking-wide text-ink-gray-5">Reachable</p>
					<p
						class="font-medium"
						:class="target.reachable ? 'text-ink-green-6' : 'text-ink-gray-6'"
					>
						{{ target.reachable ? 'Yes' : 'Not answering' }}
					</p>
				</div>
				<div v-if="target.books_from">
					<p class="text-p-xs uppercase tracking-wide text-ink-gray-5">Books from</p>
					<p class="font-medium text-ink-gray-8">{{ target.books_from }}</p>
				</div>
				<div v-if="target.voucher_types?.length">
					<p class="text-p-xs uppercase tracking-wide text-ink-gray-5">Voucher types</p>
					<p class="font-medium text-ink-gray-8">{{ target.voucher_types.length }}</p>
				</div>
			</div>
		</section>

		<section v-if="report">
			<div class="mb-3 grid gap-3 sm:grid-cols-4">
				<div
					v-for="tile in tiles"
					:key="tile.label"
					class="rounded-4 border border-outline-gray-2 p-3"
				>
					<p class="text-p-xs uppercase tracking-wide text-ink-gray-5">
						{{ tile.label }}
					</p>
					<p class="kx-nums mt-1 text-lg-semibold" :class="tile.tone">{{ tile.value }}</p>
				</div>
			</div>

			<FindingsTable
				v-if="findings.length"
				:findings="findings"
				:blocking="report.would_import === false"
			/>
			<div
				v-else
				class="rounded-4 border border-outline-green-3 bg-surface-green-1 p-4 text-p-sm text-ink-green-8"
			>
				<p class="font-medium">Nothing found.</p>
				<p class="mt-1">
					Every ledger the vouchers reference is created, every voucher balances, and
					every value is one Tally can read.
					{{ formatNumber(report.counts?.masters) }} masters and
					{{ formatNumber(report.counts?.vouchers) }} vouchers checked.
				</p>
			</div>

			<div v-if="report.unresolved_ledgers?.length" class="mt-4">
				<h2 class="mb-2 text-base-semibold text-ink-gray-8">Ledgers no master creates</h2>
				<p class="mb-2 text-p-sm text-ink-gray-6">
					Every one of these is a line Tally refuses. Zero is the only acceptable number.
				</p>
				<ul class="flex flex-wrap gap-1.5">
					<li
						v-for="name in report.unresolved_ledgers"
						:key="name"
						class="rounded-4 border border-outline-red-3 bg-surface-red-1 px-2 py-1 font-mono text-p-xs text-ink-red-8"
					>
						{{ name }}
					</li>
				</ul>
			</div>
		</section>

		<EmptyState
			v-else-if="!checking"
			title="Nothing checked yet"
			description="Run validation to generate the export, read it back and hold it against Tally's rules. It writes nothing."
		>
			<template #action>
				<Button variant="solid" theme="gray" :loading="checking" @click="run"
					>Run validation</Button
				>
			</template>
		</EmptyState>
	</div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { Button, ErrorMessage, FormControl } from 'frappe-ui'

import EmptyState from '@/components/EmptyState.vue'
import FindingsTable from '@/components/FindingsTable.vue'
import PageHeader from '@/components/PageHeader.vue'
import { api, errorMessage, formatNumber } from '@/api'
import { appStore } from '@/stores/app'

const app = appStore.state

const report = ref<any>(null)
const target = ref<any>(null)
const checking = ref(false)
const probing = ref(false)
const error = ref('')

const findings = computed(() => report.value?.validation?.findings || [])

const tiles = computed(() => {
	const v = report.value?.validation || {}
	const counts = v.counts || {}
	return [
		{
			label: 'Stops the import',
			value: formatNumber(counts.aborts ?? countBy('aborts')),
			tone: counts.aborts ?? countBy('aborts') ? 'text-ink-red-6' : 'text-ink-gray-7',
		},
		{
			label: 'Lines refused',
			value: formatNumber(counts.rejects ?? countBy('rejects')),
			tone: counts.rejects ?? countBy('rejects') ? 'text-ink-orange-3' : 'text-ink-gray-7',
		},
		{
			label: 'Books wrong',
			value: formatNumber(counts.wrong ?? countBy('wrong')),
			tone: counts.wrong ?? countBy('wrong') ? 'text-ink-amber-6' : 'text-ink-gray-7',
		},
		{
			label: 'Vouchers checked',
			value: formatNumber(report.value?.counts?.vouchers),
			tone: 'text-ink-gray-7',
		},
	]
})

function countBy(severity: string) {
	return findings.value.filter((f: any) => f.severity === severity).length
}

async function run() {
	checking.value = true
	error.value = ''
	try {
		report.value = await api('tally_connector.api.export.preflight', {
			company: app.company,
			from_date: app.fromDate,
			to_date: app.toDate,
		})
	} catch (e) {
		error.value = errorMessage(e)
	} finally {
		checking.value = false
	}
}

/** Asking Tally is a separate button because it is the only part of validation
 *  that needs Tally switched on - the whole point of the harness is that it does
 *  not have to be. */
async function probeTally() {
	probing.value = true
	error.value = ''
	try {
		target.value = await api('tally_connector.api.export.check_target_company')
	} catch (e) {
		error.value = errorMessage(e)
	} finally {
		probing.value = false
	}
}
</script>
