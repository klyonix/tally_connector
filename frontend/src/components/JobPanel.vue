<template>
  <Dialog
    :open="!!job"
    @update:open="(v: boolean) => !v && close()"
    size="xl"
    bare
  >
    <template #default>
      <div v-if="job" class="p-5">
        <div class="flex items-start justify-between gap-4">
          <div class="min-w-0">
            <Dialog.Title as="h2" class="text-lg-semibold text-ink-gray-9">
              {{ job.kind === 'export' ? 'Export' : 'Import' }} · {{ job.name }}
            </Dialog.Title>
            <p class="mt-0.5 text-p-sm text-ink-gray-6">
              {{ job.company || '—' }}
              <template v-if="job.from_date"> · {{ job.from_date }} to {{ job.to_date }}</template>
            </p>
          </div>
          <div class="flex items-center gap-2">
            <StatusBadge :status="job.status" />
            <Button variant="ghost" @click="close()">
              <template #icon><LucideX class="size-4" /></template>
            </Button>
          </div>
        </div>

        <div class="mt-5 rounded-4 border border-outline-gray-2 p-4">
          <JobProgress :job="job" />
        </div>

        <div class="mt-4 grid gap-4 md:grid-cols-[minmax(0,1fr)_14rem]">
          <!-- Outcome first, because on a finished job that is the whole
               question; on a running one it is empty and the stages carry it. -->
          <div class="order-2 md:order-1">
            <dl v-if="!job.running" class="grid grid-cols-2 gap-x-5 gap-y-3">
              <div v-for="stat in stats" :key="stat.label" class="min-w-0">
                <dt class="truncate text-p-xs uppercase tracking-wide text-ink-gray-5"
                  :title="stat.hint || stat.label">
                  {{ stat.label }}
                </dt>
                <dd class="kx-nums mt-0.5 text-base-semibold" :class="stat.tone">
                  {{ stat.value }}
                </dd>
                <dd v-if="stat.hint" class="text-p-xs text-ink-gray-5">
                  {{ stat.hint }}
                </dd>
              </div>
            </dl>

            <!-- What the warnings were. A count on its own tells somebody
                 there is a problem and nothing about it, which is exactly what
                 "2 warnings" did. -->
            <div v-if="job.findings?.length" class="mt-4">
              <p class="mb-1.5 text-p-xs uppercase tracking-wide text-ink-gray-5">
                What the validator found
              </p>
              <FindingsTable :groups="job.findings" quiet />
              <p class="mt-1.5 text-p-xs text-ink-gray-5">
                The file was still written. A finding that would stop the import shows the job as
                Partial and says so above.
              </p>
            </div>

            <div
              v-if="job.error"
              class="mt-4 rounded-4 border border-outline-red-3 bg-surface-red-1 p-3 text-p-sm text-ink-red-8"
            >
              <p class="font-medium">This job failed</p>
              <p class="mt-1 whitespace-pre-wrap font-mono text-p-xs">{{ job.error }}</p>
            </div>

            <div v-if="job.files?.length" class="mt-4">
              <p class="text-p-xs uppercase tracking-wide text-ink-gray-5">Files</p>
              <ul class="mt-2 flex flex-col gap-1">
                <li
                  v-for="file in job.files"
                  :key="file.file_url"
                  class="flex items-center justify-between gap-3 rounded-4 border border-outline-gray-2 px-3 py-1.5"
                >
                  <span class="truncate font-mono text-p-xs text-ink-gray-7">{{ file.file_name }}</span>
                  <Button size="sm" variant="subtle" @click="downloadUrl(file.file_url)">
                    Download
                  </Button>
                </li>
              </ul>
              <p class="mt-1.5 text-p-xs text-ink-gray-5">
                Import <b>01_masters.xml</b> into Tally before <b>02_vouchers.xml</b>. A voucher
                naming a ledger Tally has not yet seen is refused.
              </p>
            </div>
          </div>

          <div class="order-1 md:order-2">
            <p class="mb-1.5 text-p-xs uppercase tracking-wide text-ink-gray-5">Stages</p>
            <StageTrack
              :stages="job.stages"
              :current="job.stage"
              :reached="job.reached"
              :running="job.running"
              :failed="job.status === 'Failed'"
            />
          </div>
        </div>

        <div class="mt-5 flex flex-wrap items-center justify-between gap-2 border-t border-outline-gray-1 pt-4">
          <p class="text-p-xs text-ink-gray-5">
            <template v-if="job.running">
              This continues in the background. You can close this and keep working.
            </template>
            <template v-else-if="job.duration != null">
              Took {{ formatDuration(job.duration) }}.
            </template>
          </p>
          <div class="flex flex-wrap gap-2">
            <Button variant="subtle" @click="viewLogs">View logs</Button>
            <Button variant="subtle" @click="viewReport">View report</Button>
            <Button
              v-if="!job.running && job.status === 'Failed'"
              variant="solid"
              theme="gray"
              @click="retry"
            >
              Retry
            </Button>
            <Button variant="ghost" @click="close()">Close</Button>
          </div>
        </div>
      </div>
    </template>
  </Dialog>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { Button, Dialog } from 'frappe-ui'
import { downloadUrl, formatDuration, formatNumber } from '@/api'
import { jobsStore } from '@/stores/jobs'
import FindingsTable from './FindingsTable.vue'
import JobProgress from './JobProgress.vue'
import StageTrack from './StageTrack.vue'
import StatusBadge from './StatusBadge.vue'

const router = useRouter()
const job = jobsStore.focusedJob

const stats = computed(() => {
  const j = job.value
  if (!j) return []
  return [
    // Named "Objects" rather than "Successful", and it says what it counted.
    // It sat beside "616 / 616 processed" reading as a contradiction: 18,945 is
    // every master and voucher in the file, 616 is the vouchers the extract
    // ticked. Both true, neither explained.
    {
      label: 'Objects written',
      value: formatNumber(j.records),
      tone: 'text-ink-green-6',
      hint: 'masters + vouchers',
    },
    {
      label: 'Failed',
      value: formatNumber(j.failed),
      tone: j.failed ? 'text-ink-red-6' : 'text-ink-gray-7',
      hint: j.total ? `of ${formatNumber(j.total)} vouchers` : '',
    },
    {
      label: 'Warnings',
      value: formatNumber(j.warnings),
      tone: j.warnings ? 'text-ink-amber-6' : 'text-ink-gray-7',
    },
    { label: 'Duration', value: formatDuration(j.duration), tone: 'text-ink-gray-7' },
  ]
})

function close() {
  jobsStore.focus(null)
}

function viewReport() {
  close()
  router.push({ name: 'History', query: { job: job.value?.name } })
}

function viewLogs() {
  // The Desk list is the full record - every field, every version. Linking to it
  // rather than reimplementing it is the point of it existing.
  const doctype = job.value?.kind === 'export' ? 'tally-export-log' : 'tally-import-log'
  window.open(`/app/${doctype}/${job.value?.name}`, '_blank')
}

function retry() {
  close()
  router.push({ name: 'Export' })
}
</script>
