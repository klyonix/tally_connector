<template>
  <div>
    <PageHeader
      title="Mapping"
      subtitle="What an ERPNext document becomes in Tally, and where an amount lands when ERPNext has no account to name."
    >
      <template #actions>
        <Button variant="subtle" @click="$router.push({ name: 'Settings' })">Edit in Settings</Button>
      </template>
    </PageHeader>

    <ErrorMessage v-if="error" :message="error" class="mb-4" />

    <!-- Read-only on purpose. These values are edited in one place - Settings -
         because a second editor for the same field is how two screens come to
         disagree about which ledger the books land in. -->
    <section v-for="table in tables" :key="table.title" class="mb-5">
      <h2 class="mb-1 text-base font-semibold text-ink-gray-8">{{ table.title }}</h2>
      <p class="mb-2 text-p-sm text-ink-gray-5">{{ table.hint }}</p>
      <div class="overflow-x-auto rounded border border-outline-gray-2">
        <table class="w-full min-w-[36rem] text-p-sm">
          <thead class="bg-surface-gray-1 text-left text-p-xs uppercase tracking-wide text-ink-gray-5">
            <tr>
              <th class="px-3 py-2 font-medium">{{ table.left }}</th>
              <th class="px-3 py-2 font-medium">In Tally</th>
              <th class="px-3 py-2 font-medium">Status</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-outline-gray-1">
            <tr v-for="row in table.rows" :key="row.field" class="hover:bg-surface-gray-1">
              <td class="px-3 py-2 text-ink-gray-8">{{ row.label }}</td>
              <td class="px-3 py-2 font-mono text-p-xs"
                :class="row.value ? 'text-ink-gray-8' : 'text-ink-gray-4'">
                {{ row.value || row.fallback }}
              </td>
              <td class="px-3 py-2">
                <Badge v-if="row.value" theme="green" variant="subtle" label="Set" />
                <!-- A blank field is not an error: the code falls back to the
                     name Tally itself uses. Saying "Default" rather than
                     "Missing" is the difference between a page somebody acts on
                     and a page of red nobody trusts. -->
                <Badge v-else theme="gray" variant="subtle" label="Default" />
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="rounded border border-outline-gray-2 p-4">
      <h2 class="text-base font-semibold text-ink-gray-8">Tax ledgers</h2>
      <p class="mt-0.5 text-p-sm text-ink-gray-6">
        GST ledgers are not mapped by name. They are read from ERPNext's own GST account settings and
        placed under <b>Duties &amp; Taxes</b> with a duty head, because that is the only place Tally
        reads them from when it builds a return. A GST ledger anywhere else imports cleanly and is
        missing from GSTR-1.
      </p>
      <Button class="mt-3" variant="subtle" @click="$router.push({ name: 'Validation' })">
        Check GST placement
      </Button>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Badge, Button, ErrorMessage } from 'frappe-ui'

import PageHeader from '@/components/PageHeader.vue'
import { api, errorMessage } from '@/api'

const data = ref<any>(null)
const error = ref('')

const VOUCHER_FALLBACKS: Record<string, string> = {
  sales_voucher_type: 'Sales',
  purchase_voucher_type: 'Purchase',
  receipt_voucher_type: 'Receipt',
  payment_voucher_type: 'Payment',
  journal_voucher_type: 'Journal',
  contra_voucher_type: 'Contra',
  credit_note_voucher_type: 'Credit Note',
  debit_note_voucher_type: 'Debit Note',
}

const LEDGER_FALLBACKS: Record<string, string> = {
  sundry_debtors_ledger: 'Sundry Debtors',
  sundry_creditors_ledger: 'Sundry Creditors',
  suspense_ledger: 'Suspense A/c',
  employee_ledger_group: 'Sundry Creditors',
  sales_ledger: 'Sales',
  purchase_ledger: 'Purchase',
  tds_deductible_ledger: 'TDS Receivable',
  gst_output_ledger: 'from ERPNext GST settings',
  gst_input_ledger: 'from ERPNext GST settings',
}

const tables = computed(() => {
  if (!data.value) return []
  const build = (keys: string[], fallbacks: Record<string, string>) =>
    keys.map((field) => ({
      field,
      label: data.value.fields[field]?.label || field,
      value: data.value.values[field],
      fallback: fallbacks[field] || '—',
    }))
  return [
    {
      title: 'Voucher types',
      hint: 'A voucher type Tally does not have is refused for every voucher on it.',
      left: 'ERPNext document',
      rows: build(data.value.groups.vouchers, VOUCHER_FALLBACKS),
    },
    {
      title: 'Ledgers and groups',
      hint: 'Where a party or an unmatched amount is placed when ERPNext names no account.',
      left: 'Purpose',
      rows: build(data.value.groups.ledgers, LEDGER_FALLBACKS),
    },
  ]
})

onMounted(async () => {
  try {
    data.value = await api('tally_connector.api.settings.get')
  } catch (e) {
    error.value = errorMessage(e)
  }
})
</script>
