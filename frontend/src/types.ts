export interface Entry {
  date: string
  location: string
  time_period?: string | null  // 'Morning', 'Afternoon', or null for full day
  client?: string | null
  notes?: string | null
}

export interface BulkUpsertRequest {
  user_name: string
  entries: Entry[]
}

export interface BulkUpsertResponse {
  ok: boolean
  count: number
}

export interface SummaryRow {
  user_name: string
  date: string
  location: string
  time_period?: string | null  // 'Morning', 'Afternoon', or null for full day
  client?: string | null
  notes?: string | null
}

export interface WeekSummaryResponse {
  entries: SummaryRow[]
}

export type WorkLocation = 'Neal Street' | 'WFH' | 'Client Office' | 'Holiday' | 'Working From Abroad' | 'Other'

export interface WeekEntry {
  date: string
  dayName: string
  location: WorkLocation
  morningLocation?: WorkLocation  // Only used when split mode is enabled
  afternoonLocation?: WorkLocation  // Only used when split mode is enabled
  client: string
  morningClient?: string  // Client for morning when split
  afternoonClient?: string  // Client for afternoon when split
  notes: string
  morningNotes?: string  // Notes for morning when split
  afternoonNotes?: string  // Notes for afternoon when split
  isCustomClient: boolean
  morningIsCustomClient?: boolean
  afternoonIsCustomClient?: boolean
}

export type ClientOption = 'FT' | 'Other'

export interface ExistingEntry {
  date: string
  location: string
  time_period?: string | null
  client?: string | null
  notes?: string | null
}

export interface AdminEntry {
  id: number
  user_name: string
  date: string
  location: string
  time_period?: string | null
  client?: string | null
  notes?: string | null
  created_at: string
  updated_at?: string | null
}
