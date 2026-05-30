import { useState } from 'react'

const FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"
const MONO = "'Cascadia Code', 'Fira Code', 'JetBrains Mono', monospace"

const STATUS = {
  draft:    { label: 'Draft',    color: '#6b7280', bg: '#6b72801a', dot: '#6b7280' },
  proposed: { label: 'Proposed', color: '#6366f1', bg: '#6366f11a', dot: '#6366f1' },
  running:  { label: 'Running',  color: '#f59e0b', bg: '#f59e0b1a', dot: '#f59e0b' },
  executed: { label: 'Executed', color: '#10b981', bg: '#10b9811a', dot: '#10b981' },
  saved:    { label: 'Saved',    color: '#059669', bg: '#05966918', dot: '#059669' },
}

function relTime(ts) {
  if (!ts) return '—'
  const diff = Date.now() - new Date(ts).getTime()
  const min  = Math.floor(diff / 60_000)
  if (min < 1)  return 'just now'
  if (min < 60) return `${min}m ago`
  const h = Math.floor(min / 60)
  if (h < 24)   return `${h}h ago`
  const d = Math.floor(h / 24)
  if (d < 30)   return `${d}d ago`
  return new Date(ts).toLocaleDateString()
}

function lastActivity(ws) {
  return ws.saved_at || ws.executed_at || ws.proposed_at || ws.created_at
}

function StatusBadge({ status, C }) {
  const s = STATUS[status] || STATUS.draft
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: '4px',
      fontSize: '0.62rem', fontWeight: '700', letterSpacing: '0.04em',
      padding: '2px 7px', borderRadius: '4px',
      background: s.bg, color: s.color,
    }}>
      <span style={{ width: '5px', height: '5px', borderRadius: '50%', background: s.dot, display: 'inline-block' }} />
      {s.label}
    </span>
  )
}

// ─── Filter tabs ──────────────────────────────────────────────────────────────
const FILTERS = [
  { id: 'all',    label: 'All'    },
  { id: 'active', label: 'Active' },   // draft | proposed | executed
  { id: 'saved',  label: 'Saved'  },
]

function applyFilter(workspaces, filter) {
  if (filter === 'all')    return workspaces
  if (filter === 'saved')  return workspaces.filter(w => w.status === 'saved')
  if (filter === 'active') return workspaces.filter(w => ['draft', 'proposed', 'executed'].includes(w.status))
  return workspaces
}

