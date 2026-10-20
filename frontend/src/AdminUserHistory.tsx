import { useEffect, useRef, useState } from 'react'
import { deleteUserHistory, getAllUsers, restoreUserHistory } from './api'
import { DeletedEntry } from './types'

const UNDO_WINDOW_MS = 30000

// Admin tab section: permanently remove everything a person has saved, behind
// an "Are you sure?" dialog, with a time-limited Undo that re-inserts the
// entries the delete call handed back.
export default function AdminUserHistory({
  adminPassword,
  onChange,
}: {
  adminPassword: string
  onChange: () => void
}) {
  const [users, setUsers] = useState<string[]>([])
  const [searchTerm, setSearchTerm] = useState('')
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [pendingDelete, setPendingDelete] = useState<string | null>(null)
  const [lastDeleted, setLastDeleted] = useState<{ user: string; entries: DeletedEntry[] } | null>(null)
  const undoTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const loadUsers = async () => {
    setLoading(true)
    try {
      const result = await getAllUsers()
      setUsers(result.users)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load users')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadUsers()
    return () => {
      if (undoTimer.current) clearTimeout(undoTimer.current)
    }
  }, [])

  // Escape closes the confirm dialog
  useEffect(() => {
    if (!pendingDelete) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setPendingDelete(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [pendingDelete])

  const flash = (text: string) => {
    setMessage(text)
    setTimeout(() => setMessage(''), 3000)
  }

  const handleConfirmDelete = async () => {
    const user = pendingDelete
    if (!user) return
    setBusy(true)
    setError('')
    try {
      const result = await deleteUserHistory(user, adminPassword)
      setPendingDelete(null)
      setUsers((prev) => prev.filter((u) => u !== user))
      setLastDeleted({ user, entries: result.entries })
      if (undoTimer.current) clearTimeout(undoTimer.current)
      undoTimer.current = setTimeout(() => setLastDeleted(null), UNDO_WINDOW_MS)
      onChange()
    } catch (e) {
      setPendingDelete(null)
      setError(e instanceof Error ? e.message : `Failed to delete ${user}'s history`)
    } finally {
      setBusy(false)
    }
  }

  const handleUndo = async () => {
    if (!lastDeleted) return
    if (undoTimer.current) clearTimeout(undoTimer.current)
    setBusy(true)
    setError('')
    try {
      const result = await restoreUserHistory(lastDeleted.entries, adminPassword)
      flash(
        result.skipped
          ? `Restored ${lastDeleted.user} (${result.skipped} day${result.skipped === 1 ? '' : 's'} they'd re-entered kept as-is)`
          : `Restored ${lastDeleted.user}'s history`
      )
      setLastDeleted(null)
      await loadUsers()
      onChange()
    } catch (e) {
      // Keep the backup (and the Undo button) so it can be retried
      setError(e instanceof Error ? e.message : `Failed to restore ${lastDeleted.user}'s history`)
    } finally {
      setBusy(false)
    }
  }

  const filtered = users.filter((u) => u.toLowerCase().includes(searchTerm.toLowerCase()))

  return (
    <div className="admin-chart-card admin-user-history">
      <h3>Delete a user's history</h3>
      <p className="admin-user-history-hint">
        Removes every entry a person has ever saved, across all weeks.
      </p>

      <input
        type="text"
        className="admin-user-history-search"
        value={searchTerm}
        onChange={(e) => setSearchTerm(e.target.value)}
        placeholder="Search users"
        aria-label="Search users"
      />

      {error && <div className="error-message">{error}</div>}

      {loading && users.length === 0 ? (
        <p className="admin-user-history-hint">Loading users...</p>
      ) : filtered.length === 0 ? (
        <p className="admin-user-history-hint">{users.length === 0 ? 'No users with saved history.' : 'No matches.'}</p>
      ) : (
        <ul className="admin-user-history-list">
          {filtered.map((user) => (
            <li key={user} className="admin-user-history-row">
              <span>{user}</span>
              <button
                type="button"
                className="user-card-confirm-yes"
                onClick={() => setPendingDelete(user)}
                disabled={busy}
              >
                Delete history
              </button>
            </li>
          ))}
        </ul>
      )}

      {pendingDelete && (
        <div className="admin-modal-overlay" onClick={() => !busy && setPendingDelete(null)}>
          <div
            className="admin-modal"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="delete-history-title"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 id="delete-history-title">Are you sure?</h3>
            <p>
              This deletes <strong>all</strong> of <strong>{pendingDelete}</strong>'s saved entries, for every
              week. They'll disappear from the dashboards.
            </p>
            <div className="admin-modal-actions">
              <button
                type="button"
                className="user-card-confirm-no"
                onClick={() => setPendingDelete(null)}
                disabled={busy}
                autoFocus
              >
                Cancel
              </button>
              <button type="button" className="user-card-confirm-yes" onClick={handleConfirmDelete} disabled={busy}>
                {busy ? 'Deleting...' : 'Yes, delete'}
              </button>
            </div>
          </div>
        </div>
      )}

      {lastDeleted && (
        <div className="toast toast-undo">
          Deleted {lastDeleted.user}'s history.
          <button className="preset-btn" onClick={handleUndo} type="button" style={{ marginLeft: 8 }} disabled={busy}>
            {busy ? 'Restoring...' : 'Undo'}
          </button>
        </div>
      )}

      {message && <div className="toast">{message}</div>}
    </div>
  )
}
