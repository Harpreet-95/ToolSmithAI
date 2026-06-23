/**
 * EngineWorkspace — AI Tools library.
 *
 * UX layer only — all backend calls go through the same engine API.
 * Internal identifiers (EngineWorkspace, listEngineTools, etc.) are intentionally unchanged.
 */

import { useState, useEffect } from 'react'
import {
  planEngineTool,
  saveEngineTool,
  submitEngineTool,
  approveEngineTool,
  executeEngineTool,
  getEngineToolRuns,
  getEngineTool,
  listEngineTools,
} from '../api/client'

const FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"

// ── Status metadata ────────────────────────────────────────────────────────
const TOOL_STATUS = {
  draft:            { label: 'Draft',          color: '#94a3b8' },
  pending_approval: { label: 'Pending Review', color: '#fbbf24' },
  approved:         { label: 'Active',         color: '#22c55e' },
  deprecated:       { label: 'Deprecated',     color: '#f87171' },
}

const RUN_STATUS = {
  completed: { label: 'Completed', color: '#22c55e' },
  failed:    { label: 'Failed',    color: '#f87171' },
  running:   { label: 'Running',   color: '#38bdf8' },
  cancelled: { label: 'Cancelled', color: '#94a3b8' },
  skipped:   { label: 'Skipped',   color: '#94a3b8' },
}

function toolStatusMeta(s) { return TOOL_STATUS[s] ?? { label: s ?? 'Unknown', color: '#94a3b8' } }
function runStatusMeta(s)  { return RUN_STATUS[s]  ?? { label: s ?? 'Unknown', color: '#94a3b8' } }

// ── Helpers ────────────────────────────────────────────────────────────────
function slugToTitle(s) {
  if (!s) return 'Untitled Tool'
  return s
    .replace(/[_-]+/g, ' ')
    .replace(/([a-z])([A-Z])/g, '$1 $2')
    .replace(/\b\w/g, c => c.toUpperCase())
    .trim()
}

function displayTitle(t) {
  if (!t) return 'Untitled Tool'
  const desc = t.description ?? t.plan?.description
  if (desc) {
    const trimmed = desc.length <= 72 ? desc : desc.split(/[.!?]/)[0].trim()
    if (trimmed) return trimmed
  }
  return slugToTitle(t.name)
}

function fmtDate(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

function fmtRelative(iso) {
  if (!iso) return null
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 2)   return 'just now'
  if (mins < 60)  return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24)   return `${hrs}h ago`
  const days = Math.floor(hrs / 24)
  if (days === 1) return 'yesterday'
  if (days < 7)   return `${days}d ago`
  return fmtDate(iso)
}

// ── Status badge ───────────────────────────────────────────────────────────
function StatusBadge({ status }) {
  const m = toolStatusMeta(status)
  return (
    <span style={{
      display: 'inline-block',
      padding: '2px 9px',
      borderRadius: '10px',
      fontSize: '0.67rem',
      fontWeight: '700',
      letterSpacing: '0.05em',
      textTransform: 'uppercase',
      background: `${m.color}20`,
      color: m.color,
      border: `1px solid ${m.color}50`,
      whiteSpace: 'nowrap',
    }}>
      {m.label}
    </span>
  )
}

// ── Toast ──────────────────────────────────────────────────────────────────
function Toast({ toast, success, errCol }) {
  if (!toast) return null
  return (
    <div style={{
      position: 'fixed', bottom: '24px', right: '24px',
      background: toast.ok ? `${success}20` : `${errCol}20`,
      border: `1px solid ${toast.ok ? success + '50' : errCol + '50'}`,
      color: toast.ok ? success : errCol,
      borderRadius: '10px', padding: '10px 18px',
      fontSize: '0.81rem', fontFamily: FONT, fontWeight: '500',
      maxWidth: '380px', zIndex: 9999,
      boxShadow: '0 4px 24px rgba(0,0,0,0.45)',
    }}>
      {toast.text}
    </div>
  )
}

// ── Tab bar (future-ready structure) ──────────────────────────────────────
const TABS = [
  { id: 'my-tools',  label: 'My Tools',  enabled: true  },
  { id: 'templates', label: 'Templates', enabled: false },
  { id: 'shared',    label: 'Shared',    enabled: false },
  { id: 'usage',     label: 'Usage',     enabled: false },
]

