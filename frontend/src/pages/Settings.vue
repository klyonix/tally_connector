<template>
  <div>
    <PageHeader title="Settings" subtitle="Connection, mappings and what goes in a full export.">
      <template #actions>
        <Button variant="subtle" :loading="testing" @click="test">Test connection</Button>
        <Button variant="solid" theme="gray" :disabled="!dirty || !data?.can_write"
          :loading="saving" @click="save">
          Save changes
        </Button>
      </template>
    </PageHeader>

    <ErrorMessage v-if="error" :message="error" class="mb-4" />

    <div v-if="data && !data.can_write"
      class="mb-4 rounded border border-outline-amber-2 bg-surface-amber-1 p-3 text-p-sm text-ink-amber-4">
      You can read these settings but not change them. Running an export is clerical; changing which
      ledger the books land in is not.
    </div>

    <div v-if="data" class="flex flex-col gap-5">
      <section v-for="group in GROUPS" :key="group.key"
        class="rounded border border-outline-gray-2 p-4">
        <h2 class="text-base font-semibold text-ink-gray-8">{{ group.title }}</h2>
        <p class="mt-0.5 text-p-sm text-ink-gray-5">{{ group.hint }}</p>

        <div v-if="group.key === 'include'" class="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          <label v-for="name in data.groups[group.key]" :key="name"
            class="flex items-center gap-2 rounded border border-outline-gray-2 px-3 py-2 text-p-sm">
            <input type="checkbox" :checked="!!values[name]" :disabled="!data.can_write"
              class="rounded border-outline-gray-3 text-brand-600 focus:ring-brand-500"
              @change="set(name, ($event.target as HTMLInputElement).checked ? 1 : 0)" />
            <span class="text-ink-gray-7">{{ data.fields[name].label }}</span>
          </label>
        </div>

        <div v-else class="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          <FormControl
            v-for="name in data.groups[group.key]"
            :key="name"
            :type="controlType(name)"
            :label="data.fields[name].label"
            :options="selectOptions(name)"
            :modelValue="values[name]"
            :disabled="!data.can_write"
            :description="data.fields[name].description || undefined"
            @update:modelValue="(v: any) => set(name, v)"
          />
        </div>
      </section>
    </div>

    <div v-else-if="loading" class="py-10 text-center text-p-sm text-ink-gray-5">Loading settings…</div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Button, ErrorMessage, FormControl, toast } from 'frappe-ui'

import PageHeader from '@/components/PageHeader.vue'
import { api, errorMessage } from '@/api'
import { appStore } from '@/stores/app'

/**
 * The groups the server declares, given headings somebody reading them would
 * recognise. The field list itself comes from the server - including each
 * Select's options - so a dropdown here can never offer a value the DocType
 * would refuse.
 */
const GROUPS = [
  { key: 'connection', title: 'Tally connection',
    hint: 'Where Tally Prime is listening, and what the company is called there.' },
  { key: 'export', title: 'Export options',
    hint: 'Format, and what happens when Tally already holds what you are sending. Tally has no upsert.' },
  { key: 'include', title: 'What a full export includes',
    hint: 'Unticking one leaves it out of the complete export. The per-area buttons still work.' },
  { key: 'vouchers', title: 'Voucher mappings',
    hint: 'What each ERPNext document becomes in Tally. These must match the voucher types your company actually has.' },
  { key: 'ledgers', title: 'Ledger mappings',
    hint: 'Where amounts land when ERPNext has no account of its own to name.' },
  { key: 'sync', title: 'Scheduled sync',
    hint: 'Automatic pushes, and which side wins on bank reconciliation.' },
]

const data = ref<any>(null)
const values = ref<Record<string, any>>({})
const original = ref<Record<string, any>>({})
const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const error = ref('')

const dirty = computed(() =>
  Object.keys(values.value).some((k) => values.value[k] !== original.value[k]),
)

function controlType(name: string) {
  const t = data.value.fields[name].type
  if (t === 'Select') return 'select'
  if (t === 'Check') return 'checkbox'
  if (t === 'Date') return 'date'
  if (t === 'Int') return 'number'
  return 'text'
}

function selectOptions(name: string) {
  const options = data.value.fields[name].options
  return options ? options.map((o: string) => ({ label: o || '—', value: o })) : undefined
}

function set(name: string, value: any) {
  values.value = { ...values.value, [name]: value }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api('tally_connector.api.settings.get')
    values.value = { ...data.value.values }
    original.value = { ...data.value.values }
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    loading.value = false
  }
}

async function save() {
  saving.value = true
  error.value = ''
  try {
    // Only what changed. Sending the whole form back would rewrite every field
    // on a Single from whatever this screen last read, which quietly reverts
    // anything somebody changed in the Desk in between.
    const changed: Record<string, any> = {}
    for (const key of Object.keys(values.value)) {
      if (values.value[key] !== original.value[key]) changed[key] = values.value[key]
    }
    data.value = await api('tally_connector.api.settings.save', { values: changed })
    values.value = { ...data.value.values }
    original.value = { ...data.value.values }
    toast.success('Settings saved.')
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    saving.value = false
  }
}

async function test() {
  testing.value = true
  const result = await appStore.testConnection()
  testing.value = false
  if (result.state === 'ok') toast.success(`Tally answered. ${result.message}`)
  else toast.error(`Tally did not answer. ${result.message}`)
}

onMounted(load)
</script>
