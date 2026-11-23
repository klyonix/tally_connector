<template>
  <div>
    <PageHeader
      title="Export to Tally"
      subtitle="Masters first, then transactions. Tally refuses a voucher that names a ledger it has not yet seen."
    />

    <ErrorMessage v-if="error" :message="error" class="mb-4" />

    <!-- The flow, stated once at the top. It is a map rather than a wizard that
         locks you in: an accounts team re-runs one step far more often than it
         walks all five, and a stepper that forces the walk every time is a
         stepper people learn to fight. -->
    <ol class="mb-5 flex flex-wrap items-center gap-x-1 gap-y-2 text-p-sm">
      <li v-for="(step, i) in steps" :key="step.key" class="flex items-center gap-1">
        <span
          class="flex items-center gap-1.5 rounded-4 px-2 py-1"
          :class="step.done
            ? 'bg-brand-50 font-medium text-brand-700'
            : (step.current ? 'bg-surface-gray-2 font-medium text-ink-gray-8' : 'text-ink-gray-5')"
        >
          <LucideCheck v-if="step.done" class="size-3.5" />
          <span v-else class="kx-nums text-p-xs opacity-70">{{ i + 1 }}</span>
          {{ step.label }}
        </span>
        <LucideChevronRight v-if="i < steps.length - 1" class="size-3.5 text-ink-gray-4" />
      </li>
    </ol>

    <!-- No company control here. It is set once for the whole application in the
         sidebar, and a second editor for one value is how two screens come to
         disagree about which books are open. -->
    <section class="mb-5 rounded-4 border border-outline-gray-2 p-4">
      <div class="flex flex-wrap items-end gap-3">
        <div class="w-[9.5rem]">
          <FormControl type="date" label="From" v-model="app.fromDate" />
        </div>
        <div class="w-[9.5rem]">
          <FormControl type="date" label="To" v-model="app.toDate" />
        </div>
        <div class="ml-auto flex shrink-0 flex-nowrap items-center gap-2">
          <Button :loading="checking" @click="validate">Validate</Button>
          <!-- Two formats and one path behind them. Both are written from the
               same export, so they cannot disagree about what the books say;
               what differs is which of them Tally will take today. -->
          <Button variant="subtle" :loading="busy === 'all:xml'"
            @click="runFull(false, 'Tally XML')">
            Download XML
          </Button>
          <Button variant="subtle" :loading="busy === 'all:excel'"
            @click="runFull(false, 'Excel')">
            Download Excel
          </Button>
          <Button variant="solid" theme="gray" :loading="busy === 'all:push'" @click="runFull(true)">
            Push to Tally
          </Button>
        </div>
      </div>
      <p class="mt-2.5 text-p-xs text-ink-gray-5">
        Books: <b class="text-ink-gray-7">{{ app.company || 'no company chosen' }}</b>. The range
        applies to invoices, payments, journals and bank lines. Masters are not dated.
        <template v-if="areas?.total_vouchers != null">
          <b class="kx-nums text-ink-gray-7">{{ formatNumber(areas.total_vouchers) }}</b>
          vouchers in this range.
        </template>
      </p>
    </section>

    <section v-if="report" class="mb-5">
      <div class="mb-2 flex items-center justify-between gap-3">
        <h2 class="text-base-semibold text-ink-gray-8">Validation result</h2>
        <Button size="sm" variant="ghost" @click="report = null">Dismiss</Button>
      </div>
      <FindingsTable
        v-if="findings.length"
        :findings="findings"
        :blocking="report.would_import === false"
      />
      <div
        v-else
        class="rounded-4 border border-outline-green-3 bg-surface-green-1 p-3 text-p-sm text-ink-green-8"
      >
        <p class="font-medium">Nothing found.</p>
        <p class="mt-0.5 text-p-xs">
          Every ledger the vouchers reference is created, every voucher balances, and every value is
          one Tally can read.
        </p>
      </div>
    </section>

    <section class="mb-5">
      <div class="mb-2 flex items-baseline justify-between gap-3">
        <h2 class="text-base-semibold text-ink-gray-8">1 · Masters</h2>
        <p class="text-p-xs text-ink-gray-5">These must exist in Tally before any voucher lands.</p>
      </div>
      <AreaTable :rows="masters" :busy="busy" @push="run($event, true)" @download="run($event, false)" />
    </section>

    <section>
      <div class="mb-2 flex items-baseline justify-between gap-3">
        <h2 class="text-base-semibold text-ink-gray-8">2 · Transactions</h2>
        <p class="text-p-xs text-ink-gray-5">Vouchers for the range above.</p>
      </div>
      <AreaTable
        :rows="transactions"
        :busy="busy"
        @push="run($event, true)"
        @download="run($event, false)"
      />
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Button, ErrorMessage, FormControl, toast } from 'frappe-ui'
import LucideCheck from '~icons/lucide/check'
import LucideChevronRight from '~icons/lucide/chevron-right'

