import React, { useState, useEffect, useMemo, useRef } from 'react'
import { WeekEntry, WorkLocation, SummaryRow, Entry, ExistingEntry } from './types'
import { saveWeek, getWeekSummary, checkExistingEntries, getUserEntriesForWeek, getUsersForWeek, getAllUsers, deleteUserWeek } from './api'
// Load team and client lists from public at runtime (no imports from root)

type ViewMode = 'fill' | 'dashboard' | 'edit'

function getMondayOfWeek(date: Date): Date {
  const day = date.getDay()
  const diff = date.getDate() - day + (day === 0 ? -6 : 1) // Adjust when day is Sunday
  return new Date(date.setDate(diff))
}

function formatDate(date: Date): string {
  return date.toISOString().split('T')[0]
}

function getDayName(date: Date): string {
  return date.toLocaleDateString('en-US', { weekday: 'long' })
}

function formatWeekRangeLabel(weekStart: Date): string {
  const start = new Date(weekStart)
  const end = new Date(weekStart)
  end.setDate(start.getDate() + 4)
  const startStr = start.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' })
  const endStr = end.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' })
  return `${startStr} → ${endStr}`
}

function formatFriendlyDate(dateStr: string): string {
  const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
  const [, month, day] = dateStr.split('-').map(Number)
  return `${day} ${months[month - 1]}`
}

function formatWeekHeading(weekStart: Date): string {
  return weekStart.toLocaleDateString('en-GB', { day: 'numeric', month: 'long', year: 'numeric' })
}

// Location normalization helpers to be compatible with both old and new APIs
function normalizeLocationFromApi(location: string): WorkLocation {
  switch (location) {
    case 'Office':
      return 'Neal Street'
    case 'Client':
      return 'Client Office'
    case 'Off':
    case 'PTO':
      return 'Holiday'
    case 'WFH':
      return 'WFH'
    case 'Neal Street':
    case 'Client Office':
    case 'Holiday':
    case 'Working From Abroad':
    case 'Other':
      return location as WorkLocation
    default:
      return location as WorkLocation
  }
}

function generateWeekEntries(weekStart: Date, isSplit: boolean = false): WeekEntry[] {
  const entries: WeekEntry[] = []
  // Only generate Monday-Friday (5 days)
  for (let i = 0; i < 5; i++) {
    const date = new Date(weekStart)
    date.setDate(weekStart.getDate() + i)
    const baseEntry: WeekEntry = {
        date: formatDate(date),
        dayName: getDayName(date),
        location: 'Neal Street' as WorkLocation,
        client: '',
        notes: '',
        isCustomClient: false,
    }
    if (isSplit) {
      baseEntry.morningLocation = 'Neal Street' as WorkLocation
      baseEntry.afternoonLocation = 'Neal Street' as WorkLocation
      baseEntry.morningClient = ''
      baseEntry.afternoonClient = ''
      baseEntry.morningNotes = ''
      baseEntry.afternoonNotes = ''
      baseEntry.morningIsCustomClient = false
      baseEntry.afternoonIsCustomClient = false
    }
    entries.push(baseEntry)
  }
  return entries
}

function groupEntriesByDateAndLocation(entries: SummaryRow[]): {
  [date: string]: {
    [location: string]: SummaryRow[]
  }
} {
  return entries.reduce(
    (groups, entry) => {
      if (!groups[entry.date]) {
        groups[entry.date] = {}
      }
      // Create location key with time period if present
      const locationKey = entry.time_period
        ? `${entry.location} (${entry.time_period})`
        : entry.location
      if (!groups[entry.date][locationKey]) {
        groups[entry.date][locationKey] = []
      }
      groups[entry.date][locationKey].push(entry)
      return groups
    },
    {} as { [date: string]: { [location: string]: SummaryRow[] } }
  )
}

function getLocationAccentColor(location: string): string {
  // Returns a CSS variable so the accent tracks the active theme (see styles.css).
  switch (location) {
    case 'Neal Street': return 'var(--accent-office)'
    case 'WFH': return 'var(--accent-wfh)'
    case 'Client Office': return 'var(--accent-client)'
    case 'Holiday': return 'var(--accent-holiday)'
    case 'Working From Abroad': return 'var(--accent-abroad)'
    case 'Other': return 'var(--accent-other)'
    default: return 'var(--accent-default)'
  }
}

// Location order for consistent display
const locationOrder = ['Neal Street', 'WFH', 'Client Office', 'Working From Abroad', 'Holiday', 'Other']

function getLocationBadgeClass(location: string): string {
  switch (location.toLowerCase()) {
    case 'neal street':
      return 'location-office'
    case 'wfh':
      return 'location-wfh'
    case 'client office':
      return 'location-client'
    case 'working from abroad':
      return 'location-abroad'
    case 'holiday':
      return 'location-off'
    case 'other':
      return 'location-other'
    default:
      return ''
  }
}

type Theme = 'light' | 'dark'

function getInitialTheme(): Theme {
  if (typeof window === 'undefined') return 'light'
  const stored = localStorage.getItem('workTrackerTheme')
  if (stored === 'light' || stored === 'dark') return stored
  if (typeof window.matchMedia === 'function' && window.matchMedia('(prefers-color-scheme: dark)').matches) {
    return 'dark'
  }
  return 'light'
}