// ── Tool icon ──────────────────────────────────────────────────────────────
function ToolIcon({ accent }) {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke={accent} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>
    </svg>
  )
}

// ── Main component ─────────────────────────────────────────────────────────
export default function EngineWorkspace({ C = {}, token }) {
  const bg      = C.bg       ?? '#0f1117'
  const surface = C.surface  ?? '#1a1f2e'
  const border  = C.border   ?? '#2a2f3f'
  const text    = C.text     ?? '#e8eaf0'
  const muted   = C.textMuted ?? '#7b8099'
  const accent  = C.accent   ?? '#7c6af5'
  const success = C.success  ?? '#22c55e'
  const errCol  = C.error    ?? '#f87171'
  const warn    = C.warn     ?? '#fbbf24'

  // ── Navigation ──────────────────────────────────────────────────────────
  const [view,      setView]      = useState('list')    // 'list' | 'detail' | 'create'
  const [activeTab, setActiveTab] = useState('my-tools')

  // ── Tool list ────────────────────────────────────────────────────────────
  const [savedTools,   setSavedTools]   = useState([])
  const [toolsLoading, setToolsLoading] = useState(false)

  // ── Tool detail ───────────────────────────────────────────────────────────
  const [selectedTool, setSelectedTool] = useState(null)
  const [selectedRuns, setSelectedRuns] = useState([])
  const [runsLoading,  setRunsLoading]  = useState(false)

  // ── Create flow ───────────────────────────────────────────────────────────
  const [intent,      setIntent]      = useState('')
  const [pendingPlan, setPendingPlan] = useState(null)

  // ── Shared ────────────────────────────────────────────────────────────────
  const [busy,  setBusy]  = useState(null)
  const [toast, setToast] = useState(null)

  function notify(msg, ok = true) {
    setToast({ text: msg, ok })
    setTimeout(() => setToast(null), 5000)
  }
  function notifyErr(e) { notify(e?.message ?? String(e), false) }

  // ── Style helpers ─────────────────────────────────────────────────────────
  function card(extra = {}) {
    return { background: surface, border: `1px solid ${border}`, borderRadius: '12px', padding: '20px 22px', ...extra }
  }

  function btn(variant, disabled) {
    const base = {
      padding: '8px 16px', borderRadius: '8px', border: 'none',
      cursor: disabled ? 'not-allowed' : 'pointer',
      fontSize: '0.8rem', fontWeight: '600', fontFamily: FONT,
      opacity: disabled ? 0.42 : 1, transition: 'all 0.12s',
      display: 'inline-flex', alignItems: 'center', gap: '6px',
    }
    if (variant === 'primary') return { ...base, background: accent, color: '#fff' }
    if (variant === 'ghost')   return { ...base, background: 'transparent', color: accent, border: `1px solid ${accent}50` }
    if (variant === 'danger')  return { ...base, background: `${errCol}15`, color: errCol, border: `1px solid ${errCol}30` }
    return { ...base, background: `${border}80`, color: text, border: `1px solid ${border}` }
  }

  const sectionLabel = {
    margin: '0 0 14px', fontSize: '0.71rem', fontWeight: '700',
    letterSpacing: '0.08em', textTransform: 'uppercase', color: muted, fontFamily: FONT,
  }

  // ── Data loading ──────────────────────────────────────────────────────────
  useEffect(() => { loadTools() }, []) // eslint-disable-line

  async function loadTools() {
    setToolsLoading(true)
    try {
      const res = await listEngineTools(token)
      setSavedTools(res?.data ?? [])
    } catch (e) { notifyErr(e) }
    finally { setToolsLoading(false) }
  }

  async function loadToolRuns(toolId) {
    setRunsLoading(true)
    try {
      const res = await getEngineToolRuns(toolId, token)
      setSelectedRuns(res?.data ?? [])
    } catch { /* non-critical */ }
    finally { setRunsLoading(false) }
  }

  // ── Navigation actions ────────────────────────────────────────────────────
  async function openTool(toolId) {
    setBusy('open-' + toolId)
    try {
      const res = await getEngineTool(toolId, token)
      const td = res?.data ?? res
      setSelectedTool(td)
      setSelectedRuns([])
      setView('detail')
      loadToolRuns(toolId)
    } catch (e) { notifyErr(e) }
    finally { setBusy(null) }
  }

  function backToList() {
    setView('list')
    setSelectedTool(null)
    setSelectedRuns([])
  }

  function startCreate() {
    setIntent('')
    setPendingPlan(null)
    setView('create')
  }

  // ── Run tool ──────────────────────────────────────────────────────────────
  async function runTool(toolId) {
    setBusy('run-' + toolId)
    try {
      await executeEngineTool(toolId, {}, token)
      notify('Tool started successfully.')
      if (selectedTool?.id === toolId) loadToolRuns(toolId)
    } catch (e) { notifyErr(e) }
    finally { setBusy(null) }
  }

  // ── Create flow ───────────────────────────────────────────────────────────
  async function handleGenerate() {
    if (!intent.trim()) return
    setBusy('plan')
    setPendingPlan(null)
    try {
      const res = await planEngineTool(intent.trim(), token)
      setPendingPlan(res?.data ?? res)
    } catch (e) { notifyErr(e) }
    finally { setBusy(null) }
  }

  async function handleSaveAndActivate() {
    if (!pendingPlan) return
    setBusy('save')
    try {
      const res = await saveEngineTool(pendingPlan, token)
      const { tool_id } = res?.data ?? res
      await submitEngineTool(tool_id, token)
      await approveEngineTool(tool_id, token)
      notify(`"${displayTitle(pendingPlan)}" created and activated.`)
      await loadTools()
      setView('list')
      setIntent('')
      setPendingPlan(null)
    } catch (e) { notifyErr(e) }
    finally { setBusy(null) }
  }

  async function handleSubmitForApproval(toolId) {
    setBusy('submit-' + toolId)
    try {
      await submitEngineTool(toolId, token)
      notify('Tool submitted for approval.')
      const res = await getEngineTool(toolId, token)
      setSelectedTool(res?.data ?? res)
      await loadTools()
    } catch (e) { notifyErr(e) }
    finally { setBusy(null) }
  }

  async function handleApprove(toolId) {
    setBusy('approve-' + toolId)
    try {
      await approveEngineTool(toolId, token)
      notify('Tool approved and now active.')
      const res = await getEngineTool(toolId, token)
      setSelectedTool(res?.data ?? res)
      await loadTools()
    } catch (e) { notifyErr(e) }
    finally { setBusy(null) }
  }

  // ── Recently used ─────────────────────────────────────────────────────────
  const recentlyUsed = [...savedTools]
    .filter(t => t.updated_at)
    .sort((a, b) => new Date(b.updated_at) - new Date(a.updated_at))
    .slice(0, 5)

  // ═══════════════════════════════════════════════════════════════════════════
  // RENDER: List (landing page)
  // ═══════════════════════════════════════════════════════════════════════════
  if (view === 'list') {
    return (
      <div style={{ fontFamily: FONT, color: text, padding: '28px 32px', maxWidth: '1040px' }}>

        {/* Page header */}
        <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: '28px', flexWrap: 'wrap', gap: '16px' }}>
          <div>
            <h1 style={{ margin: 0, fontSize: '1.4rem', fontWeight: '700', color: text, letterSpacing: '-0.3px' }}>AI Tools</h1>
            <p style={{ margin: '4px 0 0', fontSize: '0.82rem', color: muted }}>Build, run, and manage your custom AI tools.</p>
          </div>
          <button style={btn('primary', false)} onClick={startCreate}>+ Create AI Tool</button>
        </div>

        {/* Tab bar */}
        <div style={{ display: 'flex', gap: '2px', borderBottom: `1px solid ${border}`, marginBottom: '28px' }}>
          {TABS.map(t => {
            const isActive = t.id === activeTab
            return (
              <button
                key={t.id}
                onClick={() => t.enabled && setActiveTab(t.id)}
                style={{
                  padding: '10px 18px', background: 'transparent', border: 'none',
                  borderBottom: isActive ? `2px solid ${accent}` : '2px solid transparent',
                  color: isActive ? accent : t.enabled ? muted : `${muted}55`,
                  fontSize: '0.83rem', fontWeight: isActive ? '600' : '500', fontFamily: FONT,
                  cursor: t.enabled ? 'pointer' : 'default', marginBottom: '-1px',
                  transition: 'all 0.15s',
                }}
              >
                {t.label}
                {!t.enabled && (
                  <span style={{ marginLeft: '6px', fontSize: '0.61rem', background: `${border}`, color: `${muted}80`, padding: '1px 5px', borderRadius: '4px' }}>
                    Soon
                  </span>
                )}
              </button>
            )
          })}
        </div>

        {/* Recently Used */}
        {recentlyUsed.length > 0 && (
          <div style={{ marginBottom: '32px' }}>
            <h3 style={sectionLabel}>Recently Used</h3>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
              {recentlyUsed.map(t => (
                <div
                  key={t.id}
                  style={{ ...card({ padding: '12px 18px' }), display: 'flex', alignItems: 'center', gap: '14px', flexWrap: 'wrap' }}
                >
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <span style={{ fontSize: '0.87rem', fontWeight: '600', color: text, display: 'block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {displayTitle(t)}
                    </span>
                    {t.updated_at && (
                      <span style={{ fontSize: '0.72rem', color: muted }}>Last run {fmtRelative(t.updated_at)}</span>
                    )}
                  </div>
                  <StatusBadge status={t.status} />
                  <button
                    style={btn('ghost', busy === 'open-' + t.id || t.status !== 'approved')}
                    disabled={busy === 'open-' + t.id || t.status !== 'approved'}
                    onClick={() => runTool(t.id)}
                  >
                    Run Again →
                  </button>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* My AI Tools */}
        <div>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px', flexWrap: 'wrap', gap: '8px' }}>
            <h3 style={{ ...sectionLabel, margin: 0 }}>My AI Tools</h3>
            <button style={btn('default', toolsLoading)} disabled={toolsLoading} onClick={loadTools}>
              {toolsLoading ? 'Refreshing…' : 'Refresh'}
            </button>
          </div>

          {toolsLoading && savedTools.length === 0 ? (
            <div style={{ ...card({ textAlign: 'center', padding: '48px 24px' }) }}>
              <p style={{ color: muted, fontSize: '0.84rem', margin: 0 }}>Loading your tools…</p>
            </div>
          ) : savedTools.length === 0 ? (
            <div style={{ ...card({ textAlign: 'center', padding: '56px 24px' }) }}>
              <div style={{ fontSize: '2rem', marginBottom: '12px' }}>🛠</div>
              <p style={{ color: text, fontSize: '0.92rem', fontWeight: '600', margin: '0 0 6px' }}>No tools yet</p>
              <p style={{ color: muted, fontSize: '0.82rem', margin: '0 0 20px' }}>Create your first AI tool to automate workflows and analyses.</p>
              <button style={btn('primary', false)} onClick={startCreate}>+ Create AI Tool</button>
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              {savedTools.map(t => {
                const title = displayTitle(t)
                const isOpening = busy === 'open-' + t.id
                const isRunning = busy === 'run-' + t.id
                const canRun    = t.status === 'approved'
                return (
                  <div
                    key={t.id}
                    style={{
                      ...card({ padding: '16px 20px' }),
                      display: 'flex', alignItems: 'center', gap: '16px', flexWrap: 'wrap',
                      cursor: 'pointer', transition: 'border-color 0.15s',
                    }}
                    onClick={() => !isOpening && openTool(t.id)}
                  >
                    {/* Icon */}
                    <div style={{
                      width: '38px', height: '38px', borderRadius: '9px',
                      background: `${accent}15`, border: `1px solid ${accent}30`,
                      display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
                    }}>
                      <ToolIcon accent={accent} />
                    </div>

                    {/* Info */}
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap', marginBottom: '3px' }}>
                        <span style={{ fontSize: '0.9rem', fontWeight: '600', color: text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {title}
                        </span>
                        <StatusBadge status={t.status} />
                      </div>
                      <span style={{ fontSize: '0.74rem', color: muted }}>
                        Created {fmtDate(t.created_at)}
                        {t.updated_at && t.updated_at !== t.created_at && ` · Last run ${fmtRelative(t.updated_at)}`}
                      </span>
                    </div>

                    {/* Actions */}
                    <div style={{ display: 'flex', gap: '6px', flexShrink: 0 }} onClick={e => e.stopPropagation()}>
                      <button
                        style={btn('ghost', !canRun || isRunning || isOpening)}
                        disabled={!canRun || isRunning || isOpening}
                        title={!canRun ? 'Tool must be Active to run' : 'Run this tool'}
                        onClick={() => runTool(t.id)}
                      >
                        {isRunning ? 'Running…' : 'Run'}
                      </button>
                      <button
                        style={btn('default', isOpening)}
                        disabled={isOpening}
                        onClick={() => openTool(t.id)}
                      >
                        {isOpening ? 'Opening…' : 'Open'}
                      </button>
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>

        <Toast toast={toast} success={success} errCol={errCol} />
      </div>
    )
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // RENDER: Detail
  // ═══════════════════════════════════════════════════════════════════════════
  if (view === 'detail' && selectedTool) {
    const tool   = selectedTool
    const title  = displayTitle(tool)
    const nodes  = tool.graph?.nodes ?? []
    const canRun = tool.status === 'approved'
    const isRunning = busy === 'run-' + tool.id

    return (
      <div style={{ fontFamily: FONT, color: text, padding: '28px 32px', maxWidth: '860px' }}>

        {/* Back + header */}
        <button style={{ ...btn('default', false), marginBottom: '20px', fontSize: '0.77rem' }} onClick={backToList}>
          ← Back to AI Tools
        </button>

        <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: '24px', flexWrap: 'wrap', gap: '14px' }}>
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '6px', flexWrap: 'wrap' }}>
              <h1 style={{ margin: 0, fontSize: '1.3rem', fontWeight: '700', color: text }}>{title}</h1>
              <StatusBadge status={tool.status} />
            </div>
            {tool.description && tool.description !== title && (
              <p style={{ margin: 0, fontSize: '0.84rem', color: muted, maxWidth: '560px', lineHeight: 1.6 }}>{tool.description}</p>
            )}
          </div>
          <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
            <button
              style={btn('primary', !canRun || isRunning)}
              disabled={!canRun || isRunning}
              title={!canRun ? `Tool must be Active to run (current: ${toolStatusMeta(tool.status).label})` : undefined}
              onClick={() => runTool(tool.id)}
            >
              {isRunning ? 'Running…' : 'Run Tool'}
            </button>
            {tool.status === 'draft' && (
              <button
                style={btn('ghost', busy === 'submit-' + tool.id)}
                disabled={busy === 'submit-' + tool.id}
                onClick={() => handleSubmitForApproval(tool.id)}
              >
                {busy === 'submit-' + tool.id ? 'Submitting…' : 'Submit for Approval'}
              </button>
            )}
            {tool.status === 'pending_approval' && (
              <button
                style={btn('ghost', busy === 'approve-' + tool.id)}
                disabled={busy === 'approve-' + tool.id}
                onClick={() => handleApprove(tool.id)}
              >
                {busy === 'approve-' + tool.id ? 'Approving…' : 'Approve'}
              </button>
            )}
            <button style={btn('default', true)} disabled title="Coming soon">Edit Tool</button>
            <button style={btn('default', true)} disabled title="Coming soon">Duplicate</button>
            <button style={btn('danger', true)} disabled title="Coming soon">Delete</button>
          </div>
        </div>

        {/* Purpose */}
        {tool.description && (
          <div style={{ ...card(), marginBottom: '14px' }}>
            <h3 style={{ ...sectionLabel, margin: '0 0 8px' }}>Purpose</h3>
            <p style={{ margin: 0, fontSize: '0.88rem', color: text, lineHeight: 1.75 }}>{tool.description}</p>
          </div>
        )}

        {/* Workflow Steps */}
        {nodes.length > 0 && (
          <div style={{ ...card(), marginBottom: '14px' }}>
            <h3 style={{ ...sectionLabel, margin: '0 0 14px' }}>Workflow Steps</h3>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              {nodes.map((n, i) => (
                <div
                  key={n.id ?? i}
                  style={{
                    display: 'flex', alignItems: 'center', gap: '12px',
                    padding: '10px 14px',
                    background: `${bg}80`, border: `1px solid ${border}`, borderRadius: '8px',
                  }}
                >
                  <span style={{
                    width: '24px', height: '24px', borderRadius: '50%', flexShrink: 0,
                    background: `${accent}18`, border: `1px solid ${accent}40`,
                    color: accent, fontSize: '0.71rem', fontWeight: '700',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                  }}>
                    {i + 1}
                  </span>
                  <div style={{ flex: 1 }}>
                    <span style={{ fontSize: '0.86rem', fontWeight: '500', color: text }}>
                      {slugToTitle(n.action_type)}
                    </span>
                    {n.config?.description && (
                      <p style={{ margin: '2px 0 0', fontSize: '0.73rem', color: muted }}>{n.config.description}</p>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Execution History */}
        <div style={card()}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px', flexWrap: 'wrap', gap: '8px' }}>
            <h3 style={{ ...sectionLabel, margin: 0 }}>Execution History</h3>
            <button style={btn('default', runsLoading)} disabled={runsLoading} onClick={() => loadToolRuns(tool.id)}>
              {runsLoading ? 'Loading…' : 'Refresh'}
            </button>
          </div>

          {runsLoading && selectedRuns.length === 0 ? (
            <p style={{ color: muted, fontSize: '0.82rem', margin: 0, fontStyle: 'italic' }}>Loading runs…</p>
          ) : selectedRuns.length === 0 ? (
            <p style={{ color: muted, fontSize: '0.82rem', margin: 0, fontStyle: 'italic' }}>
              No runs yet. Click "Run Tool" to execute this tool.
            </p>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
              {selectedRuns.map(run => {
                const rm = runStatusMeta(run.status)
                return (
                  <div
                    key={run.run_id}
                    style={{
                      display: 'flex', alignItems: 'center', gap: '12px',
                      padding: '10px 14px',
                      background: `${bg}80`, border: `1px solid ${border}`, borderRadius: '8px',
                      flexWrap: 'wrap',
                    }}
                  >
                    <span style={{ width: '8px', height: '8px', borderRadius: '50%', background: rm.color, flexShrink: 0 }} />
                    <span style={{ fontSize: '0.83rem', fontWeight: '600', color: text }}>{rm.label}</span>
                    {run.duration_ms != null && (
                      <span style={{ fontSize: '0.73rem', color: muted }}>{run.duration_ms.toLocaleString()}ms</span>
                    )}
                    <span style={{ fontSize: '0.73rem', color: muted, marginLeft: 'auto' }}>
                      {run.started_at ? fmtRelative(run.started_at) : '—'}
                    </span>
                  </div>
                )
              })}
            </div>
          )}
        </div>

        {/* Status warning for non-active tools */}
        {!canRun && (
          <div style={{
            marginTop: '16px', padding: '12px 16px',
            background: `${warn}10`, border: `1px solid ${warn}30`,
            borderRadius: '8px', fontSize: '0.82rem', color: warn, lineHeight: 1.6,
          }}>
            This tool is in <strong>{toolStatusMeta(tool.status).label}</strong> status and cannot be run yet.
            {tool.status === 'draft' && ' It needs to be saved and activated before execution.'}
            {tool.status === 'pending_approval' && ' It is awaiting review before it can be executed.'}
          </div>
        )}

        <Toast toast={toast} success={success} errCol={errCol} />
      </div>
    )
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // RENDER: Create
  // ═══════════════════════════════════════════════════════════════════════════
  if (view === 'create') {
    const planTitle = pendingPlan ? displayTitle(pendingPlan) : null
    const planNodes = pendingPlan?.graph?.nodes ?? []

    return (
      <div style={{ fontFamily: FONT, color: text, padding: '28px 32px', maxWidth: '760px' }}>

        {/* Header */}
        <button style={{ ...btn('default', false), marginBottom: '20px', fontSize: '0.77rem' }} onClick={backToList}>
          ← Back to AI Tools
        </button>
        <h1 style={{ margin: '0 0 4px', fontSize: '1.3rem', fontWeight: '700', color: text }}>Create AI Tool</h1>
        <p style={{ margin: '0 0 28px', fontSize: '0.82rem', color: muted }}>
          Describe what you want this tool to do and AI will generate it for you.
        </p>

        {/* Step 1: Describe intent */}
        <div style={{ ...card(), marginBottom: '14px' }}>
          <h3 style={{ ...sectionLabel, margin: '0 0 12px' }}>What should this tool do?</h3>
          <textarea
            style={{
              width: '100%', background: `${bg}cc`, border: `1px solid ${border}`,
              borderRadius: '8px', color: text, fontFamily: FONT, fontSize: '0.88rem',
              padding: '12px 14px', resize: 'vertical', boxSizing: 'border-box',
              outline: 'none', minHeight: '100px', lineHeight: 1.65,
            }}
            placeholder='e.g. "Analyze dataset quality and send a summary report by email"'
            value={intent}
            onChange={e => setIntent(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && !pendingPlan) handleGenerate() }}
          />
          <div style={{ marginTop: '12px', display: 'flex', gap: '8px', alignItems: 'center' }}>
            <button
              style={btn('primary', !intent.trim() || busy === 'plan')}
              disabled={!intent.trim() || busy === 'plan'}
              onClick={handleGenerate}
            >
              {busy === 'plan' ? 'Generating…' : pendingPlan ? 'Regenerate' : 'Generate Tool'}
            </button>
            {!pendingPlan && (
              <span style={{ fontSize: '0.72rem', color: muted }}>Ctrl/⌘+Enter to generate</span>
            )}
          </div>
        </div>

        {/* Step 2: Preview generated plan */}
        {pendingPlan && (
          <div style={{ ...card(), marginBottom: '14px' }}>
            <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: '16px', flexWrap: 'wrap', gap: '10px' }}>
              <div>
                <h2 style={{ margin: '0 0 4px', fontSize: '1.05rem', fontWeight: '700', color: text }}>{planTitle}</h2>
                {pendingPlan.description && pendingPlan.description !== planTitle && (
                  <p style={{ margin: 0, fontSize: '0.82rem', color: muted, lineHeight: 1.6 }}>{pendingPlan.description}</p>
                )}
              </div>
              <StatusBadge status="draft" />
            </div>

            {planNodes.length > 0 && (
              <div style={{ marginBottom: '16px' }}>
                <p style={{ margin: '0 0 10px', fontSize: '0.72rem', fontWeight: '700', color: muted, textTransform: 'uppercase', letterSpacing: '0.07em' }}>
                  Workflow Steps
                </p>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                  {planNodes.map((n, i) => (
                    <div
                      key={n.id ?? i}
                      style={{
                        display: 'flex', alignItems: 'center', gap: '10px',
                        padding: '8px 12px',
                        background: `${bg}80`, border: `1px solid ${border}`, borderRadius: '7px',
                      }}
                    >
                      <span style={{
                        width: '20px', height: '20px', borderRadius: '50%', flexShrink: 0,
                        background: `${accent}18`, border: `1px solid ${accent}40`,
                        color: accent, fontSize: '0.67rem', fontWeight: '700',
                        display: 'flex', alignItems: 'center', justifyContent: 'center',
                      }}>
                        {i + 1}
                      </span>
                      <span style={{ fontSize: '0.85rem', color: text }}>{slugToTitle(n.action_type)}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div style={{ borderTop: `1px solid ${border}`, paddingTop: '14px', display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' }}>
              <button
                style={btn('primary', busy === 'save')}
                disabled={busy === 'save'}
                onClick={handleSaveAndActivate}
              >
                {busy === 'save' ? 'Activating…' : 'Save & Activate Tool'}
              </button>
              <span style={{ fontSize: '0.73rem', color: muted }}>
                Tool will be saved and activated automatically.
              </span>
            </div>
          </div>
        )}

        <Toast toast={toast} success={success} errCol={errCol} />
      </div>
    )
  }

  return null
}