// ─── Workspace row card ───────────────────────────────────────────────────────
function WorkspaceCard({ ws, onReopen, runningWorkspaceId, C }) {
  const reportType = ws.proposal_source === 'ai_assisted' ? 'AI-assisted' : 'Smart Plan'
  const title = ws.title || ws.intent_text?.slice(0, 60) || 'Untitled'
  const displayStatus = ws.id === runningWorkspaceId ? 'running' : ws.status

  return (
    <div
      onClick={() => onReopen(ws)}
      style={{
        background: C.bg,
        border: `1px solid ${C.border}`,
        borderRadius: '10px',
        padding: '12px 14px',
        cursor: 'pointer',
        transition: 'border-color 0.12s',
      }}
      onMouseEnter={e => e.currentTarget.style.borderColor = '#6366f1'}
      onMouseLeave={e => e.currentTarget.style.borderColor = C.border}
    >
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: '10px', marginBottom: '7px' }}>
        <StatusBadge status={displayStatus} C={C} />
        <span style={{
          fontSize: '0.82rem', fontWeight: '600', color: C.text, flex: 1, minWidth: 0,
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}>
          {title}
        </span>
        <span style={{ fontSize: '0.66rem', color: C.textMuted, flexShrink: 0 }}>
          {relTime(lastActivity(ws))}
        </span>
      </div>

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px', alignItems: 'center' }}>
        {ws.dataset_filename && (
          <span style={{ fontSize: '0.67rem', color: C.textMuted, fontFamily: MONO, background: C.surface, border: `1px solid ${C.border}`, padding: '1px 6px', borderRadius: '4px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '160px' }}>
            {ws.dataset_filename}
          </span>
        )}
        {ws.proposal_source && (
          <span style={{ fontSize: '0.64rem', color: '#8b5cf6', background: '#8b5cf61a', padding: '1px 6px', borderRadius: '4px' }}>
            {reportType}
          </span>
        )}
        {ws.report_id && (
          <span style={{ fontSize: '0.64rem', color: '#10b981', background: '#10b9811a', padding: '1px 6px', borderRadius: '4px' }}>
            Report attached
          </span>
        )}
        {ws.intent_text && (
          <span style={{ fontSize: '0.67rem', color: C.textMuted, fontStyle: 'italic', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '260px', flex: 1 }}>
            "{ws.intent_text.length > 80 ? ws.intent_text.slice(0, 79) + '…' : ws.intent_text}"
          </span>
        )}
      </div>

      {ws.executed_at && (
        <div style={{ marginTop: '5px', display: 'flex', gap: '12px' }}>
          {ws.executed_at && (
            <span style={{ fontSize: '0.63rem', color: C.textMuted }}>
              Executed {relTime(ws.executed_at)}
            </span>
          )}
          {ws.saved_at && (
            <span style={{ fontSize: '0.63rem', color: '#059669' }}>
              Saved {relTime(ws.saved_at)}
            </span>
          )}
        </div>
      )}
    </div>
  )
}

// ─── Main component ───────────────────────────────────────────────────────────
export default function WorkspaceHistory({ workspaces, loading, onReopen, runningWorkspaceId = null, C }) {
  const [filter, setFilter] = useState('all')
  const [search, setSearch] = useState('')

  const visible = applyFilter(workspaces, filter).filter(w => {
    if (!search) return true
    const q = search.toLowerCase()
    return (
      (w.title || '').toLowerCase().includes(q) ||
      (w.intent_text || '').toLowerCase().includes(q) ||
      (w.dataset_filename || '').toLowerCase().includes(q)
    )
  })

  const counts = {
    all:    workspaces.length,
    active: workspaces.filter(w => ['draft', 'proposed', 'executed'].includes(w.status)).length,
    saved:  workspaces.filter(w => w.status === 'saved').length,
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '0' }}>

      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: '14px' }}>
        <div>
          <h2 style={{ margin: '0 0 3px', fontSize: '1.35rem', fontWeight: '700', color: C.text, letterSpacing: '-0.4px' }}>
            AI Workspaces
          </h2>
          <p style={{ margin: 0, color: C.textMuted, fontSize: '0.74rem' }}>
            Persistent sessions — reopen any workspace to continue where you left off.
          </p>
        </div>
      </div>

      {/* Filter tabs + search */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '14px', flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', gap: '2px', background: C.surface, border: `1px solid ${C.border}`, borderRadius: '8px', padding: '3px' }}>
          {FILTERS.map(f => (
            <button
              key={f.id}
              onClick={() => setFilter(f.id)}
              style={{
                padding: '5px 12px', borderRadius: '6px', border: 'none', cursor: 'pointer',
                fontSize: '0.74rem', fontFamily: FONT, fontWeight: filter === f.id ? '600' : '400',
                color: filter === f.id ? '#6366f1' : C.textSec,
                background: filter === f.id ? '#6366f11a' : 'transparent',
                transition: 'all 0.1s',
              }}
            >
              {f.label}
              {counts[f.id] > 0 && (
                <span style={{ marginLeft: '5px', fontSize: '0.62rem', color: filter === f.id ? '#6366f1' : C.textMuted, fontWeight: '600' }}>
                  {counts[f.id]}
                </span>
              )}
            </button>
          ))}
        </div>

        <input
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder="Search workspaces…"
          style={{
            flex: 1, minWidth: '160px', background: C.surface, border: `1px solid ${C.border}`,
            borderRadius: '8px', padding: '6px 12px', fontSize: '0.78rem', color: C.text,
            fontFamily: FONT, outline: 'none',
          }}
        />
      </div>

      {/* Workspace list */}
      {loading ? (
        <div style={{ textAlign: 'center', padding: '32px 0', color: C.textMuted, fontSize: '0.82rem' }}>
          Loading workspaces…
        </div>
      ) : visible.length === 0 ? (
        <div style={{ textAlign: 'center', padding: '40px 0', color: C.textMuted }}>
          <div style={{ fontSize: '2rem', marginBottom: '10px' }}>⬡</div>
          <div style={{ fontSize: '0.88rem', fontWeight: '500', marginBottom: '4px' }}>
            {search ? 'No workspaces match your search.' : `No ${filter !== 'all' ? filter + ' ' : ''}workspaces yet.`}
          </div>
          {!search && filter === 'all' && (
            <div style={{ fontSize: '0.74rem', color: C.textMuted, marginTop: '4px' }}>
              Use "Compose Plan" on the Overview tab to create your first workspace.
            </div>
          )}
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
          {visible.map(ws => (
            <WorkspaceCard key={ws.id} ws={ws} onReopen={onReopen} runningWorkspaceId={runningWorkspaceId} C={C} />
          ))}
        </div>
      )}
    </div>
  )
}
