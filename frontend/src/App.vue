<template>
  <FrappeUIProvider>
  <div class="flex h-screen w-screen overflow-hidden bg-surface-base text-ink-gray-8">
    <Sidebar v-model:collapsed="collapsed">
      <SidebarHeader
        :title="sidebarHeader.title"
        :subtitle="sidebarHeader.subtitle"
        :menu-items="sidebarHeader.menuItems"
      />
      <div class="flex-1 overflow-y-auto px-2 py-1">
        <template v-for="section in sections" :key="section.label || 'main'">
          <SidebarSection v-if="section.label" :label="section.label" collapsible>
            <SidebarItem
              v-for="entry in section.items"
              :key="entry.label"
              :label="entry.label"
              :icon="entry.icon"
              :route="entry.to"
              :active="entry.isActive"
            />
          </SidebarSection>
          <template v-else>
            <SidebarItem
              v-for="entry in section.items"
              :key="entry.label"
              :label="entry.label"
              :icon="entry.icon"
              :route="entry.to"
              :active="entry.isActive"
            />
          </template>
        </template>
      </div>
      <div class="px-2 pb-2">
        <SidebarItem
          v-if="runningCount"
          label="Sync running"
          :suffix="`${runningCount}`"
          @click="openRunning"
        >
          <template #prefix><LucideActivity class="size-4 text-brand-600" /></template>
        </SidebarItem>
        <SidebarCollapseToggle />
      </div>
    </Sidebar>

    <div class="flex min-w-0 flex-1 flex-col">
      <!--
        One flex row that shrinks in a fixed order. The breadcrumbs are the only
        thing allowed to give up width; everything on the right is `shrink-0`,
        because a control that is squeezed to half its label is worse than one
        that is not drawn. The first version let both sides shrink and they
        overlapped at anything under a laptop.
      -->
      <header
        class="flex h-12 shrink-0 items-center gap-2 border-b border-outline-gray-1 px-3 sm:px-4"
      >
        <Breadcrumbs :items="crumbs" class="min-w-0 flex-1 overflow-hidden" />

        <div class="flex shrink-0 items-center gap-1 sm:gap-2">
          <JobIndicator />
          <SyncSwitch />
          <ConnectionBadge />
          <NotificationBell />

          <Dropdown :options="userMenu" align="end">
            <button
              type="button"
              class="flex items-center gap-1 rounded-4 p-0.5 hover:bg-surface-gray-2"
              :aria-label="`Signed in as ${userName}`"
            >
              <Avatar :label="userName" size="md" />
              <LucideChevronDown class="size-3.5 text-ink-gray-5" />
            </button>
          </Dropdown>
        </div>
      </header>

      <main class="min-h-0 flex-1 overflow-y-auto">
        <div class="mx-auto w-full max-w-6xl px-3 py-4 sm:px-5 sm:py-5">
          <router-view v-slot="{ Component }">
            <component :is="Component" />
          </router-view>
        </div>
      </main>
    </div>

    <!-- Above the router, so a job stays visible whatever page is open. -->
    <JobPanel />
  </div>
  </FrappeUIProvider>
</template>

<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  Avatar,
  Breadcrumbs,
  call,
  Dropdown,
  FrappeUIProvider,
  Sidebar,
  SidebarCollapseToggle,
  SidebarHeader,
  SidebarItem,
  SidebarSection,
} from 'frappe-ui'

// Imported as components rather than named as strings: `SidebarItem` renders a
// string icon as literal text, so `icon: 'LucideUpload'` puts the words
// "LucideUpload" in the sidebar. Found by reading the component, which is
// cheaper than finding it on screen.
import LucideActivity from '~icons/lucide/activity'
import LucideArrowLeftRight from '~icons/lucide/arrow-left-right'
import LucideBanknote from '~icons/lucide/banknote'
import LucideBookOpen from '~icons/lucide/book-open'
import LucideBoxes from '~icons/lucide/boxes'
import LucideChevronDown from '~icons/lucide/chevron-down'
import LucideDownload from '~icons/lucide/download'
import LucideFileText from '~icons/lucide/file-text'
import LucideHistory from '~icons/lucide/history'
import LucideLandmark from '~icons/lucide/landmark'
import LucideLayoutDashboard from '~icons/lucide/layout-dashboard'
import LucideReceipt from '~icons/lucide/receipt'
import LucideRuler from '~icons/lucide/ruler'
import LucideSettings from '~icons/lucide/settings'
import LucideShieldAlert from '~icons/lucide/shield-alert'
import LucideShoppingCart from '~icons/lucide/shopping-cart'
import LucideUpload from '~icons/lucide/upload'
import LucideUsers from '~icons/lucide/users'
import LucideWallet from '~icons/lucide/wallet'

import ConnectionBadge from '@/components/ConnectionBadge.vue'
import NotificationBell from '@/components/NotificationBell.vue'
import SyncSwitch from '@/components/SyncSwitch.vue'
import JobIndicator from '@/components/JobIndicator.vue'
import JobPanel from '@/components/JobPanel.vue'
import { appStore } from '@/stores/app'
import { jobsStore } from '@/stores/jobs'

const route = useRoute()
const router = useRouter()
const app = appStore.state

/**
 * The sidebar's collapsed state, kept in step with what is actually on screen.
 *
 * frappe-ui's `Sidebar` narrows itself to a 48px rail when the viewport is below
 * `sm`, but it `provide`s the **model** rather than that effective state - so
 * every `SidebarItem` and `SidebarSection` inside still believes it is expanded
 * and renders a full-width label into a 48px box. That is why the rail showed
 * "Mast" and "Trans" clipped mid-word.
 *
 * Answering the same media query here and folding it into the model makes the
 * two agree. Widening the window restores whatever the person had chosen, and
 * on a narrow screen Expand stays inert - which is the component's own
 * behaviour, not something added here.
 */
