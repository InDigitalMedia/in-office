import { CSSProperties, Dispatch, SetStateAction, useEffect, useMemo, useState } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { WorkLocation, AdminEntry } from './types'
import { getEntries } from './api'
import { locationOrder, normalizeLocationFromApi, getLocationAccentColor, formatFriendlyDate } from './App'

const REMOTE: WorkLocation[] = ['WFH', 'Working From Abroad']
const AWAY: WorkLocation[] = ['Holiday', 'Other']

const LOCATION_CODE: Record<WorkLocation, string> = {
  'Neal Street': 'NS',
  WFH: 'WFH',
  'Client Office': 'CO',
  'Working From Abroad': 'ABR',
  Holiday: 'HOL',
  Other: 'OTH',
}

// Text colour that stays legible sitting on top of each location's accent fill
// (used by the heatmap cells). These are the same --loc-*-text pairings the
// location badges use elsewhere in the app, so they flip with the theme: every
// dark-theme accent is bright enough for black text, but the light theme's
// navy "Working From Abroad", magenta Holiday and grey Other accents need white.
const LOCATION_ON_ACCENT_TEXT: Record<WorkLocation, string> = {
  'Neal Street': 'var(--loc-office-text)',
  WFH: 'var(--loc-wfh-text)',
  'Client Office': 'var(--loc-client-text)',
  'Working From Abroad': 'var(--loc-abroad-text)',
  Holiday: 'var(--loc-off-text)',
  Other: 'var(--loc-other-text)',
}

const WEEKDAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']

// Recharts inline-styles the tooltip DOM node with light-theme defaults (white
// background, #ccc border), which CSS classes can't override -- so the tooltip
// chrome has to be themed through props. Axis ticks, grid lines, the hover
// cursor and legend labels are plain SVG/DOM and are themed in styles.css
// under `.admin-chart-card .recharts-*`.
const TOOLTIP_CONTENT_STYLE: CSSProperties = {
  background: 'var(--bg-surface)',
  border: '1px solid var(--border-input-color)',
  borderRadius: 4,
  boxShadow: 'var(--shadow-hover)',
  fontSize: 13,
}
const TOOLTIP_LABEL_STYLE: CSSProperties = {
  color: 'var(--text-heading)',
  fontWeight: 700,
  marginBottom: 2,
}
// Neutral rather than per-series colour: several accents are far too
// low-contrast as text on the light theme's white surface. Every tooltip row
// still names its location, so nothing is identified by colour alone.
const TOOLTIP_ITEM_STYLE: CSSProperties = { color: 'var(--text-primary)' }
const LEGEND_WRAPPER_STYLE: CSSProperties = { paddingTop: 4 }

