/**
 * One way to call the server, and one way to explain a failure.
 *
 * The old page had `message(e)` copied inline with a comment recording that a
 * gateway timeout arrives as `undefined` and threw while trying to read
 * `e.exc_type` - so a 504 reached the screen as "Cannot read properties of
 * undefined". That handling is worth keeping and worth having in one place.
 */
import { call } from 'frappe-ui'

export function errorMessage(e: any): string {
  // A request that never came back has no JSON body at all, so `e` is undefined
  // and every property read on it throws.
  if (!e) return 'The request did not come back. It may still be running - check Sync History.'
  if (Array.isArray(e.messages) && e.messages.length) return e.messages.join(' ')
  if (typeof e === 'string') return e
  return e.message || e.exc_type || 'The request failed.'
}

export async function api<T = any>(method: string, args?: Record<string, any>): Promise<T> {
  return (await call(method, args)) as T
}

/** Fire a browser download for a file the server has already written. */
export function downloadUrl(url: string) {
  const a = document.createElement('a')
  a.href = url
  a.download = ''
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
}

export function formatNumber(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—'
  return n.toLocaleString()
}

/** A duration in seconds, said the way somebody would say it. */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  if (seconds < 60) return `${Math.round(seconds)}s`
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  if (m < 60) return s ? `${m}m ${s}s` : `${m}m`
  return `${Math.floor(m / 60)}h ${m % 60}m`
}

/** A timestamp, short. Dates in this app are always the site's own timezone. */
export function formatWhen(value: string | null | undefined): string {
  if (!value) return '—'
  const d = new Date(value.replace(' ', 'T'))
  if (Number.isNaN(d.getTime())) return value
  const today = new Date()
  const sameDay = d.toDateString() === today.toDateString()
  const time = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
  if (sameDay) return `Today ${time}`
  return `${d.toLocaleDateString(undefined, { day: '2-digit', month: 'short' })} ${time}`
}

/**
 * A date as `YYYY-MM-DD` in the **local** calendar.
 *
 * Not `toISOString().split('T')[0]`, which is what this used to do and which
 * answers with the UTC date: at +05:30 that is the day before until half past
 * five in the morning, and `new Date(2026, 3, 1)` - the first of April - came
 * back as `2026-03-31`. A date field that is off by one is worse than one that
 * is empty, because nobody checks it.
 */
export function localDate(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

/** The first day of the month showing now. */
export function monthStart(): string {
  const now = new Date()
  return localDate(new Date(now.getFullYear(), now.getMonth(), 1))
}

/**
 * The last day of the month showing now.
 *
 * Day 0 of the next month is the last day of this one, so February and a leap
 * year are the calendar's problem rather than ours.
 */
export function monthEnd(): string {
  const now = new Date()
  return localDate(new Date(now.getFullYear(), now.getMonth() + 1, 0))
}

export function today(): string {
  return localDate(new Date())
}
