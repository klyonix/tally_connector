<template>
  <div>
    <div
      v-if="!quiet"
      class="rounded-4 border p-3 text-p-sm"
      :class="blocking
        ? 'border-outline-red-3 bg-surface-red-1 text-ink-red-8'
        : 'border-outline-amber-3 bg-surface-amber-1 text-ink-amber-8'"
    >
      <p class="font-medium">
        {{ blocking
          ? 'Tally would refuse this import.'
          : 'This would import, but the books would be wrong in places.' }}
      </p>
      <p class="mt-0.5 text-p-xs opacity-80">
        Grouped by check. {{ total }} findings across {{ grouped.length }}
        {{ grouped.length === 1 ? 'kind of problem' : 'kinds of problem' }}.
      </p>
    </div>

    <!-- Grouped rather than listed: 465 party lines missing a bill reference is
         one thing to fix, and a list of 465 is a list nobody reads. -->
    <!-- Stacked in a narrow column, tabular on a page. The table's minimum
         width clipped the subject and the detail inside the job panel - the
         half somebody actually needs - so `quiet` gets a list instead. -->
    <ul v-if="quiet" class="divide-y divide-outline-gray-1 rounded-4 border border-outline-gray-2">
      <li v-for="group in grouped" :key="group.key" class="px-3 py-2">
        <div class="flex flex-wrap items-baseline gap-x-2 gap-y-1">
          <Badge :theme="SEVERITY[group.severity]?.theme || 'gray'" variant="subtle"
            :label="SEVERITY[group.severity]?.label || group.severity" />
          <span class="text-p-sm-medium text-ink-gray-8">
            {{ CHECK_LABELS[group.check] || group.check }}
          </span>
          <span v-if="group.count > 1" class="kx-nums text-p-xs text-ink-gray-5">
            &times;{{ group.count }}
          </span>
        </div>
        <p class="mt-1 text-p-xs text-ink-gray-6">
          <span class="font-medium text-ink-gray-7">{{ group.example.subject }}</span>
          — {{ group.example.detail }}
        </p>
        <p v-if="group.example.tally_says" class="mt-0.5 text-p-xs text-ink-gray-5">
          Tally says: {{ group.example.tally_says }}
        </p>
      </li>
    </ul>

    <div v-else class="mt-3 overflow-x-auto rounded-4 border border-outline-gray-2">
      <table class="w-full min-w-[48rem] text-p-sm">
        <thead class="bg-surface-gray-1 text-left text-p-xs uppercase tracking-wide text-ink-gray-5">
          <tr>
            <th class="px-3 py-2 font-medium">Severity</th>
            <th class="px-3 py-2 font-medium">Check</th>
            <th class="px-3 py-2 text-right font-medium">Count</th>
            <th class="px-3 py-2 font-medium">Example</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-outline-gray-1">
          <tr v-for="group in grouped" :key="group.key" class="align-top">
            <td class="whitespace-nowrap px-3 py-2">
              <Badge :theme="SEVERITY[group.severity]?.theme || 'gray'" variant="subtle"
                :label="SEVERITY[group.severity]?.label || group.severity" />
            </td>
            <td class="whitespace-nowrap px-3 py-2 font-medium text-ink-gray-8">
              {{ CHECK_LABELS[group.check] || group.check }}
            </td>
            <td class="kx-nums px-3 py-2 text-right text-ink-gray-7">{{ group.count }}</td>
            <td class="px-3 py-2 text-ink-gray-7">
              <p>
                <!-- The subject is an ERPNext name wherever the validator knows
                     one, so it is a link back to the record somebody has to fix
                     rather than an identifier they must go and search for. -->
                <a
                  v-if="group.example.link"
                  :href="group.example.link"
                  target="_blank"
                  class="font-medium text-brand-700 underline decoration-brand-200 underline-offset-2"
                >{{ group.example.subject }}</a>
                <span v-else class="font-medium">{{ group.example.subject }}</span>
                <span class="text-ink-gray-6"> — {{ group.example.detail }}</span>
              </p>
              <p v-if="group.example.tally_says" class="mt-0.5 text-p-xs text-ink-gray-5">
                Tally says: {{ group.example.tally_says }}
              </p>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Badge } from 'frappe-ui'