const chosenCollapsed = ref(false)
const narrow = ref(false)
let media: MediaQueryList | null = null

function onWidthChange(e: MediaQueryListEvent | MediaQueryList) {
  narrow.value = e.matches
}

const collapsed = computed({
  get: () => chosenCollapsed.value || narrow.value,
  set: (value: boolean) => (chosenCollapsed.value = value),
})

const runningCount = computed(() => jobsStore.running.value.length)

// From the server, not from `window.frappe.boot` - this application has no
// boot dict, so that read was always undefined and every avatar showed an "A".
const userName = computed(() => app.user.full_name || app.user.name || 'Account')

/**
 * The company is the scope of every page, so it belongs where the application
 * says what it is rather than crowded into the top bar beside the breadcrumbs.
 * This is the slot CRM and Helpdesk use for their workspace switcher, and it has
 * room for a full company name - which the header select did not: it clipped
 * a long company name and then collided with the avatar.
 */
const sidebarHeader = computed(() => ({
  title: 'Tally Connector',
  subtitle: app.company || 'KlyONIX Tech',
  menuItems: [
    {
      group: app.companies.length > 1 ? 'Switch company' : 'Company',
      options: app.companies.map((c) => ({
        label: c.name,
        description: c.abbr,
        // A tick on the current one, so the dropdown says which books are open
        // rather than only offering to change them.
        selected: c.name === app.company,
        onClick: () => appStore.setCompany(c.name),
      })),
    },
    {
      group: 'App',
      hideLabel: true,
      options: [
        { label: 'Settings', icon: 'lucide-settings', onClick: () => router.push({ name: 'Settings' }) },
        {
          label: 'Open in Desk',
          icon: 'lucide-external-link',
          onClick: () => window.open('/app/tally-settings', '_blank'),
        },
      ],
    },
  ],
}))

/**
 * Masters before Transactions, because that is the order they must reach Tally:
 * a voucher naming a ledger Tally has not yet seen is refused. The sidebar is
 * the first place somebody learns that, so it is not sorted alphabetically.
 */
const sections = computed(() => [
  {
    items: [
      item('Overview', LucideLayoutDashboard, { name: 'Overview' }),
      item('Export to Tally', LucideUpload, { name: 'Export' }),
      item('Import from Tally', LucideDownload, { name: 'Import' }),
      item('Sync History', LucideHistory, { name: 'History' }),
      item('Validation & Errors', LucideShieldAlert, { name: 'Validation' }),
      item('Mapping', LucideArrowLeftRight, { name: 'Mapping' }),
      item('Settings', LucideSettings, { name: 'Settings' }),
    ],
  },
  {
    label: 'Masters',
    collapsible: true,
    items: [
      item('Units', LucideRuler, area('uoms')),
      item('Stock Items', LucideBoxes, area('stock_items')),
      item('Chart of Accounts', LucideBookOpen, area('chart_of_accounts')),
      item('Parties', LucideUsers, area('parties')),
    ],
  },
  {
    label: 'Transactions',
    collapsible: true,
    items: [
      item('Sales', LucideReceipt, area('sales_invoices')),
      item('Purchases', LucideShoppingCart, area('purchase_invoices')),
      item('Receipts', LucideWallet, area('payment_in')),
      item('Payments', LucideBanknote, area('payment_out')),
      item('Journals', LucideFileText, area('journal_entries')),
      item('Bank Transactions', LucideLandmark, area('bank_transactions')),
    ],
  },
])

/**
 * `SidebarItem` works out `active` by matching its route, and the ten area
 * entries share one route name, so it is passed explicitly. Matched on the resolved path rather
 * than on the route name, because the ten area entries all share one name and
 * differ only in their parameter - matching on the name would light all ten.
 */
function item(label: string, icon: any, to: any) {
  return { label, icon, to, isActive: router.resolve(to).path === route.path }
}

function area(key: string) {
  return { name: 'Area', params: { area: key } }
}

const crumbs = computed(() => {
  const items: { label: string; route?: any }[] = [
    { label: 'Tally Connector', route: { name: 'Overview' } },
  ]
  if (route.name !== 'Overview') {
    items.push({ label: (route.meta.title as string) || String(route.name) })
  }
  return items
})

const userMenu = computed(() => [
  {
    group: userName.value,
    options: [
      { label: 'Settings', icon: 'lucide-settings', onClick: () => router.push({ name: 'Settings' }) },
      {
        label: 'Open in Desk',
        icon: 'lucide-external-link',
        onClick: () => window.open('/app/tally-settings', '_blank'),
      },
      { label: 'Log out', icon: 'lucide-log-out', onClick: logOut },
    ],
  },
])

/** Frappe accepts a log out only by POST; a link to it is refused and leaves
 *  the person signed in. */
async function logOut() {
  try {
    await call('logout')
  } finally {
    window.location.href = '/login'
  }
}

function openRunning() {
  const first = jobsStore.running.value[0]
  if (first) jobsStore.focus(first)
}

onMounted(() => {
  media = window.matchMedia('(max-width: 639.9px)')
  narrow.value = media.matches
  media.addEventListener('change', onWidthChange)

  appStore.load()
  // Started here rather than on a page, so a job survives navigation and is
  // picked up again on a reload.
  jobsStore.start()
})

onBeforeUnmount(() => {
  media?.removeEventListener('change', onWidthChange)
  jobsStore.stop()
})
</script>