import AreaTable from '@/components/AreaTable.vue'
import FindingsTable from '@/components/FindingsTable.vue'
import PageHeader from '@/components/PageHeader.vue'
import { api, downloadUrl, errorMessage, formatNumber } from '@/api'
import { appStore } from '@/stores/app'
import { jobsStore } from '@/stores/jobs'

const route = useRoute()
const app = appStore.state

const areas = ref<any>(null)
const report = ref<any>(null)
const error = ref('')
const busy = ref('')
const checking = ref(false)

const masters = computed(() => areas.value?.rows.filter((r: any) => r.group === 'masters') || [])
const transactions = computed(
  () => areas.value?.rows.filter((r: any) => r.group === 'transactions') || [],
)
const findings = computed(() => report.value?.validation?.findings || [])

/**
 * Where this pass has got to.
 *
 * Cumulative on purpose: a step is only shown as done when every step before it
 * is done too. The first version ticked each condition independently, which on
 * a company whose masters went out last week but which had not been validated
 * today drew "1 ✓ 2 ○ 3 ✓ 4 ✓ 5 ✓" - a sequence with a hole in it, which reads
 * as a broken control rather than as a true statement about five facts. The
 * per-area truth is in the tables below, where a hole is meaningful.
 */
const steps = computed(() => {
  const validated = !!report.value
  const clean = validated && report.value.would_import !== false
  const anyMaster = masters.value.some((r: any) => r.last)
  const anyTxn = transactions.value.some((r: any) => r.last)

  const raw = [
    { key: 'range', label: 'Company & range', reached: !!app.company },
    { key: 'validate', label: 'Validate', reached: clean },
    { key: 'masters', label: 'Export masters', reached: anyMaster },
    { key: 'txn', label: 'Export transactions', reached: anyTxn },
    { key: 'done', label: 'Complete', reached: anyMaster && anyTxn && clean },
  ]

  let stillGoing = true
  return raw.map((step) => {
    const done = stillGoing && step.reached
    const current = stillGoing && !step.reached
    if (!step.reached) stillGoing = false
    return { ...step, done, current }
  })
})

function args() {
  return { company: app.company, from_date: app.fromDate, to_date: app.toDate }
}

async function loadAreas() {
  if (!app.company) return
  try {
    areas.value = await api('tally_connector.api.dashboard.areas', args())
  } catch (e) {
    error.value = errorMessage(e)
  }
}

async function validate() {
  checking.value = true
  error.value = ''
  try {
    report.value = await api('tally_connector.api.export.preflight', args())
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    checking.value = false
  }
}

async function runFull(push: boolean, format = 'Tally XML') {
  busy.value = push ? 'all:push' : (format === 'Excel' ? 'all:excel' : 'all:xml')
  error.value = ''
  try {
    const r = await api('tally_connector.api.export.export_all', {
      ...args(),
      push_to_tally_flag: push,
      export_format: format,
    })
    if (!r.success) {
      error.value = r.error || 'The export failed.'
      return
    }

    // Either way there is a log to watch, so the store takes it from here - and
    // the progress stays visible if this page is left. The old code held the
    // polling loop inside this component, which is why leaving it lost the job.
    if (r.log) await jobsStore.track(r.log, 'export', true)

    if (r.queued) {
      toast.info(r.message || 'Running in the background.')
      return
    }
    if (!push) {
      if (r.masters_url) downloadUrl(r.masters_url)
      if (r.vouchers_url) setTimeout(() => downloadUrl(r.vouchers_url), 600)
    }
    await loadAreas()
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    busy.value = ''
  }
}

async function run(row: any, push: boolean) {
  busy.value = `${row.key}:${push ? 'push' : 'xml'}`
  error.value = ''
  try {
    const r = await api(`tally_connector.api.export.export_${row.key}`, {
      ...args(),
      push_to_tally_flag: push,
    })
    if (!r.success) {
      error.value = r.error || 'The export failed.'
      return
    }
    if (!push && r.xml) {
      const blob = new Blob([r.xml], { type: 'application/xml' })
      const url = URL.createObjectURL(blob)
      downloadUrl(url)
      URL.revokeObjectURL(url)
    }
    toast.success(`${row.label} exported - ${formatNumber(r.records)} records.`)
    await loadAreas()
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    busy.value = ''
  }
}

onMounted(async () => {
  await appStore.load()
  await loadAreas()
  if (route.query.download) runFull(false)
})

watch(() => [app.company, app.fromDate, app.toDate], loadAreas)
</script>