const props = defineProps<{
  /** Raw findings, grouped here. */
  findings?: any[]
  /** Already grouped, by the server. The job panel stores the grouped shape on
   *  the log, because a row on a log is not the place for six hundred findings. */
  groups?: any[]
  blocking?: boolean
  /** A finished job's findings are history, not a verdict on what to do next. */
  quiet?: boolean
}>()

/**
 * The validator's three severities, said the way somebody deciding what to do
 * about them would say them. "aborts" and "rejects" are precise and mean
 * nothing to a person who has not read the validator.
 */
const SEVERITY: Record<string, { label: string; theme: string; rank: number }> = {
  aborts: { label: 'Stops the import', theme: 'red', rank: 0 },
  rejects: { label: 'Line refused', theme: 'orange', rank: 1 },
  wrong: { label: 'Books wrong', theme: 'amber', rank: 2 },
}

/**
 * The validator's check names, said the way somebody deciding what to do would
 * say them.
 *
 * **The keys are the validator's own strings**, which use spaces - `gst ledger`,
 * `invoice view`, `name collision`. The first version of this table guessed at
 * underscored names and eight of nineteen never matched anything, so those
 * findings silently rendered their raw check name. It read plausibly, which is
 * why nobody noticed. `test_dashboard_wiring` now fails the build on a check the
 * validator can emit and this table has no words for.
 */
const CHECK_LABELS: Record<string, string> = {
  amount: 'Amount Tally cannot read',
  balance: 'Voucher does not balance',
  'bill-wise': 'Bill reference',
  closure: 'Ledger no master creates',
  'cost centre': 'Cost centre allocation',
  currency: 'Currency Tally does not have',
  date: 'Date outside the books',
  duplicate: 'Defined more than once',
  'destination inventory': 'Tally company keeps no stock',
  godown: 'Godown not created',
  'master order': 'Master arrives before its parent',
  'master parent': 'Parent master is never created',
  'group cycle': 'Groups are each other\'s parent',
  'group parent': 'Group parent does not exist',
  'gst ledger': 'GST ledger misplaced',
  hsn: 'Missing HSN code',
  'item parent': 'Stock group is never created',
  'ledger parent': 'Ledger group does not exist',
  'registration type': 'GST registration Tally refuses',
  state: 'State Tally does not have',
  'stock ledger': 'Ledger does not affect inventory',
  'invoice view': 'Invoice view',
  'journal cash': 'Cash ledger in a journal',
  narration: 'Narration too long for Tally',
  name: 'Name Tally will not accept',
  'name collision': 'Two records with one name',
  party: 'Party ledger',
  're-import': 'Re-import would duplicate',
  'reserved name': 'Name Tally already owns',
  unit: 'Unit Tally will not accept',
  'unit symbol': 'Unit symbol Tally refuses',
  'voucher type': 'Voucher type missing in Tally',
}

const total = computed(() =>
  props.groups?.length
    ? props.groups.reduce((n: number, g: any) => n + (g.count || 0), 0)
    : (props.findings?.length || 0),
)

const grouped = computed(() => {
  // Already grouped by the server - used as it arrives, so the two callers
  // cannot disagree about what a group is.
  if (props.groups?.length) {
    return props.groups.map((g: any) => ({
      key: `${g.severity}/${g.check}`,
      severity: g.severity,
      check: g.check,
      count: g.count,
      example: { subject: g.subject, detail: g.detail, tally_says: g.tally_says, link: g.link },
    }))
  }
  const groups: Record<string, any> = {}
  for (const f of props.findings || []) {
    const key = `${f.severity}/${f.check}`
    if (!groups[key]) {
      groups[key] = { key, severity: f.severity, check: f.check, count: 0, example: f }
    }
    groups[key].count += 1
  }
  return Object.values(groups).sort(
    (a: any, b: any) => (SEVERITY[a.severity]?.rank ?? 9) - (SEVERITY[b.severity]?.rank ?? 9),
  )
})
</script>
