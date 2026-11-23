<template>
  <div>
    <PageHeader :title="area?.label || 'Data'" :subtitle="area?.description">
      <template #actions>
        <Button variant="subtle" :loading="busy === 'xml'" @click="run(false)">Download XML</Button>
        <Button variant="solid" theme="gray" :loading="busy === 'push'" @click="run(true)">
          Push to Tally
        </Button>
      </template>
    </PageHeader>

    <ErrorMessage v-if="error" :message="error" class="mb-4" />

    <div v-if="area">
      <section class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <div class="rounded border border-outline-gray-2 p-3">
          <p class="text-p-xs uppercase tracking-wide text-ink-gray-5">Records</p>
          <p class="kx-nums mt-1 text-lg font-semibold text-ink-gray-9">
            {{ formatNumber(area.count) }}
          </p>
          <p class="mt-0.5 text-p-xs text-ink-gray-5">
            {{ area.dated ? `${app.fromDate} to ${app.toDate}` : 'Not affected by the date range' }}
          </p>
        </div>
        <div class="rounded border border-outline-gray-2 p-3">
          <p class="text-p-xs uppercase tracking-wide text-ink-gray-5">Last export</p>
          <p class="mt-1 text-base font-semibold text-ink-gray-9">
            {{ area.last ? formatWhen(area.last.at) : 'Never' }}
          </p>
          <p v-if="area.last" class="mt-0.5 text-p-xs text-ink-gray-5">
            {{ formatNumber(area.last.records) }} records
          </p>
        </div>
        <div class="rounded border border-outline-gray-2 p-3">
          <p class="text-p-xs uppercase tracking-wide text-ink-gray-5">Status</p>
          <div class="mt-1.5">
            <StatusBadge v-if="area.last" :status="area.last.status" />
            <span v-else class="text-base text-ink-gray-6">Not exported</span>
          </div>
        </div>
        <div class="rounded border border-outline-gray-2 p-3">
          <p class="text-p-xs uppercase tracking-wide text-ink-gray-5">In full export</p>
          <div class="mt-1.5">
            <Badge :theme="area.included ? 'green' : 'gray'" variant="subtle"
              :label="area.included ? 'Included' : 'Excluded'" />
          </div>
          <p v-if="!area.included" class="mt-1 text-p-xs text-ink-gray-5">
            Turn this back on in Settings.
          </p>
        </div>
      </section>

      <section v-if="area.group === 'masters'"
        class="mt-5 rounded border border-outline-gray-2 p-4 text-p-sm text-ink-gray-6">
        <p class="font-medium text-ink-gray-8">Masters go first</p>
        <p class="mt-1">
          Tally refuses a voucher that names a ledger, unit or stock item it has not yet seen, and it
          stops the whole file at the first line it cannot read. Sending this before the transactions
          is what makes the difference between one refused line and a refused import.
        </p>
      </section>

      <section v-else class="mt-5 rounded border border-outline-gray-2 p-4 text-p-sm text-ink-gray-6">
        <p class="font-medium text-ink-gray-8">Send the masters first</p>
        <p class="mt-1">
          These vouchers reference ledgers, units and stock items. If Tally does not already hold
          them, run the four master exports before this one.
        </p>
        <Button class="mt-3" size="sm" variant="subtle" @click="$router.push({ name: 'Export' })">
          Open the export workflow
        </Button>
      </section>
    </div>

    <EmptyState v-else-if="!loading" title="Unknown data type"
      description="Choose one from the sidebar." />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Badge, Button, ErrorMessage, toast } from 'frappe-ui'

import EmptyState from '@/components/EmptyState.vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { api, downloadUrl, errorMessage, formatNumber, formatWhen } from '@/api'
import { appStore } from '@/stores/app'

const route = useRoute()
const app = appStore.state

const rows = ref<any[]>([])
const loading = ref(false)
const busy = ref('')
const error = ref('')

// One page behind all ten sidebar entries. Ten near-identical pages is how four
// of them come to disagree about what a status chip means.
const area = computed(() => rows.value.find((r) => r.key === route.params.area) || null)

async function load() {
  if (!app.company) return
  loading.value = true
  try {
    const data = await api('tally_connector.api.dashboard.areas', {
      company: app.company,
      from_date: app.fromDate,
      to_date: app.toDate,
    })
    rows.value = data.rows
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    loading.value = false
  }
}

async function run(push: boolean) {
  if (!area.value) return
  busy.value = push ? 'push' : 'xml'
  error.value = ''
  try {
    const r = await api(`tally_connector.api.export.export_${area.value.key}`, {
      company: app.company,
      from_date: app.fromDate,
      to_date: app.toDate,
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
    toast.success(`${area.value.label} exported - ${formatNumber(r.records)} records.`)
    await load()
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    busy.value = ''
  }
}

onMounted(async () => {
  await appStore.load()
  await load()
})
watch(() => [route.params.area, app.company, app.fromDate, app.toDate], load)
</script>