// Mirrors App.tsx's `isMobile` (window.innerWidth < 768) for the handful of
// chart props recharts only accepts as numbers, not CSS.
function useIsNarrow(): boolean {
  const [isNarrow, setIsNarrow] = useState(() => typeof window !== 'undefined' && window.innerWidth < 768)
  useEffect(() => {
    const handleResize = () => setIsNarrow(window.innerWidth < 768)
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [])
  return isNarrow
}

// Entries with a time_period ('Morning'/'Afternoon') are half a working day.
function weightOf(entry: AdminEntry): number {
  return entry.time_period ? 0.5 : 1
}

// Monday of the ISO week containing dateStr, computed at UTC noon to dodge
// local-timezone/DST edge cases around midnight.
function mondayOf(dateStr: string): string {
  const [y, m, d] = dateStr.split('-').map(Number)
  const dt = new Date(Date.UTC(y, m - 1, d))
  const day = dt.getUTCDay()
  const diff = day === 0 ? -6 : 1 - day
  dt.setUTCDate(dt.getUTCDate() + diff)
  return dt.toISOString().split('T')[0]
}

function addDaysISO(dateStr: string, days: number): string {
  const [y, m, d] = dateStr.split('-').map(Number)
  const dt = new Date(Date.UTC(y, m - 1, d))
  dt.setUTCDate(dt.getUTCDate() + days)
  return dt.toISOString().split('T')[0]
}

function todayISO(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

function startOfMonthISO(dateStr: string): string {
  return `${dateStr.slice(0, 7)}-01`
}

// "Thursday, 6 Aug" — used in the by-day chart's tooltip so a hover shows the
// weekday, not just the date (the x-axis tick stays short).
function weekdayLabel(dateStr: string): string {
  const [y, m, d] = dateStr.split('-').map(Number)
  const dt = new Date(Date.UTC(y, m - 1, d))
  const weekday = dt.toLocaleDateString('en-GB', { weekday: 'long', timeZone: 'UTC' })
  return `${weekday}, ${formatFriendlyDate(dateStr)}`
}

function pct(part: number, total: number): string {
  if (total <= 0) return '0%'
  return `${Math.round((part / total) * 100)}%`
}

function SortArrow({ active, dir }: { active: boolean; dir: 'asc' | 'desc' }) {
  if (!active) return null
  return <span className="admin-sort-arrow">{dir === 'asc' ? '▲' : '▼'}</span>
}

function csvEscape(value: string): string {
  if (/[",\n]/.test(value)) {
    return `"${value.replace(/"/g, '""')}"`
  }
  return value
}

function downloadCSV(filename: string, rows: string[][]) {
  const csv = rows.map((row) => row.map(csvEscape).join(',')).join('\n')
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  URL.revokeObjectURL(url)
}

// Search-as-you-type multi-select checkbox list, mirroring the app's existing
// searchable-dropdown pattern (ClientSearchInput/"Your name" field in App.tsx).
function UserMultiSelect({
  allUsers,
  selected,
  onChange,
}: {
  allUsers: string[]
  selected: Set<string>
  onChange: Dispatch<SetStateAction<Set<string>>>
}) {
  const [searchTerm, setSearchTerm] = useState('')
  const [open, setOpen] = useState(false)

  const filtered = allUsers.filter((u) => u.toLowerCase().includes(searchTerm.toLowerCase()))

  function toggle(user: string) {
    const next = new Set(selected)
    if (next.has(user)) next.delete(user)
    else next.add(user)
    onChange(next)
  }

  return (
    <div className="admin-multiselect">
      <input
        id="admin-user-search"
        type="text"
        placeholder={selected.size === 0 ? 'All team members' : `${selected.size} selected`}
        value={searchTerm}
        onChange={(e) => setSearchTerm(e.target.value)}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 200)}
      />
      {selected.size > 0 && (
        <button type="button" className="admin-multiselect-clear" onClick={() => onChange(new Set())}>
          Clear
        </button>
      )}
      {open && (
        <div className="admin-multiselect-dropdown" role="listbox">
          {filtered.length === 0 && <div className="admin-multiselect-empty">No matches</div>}
          {filtered.map((user) => (
            <label key={user} className="admin-multiselect-option">
              <input type="checkbox" checked={selected.has(user)} onChange={() => toggle(user)} />
              {user}
            </label>
          ))}
        </div>
      )}
    </div>
  )
}

export default function AdminDashboard({ teamMembers }: { teamMembers: string[] }) {
  const isNarrow = useIsNarrow()
  const [rawEntries, setRawEntries] = useState<AdminEntry[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [selectedUsers, setSelectedUsers] = useState<Set<string>>(new Set())
  const [selectedLocations, setSelectedLocations] = useState<Set<WorkLocation>>(
    new Set(locationOrder as WorkLocation[])
  )
  const [heatmapUser, setHeatmapUser] = useState('')
  const [selectedClient, setSelectedClient] = useState('')

  // "All time" still means "don't go into the future" -- entries can exist
  // for weeks people have already pre-filled, so an unbounded upper date
  // pulled those in too. Capped at this week's Friday rather than today so
  // the current week's already-filled days stay visible.
  const today = todayISO()
  const thisMonday = mondayOf(today)
  const thisFriday = addDaysISO(thisMonday, 4)
  const lastMonday = addDaysISO(thisMonday, -7)
  const lastFriday = addDaysISO(lastMonday, 4)
  const thisMonthStart = startOfMonthISO(today)
  const lastMonthEnd = addDaysISO(thisMonthStart, -1)
  const lastMonthStart = startOfMonthISO(lastMonthEnd)

  const QUICK_FILTERS = [
    { label: 'This week', from: thisMonday, to: thisFriday },
    { label: 'Last week', from: lastMonday, to: lastFriday },
    { label: 'This month', from: thisMonthStart, to: today },
    { label: 'Last month', from: lastMonthStart, to: lastMonthEnd },
    { label: 'All time', from: '', to: '' },
  ]

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    getEntries(dateFrom || undefined, dateTo || thisFriday)
      .then((entries) => {
        if (!cancelled) setRawEntries(entries)
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load entries')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [dateFrom, dateTo])

  const usersInData = useMemo(() => {
    const names = new Set(rawEntries.map((e) => e.user_name))
    teamMembers.forEach((m) => names.add(m))
    return Array.from(names).sort((a, b) => a.localeCompare(b))
  }, [rawEntries, teamMembers])

  // Respects the date range (server-side), user filter and location filter.
  // Drives the charts, which are meant to zoom into whatever's checked above.
  const filteredEntries = useMemo(() => {
    return rawEntries.filter(
      (e) =>
        (selectedUsers.size === 0 || selectedUsers.has(e.user_name)) &&
        selectedLocations.has(normalizeLocationFromApi(e.location))
    )
  }, [rawEntries, selectedUsers, selectedLocations])

  // Respects date range + user filter only — the "who's where" breakdown is
  // meant to always show the full category split, regardless of which
  // categories are checked in the chart filter above.
  const tableEntries = useMemo(() => {
    return rawEntries.filter((e) => selectedUsers.size === 0 || selectedUsers.has(e.user_name))
  }, [rawEntries, selectedUsers])

  const kpis = useMemo(() => {
    let total = 0
    let nealStreet = 0
    let clientOffice = 0
    let remote = 0
    let away = 0
    for (const e of tableEntries) {
      const loc = normalizeLocationFromApi(e.location)
      const w = weightOf(e)
      total += w
      if (loc === 'Neal Street') nealStreet += w
      else if (loc === 'Client Office') clientOffice += w
      else if (REMOTE.includes(loc)) remote += w
      else if (AWAY.includes(loc)) away += w
    }
    return { total, nealStreet, clientOffice, remote, away }
  }, [tableEntries])

  // Highest-to-lowest ordering by total tracked days in the current filter --
  // computed once here and reused for every chart below ("Days by location",
  // the "Location mix by day" stack, and the weekly trend lines) so location
  // order and colour stay identical across every bar/day instead of being
  // resorted per data point.
  const locationOrderByCount = useMemo(() => {
    const totals = new Map<WorkLocation, number>()
    ;(locationOrder as WorkLocation[]).forEach((loc) => totals.set(loc, 0))
    for (const e of filteredEntries) {
      const loc = normalizeLocationFromApi(e.location)
      totals.set(loc, (totals.get(loc) || 0) + weightOf(e))
    }
    return (locationOrder as WorkLocation[]).slice().sort((a, b) => (totals.get(b) || 0) - (totals.get(a) || 0))
  }, [filteredEntries])

  const locationBreakdown = useMemo(() => {
    const totals = new Map<WorkLocation, number>()
    locationOrder.forEach((loc) => totals.set(loc as WorkLocation, 0))
    for (const e of filteredEntries) {
      const loc = normalizeLocationFromApi(e.location)
      totals.set(loc, (totals.get(loc) || 0) + weightOf(e))
    }
    const grandTotal = Array.from(totals.values()).reduce((a, b) => a + b, 0)
    return locationOrderByCount
      .filter((loc) => selectedLocations.has(loc))
      .map((loc) => ({
        location: loc,
        days: Math.round((totals.get(loc) || 0) * 10) / 10,
        pct: pct(totals.get(loc) || 0, grandTotal),
      }))
  }, [filteredEntries, selectedLocations, locationOrderByCount])

  // Stacked bar of location mix per calendar day. Capped to the most recent
  // 60 days of the filtered range so an "all time" view doesn't render
  // hundreds of bars — the weekly trend chart below covers the longer view.
  const byDayChart = useMemo(() => {
    const byDate = new Map<string, Record<string, number>>()
    for (const e of filteredEntries) {
      const loc = normalizeLocationFromApi(e.location)
      if (!byDate.has(e.date)) byDate.set(e.date, {})
      const row = byDate.get(e.date)!
      row[loc] = (row[loc] || 0) + weightOf(e)
    }
    const dates = Array.from(byDate.keys()).sort()
    const truncated = dates.length > 60
    const shown = truncated ? dates.slice(dates.length - 60) : dates
    const rows = shown.map((date) => ({
      date,
      label: formatFriendlyDate(date),
      fullLabel: weekdayLabel(date),
      ...byDate.get(date),
    }))
    return { rows, truncated, totalDays: dates.length }
  }, [filteredEntries])

  const weeklyTrendChart = useMemo(() => {
    const byWeek = new Map<string, Record<string, number>>()
    for (const e of filteredEntries) {
      const loc = normalizeLocationFromApi(e.location)
      const week = mondayOf(e.date)
      if (!byWeek.has(week)) byWeek.set(week, {})
      const row = byWeek.get(week)!
      row[loc] = (row[loc] || 0) + weightOf(e)
    }
    const weeks = Array.from(byWeek.keys()).sort()
    return weeks.map((week) => ({ week, label: formatFriendlyDate(week), ...byWeek.get(week) }))
  }, [filteredEntries])

  const whosWhereTable = useMemo(() => {
    const perUser = new Map<string, Record<WorkLocation, number>>()
    for (const e of tableEntries) {
      const loc = normalizeLocationFromApi(e.location)
      if (!perUser.has(e.user_name)) {
        perUser.set(e.user_name, {
          'Neal Street': 0,
          WFH: 0,
          'Client Office': 0,
          'Working From Abroad': 0,
          Holiday: 0,
          Other: 0,
        })
      }
      perUser.get(e.user_name)![loc] += weightOf(e)
    }
    return Array.from(perUser.entries())
      .map(([user, totals]) => {
        const total = Object.values(totals).reduce((a, b) => a + b, 0)
        return { user, totals, total }
      })
      .filter((row) => row.total > 0)
  }, [tableEntries])

  const [whoSort, setWhoSort] = useState<{ key: WorkLocation | 'user' | 'total'; dir: 'asc' | 'desc' }>({
    key: 'total',
    dir: 'desc',
  })

  function toggleWhoSort(key: WorkLocation | 'user' | 'total') {
    setWhoSort((prev) =>
      prev.key === key ? { key, dir: prev.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: key === 'user' ? 'asc' : 'desc' }
    )
  }

  const sortedWhosWhereTable = useMemo(() => {
    const { key, dir } = whoSort
    const sign = dir === 'asc' ? 1 : -1
    return whosWhereTable.slice().sort((a, b) => {
      const cmp = key === 'user' ? a.user.localeCompare(b.user) : key === 'total' ? a.total - b.total : a.totals[key] - b.totals[key]
      return cmp * sign
    })
  }, [whosWhereTable, whoSort])

  // Client Office visits, regardless of the location checkbox filter -- same
  // rationale as tableEntries above: drilling into one client shouldn't
  // depend on "Client Office" still being checked in the chart filter.
  const clientOfficeEntries = useMemo(
    () => tableEntries.filter((e) => normalizeLocationFromApi(e.location) === 'Client Office' && e.client),
    [tableEntries]
  )

  const clientsInData = useMemo(() => {
    const names = new Set(clientOfficeEntries.map((e) => e.client as string))
    return Array.from(names).sort((a, b) => a.localeCompare(b))
  }, [clientOfficeEntries])

  useEffect(() => {
    if (selectedClient && !clientsInData.includes(selectedClient)) setSelectedClient('')
  }, [clientsInData, selectedClient])

  const clientBreakdown = useMemo(() => {
    if (!selectedClient) return null
    const perUser = new Map<string, number>()
    clientOfficeEntries
      .filter((e) => e.client === selectedClient)
      .forEach((e) => perUser.set(e.user_name, (perUser.get(e.user_name) || 0) + weightOf(e)))
    const rows = Array.from(perUser.entries()).map(([user, days]) => ({ user, days }))
    const total = rows.reduce((sum, r) => sum + r.days, 0)
    return { rows, total }
  }, [clientOfficeEntries, selectedClient])

  const [clientSort, setClientSort] = useState<{ key: 'user' | 'days'; dir: 'asc' | 'desc' }>({
    key: 'days',
    dir: 'desc',
  })

  function toggleClientSort(key: 'user' | 'days') {
    setClientSort((prev) =>
      prev.key === key ? { key, dir: prev.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: key === 'user' ? 'asc' : 'desc' }
    )
  }

  const sortedClientRows = useMemo(() => {
    if (!clientBreakdown) return []
    const { key, dir } = clientSort
    const sign = dir === 'asc' ? 1 : -1
    return clientBreakdown.rows
      .slice()
      .sort((a, b) => (key === 'user' ? a.user.localeCompare(b.user) : a.days - b.days) * sign)
  }, [clientBreakdown, clientSort])

  const heatmapWeeks = useMemo(() => {
    if (!heatmapUser) return []
    const userEntries = rawEntries.filter((e) => e.user_name === heatmapUser)
    const byDate = new Map<string, AdminEntry[]>()
    userEntries.forEach((e) => {
      if (!byDate.has(e.date)) byDate.set(e.date, [])
      byDate.get(e.date)!.push(e)
    })
    const allDates = Array.from(byDate.keys()).sort()
    if (allDates.length === 0) return []
    const lastMonday = mondayOf(allDates[allDates.length - 1])
    const weeks: { weekLabel: string; days: { date: string; entries: AdminEntry[] }[] }[] = []
    for (let w = 0; w < 12; w++) {
      const monday = new Date(lastMonday + 'T00:00:00Z')
      monday.setUTCDate(monday.getUTCDate() - w * 7)
      const days = []
      for (let i = 0; i < 5; i++) {
        const d = new Date(monday)
        d.setUTCDate(d.getUTCDate() + i)
        const dateStr = d.toISOString().split('T')[0]
        days.push({ date: dateStr, entries: byDate.get(dateStr) || [] })
      }
      if (days.some((d) => d.entries.length > 0)) {
        weeks.push({ weekLabel: formatFriendlyDate(monday.toISOString().split('T')[0]), days })
      }
    }
    return weeks
  }, [rawEntries, heatmapUser])

  // With nothing to plot the three charts render as empty axis frames, which
  // reads as a broken page rather than "no data" -- so they swap for a note.
  const noChartData = filteredEntries.length === 0

  function toggleLocation(loc: WorkLocation) {
    const next = new Set(selectedLocations)
    if (next.has(loc)) next.delete(loc)
    else next.add(loc)
    setSelectedLocations(next)
  }

  function exportCSV() {
    const rows: string[][] = [['User', 'Date', 'Location', 'Time Period', 'Client', 'Notes']]
    filteredEntries
      .slice()
      .sort((a, b) => (a.date === b.date ? a.user_name.localeCompare(b.user_name) : a.date.localeCompare(b.date)))
      .forEach((e) => {
        rows.push([e.user_name, e.date, normalizeLocationFromApi(e.location), e.time_period || '', e.client || '', e.notes || ''])
      })
    const suffix = dateFrom || dateTo ? `_${dateFrom || 'start'}_to_${dateTo || 'now'}` : '_all-time'
    downloadCSV(`location-report${suffix}.csv`, rows)
  }

  return (
    <div className="admin-dashboard">
      <h2>Team Location Dashboard</h2>

      <div className="admin-filter-bar">
        {/* From / To / Team member / Location all sit at one label level so the
            headings and the controls beneath them line up across the bar. */}
        <div className="admin-filter-group admin-date-range">
          <div className="admin-date-field">
            <label className="admin-filter-label" htmlFor="admin-date-from">
              From
            </label>
            <input
              id="admin-date-from"
              type="date"
              value={dateFrom}
              max={thisFriday}
              onChange={(e) => setDateFrom(e.target.value)}
            />
          </div>
          <div className="admin-date-field">
            <label className="admin-filter-label" htmlFor="admin-date-to">
              To
            </label>
            <input
              id="admin-date-to"
              type="date"
              value={dateTo}
              max={thisFriday}
              onChange={(e) => setDateTo(e.target.value)}
            />
          </div>
        </div>
        <div className="admin-filter-group admin-quick-filters">
          <span className="admin-filter-label">Quick range</span>
          {QUICK_FILTERS.map((f) => {
            const active = dateFrom === f.from && dateTo === f.to
            return (
              <button
                key={f.label}
                type="button"
                className={`preset-btn${active ? ' preset-btn-highlight' : ''}`}
                onClick={() => {
                  setDateFrom(f.from)
                  setDateTo(f.to)
                }}
              >
                {f.label}
              </button>
            )
          })}
        </div>
        <div className="admin-filter-group">
          <label className="admin-filter-label" htmlFor="admin-user-search">
            Team member
          </label>
          <UserMultiSelect allUsers={usersInData} selected={selectedUsers} onChange={setSelectedUsers} />
        </div>
        <div className="admin-filter-group admin-location-checks">
          <span className="admin-filter-label">Location</span>
          {locationOrder.map((loc) => {
            const on = selectedLocations.has(loc as WorkLocation)
            return (
              <label
                key={loc}
                className={`admin-location-check${on ? '' : ' is-off'}`}
                style={on ? { borderColor: getLocationAccentColor(loc) } : undefined}
              >
                <input
                  type="checkbox"
                  checked={on}
                  onChange={() => toggleLocation(loc as WorkLocation)}
                />
                {loc}
              </label>
            )
          })}
        </div>
      </div>

      {error && <div className="error-message">{error}</div>}
      {loading && <p>Loading…</p>}

      {!loading && !error && (
        <>
          <div className="admin-kpi-row">
            <div className="admin-kpi-card">
              <div className="admin-kpi-value">{kpis.total.toFixed(1)}</div>
              <div className="admin-kpi-label">Total tracked days</div>
            </div>
            <div className="admin-kpi-card" style={{ borderLeftColor: 'var(--accent-office)' }}>
              <div className="admin-kpi-value">{pct(kpis.nealStreet, kpis.total)}</div>
              <div className="admin-kpi-label">Neal Street</div>
            </div>
            <div className="admin-kpi-card" style={{ borderLeftColor: 'var(--accent-client)' }}>
              <div className="admin-kpi-value">{pct(kpis.clientOffice, kpis.total)}</div>
              <div className="admin-kpi-label">Client Office</div>
            </div>
            <div className="admin-kpi-card" style={{ borderLeftColor: 'var(--accent-wfh)' }}>
              <div className="admin-kpi-value">{pct(kpis.remote, kpis.total)}</div>
              <div className="admin-kpi-label">Remote (WFH + Abroad)</div>
            </div>
            <div className="admin-kpi-card" style={{ borderLeftColor: 'var(--accent-holiday)' }}>
              <div className="admin-kpi-value">{pct(kpis.away, kpis.total)}</div>
              <div className="admin-kpi-label">Away (Holiday + Other)</div>
            </div>
          </div>

          <div className="admin-chart-card">
            <h3>Days by location</h3>
            {noChartData ? (
              <p className="admin-chart-empty">No entries match the filters above.</p>
            ) : (
            <ResponsiveContainer width="100%" height={260}>
              <BarChart data={locationBreakdown} layout="vertical" margin={{ left: 4, right: 16 }}>
                <CartesianGrid strokeDasharray="3 3" horizontal={false} />
                <XAxis type="number" />
                {/* Wide enough for "Working From Abroad" on one line on desktop;
                    on a phone it falls back to the narrower wrapped label. */}
                <YAxis type="category" dataKey="location" width={isNarrow ? 96 : 170} />
                <Tooltip
                  contentStyle={TOOLTIP_CONTENT_STYLE}
                  labelStyle={TOOLTIP_LABEL_STYLE}
                  itemStyle={TOOLTIP_ITEM_STYLE}
                  formatter={(value, _name, item) => {
                    const payload = item.payload as { location: string; pct: string }
                    return [`${value} days (${payload.pct})`, payload.location]
                  }}
                />
                <Bar dataKey="days" radius={[0, 4, 4, 0]}>
                  {locationBreakdown.map((row) => (
                    <Cell key={row.location} fill={getLocationAccentColor(row.location)} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
            )}
          </div>

          <div className="admin-chart-card">
            <h3>
              Location mix by day
              {byDayChart.truncated && (
                <span className="admin-chart-note"> — showing most recent 60 of {byDayChart.totalDays} days</span>
              )}
            </h3>
            {noChartData ? (
              <p className="admin-chart-empty">No entries match the filters above.</p>
            ) : (
            <ResponsiveContainer width="100%" height={280}>
              <BarChart data={byDayChart.rows} margin={{ top: 4, right: 16, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" vertical={false} />
                <XAxis dataKey="label" angle={-45} textAnchor="end" interval={Math.ceil(byDayChart.rows.length / (isNarrow ? 6 : 20))} height={60} />
                <YAxis width={36} />
                <Tooltip
                  contentStyle={TOOLTIP_CONTENT_STYLE}
                  labelStyle={TOOLTIP_LABEL_STYLE}
                  itemStyle={TOOLTIP_ITEM_STYLE}
                  labelFormatter={(value, payload) => {
                    const first = payload && payload[0]
                    return first ? (first.payload as { fullLabel: string }).fullLabel : value
                  }}
                />
                <Legend wrapperStyle={LEGEND_WRAPPER_STYLE} itemSorter={() => 0} />
                {locationOrderByCount
                  .filter((loc) => selectedLocations.has(loc))
                  .map((loc) => (
                    <Bar key={loc} dataKey={loc} stackId="day" name={loc} fill={getLocationAccentColor(loc)} />
                  ))}
              </BarChart>
            </ResponsiveContainer>
            )}
          </div>

          <div className="admin-chart-card">
            <h3>Weekly trend</h3>
            {noChartData ? (
              <p className="admin-chart-empty">No entries match the filters above.</p>
            ) : (
            <ResponsiveContainer width="100%" height={280}>
              <LineChart data={weeklyTrendChart} margin={{ top: 4, right: 24, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" vertical={false} />
                <XAxis dataKey="label" angle={-45} textAnchor="end" interval={Math.ceil(weeklyTrendChart.length / (isNarrow ? 5 : 15))} height={60} />
                <YAxis width={36} />
                <Tooltip
                  contentStyle={TOOLTIP_CONTENT_STYLE}
                  labelStyle={TOOLTIP_LABEL_STYLE}
                  itemStyle={TOOLTIP_ITEM_STYLE}
                />
                <Legend wrapperStyle={LEGEND_WRAPPER_STYLE} itemSorter={() => 0} />
                {locationOrderByCount
                  .filter((loc) => selectedLocations.has(loc))
                  .map((loc) => (
                    <Line key={loc} type="monotone" dataKey={loc} name={loc} stroke={getLocationAccentColor(loc)} strokeWidth={2} dot={false} />
                  ))}
              </LineChart>
            </ResponsiveContainer>
            )}
          </div>

          <div className="admin-chart-card">
            <div className="admin-table-header">
              <h3>Who's where, on average</h3>
              <button type="button" className="preset-btn" onClick={exportCSV}>
                Export CSV
              </button>
            </div>
            <p className="admin-chart-note">Full category breakdown per person — not affected by the location filter above.</p>
            <p className="admin-chart-note admin-scroll-hint">Scroll the table sideways to see every location.</p>
            <div className="admin-table-scroll">
              <table className="admin-who-table">
                <thead>
                  <tr>
                    <th className="admin-sort-th" onClick={() => toggleWhoSort('user')}>
                      Name
                      <SortArrow active={whoSort.key === 'user'} dir={whoSort.dir} />
                    </th>
                    {locationOrder.map((loc) => (
                      <th key={loc} className="admin-sort-th" onClick={() => toggleWhoSort(loc as WorkLocation)}>
                        {loc}
                        <SortArrow active={whoSort.key === loc} dir={whoSort.dir} />
                      </th>
                    ))}
                    <th className="admin-sort-th" onClick={() => toggleWhoSort('total')}>
                      Total days
                      <SortArrow active={whoSort.key === 'total'} dir={whoSort.dir} />
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {sortedWhosWhereTable.map((row) => (
                    <tr key={row.user}>
                      <td>{row.user}</td>
                      {locationOrder.map((loc) => {
                        const value = pct(row.totals[loc as WorkLocation], row.total)
                        // 0% still rendered, just dimmed, so the real numbers carry.
                        return (
                          <td key={loc} className={value === '0%' ? 'admin-zero' : undefined}>
                            {value}
                          </td>
                        )
                      })}
                      <td>{row.total.toFixed(1)}</td>
                    </tr>
                  ))}
                  {sortedWhosWhereTable.length === 0 && (
                    <tr>
                      <td className="admin-empty-row" colSpan={locationOrder.length + 2}>
                        No entries in this range.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

          <div className="admin-chart-card">
            <h3>Individual attendance</h3>
            <select
              className="admin-user-select"
              aria-label="Team member for attendance grid"
              value={heatmapUser}
              onChange={(e) => setHeatmapUser(e.target.value)}
            >
              <option value="">Choose a team member…</option>
              {usersInData.map((u) => (
                <option key={u} value={u}>
                  {u}
                </option>
              ))}
            </select>
            {heatmapUser && (
              <>
                <p className="admin-chart-note">Last 12 weeks of data for {heatmapUser}, regardless of the filters above.</p>
                {heatmapWeeks.length === 0 && <p className="admin-chart-note">No entries found.</p>}
                {heatmapWeeks.length > 0 && (
                  <div className="admin-heatmap">
                    {/* Weekday header — without it there's nothing saying which
                        column is which day. */}
                    <div className="admin-heatmap-head">
                      <span className="admin-heatmap-head-spacer" />
                      {WEEKDAY_LABELS.map((day) => (
                        <span key={day} className="admin-heatmap-head-day">
                          {day}
                        </span>
                      ))}
                    </div>
                    {heatmapWeeks.map((week) => (
                      <div key={week.weekLabel} className="admin-heatmap-row">
                        <span className="admin-heatmap-week-label">{week.weekLabel}</span>
                        {week.days.map((day) => (
                          <div key={day.date} className="admin-heatmap-cell-group" title={formatFriendlyDate(day.date)}>
                            {day.entries.length === 0 && <div className="admin-heatmap-cell admin-heatmap-empty">–</div>}
                            {day.entries.map((e) => {
                              const loc = normalizeLocationFromApi(e.location)
                              return (
                                <div
                                  key={e.id}
                                  className="admin-heatmap-cell"
                                  style={{
                                    background: getLocationAccentColor(loc),
                                    color: LOCATION_ON_ACCENT_TEXT[loc],
                                  }}
                                  title={`${e.time_period || 'Full day'}: ${loc}${e.client ? ` (${e.client})` : ''}`}
                                >
                                  {LOCATION_CODE[loc]}
                                </div>
                              )
                            })}
                          </div>
                        ))}
                      </div>
                    ))}
                  </div>
                )}
              </>
            )}
          </div>

          <div className="admin-chart-card">
            <h3>Client Office breakdown</h3>
            {clientsInData.length === 0 ? (
              <p className="admin-chart-note">No Client Office entries in the selected date range.</p>
            ) : (
              <>
                <select
                  className="admin-user-select"
                  aria-label="Client for office-visit breakdown"
                  value={selectedClient}
                  onChange={(e) => setSelectedClient(e.target.value)}
                >
                  <option value="">Choose a client…</option>
                  {clientsInData.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </select>
                {selectedClient && clientBreakdown && (
                  <>
                    <p className="admin-chart-note">
                      {clientBreakdown.total.toFixed(1)} team-days at {selectedClient} in the selected range,
                      regardless of the location filter above.
                    </p>
                    <div className="admin-table-scroll">
                      <table className="admin-who-table admin-client-table">
                        <thead>
                          <tr>
                            <th className="admin-sort-th" onClick={() => toggleClientSort('user')}>
                              Name
                              <SortArrow active={clientSort.key === 'user'} dir={clientSort.dir} />
                            </th>
                            <th className="admin-sort-th" onClick={() => toggleClientSort('days')}>
                              Days
                              <SortArrow active={clientSort.key === 'days'} dir={clientSort.dir} />
                            </th>
                          </tr>
                        </thead>
                        <tbody>
                          {sortedClientRows.map((r) => (
                            <tr key={r.user}>
                              <td>{r.user}</td>
                              <td>{r.days.toFixed(1)}</td>
                            </tr>
                          ))}
                          {sortedClientRows.length === 0 && (
                            <tr>
                              <td className="admin-empty-row" colSpan={2}>
                                No visits recorded.
                              </td>
                            </tr>
                          )}
                        </tbody>
                      </table>
                    </div>
                  </>
                )}
              </>
            )}
          </div>
        </>
      )}
    </div>
  )
}
