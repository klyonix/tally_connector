/**
 * Every export and import that is currently running, wherever it was started.
 *
 * This lives above the router on purpose. A job is not the property of the page
 * that launched it: navigating away, or reloading, or opening the app in a
 * second tab must not lose sight of work that is still writing files - which is
 * exactly what the old page did, because its only record of a running export
 * was a local `busy` flag and a polling loop inside one component.
 *
 * Two sources, and they answer different questions:
 *
 *   - the **socket** carries fine-grained updates for a job we already know
 *     about. It is the reason the bar moves smoothly rather than in ten-second
 *     steps.
 *   - the **poll** discovers jobs this browser did not start, and repairs
 *     anything a dropped connection lost. It is slow on purpose: once a second
 *     it would be a second implementation of the socket.
 *
 * Neither invents a number. Both read counts a worker actually reached.
 */
import { reactive, computed } from 'vue'
import { api, errorMessage } from '@/api'
import { onEvent, subscribeToDoc, unsubscribeFromDoc } from '@/socket'

export type Job = {
  name: string
  kind: 'export' | 'import'
  doctype: string
  title: string
  company: string
  status: string
  stage: string
  stages: string[]
  /** The stages this run actually entered, so the panel ticks what happened
   *  rather than everything before the current one. */
  reached?: string[]
  progress: number
  processed: number
  total: number
  warnings: number
  records: number
  failed: number
  from_date?: string | null
  to_date?: string | null
  started_at?: string | null
  finished_at?: string | null
  duration?: number | null
  error?: string
  /** What the validator found, grouped by check, as the server stored it. */
  findings?: {
    severity: string
    check: string
    count: number
    subject: string
    detail: string
    tally_says?: string
  }[]
  running: boolean
  /** Not finished, and not moving either - the worker never picked it up, or
   *  died. Counted apart from live work so the header stops claiming a sync is
   *  running when nothing is. */
  stalled?: boolean
  last_seen?: string | null
  files?: { file_name: string; file_url: string }[]
}

const TERMINAL = ['Success', 'Failed', 'Partial', 'Cancelled']

// Slow enough that it is plainly a safety net and not the mechanism. The socket
// does the work; this catches what the socket could not have known about.
const POLL_MS = 10000

const state = reactive({
  jobs: {} as Record<string, Job>,
  // The job whose details panel is open, if any.
  focused: null as string | null,
  started: false,
  error: '',
})

const subscribed = new Set<string>()
let timer: any = null

function key(job: { name: string; kind: string }) {
  return `${job.kind}:${job.name}`
}

function watch(job: Job) {
  const k = key(job)
  if (subscribed.has(k)) return
  subscribed.add(k)
  subscribeToDoc(job.doctype, job.name)
}

function unwatch(job: Job) {
  const k = key(job)
  if (!subscribed.has(k)) return
  subscribed.delete(k)
  unsubscribeFromDoc(job.doctype, job.name)
}

/** Merge a server row in, keeping the panel's focus and any file list we hold. */
function put(job: Job) {
  const k = key(job)
  const existing = state.jobs[k]
  state.jobs[k] = { ...existing, ...job, files: job.files ?? existing?.files }
  if (job.running) watch(state.jobs[k])
  else unwatch(state.jobs[k])
  return state.jobs[k]
}

