/**
 * The things every page needs: which company, which date range, and whether
 * Tally is reachable.
 *
 * The company lives here rather than on the export page because it is in the
 * header - and because getting it wrong is the defect this connector already
 * had once: with no selector the API took whichever company the database
 * returned first, so one set of books could be exported into another company's
 * Tally.
 */
import { reactive, computed } from 'vue'
import { api, errorMessage, monthEnd, monthStart } from '@/api'

const COMPANY_KEY = 'tally-connector:company'

const state = reactive({
  companies: [] as { name: string; abbr: string }[],
  company: '',
  user: { name: '', full_name: '' },
  syncEnabled: true,
  // The month showing now, start to end. A range that opens on something
  // somebody has to correct before every export is a default that costs more
  // than it saves.
  fromDate: monthStart(),
  toDate: monthEnd(),
  loaded: false,
  error: '',

  // Reported from the last time somebody asked, never probed on page load. A
  // dashboard that opens a socket to an office PC every time it renders is a
  // dashboard that hangs whenever that PC is switched off.
  connection: { state: 'unknown' as 'unknown' | 'testing' | 'ok' | 'down', message: '', at: '' },
})

export const appStore = {
  state,

  range: computed(() => ({ from_date: state.fromDate, to_date: state.toDate })),

  async load() {
    if (state.loaded) return
    try {
      const data = await api('tally_connector.api.dashboard.companies_with_defaults')
      state.companies = data.companies || []
      state.user = data.user || state.user
      state.syncEnabled = data.sync_enabled !== false
      const remembered = localStorage.getItem(COMPANY_KEY)
      const names = state.companies.map((c) => c.name)
      // A remembered company that no longer exists - or that this account may
      // no longer read - falls back rather than sending an unreadable name to
      // every endpoint on the page.
      state.company = (remembered && names.includes(remembered) && remembered)
        || data.default
        || names[0]
        || ''
      state.loaded = true
    } catch (e) {
      state.error = errorMessage(e)
    }
  },

  setCompany(name: string) {
    state.company = name
    try {
      localStorage.setItem(COMPANY_KEY, name)
    } catch {
      // A browser with site data blocked still has to work; the company is
      // simply chosen again next visit.
    }
  },

  async testConnection() {
    state.connection = { state: 'testing', message: '', at: '' }
    try {
      const r = await api('tally_connector.api.export.test_tally_connection')
      state.connection = {
        state: r.success ? 'ok' : 'down',
        message: r.success ? 'Tally answered' : r.response || 'Tally did not answer',
        at: new Date().toISOString(),
      }
    } catch (e) {
      state.connection = { state: 'down', message: errorMessage(e), at: new Date().toISOString() }
    }
    return state.connection
  },
}