// Searchable client picker - type to filter, click to select. Mirrors the
// "Your name" field's UX. A separate component (not a plain render helper)
// because each call site (one per day/period row) needs its own independent
// search-term/dropdown-open state.
function ClientSearchInput({
  value,
  options,
  onSelect,
}: {
  value: string
  options: string[]
  onSelect: (client: string) => void
}) {
  const [searchTerm, setSearchTerm] = useState(value || '')
  const [showDropdown, setShowDropdown] = useState(false)

  // Keep the visible text in sync if the value changes from outside (e.g.
  // switching to a different day/period, or loading existing entries)
  useEffect(() => {
    setSearchTerm(value || '')
  }, [value])

  const filtered = options.filter((o) => o.toLowerCase().includes(searchTerm.toLowerCase()))

  return (
    <div style={{ position: 'relative' }}>
      <input
        type="text"
        value={searchTerm}
        onChange={(e) => {
          setSearchTerm(e.target.value)
          onSelect(e.target.value)
          setShowDropdown(true)
        }}
        onFocus={() => setShowDropdown(true)}
        onBlur={() => setTimeout(() => setShowDropdown(false), 200)}
        placeholder="Search client"
        style={{
          width: '100%',
          padding: '8px',
          border: 'var(--border-input)',
          borderRadius: '4px',
          fontSize: '14px',
          background: 'var(--select-bg)',
          color: 'var(--select-text)',
          fontWeight: '600',
        }}
      />
      {showDropdown && (
        <div
          className="user-dropdown"
          role="listbox"
          style={{
            position: 'absolute',
            top: '100%',
            left: 0,
            right: 0,
            backgroundColor: 'var(--bg-surface)',
            border: 'var(--border-input)',
            borderRadius: '4px',
            marginTop: '4px',
            maxHeight: '200px',
            overflowY: 'auto',
            zIndex: 1000,
            boxShadow: 'var(--shadow-hover)',
          }}
        >
          {filtered.map((c, index, arr) => (
            <div
              key={c}
              role="option"
              aria-selected={c === value}
              tabIndex={0}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => {
                setSearchTerm(c)
                onSelect(c)
                setShowDropdown(false)
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  setSearchTerm(c)
                  onSelect(c)
                  setShowDropdown(false)
                }
              }}
              style={{
                padding: '12px 16px',
                cursor: 'pointer',
                borderBottom: index < arr.length - 1 ? '1px solid var(--border-subtle-color)' : 'none',
                backgroundColor: c === value ? 'var(--bg-muted)' : 'transparent',
                transition: 'background-color 0.2s',
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.backgroundColor = 'var(--bg-muted)'
              }}
              onMouseLeave={(e) => {
                if (c !== value) {
                  e.currentTarget.style.backgroundColor = 'transparent'
                }
              }}
            >
              {c}
            </div>
          ))}
          {filtered.length === 0 && (
            <div style={{ padding: '12px 16px', color: 'var(--text-muted)', fontStyle: 'italic' }}>
              No matches found
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function App() {
  const [viewMode, setViewMode] = useState<ViewMode>('fill')
  const [theme, setTheme] = useState<Theme>(getInitialTheme)
  const [weekStart, setWeekStart] = useState<Date>(getMondayOfWeek(new Date()))
  const [userName, setUserName] = useState(() => {
    // Load from localStorage on mount
    return localStorage.getItem('workTrackerUserName') || ''
  })
  const [weekEntries, setWeekEntries] = useState<WeekEntry[]>([])
  const [summaryEntries, setSummaryEntries] = useState<SummaryRow[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [toast, setToast] = useState('')
  const [existingEntriesCount, setExistingEntriesCount] = useState(0)
  const [isEditMode, setIsEditMode] = useState(false)
  const [userList, setUserList] = useState<string[]>([])
  const [showUserDropdown, setShowUserDropdown] = useState(false)
  const [userSearchTerm, setUserSearchTerm] = useState('')
  const [editSearchTerm, setEditSearchTerm] = useState('')
  const [confirmDeleteUser, setConfirmDeleteUser] = useState<string | null>(null)
  const [deletingUser, setDeletingUser] = useState<string | null>(null)
  const [isMobile, setIsMobile] = useState(() => typeof window !== 'undefined' && window.innerWidth < 768)
  const [showReminderBanner, setShowReminderBanner] = useState(false)

  // Split working mode (morning/afternoon) - per day (set of dates that are split)
  const [splitDays, setSplitDays] = useState<Set<string>>(new Set())

  // Overwrite confirmation + undo
  const [showOverwriteConfirm, setShowOverwriteConfirm] = useState(false)
  const [isOverwriteConfirmed, setIsOverwriteConfirmed] = useState(false)
  const [backupBeforeSave, setBackupBeforeSave] = useState<Array<{date: string; location: string; time_period?: string | null; client?: string | null; notes?: string | null}>>([])
  const [showUndoBar, setShowUndoBar] = useState(false)

  // Runtime-loaded config
  const [allUsers, setAllUsers] = useState<string[]>([])
  const [clientOptions, setClientOptions] = useState<string[]>([])

  // Apply + persist the theme whenever it changes
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    localStorage.setItem('workTrackerTheme', theme)
  }, [theme])

  // Save user name to localStorage whenever it changes
  useEffect(() => {
    if (userName.trim()) {
      localStorage.setItem('workTrackerUserName', userName.trim())
    }
  }, [userName])

  // Initialize week entries when week start changes
  useEffect(() => {
    setWeekEntries(generateWeekEntries(weekStart, false))
    // Reset split days when week changes
    setSplitDays(new Set())
  }, [weekStart])

  // Load client and team lists from public at runtime
  useEffect(() => {
    const loadConfigs = async () => {
      try {
        const [clientsResp, teamResp] = await Promise.all([
          fetch('/clients.json'),
          fetch('/team-members.json'),
        ])
        const clientsJson = await clientsResp.json()
        const teamJson = await teamResp.json()
        setClientOptions(Array.isArray(clientsJson?.clients) ? clientsJson.clients : [])
        setAllUsers(Array.isArray(teamJson?.teamMembers) ? teamJson.teamMembers : [])
      } catch (e) {
        setClientOptions([])
        setAllUsers([])
      }
    }
    loadConfigs()
  }, [])

  // Load summary when switching to dashboard view
  useEffect(() => {
    if (viewMode === 'dashboard') {
      loadWeekSummary()
    }
  }, [viewMode, weekStart])

  // Scroll to today's section whenever the dashboard finishes loading (keyed on
  // `loading` transitioning to false, not summaryEntries.length - two different
  // weeks can coincidentally have the same entry count, which would silently
  // skip this effect if it depended on the count instead).
  useEffect(() => {
    if (viewMode !== 'dashboard' || loading) return
    // Wait a couple of paint cycles so the day-section refs are attached and
    // layout (including the sticky nav's height) has settled before scrolling -
    // more reliable than a fixed setTimeout guess.
    let rafId = requestAnimationFrame(() => {
      rafId = requestAnimationFrame(() => {
        todaySectionRef.current?.scrollIntoView({
          behavior: 'smooth',
          block: 'start',
          inline: 'nearest'
        })
      })
    })
    return () => cancelAnimationFrame(rafId)
  }, [viewMode, weekStart, loading])

  // Check for existing entries when user name or week changes (debounced so typing
  // a name doesn't fire one request per keystroke)
  useEffect(() => {
    const timeoutId = setTimeout(() => {
      const checkForExisting = async () => {
        if (userName.trim()) {
          try {
            const result = await checkExistingEntries(userName.trim(), formatDate(weekStart))
            setExistingEntriesCount(result.exists ? result.count : 0)
          } catch (err) {
            setExistingEntriesCount(0)
          }
        } else {
          setExistingEntriesCount(0)
        }
      }
      checkForExisting()
    }, 350)
    return () => clearTimeout(timeoutId)
  }, [userName, weekStart])

  // Load user list when in edit mode
  useEffect(() => {
    if (viewMode === 'edit') {
      loadUserList()
    }
  }, [viewMode, weekStart])

  // Mobile detection
  useEffect(() => {
    const handleResize = () => setIsMobile(window.innerWidth < 768)
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [])

  // Reminder: show banner any day, as long as we're looking at the current
  // week and it hasn't been filled in yet
  useEffect(() => {
    const isCurrentWeek = formatDate(weekStart) === formatDate(getMondayOfWeek(new Date()))
    if (isCurrentWeek && existingEntriesCount === 0) {
      setShowReminderBanner(true)
    } else {
      setShowReminderBanner(false)
    }
  }, [existingEntriesCount, weekStart])

  const loadUserList = async () => {
    try {
      setLoading(true)
      const result = await getUsersForWeek(formatDate(weekStart))
      setUserList(result.users)
    } catch (err) {
      // Fallback: try all users across all time
      try {
        const all = await getAllUsers()
        setUserList(all.users)
        // do not surface an error if fallback worked
      } catch (e) {
        setError('Failed to load user list')
        setUserList([])
      }
    } finally {
      setLoading(false)
    }
  }

  const loadExistingEntries = async (user: string, week: string) => {
    try {
      setLoading(true)
      const existingEntries = await getUserEntriesForWeek(user, week)

      // Group entries by date and time_period
      const entriesByDate = new Map<string, { morning?: ExistingEntry, afternoon?: ExistingEntry, full?: ExistingEntry }>()

      for (const entry of existingEntries) {
        const date = entry.date
        if (!entriesByDate.has(date)) {
          entriesByDate.set(date, {})
        }
        const dayEntries = entriesByDate.get(date)!
        if (entry.time_period === 'Morning') {
          dayEntries.morning = entry
        } else if (entry.time_period === 'Afternoon') {
          dayEntries.afternoon = entry
        } else {
          dayEntries.full = entry
        }
      }

      // Update week entries with existing data
      const updatedEntries = weekEntries.map(entry => {
        const dayEntries = entriesByDate.get(entry.date)
        if (!dayEntries) return entry

        // If we have full day entry and no split entries, use it
        if (dayEntries.full && !dayEntries.morning && !dayEntries.afternoon) {
          const normalizedLocation = normalizeLocationFromApi(dayEntries.full.location) as WorkLocation
          let isCustomClient = false
          if (normalizedLocation === 'Client Office') {
            isCustomClient = !clientOptions.includes(dayEntries.full.client || '')
          } else if (normalizedLocation === 'Other') {
            isCustomClient = true
          }
          return {
            ...entry,
            location: normalizedLocation,
            client: dayEntries.full.client || '',
            notes: dayEntries.full.notes || '',
            isCustomClient: isCustomClient
          }
        }

        // If we have morning/afternoon entries, mark this day as split
        if (dayEntries.morning || dayEntries.afternoon) {
          setSplitDays(prev => new Set(prev).add(entry.date))
          const result = { ...entry }

          if (dayEntries.morning) {
            const normalizedLocation = normalizeLocationFromApi(dayEntries.morning.location) as WorkLocation
            result.morningLocation = normalizedLocation
            result.morningClient = dayEntries.morning.client || ''
            result.morningNotes = dayEntries.morning.notes || ''
            if (normalizedLocation === 'Client Office') {
              result.morningIsCustomClient = !clientOptions.includes(dayEntries.morning.client || '')
            } else if (normalizedLocation === 'Other') {
              result.morningIsCustomClient = true
            } else {
              result.morningIsCustomClient = false
            }
          } else {
            // Initialize morning fields if afternoon exists but morning doesn't
            result.morningLocation = 'Neal Street' as WorkLocation
            result.morningClient = ''
            result.morningNotes = ''
            result.morningIsCustomClient = false
          }

          if (dayEntries.afternoon) {
            const normalizedLocation = normalizeLocationFromApi(dayEntries.afternoon.location) as WorkLocation
            result.afternoonLocation = normalizedLocation
            result.afternoonClient = dayEntries.afternoon.client || ''
            result.afternoonNotes = dayEntries.afternoon.notes || ''
            if (normalizedLocation === 'Client Office') {
              result.afternoonIsCustomClient = !clientOptions.includes(dayEntries.afternoon.client || '')
            } else if (normalizedLocation === 'Other') {
              result.afternoonIsCustomClient = true
            } else {
              result.afternoonIsCustomClient = false
            }
          } else {
            // Initialize afternoon fields if morning exists but afternoon doesn't
            result.afternoonLocation = 'Neal Street' as WorkLocation
            result.afternoonClient = ''
            result.afternoonNotes = ''
            result.afternoonIsCustomClient = false
          }

          return result
        }

        return entry
      })

      setWeekEntries(updatedEntries)
    } catch (err) {
      setError('Failed to load existing entries')
    } finally {
      setLoading(false)
    }
  }

  const handleUserSelect = async (selectedUser: string) => {
    setUserName(selectedUser)
    setIsEditMode(true)
    setViewMode('fill')
    await loadExistingEntries(selectedUser, formatDate(weekStart))
  }

  const handleDeleteUserWeek = async (user: string) => {
    setDeletingUser(user)
    try {
      await deleteUserWeek(user, formatDate(weekStart))
      setToast(`Deleted ${user}'s entries for this week`)
      setTimeout(() => setToast(''), 3000)
      setConfirmDeleteUser(null)
      await loadUserList()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete entries')
    } finally {
      setDeletingUser(null)
    }
  }

  const loadWeekSummary = async () => {
    try {
      setLoading(true)
      setError('')
      const response = await getWeekSummary(formatDate(weekStart))
      // Normalize any legacy location names coming from API
      const normalized = response.entries.map(e => ({
        ...e,
        location: normalizeLocationFromApi(e.location),
      }))
      setSummaryEntries(normalized)
    } catch (err) {
      setError(
        err instanceof Error ? err.message : 'Failed to load week summary'
      )
    } finally {
      setLoading(false)
    }
  }

  const handleWeekStartChange = (
    event: React.ChangeEvent<HTMLInputElement>
  ) => {
    const selectedDate = new Date(event.target.value)
    const monday = getMondayOfWeek(selectedDate)
    setWeekStart(monday)
  }

  const goToPrevWeek = () => {
    const d = new Date(weekStart)
    d.setDate(d.getDate() - 7)
    setWeekStart(getMondayOfWeek(d))
  }

  const goToNextWeek = () => {
    const d = new Date(weekStart)
    d.setDate(d.getDate() + 7)
    setWeekStart(getMondayOfWeek(d))
  }

  const goToThisWeek = () => {
    setWeekStart(getMondayOfWeek(new Date()))
  }

  const dateInputRef = useRef<HTMLInputElement | null>(null)
  const todaySectionRef = useRef<HTMLDivElement | null>(null)

  const openNativeDatePicker = () => {
    const el = dateInputRef.current
    if (!el) return
    const anyEl = el as any
    if (typeof anyEl.showPicker === 'function') {
      anyEl.showPicker()
    } else {
      el.focus()
      el.click()
    }
  }

  const handleLocationChange = (index: number, location: WorkLocation, period?: 'morning' | 'afternoon') => {
    const newEntries = [...weekEntries]
    const entry = newEntries[index]
    const isSplit = splitDays.has(entry.date)

    if (isSplit && period) {
      if (period === 'morning') {
        newEntries[index].morningLocation = location
        if (location !== 'Client Office' && location !== 'Other') {
          newEntries[index].morningClient = ''
          newEntries[index].morningIsCustomClient = false
        } else if (location === 'Other') {
          newEntries[index].morningIsCustomClient = true
          if (!newEntries[index].morningClient) {
            newEntries[index].morningClient = ''
          }
        }
      } else {
        newEntries[index].afternoonLocation = location
        if (location !== 'Client Office' && location !== 'Other') {
          newEntries[index].afternoonClient = ''
          newEntries[index].afternoonIsCustomClient = false
        } else if (location === 'Other') {
          newEntries[index].afternoonIsCustomClient = true
          if (!newEntries[index].afternoonClient) {
            newEntries[index].afternoonClient = ''
          }
        }
      }
    } else {
    newEntries[index].location = location
    if (location !== 'Client Office' && location !== 'Other') {
      newEntries[index].client = ''
      newEntries[index].isCustomClient = false
    } else if (location === 'Other') {
      // For "Other", always show custom input
      newEntries[index].isCustomClient = true
      if (!newEntries[index].client) {
        newEntries[index].client = ''
        }
      }
    }
    setWeekEntries(newEntries)
  }

  const handleClientChange = (index: number, client: string, period?: 'morning' | 'afternoon') => {
    const newEntries = [...weekEntries]
    const entry = newEntries[index]
    const isSplit = splitDays.has(entry.date)

    if (isSplit && period) {
      if (period === 'morning') {
        newEntries[index].morningClient = client
      } else {
        newEntries[index].afternoonClient = client
      }
    } else {
    newEntries[index].client = client
    }
    setWeekEntries(newEntries)
  }

  const handleNotesChange = (index: number, notes: string, period?: 'morning' | 'afternoon') => {
    const newEntries = [...weekEntries]
    const entry = newEntries[index]
    const isSplit = splitDays.has(entry.date)

    if (isSplit && period) {
      if (period === 'morning') {
        newEntries[index].morningNotes = notes
      } else {
        newEntries[index].afternoonNotes = notes
      }
    } else {
    newEntries[index].notes = notes
    }
    setWeekEntries(newEntries)
  }

  // Shared between mobile card and desktop table layouts
  const toggleSplit = (index: number) => {
    const entry = weekEntries[index]
    setSplitDays(prev => {
      const newSet = new Set(prev)
      if (newSet.has(entry.date)) {
        newSet.delete(entry.date)
        // Clear split fields when unsplitting
        const newEntries = [...weekEntries]
        newEntries[index] = {
          ...newEntries[index],
          morningLocation: undefined,
          afternoonLocation: undefined,
          morningClient: undefined,
          afternoonClient: undefined,
          morningNotes: undefined,
          afternoonNotes: undefined,
          morningIsCustomClient: undefined,
          afternoonIsCustomClient: undefined,
        }
        setWeekEntries(newEntries)
      } else {
        newSet.add(entry.date)
        // Initialize split fields when splitting
        const newEntries = [...weekEntries]
        const current = newEntries[index]
        newEntries[index] = {
          ...current,
          morningLocation: current.location || 'Neal Street' as WorkLocation,
          afternoonLocation: current.location || 'Neal Street' as WorkLocation,
          morningClient: current.client || '',
          afternoonClient: current.client || '',
          morningNotes: current.notes || '',
          afternoonNotes: current.notes || '',
          morningIsCustomClient: current.isCustomClient || false,
          afternoonIsCustomClient: current.isCustomClient || false,
        }
        setWeekEntries(newEntries)
      }
      return newSet
    })
  }

  const applyPreset = (presetType: 'all-office' | 'all-wfh') => {
    const newEntries = [...weekEntries]
    const location: WorkLocation = presetType === 'all-office' ? 'Neal Street' : 'WFH'

    newEntries.forEach(entry => {
      entry.location = location
      entry.client = ''
      entry.notes = ''
      entry.isCustomClient = false
      // Also update split morning/afternoon if the day is in split mode
      if (splitDays.has(entry.date)) {
        entry.morningLocation = location
        entry.afternoonLocation = location
        entry.morningClient = ''
        entry.afternoonClient = ''
        entry.morningNotes = ''
        entry.afternoonNotes = ''
        entry.morningIsCustomClient = false
        entry.afternoonIsCustomClient = false
      }
    })

    setWeekEntries(newEntries)
    setToast('Preset applied!')
    setTimeout(() => setToast(''), 2000)
  }

  const fillFromLastWeek = async () => {
    if (!userName.trim()) {
      setError('Please enter your name first')
      return
    }

    const prevWeekStart = new Date(weekStart)
    prevWeekStart.setDate(prevWeekStart.getDate() - 7)
    const prevWeekStr = formatDate(prevWeekStart)

    const dayOffset = (dateStr: string, mondayStr: string): number => {
      const [y1, m1, d1] = dateStr.split('-').map(Number)
      const [y2, m2, d2] = mondayStr.split('-').map(Number)
      return Math.round((new Date(y1, m1 - 1, d1).getTime() - new Date(y2, m2 - 1, d2).getTime()) / 86400000)
    }

    try {
      setLoading(true)
      setError('')
      const lastWeekEntries = await getUserEntriesForWeek(userName.trim(), prevWeekStr)

      if (lastWeekEntries.length === 0) {
        setToast('No entry found for last week')
        setTimeout(() => setToast(''), 3000)
        return
      }

      // Group last week's entries by day offset (0=Mon … 4=Fri) and time period
      const byOffset = new Map<number, { morning?: ExistingEntry; afternoon?: ExistingEntry; full?: ExistingEntry }>()
      for (const e of lastWeekEntries) {
        const offset = dayOffset(e.date, prevWeekStr)
        if (offset < 0 || offset > 4) continue
        if (!byOffset.has(offset)) byOffset.set(offset, {})
        const slot = byOffset.get(offset)!
        if (e.time_period === 'Morning') slot.morning = e
        else if (e.time_period === 'Afternoon') slot.afternoon = e
        else slot.full = e
      }

      const newEntries = [...weekEntries]
      const newSplitDays = new Set(splitDays)

      newEntries.forEach((entry, index) => {
        const offset = dayOffset(entry.date, formatDate(weekStart))
        const slot = byOffset.get(offset)
        if (!slot) return

        if (slot.full && !slot.morning && !slot.afternoon) {
          const loc = normalizeLocationFromApi(slot.full.location) as WorkLocation
          const isCustomClient = loc === 'Other' ? true
            : loc === 'Client Office' ? !clientOptions.includes(slot.full.client || '') : false
          newEntries[index] = {
            ...entry,
            location: loc,
            client: slot.full.client || '',
            notes: slot.full.notes || '',
            isCustomClient,
            morningLocation: undefined, afternoonLocation: undefined,
            morningClient: undefined, afternoonClient: undefined,
            morningNotes: undefined, afternoonNotes: undefined,
            morningIsCustomClient: undefined, afternoonIsCustomClient: undefined,
          }
          newSplitDays.delete(entry.date)
        } else if (slot.morning || slot.afternoon) {
          newSplitDays.add(entry.date)
          const result = { ...entry }
          if (slot.morning) {
            const loc = normalizeLocationFromApi(slot.morning.location) as WorkLocation
            result.morningLocation = loc
            result.morningClient = slot.morning.client || ''
            result.morningNotes = slot.morning.notes || ''
            result.morningIsCustomClient = loc === 'Other' ? true
              : loc === 'Client Office' ? !clientOptions.includes(slot.morning.client || '') : false
          } else {
            result.morningLocation = 'Neal Street'; result.morningClient = ''; result.morningNotes = ''; result.morningIsCustomClient = false
          }
          if (slot.afternoon) {
            const loc = normalizeLocationFromApi(slot.afternoon.location) as WorkLocation
            result.afternoonLocation = loc
            result.afternoonClient = slot.afternoon.client || ''
            result.afternoonNotes = slot.afternoon.notes || ''
            result.afternoonIsCustomClient = loc === 'Other' ? true
              : loc === 'Client Office' ? !clientOptions.includes(slot.afternoon.client || '') : false
          } else {
            result.afternoonLocation = 'Neal Street'; result.afternoonClient = ''; result.afternoonNotes = ''; result.afternoonIsCustomClient = false
          }
          newEntries[index] = result
        }
      })

      setWeekEntries(newEntries)
      setSplitDays(newSplitDays)
      setToast('Filled from last week!')
      setTimeout(() => setToast(''), 2500)
    } catch {
      setToast('No entry found for last week')
      setTimeout(() => setToast(''), 3000)
    } finally {
      setLoading(false)
    }
  }

  const validateEntries = (): string | null => {
    if (!userName.trim()) {
      return 'Please enter your name'
    }

    for (const entry of weekEntries) {
      const isSplit = splitDays.has(entry.date)

      if (isSplit) {
        // Validate morning entry if location is set
        if (entry.morningLocation) {
          if (entry.morningLocation === 'Client Office') {
            if (entry.morningIsCustomClient && !entry.morningClient?.trim()) {
              return `Please enter a client name for ${entry.dayName} (Morning)`
            }
            if (!entry.morningIsCustomClient && !entry.morningClient?.trim()) {
              return `Client name is required for ${entry.dayName} (Morning)`
            }
          }
          if (entry.morningLocation === 'Other') {
            if (!entry.morningClient?.trim()) {
              return `Please enter a location description for ${entry.dayName} (Morning)`
            }
          }
        }
        // Validate afternoon entry if location is set
        if (entry.afternoonLocation) {
          if (entry.afternoonLocation === 'Client Office') {
            if (entry.afternoonIsCustomClient && !entry.afternoonClient?.trim()) {
              return `Please enter a client name for ${entry.dayName} (Afternoon)`
            }
            if (!entry.afternoonIsCustomClient && !entry.afternoonClient?.trim()) {
              return `Client name is required for ${entry.dayName} (Afternoon)`
            }
          }
          if (entry.afternoonLocation === 'Other') {
            if (!entry.afternoonClient?.trim()) {
              return `Please enter a location description for ${entry.dayName} (Afternoon)`
            }
          }
        }
      } else {
        // Validate full day entry
      if (entry.location === 'Client Office') {
        // If "Other" was selected, isCustomClient=true and client must be non-empty
        if (entry.isCustomClient && !entry.client.trim()) {
          return `Please enter a client name for ${entry.dayName}`
        }
        if (!entry.isCustomClient && !entry.client.trim()) {
          return `Client name is required for ${entry.dayName}`
        }
      }
      if (entry.location === 'Other') {
        if (!entry.client.trim()) {
          return `Please enter a location description for ${entry.dayName}`
          }
        }
      }
    }

    return null
  }

  const handleSaveWeek = async () => {
    const validationError = validateEntries()
    if (validationError) {
      setError(validationError)
      return
    }

    // Require explicit confirmation if overwriting
    if (existingEntriesCount > 0 && !isOverwriteConfirmed) {
      setShowOverwriteConfirm(true)
      return
    }

    try {
      setLoading(true)
      setError('')

      // Backup current entries from DB (for undo) if we are overwriting. Fetched fresh
      // here rather than reused from the existing-entries check, since that check is
      // debounced and could be stale relative to what's actually about to be overwritten.
      let backup: Array<{date: string; location: string; time_period?: string | null; client?: string | null; notes?: string | null}> = []
      if (existingEntriesCount > 0) {
        try {
          const current = await getUserEntriesForWeek(userName.trim(), formatDate(weekStart))
          backup = current.map(e => ({ date: e.date, location: e.location, time_period: e.time_period, client: e.client, notes: e.notes }))
          setBackupBeforeSave(backup)
        } catch {
          // if backup fails, proceed without undo
          setBackupBeforeSave([])
        }
      }

      // Build entries array - either split or full day entries
      const entries: Entry[] = []

      for (const entry of weekEntries) {
        const isSplit = splitDays.has(entry.date)

        if (isSplit && (entry.morningLocation || entry.afternoonLocation)) {
          // Add morning entry if location is set
          if (entry.morningLocation) {
            entries.push({
          date: entry.date,
              location: entry.morningLocation,
              time_period: 'Morning',
              client: (entry.morningLocation === 'Client Office' || entry.morningLocation === 'Other') && entry.morningClient?.trim()
                ? entry.morningClient.trim()
                : undefined,
              notes: entry.morningNotes?.trim() || undefined,
            })
          }
          // Add afternoon entry if location is set
          if (entry.afternoonLocation) {
            entries.push({
              date: entry.date,
              location: entry.afternoonLocation,
              time_period: 'Afternoon',
              client: (entry.afternoonLocation === 'Client Office' || entry.afternoonLocation === 'Other') && entry.afternoonClient?.trim()
                ? entry.afternoonClient.trim()
                : undefined,
              notes: entry.afternoonNotes?.trim() || undefined,
            })
          }
        } else {
          // Full day entry
          entries.push({
            date: entry.date,
            location: entry.location,
            time_period: null,
          client: (entry.location === 'Client Office' || entry.location === 'Other') && entry.client.trim()
            ? entry.client.trim()
            : undefined,
          notes: entry.notes.trim() || undefined,
          })
        }
      }

      const request = {
        user_name: userName.trim(),
        entries: entries,
      }

      await saveWeek(request)
      setToast('Week Saved!')
      setTimeout(() => setToast(''), 3000)
      setIsEditMode(false)
      setShowReminderBanner(false)
      // Navigate to Who's where tab
      setViewMode('dashboard')

      if (existingEntriesCount > 0 && backup.length > 0) {
        setShowUndoBar(true)
        // auto-hide undo after 15s
        setTimeout(() => setShowUndoBar(false), 15000)
      }

      // reset confirmation state
      setShowOverwriteConfirm(false)
      setIsOverwriteConfirmed(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save week')
    } finally {
      setLoading(false)
    }
  }

  const confirmOverwriteAndSave = async () => {
    setIsOverwriteConfirmed(true)
    setShowOverwriteConfirm(false)
    await handleSaveWeek()
  }

  const cancelOverwrite = () => {
    setShowOverwriteConfirm(false)
    setIsOverwriteConfirmed(false)
  }

  const handleUndo = async () => {
    if (!backupBeforeSave.length) {
      setShowUndoBar(false)
      return
    }
    try {
      setLoading(true)
      const request = {
        user_name: userName.trim(),
        entries: backupBeforeSave.map(e => ({
          date: e.date,
          location: e.location as any,
          time_period: e.time_period,
          client: e.client || undefined,
          notes: e.notes || undefined,
        })),
      }
      await saveWeek(request)
      setToast('Reverted previous entries')
      setTimeout(() => setToast(''), 3000)
      if (viewMode === 'dashboard') {
        await loadWeekSummary()
      }
    } catch (e) {
      setError('Failed to undo changes')
    } finally {
      setShowUndoBar(false)
      setLoading(false)
    }
  }

  // Derived dashboard data — recomputed only when the underlying entries change,
  // not on every render (e.g. while typing on the "Fill my week" tab)
  const { groupedEntries, locationGroupsByDate } = useMemo(() => {
    const grouped = groupEntriesByDateAndLocation(summaryEntries)
    const groupsByDate: { [date: string]: { [baseLoc: string]: { [key: string]: SummaryRow[] } } } = {}
    for (const date of Object.keys(grouped)) {
      const allLocationKeys = Object.keys(grouped[date] || {})
      const locationGroups: { [baseLoc: string]: { [key: string]: SummaryRow[] } } = {}
      for (const key of allLocationKeys) {
        // Extract base location (remove "(Morning)" or "(Afternoon)" suffix)
        const baseLoc = key.replace(/\s*\(Morning\)$/, '').replace(/\s*\(Afternoon\)$/, '')
        if (!locationGroups[baseLoc]) {
          locationGroups[baseLoc] = {}
        }
        locationGroups[baseLoc][key] = grouped[date][key] || []
      }
      groupsByDate[date] = locationGroups
    }
    return { groupedEntries: grouped, locationGroupsByDate: groupsByDate }
  }, [summaryEntries])

  const todayStr = formatDate(new Date())

  // Hoisted render helpers (shared between mobile cards and desktop table)
  const renderLocationSelect = (
    location: WorkLocation | undefined,
    onChange: (loc: WorkLocation) => void,
  ) => (
    <select
      value={location || 'Neal Street'}
      onChange={(e) => onChange(e.target.value as WorkLocation)}
    >
      <option value="Neal Street">Neal Street</option>
      <option value="WFH">WFH</option>
      <option value="Client Office">Client Office</option>
      <option value="Working From Abroad">Working From Abroad</option>
      <option value="Holiday">Holiday</option>
      <option value="Other">Other</option>
    </select>
  )

  const renderClientInput = (
    location: WorkLocation | undefined,
    client: string | undefined,
    isCustom: boolean | undefined,
    onChange: (client: string) => void,
    variant: 'table' | 'card' = 'table'
  ) => {
    const naPlaceholder = variant === 'card' ? null : <span style={{ color: 'var(--text-muted)', fontStyle: 'italic' }}>N/A</span>
    if (!location) return naPlaceholder
    if (location === 'Client Office') {
      return isCustom ? (
        <input
          className="client-input"
          type="text"
          value={client || ''}
          onChange={(e) => onChange(e.target.value)}
          placeholder="Enter client name"
          style={{ marginTop: '4px' }}
        />
      ) : (
        <ClientSearchInput
          value={client || ''}
          options={clientOptions}
          onSelect={onChange}
        />
      )
    } else if (location === 'Other') {
      return (
        <input
          className="client-input"
          type="text"
          value={client || ''}
          onChange={(e) => onChange(e.target.value)}
          placeholder="Enter location description"
          style={{ marginTop: '4px' }}
        />
      )
    }
    return naPlaceholder
  }

  return (
    <div className="container">
      <div className="header">
        <div className="header-content">
          <img src="/logo.png" alt="Logo" className="logo" />
          <div className="header-text">
            <h1>In Office</h1>
            <p>Track where your team is working this week</p>
          </div>
          <button
            type="button"
            className="theme-toggle"
            onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
            aria-label={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
            title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
          >
            <span className="theme-toggle-icon" aria-hidden="true">{theme === 'dark' ? '☀️' : '🌙'}</span>
            {theme === 'dark' ? 'Light' : 'Dark'}
          </button>
        </div>
      </div>

      <div className="toggle-buttons">
        <button
          className={`toggle-btn ${viewMode === 'fill' ? 'active' : ''}`}
          onClick={() => {
            setViewMode('fill')
            setIsEditMode(false)
          }}
        >
          Fill my week
        </button>
        <button
          className={`toggle-btn ${viewMode === 'edit' ? 'active' : ''}`}
          onClick={() => {
            setViewMode('edit')
            setIsEditMode(false)
          }}
        >
          Edit my week
        </button>
        <button
          className={`toggle-btn ${viewMode === 'dashboard' ? 'active' : ''}`}
          onClick={() => {
            setViewMode('dashboard')
            // If we're already on this tab with data loaded, jump straight to
            // today - otherwise the scroll-to-today effect picks it up once
            // the week summary finishes loading.
            todaySectionRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start', inline: 'nearest' })
          }}
        >
          Who's where
        </button>
      </div>

      <div className="week-selector">
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <button className="preset-btn" type="button" onClick={goToPrevWeek}>{'<'} Prev</button>

          {/* Hidden native input for calendar; themed launcher button opens it */}
          <input
            ref={dateInputRef}
            id="week-start"
            type="date"
            value={formatDate(weekStart)}
            onChange={handleWeekStartChange}
            style={{ position: 'absolute', width: 1, height: 1, opacity: 0, pointerEvents: 'none' }}
            aria-hidden="true"
            tabIndex={-1}
          />

          <button
            className="preset-btn"
            type="button"
            onClick={openNativeDatePicker}
            aria-label="Open calendar to select week"
          >
            📅 Select week: {formatWeekRangeLabel(weekStart)}
          </button>

          <button className="preset-btn" type="button" onClick={goToNextWeek}>Next {'>'}</button>

          <button
            className="preset-btn"
            type="button"
            onClick={goToThisWeek}
            style={{
              marginLeft: '8px',
              fontWeight: 700
            }}
          >
            📍 This Week
          </button>
        </div>

      </div>

      {error && <div className="error-message">{error}</div>}

      {toast && <div className="toast">{toast}</div>}

      {/* Monday reminder banner */}
      {showReminderBanner && viewMode === 'fill' && (
        <div className="reminder-banner">
          <span>👋 Don't forget to fill in your week!</span>
          <button
            onClick={() => setShowReminderBanner(false)}
            style={{
              background: 'none',
              border: 'none',
              color: 'var(--warn-text)',
              cursor: 'pointer',
              fontSize: '18px',
              fontWeight: 700,
              lineHeight: 1,
              padding: '0 4px',
            }}
            aria-label="Dismiss reminder"
          >
            ✕
          </button>
        </div>
      )}

      {viewMode === 'edit' && (
        <div className="form-section">
          <h2>Select your name to edit:</h2>

          {/* Quick jump to your own entry */}
          {userName.trim() && userList.includes(userName.trim()) && (
            <div style={{ marginBottom: '16px' }}>
              <button
                className="preset-btn preset-btn-highlight"
                onClick={() => handleUserSelect(userName.trim())}
                type="button"
              >
                ✏️ Edit my entry (<span style={{ textTransform: 'none' }}>{userName.trim()}</span>)
              </button>
            </div>
          )}

          <div className="form-group">
            <input
              type="text"
              value={editSearchTerm}
              onChange={(e) => setEditSearchTerm(e.target.value)}
              placeholder="Search people who have filled this week"
            />
          </div>
          {loading ? (
            <div className="user-list">
              {[1, 2, 3, 4, 5, 6, 8].map(i => (
                <div key={i} className="skeleton-user-card" />
              ))}
            </div>
          ) : userList.length === 0 ? (
            <div className="empty-state">
              <h3>No entries found for this week</h3>
              <p>No one has submitted their work locations yet.</p>
              <button
                className="preset-btn preset-btn-highlight"
                type="button"
                onClick={() => { setViewMode('fill'); setIsEditMode(false) }}
                style={{ marginTop: '12px' }}
              >
                Fill my week
              </button>
            </div>
          ) : (
            <div className="user-list">
              {userList
                .filter((u) =>
                  u.toLowerCase().includes(editSearchTerm.toLowerCase())
                )
                .map((user, index) => (
                <div
                  key={index}
                  className={`user-card ${user === userName.trim() ? 'current-user' : ''}`}
                >
                  <button
                    className="user-card-name"
                    onClick={() => handleUserSelect(user)}
                    type="button"
                  >
                    {user}
                    {user === userName.trim() && <span style={{ marginLeft: '6px', fontSize: '14px' }}>✓</span>}
                  </button>
                  {confirmDeleteUser === user ? (
                    <div className="user-card-confirm">
                      <button
                        type="button"
                        className="user-card-confirm-yes"
                        disabled={deletingUser === user}
                        onClick={() => handleDeleteUserWeek(user)}
                      >
                        {deletingUser === user ? 'Deleting...' : 'Confirm delete?'}
                      </button>
                      <button
                        type="button"
                        className="user-card-confirm-no"
                        disabled={deletingUser === user}
                        onClick={() => setConfirmDeleteUser(null)}
                        aria-label="Cancel delete"
                      >
                        ✕
                      </button>
                    </div>
                  ) : (
                    <button
                      type="button"
                      className="user-card-delete"
                      onClick={() => setConfirmDeleteUser(user)}
                      aria-label={`Delete ${user}'s entries for this week`}
                      title={`Delete ${user}'s entries for this week`}
                    >
                      🗑️
                    </button>
                  )}
                </div>
              ))}
              {userList.filter((u) => u.toLowerCase().includes(editSearchTerm.toLowerCase())).length === 0 && (
                <div className="empty-state" style={{ gridColumn: '1 / -1' }}>
                  <h3>No matches</h3>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {viewMode === 'fill' && (
        <div className="form-section">
          <div className="form-group" style={{ position: 'relative' }}>
            <div className="name-field-row">
              <label htmlFor="user-name">Your name:</label>
              <div className="name-input-wrapper">
              <input
                id="user-name"
                type="text"
                value={userName}
                onChange={(e) => {
                  setUserName(e.target.value)
                  setUserSearchTerm(e.target.value)
                  setShowUserDropdown(true)
                }}
                onFocus={() => setShowUserDropdown(true)}
                onBlur={() => setTimeout(() => setShowUserDropdown(false), 200)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault()
                    setShowUserDropdown(false)
                    const firstSelect = document.querySelector('.week-table select, .week-cards select') as HTMLSelectElement | null
                    firstSelect?.focus()
                  }
                }}
                placeholder="Search or enter your name"
                required
              />
              {showUserDropdown && (
                <div
                  className="user-dropdown"
                  role="listbox"
                  style={{
                    position: 'absolute',
                    top: '100%',
                    left: 0,
                    right: 0,
                    backgroundColor: 'var(--bg-surface)',
                    border: 'var(--border-input)',
                    borderRadius: '4px',
                    marginTop: '4px',
                    maxHeight: '200px',
                    overflowY: 'auto',
                    zIndex: 1000,
                    boxShadow: 'var(--shadow-hover)'
                  }}
                >
            {allUsers
                    .filter(user =>
                      user.toLowerCase().includes(userSearchTerm.toLowerCase())
                    )
                    .map((user, index, filtered) => (
                      <div
                        key={index}
                        role="option"
                        aria-selected={user === userName}
                        tabIndex={0}
                        onClick={() => {
                          setUserName(user)
                          setUserSearchTerm('')
                          setShowUserDropdown(false)
                        }}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' || e.key === ' ') {
                            e.preventDefault()
                            setUserName(user)
                            setUserSearchTerm('')
                            setShowUserDropdown(false)
                          }
                        }}
                        onMouseDown={(e) => e.preventDefault()}
                        style={{
                          padding: '12px 16px',
                          cursor: 'pointer',
                          borderBottom: index < filtered.length - 1 ? '1px solid var(--border-subtle-color)' : 'none',
                          backgroundColor: user === userName ? 'var(--bg-muted)' : 'transparent',
                          transition: 'background-color 0.2s',
                          whiteSpace: 'nowrap',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis'
                        }}
                        onMouseEnter={(e) => {
                          e.currentTarget.style.backgroundColor = 'var(--bg-muted)'
                        }}
                        onMouseLeave={(e) => {
                          if (user !== userName) {
                            e.currentTarget.style.backgroundColor = 'transparent'
                          }
                        }}
                      >
                        {user}
                      </div>
                    ))}
                  {allUsers.filter(user =>
                    user.toLowerCase().includes(userSearchTerm.toLowerCase())
                  ).length === 0 && (
                    <div style={{ padding: '12px 16px', color: 'var(--text-muted)', fontStyle: 'italic' }}>
                      No matches found
                    </div>
                  )}
                </div>
              )}
              </div>
            </div>
            {existingEntriesCount > 0 && (
              <div className="update-warning">
                ℹ️ You already have {existingEntriesCount} entry/entries for this week.
                Submitting will replace them.
              </div>
            )}
          </div>

          <div className="preset-buttons">
            <span className="preset-label">Quick fill:</span>
            <button
              className="preset-btn"
              onClick={() => applyPreset('all-office')}
              type="button"
            >
              <span style={{ fontSize: '18px', marginRight: '6px' }}>🏢</span> All Office
            </button>
            <button
              className="preset-btn"
              onClick={() => applyPreset('all-wfh')}
              type="button"
            >
              <span style={{ fontSize: '18px', marginRight: '6px' }}>🏠</span> All WFH
            </button>
            <button
              className="preset-btn"
              onClick={fillFromLastWeek}
              type="button"
              disabled={loading}
              style={{ borderColor: 'var(--border-input-color)', color: 'var(--text-muted)' }}
            >
              <span style={{ fontSize: '16px', marginRight: '6px' }}>📋</span> Same As Last Week
            </button>
          </div>

          {/* Mobile card layout */}
          {isMobile ? (
            <div className="week-cards">
              {weekEntries.map((entry, index) => {
                const isSplit = splitDays.has(entry.date)

                return (
                  <div key={entry.date} className="day-card">
                    <div className="day-card-header">
                      <span className="day-card-name">{entry.dayName}</span>
                      <span className="day-card-date">{formatFriendlyDate(entry.date)}</span>
                    </div>

                    {isSplit ? (
                      <>
                        <div className="day-card-period">
                          <div className="period-label">Morning</div>
                          {renderLocationSelect(entry.morningLocation, (loc) => handleLocationChange(index, loc, 'morning'))}
                          {renderClientInput(
                            entry.morningLocation,
                            entry.morningClient,
                            entry.morningIsCustomClient,
                            (c) => handleClientChange(index, c, 'morning'),
                            'card'
                          )}
                          <input
                            type="text"
                            value={entry.morningNotes || ''}
                            onChange={(e) => handleNotesChange(index, e.target.value, 'morning')}
                            placeholder="Optional notes"
                            style={{ width: '100%', marginTop: '8px' }}
                          />
                        </div>
                        <div className="day-card-period">
                          <div className="period-label">Afternoon</div>
                          {renderLocationSelect(entry.afternoonLocation, (loc) => handleLocationChange(index, loc, 'afternoon'))}
                          {renderClientInput(
                            entry.afternoonLocation,
                            entry.afternoonClient,
                            entry.afternoonIsCustomClient,
                            (c) => handleClientChange(index, c, 'afternoon'),
                            'card'
                          )}
                          <input
                            type="text"
                            value={entry.afternoonNotes || ''}
                            onChange={(e) => handleNotesChange(index, e.target.value, 'afternoon')}
                            placeholder="Optional notes"
                            style={{ width: '100%', marginTop: '8px' }}
                          />
                        </div>
                        <button
                          className="preset-btn"
                          onClick={() => toggleSplit(index)}
                          type="button"
                          style={{ fontSize: '11px', padding: '6px 10px', borderColor: 'var(--greenpill-border)', backgroundColor: 'var(--greenpill-bg)', color: 'var(--greenpill-text)', marginTop: '8px' }}
                        >
                          Unsplit
                        </button>
                      </>
                    ) : (
                      <>
                        {renderLocationSelect(entry.location, (loc) => handleLocationChange(index, loc))}
                        {renderClientInput(
                          entry.location,
                          entry.client,
                          entry.isCustomClient,
                          (c) => handleClientChange(index, c),
                          'card'
                        )}
                        <input
                          type="text"
                          value={entry.notes}
                          onChange={(e) => handleNotesChange(index, e.target.value)}
                          placeholder="Optional notes"
                          style={{ width: '100%', marginTop: '8px' }}
                        />
                        <button
                          className="preset-btn"
                          onClick={() => toggleSplit(index)}
                          type="button"
                          style={{ fontSize: '11px', padding: '6px 10px', marginTop: '8px', borderWidth: '1px' }}
                        >
                          <span style={{ fontSize: '14px', marginRight: '4px' }}>✂️</span> Split
                        </button>
                      </>
                    )}
                  </div>
                )
              })}
            </div>
          ) : (
            /* Desktop table layout */
            <table className="week-table">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Day</th>
                  <th>Location</th>
                  <th>Client</th>
                  <th>Notes</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {weekEntries.map((entry, index) => {
                  const isSplit = splitDays.has(entry.date)

                  if (isSplit) {
                    return (
                      <React.Fragment key={entry.date}>
                        <tr className="split-morning-row">
                          <td rowSpan={2} style={{ verticalAlign: 'top', paddingTop: '16px' }}>{formatFriendlyDate(entry.date)}</td>
                          <td rowSpan={2} style={{ verticalAlign: 'top', paddingTop: '16px' }}>{entry.dayName}</td>
                          <td>
                            <div className="period-label">Morning</div>
                            {renderLocationSelect(entry.morningLocation, (loc) => handleLocationChange(index, loc, 'morning'))}
                          </td>
                          <td>
                            {renderClientInput(
                              entry.morningLocation,
                              entry.morningClient,
                              entry.morningIsCustomClient,
                              (client) => handleClientChange(index, client, 'morning')
                            )}
                          </td>
                          <td>
                            <input
                              className="notes-input"
                              type="text"
                              value={entry.morningNotes || ''}
                              onChange={(e) => handleNotesChange(index, e.target.value, 'morning')}
                              placeholder="Optional notes"
                            />
                          </td>
                          <td rowSpan={2} style={{ verticalAlign: 'top', paddingTop: '16px' }}>
                            <button
                              className="preset-btn"
                              onClick={() => toggleSplit(index)}
                              type="button"
                              style={{
                                fontSize: '11px',
                                padding: '6px 10px',
                                borderColor: 'var(--greenpill-border)',
                                backgroundColor: 'var(--greenpill-bg)',
                                color: 'var(--greenpill-text)',
                              }}
                            >
                              Unsplit
                            </button>
                          </td>
                        </tr>
                        <tr className="split-afternoon-row">
                          <td>
                            <div className="period-label">Afternoon</div>
                            {renderLocationSelect(entry.afternoonLocation, (loc) => handleLocationChange(index, loc, 'afternoon'))}
                          </td>
                          <td>
                            {renderClientInput(
                              entry.afternoonLocation,
                              entry.afternoonClient,
                              entry.afternoonIsCustomClient,
                              (client) => handleClientChange(index, client, 'afternoon')
                            )}
                          </td>
                          <td>
                            <input
                              className="notes-input"
                              type="text"
                              value={entry.afternoonNotes || ''}
                              onChange={(e) => handleNotesChange(index, e.target.value, 'afternoon')}
                              placeholder="Optional notes"
                            />
                          </td>
                        </tr>
                      </React.Fragment>
                    )
                  }

                  return (
                    <tr key={entry.date}>
                      <td>{formatFriendlyDate(entry.date)}</td>
                      <td>{entry.dayName}</td>
                      <td>
                        {renderLocationSelect(entry.location, (loc) => handleLocationChange(index, loc))}
                      </td>
                      <td>
                        {renderClientInput(
                          entry.location,
                          entry.client,
                          entry.isCustomClient,
                          (client) => handleClientChange(index, client)
                      )}
                    </td>
                    <td>
                      <input
                        className="notes-input"
                        type="text"
                        value={entry.notes}
                        onChange={(e) => handleNotesChange(index, e.target.value)}
                        placeholder="Optional notes"
                      />
                    </td>
                      <td>
                        <button
                          className="preset-btn"
                          onClick={() => toggleSplit(index)}
                          type="button"
                          style={{
                            fontSize: '11px',
                            padding: '6px 10px',
                            borderWidth: '1px',
                          }}
                        >
                          <span style={{ fontSize: '14px', marginRight: '4px' }}>✂️</span> Split
                        </button>
                      </td>
                  </tr>
                  )
                })}
              </tbody>
            </table>
          )}

          <button
            className="save-btn"
            onClick={handleSaveWeek}
            disabled={loading}
          >
            {loading ? 'Saving...' : isEditMode ? 'Update my week' : 'Save my week'}
          </button>

          {/* Overwrite confirmation bar */}
          {showOverwriteConfirm && (
            <div style={{
              marginTop: '8px',
              padding: '10px 14px',
              background: 'var(--warn-bg)',
              border: '1px solid var(--warn-border)',
              borderRadius: '4px',
              color: 'var(--warn-text)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              gap: '12px',
              fontSize: '14px'
            }}>
              <div style={{ fontWeight: 700 }}>
                Update week for {userName.trim()}? This replaces previous entries.
              </div>
              <div style={{ display: 'flex', gap: '8px' }}>
                <button className="preset-btn" onClick={cancelOverwrite} type="button">Cancel</button>
                <button className="preset-btn" onClick={confirmOverwriteAndSave} type="button" style={{ borderColor: 'var(--btn-highlight-color)', color: 'var(--btn-highlight-color)' }}>Confirm update</button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Undo bar after save */}
      {showUndoBar && (
        <div className="toast toast-undo">
          Updated. <button className="preset-btn" onClick={handleUndo} type="button" style={{ marginLeft: 8 }}>Undo</button>
        </div>
      )}

      {viewMode === 'dashboard' && (
        <div className="dashboard">
          <h2>Team Dashboard — Week of {formatWeekHeading(weekStart)}</h2>

          {loading ? (
            <div className="skeleton-container">
              {[1, 2, 3, 4, 5].map(i => (
                <div key={i} className="skeleton-day">
                  <div className="skeleton-bar skeleton-title-bar" />
                  <div className="skeleton-bar" />
                  <div className="skeleton-bar skeleton-bar-short" />
                </div>
              ))}
            </div>
          ) : Object.keys(groupedEntries).length === 0 ? (
            <div className="empty-state">
              <h3>No entries found for this week</h3>
              <p>Team members haven't submitted their work locations yet.</p>
              <button
                className="preset-btn preset-btn-highlight"
                type="button"
                onClick={() => { setViewMode('fill'); setIsEditMode(false) }}
                style={{ marginTop: '12px' }}
              >
                Fill my week
              </button>
            </div>
          ) : (
            <>
              {/* Sticky day-of-week navigation */}
              <div className="day-nav-sticky">
                {Object.keys(groupedEntries).sort().map((date) => {
                  const [y, m, d] = date.split('-').map(Number)
                  const dayShort = new Date(y, m - 1, d).toLocaleDateString('en-US', { weekday: 'short' })
                  const isToday = date === todayStr
                  return (
                    <button
                      key={date}
                      className={`day-nav-btn ${isToday ? 'active' : ''}`}
                      onClick={() => document.getElementById(`day-${date}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })}
                    >
                      {dayShort}
                    </button>
                  )
                })}
                <button
                  className="day-nav-btn day-nav-top-btn"
                  onClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}
                  type="button"
                  aria-label="Back to top"
                  title="Back to top"
                >
                  <span aria-hidden="true">↑</span> Top
                </button>
              </div>

              {/* Regular by location view */}
              {Object.keys(groupedEntries)
                .sort()
                .map((date) => {
                  // Check if this date is today
                  const isToday = date === todayStr

                  // Compute attendance stats for this day
                  const dayAllEntries = groupedEntries[date] ? Object.values(groupedEntries[date]).flat() : []
                  const enteredUsers = new Set(dayAllEntries.map(e => e.user_name))
                  const nealStreetCount = new Set(dayAllEntries.filter(e => e.location === 'Neal Street').map(e => e.user_name)).size
                  const clientOfficeCount = new Set(dayAllEntries.filter(e => e.location === 'Client Office').map(e => e.user_name)).size
                  const wfhCount = new Set(dayAllEntries.filter(e => e.location === 'WFH').map(e => e.user_name)).size
                  const workingFromAbroadCount = new Set(dayAllEntries.filter(e => e.location === 'Working From Abroad').map(e => e.user_name)).size
                  const holidayCount = new Set(dayAllEntries.filter(e => e.location === 'Holiday').map(e => e.user_name)).size
                  const otherCount = new Set(dayAllEntries.filter(e => e.location === 'Other').map(e => e.user_name)).size
                  const notEnteredCount = allUsers.length - enteredUsers.size

                  const attendanceItems = [
                    { emoji: '🏢', count: nealStreetCount, label: 'Neal Street' },
                    { emoji: '💼', count: clientOfficeCount, label: 'Client Office' },
                    { emoji: '🏠', count: wfhCount, label: 'WFH' },
                    { emoji: '✈️', count: workingFromAbroadCount, label: 'Working From Abroad' },
                    { emoji: '🌴', count: holidayCount, label: 'Holiday' },
                    { emoji: '📝', count: otherCount, label: 'Other' },
                    { emoji: '❓', count: notEnteredCount, label: 'Not entered' },
                  ].filter(item => item.count > 0)

                  return (
                  <div
                    id={`day-${date}`}
                    key={date}
                    className={`day-section ${isToday ? 'today' : ''}`}
                    ref={isToday ? todaySectionRef : null}
                  >
                    <h3>
                      {new Date(date).toLocaleDateString('en-US', {
                        weekday: 'long',
                        year: 'numeric',
                        month: 'long',
                        day: 'numeric',
                      })}
                    </h3>

                    {/* Attendance summary bar */}
                    <div className="attendance-bar">
                      {attendanceItems.map((item, index) => (
                        <React.Fragment key={item.label}>
                          {index > 0 && <span className="attendance-dot">·</span>}
                          <span>{item.emoji} {item.count} {item.label}</span>
                        </React.Fragment>
                      ))}
                    </div>

                    {(() => {
                      // Base-location grouping is precomputed in the dashboardData useMemo above
                      const locationGroups = locationGroupsByDate[date] || {}

                      // Render each location group
                      return locationOrder.map((baseLocation) => {
                        const locationVariants = locationGroups[baseLocation]
                        if (!locationVariants || Object.keys(locationVariants).length === 0) return null

                        // Get all entries for this base location (across all time periods)
                        const allEntriesForLocation: SummaryRow[] = []
                        for (const variantKey of Object.keys(locationVariants)) {
                          allEntriesForLocation.push(...locationVariants[variantKey])
                        }

                        if (allEntriesForLocation.length === 0) return null

                        // If location is Client Office or Other, group by client/description only (time_period shown next to name)
                        if (baseLocation === 'Client Office' || baseLocation === 'Other') {
                          // Group by description only (not time_period)
                          const entriesByDescription = allEntriesForLocation.reduce((acc, entry) => {
                            const description = entry.client || (baseLocation === 'Other' ? 'No description' : 'No Client')
                            if (!acc[description]) {
                              acc[description] = []
                            }
                            acc[description].push(entry)
                            return acc
                          }, {} as { [key: string]: SummaryRow[] })

                        return (
                            <div key={baseLocation} className="location-group" style={{ borderLeft: `3px solid ${getLocationAccentColor(baseLocation)}` }}>
                            <div className="location-group-title">
                                <span className={`location-badge ${getLocationBadgeClass(baseLocation)}`}>
                                  {baseLocation}
                              </span>
                            </div>
                              {Object.keys(entriesByDescription).sort().map((description) => {
                                const entries = entriesByDescription[description]
                              // For Client Office, check if it's a custom client
                                const isCustomClient = baseLocation === 'Client Office' && !clientOptions.includes(description)
                                const heading = baseLocation === 'Other'
                                  ? description
                                : isCustomClient
                                    ? `Other (${description})`
                                    : description

                              return (
                                  <div key={description} style={{ marginTop: '10px' }}>
                                  <div style={{ fontSize: '14px', fontWeight: '700', marginBottom: '5px', color: 'var(--text-heading)' }}>
                                    📊 {heading}
                                  </div>
                                  <div className="location-people">
                                      {entries.map((entry, index) => (
                                      <span key={`${entry.user_name}-${index}`} className="person-name-inline">
                                        {entry.user_name}
                                        {/* Show time period next to name if it's not a full day */}
                                        {entry.time_period && entry.time_period !== 'Full Day' && ` (${entry.time_period})`}
                                      </span>
                                    ))}
                                  </div>
                                </div>
                              )
                            })}
                          </div>
                          )
                        }

                        // Regular display for other locations - show all entries together with time period next to name
                        return (
                          <div key={baseLocation} className="location-group" style={{ borderLeft: `3px solid ${getLocationAccentColor(baseLocation)}` }}>
                            <div className="location-group-title">
                              <span className={`location-badge ${getLocationBadgeClass(baseLocation)}`}>
                                {baseLocation}
                              </span>
                            </div>
                            <div className="location-people">
                              {allEntriesForLocation.map((entry, index) => (
                                <span key={`${entry.user_name}-${entry.time_period || 'full'}-${index}`} className="person-name-inline">
                                  {entry.user_name}
                                  {/* Show time period next to name if it's not a full day */}
                                  {entry.time_period && entry.time_period !== 'Full Day' && ` (${entry.time_period})`}
                                  {(entry.client && (baseLocation === 'Client Office' || baseLocation === 'Other')) && ` (${entry.client})`}
                                </span>
                              ))}
                            </div>
                          </div>
                        )
                      })
                    })()}

                    {/* Show people who haven't entered */}
                    {(() => {
                      // Get all people who have entered for this date
                      const enteredUsersForDay = new Set(
                        (groupedEntries[date] ? Object.values(groupedEntries[date]).flat() : []).map(e => e.user_name)
                      )

                      // Get people from team-members.json who haven't entered, sorted alphabetically
                      const notEntered = allUsers
                        .filter(user => !enteredUsersForDay.has(user))
                        .sort()

                      if (notEntered.length === 0) return null

                      return (
                        <div className="location-group" style={{ marginTop: '16px', borderLeft: '3px solid var(--border-input-color)' }}>
                          <div className="location-group-title">
                            <span className="location-badge location-none">
                              Not Entered ({notEntered.length})
                            </span>
                          </div>
                          <div className="not-entered-pills">
                            {notEntered.map((name) => (
                              <span key={`missing-${name}`} className="not-entered-pill">{name}</span>
                            ))}
                          </div>
                        </div>
                      )
                    })()}
                  </div>
                  )
                })}
            </>
          )}
        </div>
      )}
    </div>
  )
}

export default App