export const jobsStore = {
  state,

  /** Every job genuinely in flight, newest first - what the header counts.
   *
   *  A stalled job is deliberately not here. Two queued exports whose worker
   *  never started them made the header read "2 Syncs Running · 0%" for ever,
   *  which is the dashboard asserting something false about work nobody was
   *  doing. They are listed separately and can be cancelled. */
  running: computed(() =>
    Object.values(state.jobs)
      .filter((j) => j.running && !j.stalled)
      .sort((a, b) => (b.started_at || '').localeCompare(a.started_at || '')),
  ),

  /** Jobs that finished while this page was open, so a completion is not lost
   *  the instant it happens. Cleared by the panel, never by a timer. */
  finished: computed(() =>
    Object.values(state.jobs)
      .filter((j) => !j.running)
      .sort((a, b) => (b.finished_at || '').localeCompare(a.finished_at || '')),
  ),

  /** One number for the header: the least-finished job, because that is the one
   *  somebody is waiting on. Averaging two jobs would produce a figure that
   *  describes neither. */
  headline: computed(() => {
    const live = Object.values(state.jobs).filter((j) => j.running && !j.stalled)
    if (!live.length) return null
    const slowest = live.reduce((a, b) => (a.progress <= b.progress ? a : b))
    return { count: live.length, progress: slowest.progress, job: slowest }
  }),

  /** In flight but silent. Shown apart, never counted as progress. */
  stalled: computed(() =>
    Object.values(state.jobs)
      .filter((j) => j.running && j.stalled)
      .sort((a, b) => (b.started_at || '').localeCompare(a.started_at || '')),
  ),

  focusedJob: computed(() => (state.focused ? state.jobs[state.focused] || null : null)),

  get(name: string, kind: 'export' | 'import' = 'export') {
    return state.jobs[`${kind}:${name}`] || null
  },

  focus(job: Job | null) {
    state.focused = job ? key(job) : null
  },

  /** Start watching. Idempotent - the shell calls it on mount and nothing else
   *  should have to think about whether it has been called. */
  start() {
    if (state.started) return
    state.started = true

    onEvent('tally_connector:job', (data: any) => {
      if (!data?.name) return
      const k = `${data.kind}:${data.name}`
      const existing = state.jobs[k]
      if (!existing) {
        // An event for a job we have never seen: ask for the whole row rather
        // than building a half one out of a progress payload.
        jobsStore.refreshOne(data.name, data.kind)
        return
      }
      Object.assign(existing, {
        stage: data.stage ?? existing.stage,
        progress: data.progress ?? existing.progress,
        processed: data.processed ?? existing.processed,
        total: data.total ?? existing.total,
        warnings: data.warnings ?? existing.warnings,
      })
      if (data.status && TERMINAL.includes(data.status)) {
        // The event says it is over; the row is fetched so the completion
        // summary is the server's figures and not an inference from a bar.
        jobsStore.refreshOne(data.name, data.kind)
      }
    })

    jobsStore.refresh()
    timer = setInterval(() => jobsStore.refresh(), POLL_MS)
    document.addEventListener('visibilitychange', jobsStore.onVisible)
  },

  stop() {
    if (timer) clearInterval(timer)
    timer = null
    state.started = false
    document.removeEventListener('visibilitychange', jobsStore.onVisible)
  },

  onVisible() {
    // A backgrounded tab has its timers throttled, so what it shows on return
    // can be minutes stale. Ask once, immediately.
    if (document.visibilityState === 'visible') jobsStore.refresh()
  },

  async refresh() {
    try {
      const rows: Job[] = await api('tally_connector.api.jobs.running')
      const live = new Set(rows.map((r) => key(r)))
      rows.forEach(put)
      // Anything we thought was running that the server no longer lists has
      // finished - fetch its outcome rather than leaving a bar at 64% for ever.
      for (const [k, job] of Object.entries(state.jobs)) {
        if (job.running && !live.has(k)) await jobsStore.refreshOne(job.name, job.kind)
      }
      state.error = ''
    } catch (e) {
      state.error = errorMessage(e)
    }
  },

  async refreshOne(name: string, kind: 'export' | 'import' = 'export') {
    try {
      const row: Job = await api('tally_connector.api.jobs.status', { name, kind })
      return put(row)
    } catch (e) {
      state.error = errorMessage(e)
      return null
    }
  },

  /** Adopt a job the current page has just started, so the header shows it
   *  before the next poll comes round. */
  async track(name: string, kind: 'export' | 'import' = 'export', focus = true) {
    const row = await jobsStore.refreshOne(name, kind)
    if (row && focus) jobsStore.focus(row)
    return row
  },

  /** Mark a stalled job cancelled on the server, so it stops being counted
   *  everywhere rather than only in this tab. */
  async cancel(job: Job) {
    const row = await api<Job>('tally_connector.api.jobs.cancel', {
      name: job.name, kind: job.kind,
    })
    return put(row)
  },

  dismiss(job: Job) {
    delete state.jobs[key(job)]
    if (state.focused === key(job)) state.focused = null
  },
}
