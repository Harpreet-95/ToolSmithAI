/**
 * EngineWorkspace — Dynamic Tool Creation Engine lifecycle testing panel.
 *
 * Full flow: plan → save → submit → approve → execute → view runs / step results.
 * This is a functional testing workspace, not a polished product UI.
 */

import { useState, useEffect } from 'react'
import {
  planEngineTool,
  saveEngineTool,
  submitEngineTool,
  approveEngineTool,
  executeEngineTool,
  getEngineToolRuns,
  getEngineRun,
  getEngineTool,
  listEngineTools,
} from '../api/client'

const FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"
const MONO = "'Cascadia Code', 'Fira Code', 'JetBrains Mono', monospace"

// ── Status badge colours ─────────────────────────────────────────────────────
const STATUS_COLORS = {
  draft:            '#94a3b8',
  pending_approval: '#fbbf24',
  approved:         '#22c55e',
  deprecated:       '#f87171',
  completed:        '#22c55e',
  failed:           '#f87171',
  running:          '#38bdf8',
  cancelled:        '#94a3b8',
  skipped:          '#94a3b8',
}

function statusColor(s) { return STATUS_COLORS[s] ?? '#94a3b8' }

function StatusBadge({ status }) {
  if (!status) return null
  const c = statusColor(status)
  return (
    <span style={{
      display: 'inline-block',
      padding: '2px 9px',
      borderRadius: '10px',
      fontSize: '0.68rem',
      fontWeight: '700',
      letterSpacing: '0.05em',
      textTransform: 'uppercase',
      background: `${c}20`,
      color: c,
      border: `1px solid ${c}50`,
      whiteSpace: 'nowrap',
    }}>
      {status.replace(/_/g, ' ')}
    </span>
  )
}

// ── Main component ────────────────────────────────────────────────────────────
export default function EngineWorkspace({ C = {}, token }) {
  // Colour tokens with fallbacks so the panel works even without C prop
  const bg      = C.bg       ?? '#0f1117'
  const surface = C.surface  ?? '#1a1f2e'
  const border  = C.border   ?? '#2a2f3f'
  const text    = C.text     ?? '#e8eaf0'
  const muted   = C.textMuted ?? '#7b8099'
  const accent  = C.accent   ?? '#7c6af5'
  const success = C.success  ?? '#22c55e'
  const errCol  = C.error    ?? '#f87171'
  const warn    = C.warn     ?? '#fbbf24'

  // ── State ────────────────────────────────────────────────────────────────
  const [intent,      setIntent]      = useState('')
  const [toolDef,     setToolDef]     = useState(null)   // planned ToolDefinition dict
  const [savedToolId, setSavedToolId] = useState(null)   // UUID after save
  const [toolStatus,  setToolStatus]  = useState(null)   // 'draft' | 'pending_approval' | 'approved'
  const [inputs,      setInputs]      = useState('{}')   // JSON string for execute
  const [runs,        setRuns]        = useState([])
  const [selectedRun, setSelectedRun] = useState(null)
  const [busy,        setBusy]        = useState(null)   // which step is in-flight
  const [toast,       setToast]       = useState(null)   // {ok, text}
  const [loadId,      setLoadId]      = useState('')     // tool_id input for load-saved flow
  const [savedTools,  setSavedTools]  = useState([])    // summary list from GET /engine/tools
  const [toolsLoading, setToolsLoading] = useState(false)

  // ── Helpers ──────────────────────────────────────────────────────────────
  function notify(text, ok = true) {
    setToast({ text, ok })
    setTimeout(() => setToast(null), 5000)
  }
  function notifyErr(e) { notify(e?.message ?? String(e), false) }

  // Button style factory
  function btn(variant, disabled) {
    const base = {
      padding: '6px 14px',
      borderRadius: '7px',
      border: 'none',
      cursor: disabled ? 'not-allowed' : 'pointer',
      fontSize: '0.79rem',
      fontWeight: '600',
      fontFamily: FONT,
      opacity: disabled ? 0.42 : 1,
      transition: 'opacity 0.12s',
    }
    if (variant === 'primary') return { ...base, background: accent,        color: '#fff' }
    if (variant === 'success') return { ...base, background: `${success}22`, color: success, border: `1px solid ${success}50` }
    if (variant === 'warn')    return { ...base, background: `${warn}22`,    color: warn,    border: `1px solid ${warn}50` }
    return { ...base, background: `${border}88`, color: text, border: `1px solid ${border}` }
  }

  const card = {
    background: surface,
    border: `1px solid ${border}`,
    borderRadius: '10px',
    padding: '16px 18px',
    marginBottom: '14px',
  }

  const label = {
    fontSize: '0.71rem',
    fontWeight: '700',
    letterSpacing: '0.08em',
    textTransform: 'uppercase',
    color: muted,
    marginBottom: '10px',
    display: 'block',
    fontFamily: FONT,
  }

  const textareaStyle = {
    width: '100%',
    background: `${bg}cc`,
    border: `1px solid ${border}`,
    borderRadius: '7px',
    color: text,
    fontFamily: FONT,
    fontSize: '0.84rem',
    padding: '10px 12px',
    resize: 'vertical',
    boxSizing: 'border-box',
    outline: 'none',
  }

  const preStyle = {
    background: `${bg}cc`,
    border: `1px solid ${border}`,
    borderRadius: '7px',
    padding: '12px 14px',
    fontFamily: MONO,
    fontSize: '0.71rem',
    color: text,
    overflowX: 'auto',
    overflowY: 'auto',
    maxHeight: '300px',
    whiteSpace: 'pre',
    margin: 0,
  }

  // ── Saved tools list ─────────────────────────────────────────────────────
  async function handleRefreshTools() {
    setToolsLoading(true)
    try {
      const res = await listEngineTools(token)
      setSavedTools(res?.data ?? [])
    } catch (e) { notifyErr(e) }
    finally { setToolsLoading(false) }
  }

  useEffect(() => { handleRefreshTools() }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // ── Load saved tool (accepts explicit id from list click or falls back to input) ──
  async function handleLoadTool(explicitId) {
    const id = (explicitId ?? loadId).trim()
    if (!id) return notify('Enter a tool ID.', false)
    setBusy('load'); setToast(null)
    try {
      const res = await getEngineTool(id, token)
      const td = res?.data ?? res
      setToolDef(td)
      setSavedToolId(td.id)
      setToolStatus(td.status ?? 'draft')
      setRuns([])
      setSelectedRun(null)
      const req = (td?.inputs ?? []).filter(i => i.required).map(i => [i.name, ''])
      if (req.length > 0) setInputs(JSON.stringify(Object.fromEntries(req), null, 2))
      notify(`Loaded "${td?.name}" — status: ${td?.status}`)
    } catch (e) { notifyErr(e) }
    finally { setBusy(null) }
  }

  // ── Lifecycle handlers ────────────────────────────────────────────────────
  async function handlePlan() {
    if (!intent.trim()) return notify('Enter an intent first.', false)
    setBusy('plan'); setToast(null)
    setToolDef(null); setSavedToolId(null); setToolStatus(null)
    setRuns([]); setSelectedRun(null)
    try {
      const res = await planEngineTool(intent.trim(), token)
      const td = res?.data ?? res
      setToolDef(td)
      // Pre-fill inputs JSON with required fields from the plan
      const req = (td?.inputs ?? []).filter(i => i.required).map(i => [i.name, ''])
      if (req.length > 0) setInputs(JSON.stringify(Object.fromEntries(req), null, 2))
      notify(`Planned "${td?.name}" — ${(td?.graph?.nodes ?? []).length} node(s)`)
    } catch (e) { notifyErr(e) }
    finally { setBusy(null) }
  }

  async function handleSave() {
    if (!toolDef) return
    setBusy('save'); setToast(null)
    try {
      const res = await saveEngineTool(toolDef, token)
      const d = res?.data ?? res
      setSavedToolId(d.tool_id)
      setToolStatus(d.status ?? 'draft')
      notify(`Saved. tool_id = ${d.tool_id}`)
    } catch (e) { notifyErr(e) }
    finally { setBusy(null) }
  }

  async function handleSubmit() {
    if (!savedToolId) return
    setBusy('submit'); setToast(null)
    try {
      await submitEngineTool(savedToolId, token)
      setToolStatus('pending_approval')
      notify('Submitted for approval.')
    } catch (e) { notifyErr(e) }
    finally { setBusy(null) }
  }

  async function handleApprove() {
    if (!savedToolId) return
    setBusy('approve'); setToast(null)
    try {
      await approveEngineTool(savedToolId, token)
      setToolStatus('approved')
      notify('Tool approved — ready to execute.')
    } catch (e) { notifyErr(e) }
    finally { setBusy(null) }
  }

  async function handleExecute() {
    if (!savedToolId) return
    let parsedInputs = {}
    try { parsedInputs = JSON.parse(inputs || '{}') }
    catch { return notify('Inputs field is not valid JSON.', false) }
    setBusy('execute'); setToast(null); setSelectedRun(null)
    try {
      const res = await executeEngineTool(savedToolId, parsedInputs, token)
      const record = res?.data ?? res
      setSelectedRun(record)
      const steps = record?.step_results?.length ?? 0
      notify(`Run ${record?.status} — ${steps} step(s) completed`)
      // Refresh run list after execute
      const runsRes = await getEngineToolRuns(savedToolId, token)
      setRuns(runsRes?.data ?? [])
    } catch (e) { notifyErr(e) }
    finally { setBusy(null) }
  }

  async function handleLoadRuns() {
    if (!savedToolId) return
    setBusy('runs'); setToast(null)
    try {
      const res = await getEngineToolRuns(savedToolId, token)
      setRuns(res?.data ?? [])
    } catch (e) { notifyErr(e) }
    finally { setBusy(null) }
  }

  async function handleSelectRun(runId) {
    if (selectedRun?.run_id === runId) { setSelectedRun(null); return }
    setBusy('run-' + runId)
    try {
      const res = await getEngineRun(runId, token)
      setSelectedRun(res?.data ?? res)
    } catch (e) { notifyErr(e) }
    finally { setBusy(null) }
  }

  // ── Computed button states ────────────────────────────────────────────────
  const canSave    = !!toolDef && !savedToolId
  const canSubmit  = !!savedToolId && toolStatus === 'draft'
  const canApprove = !!savedToolId && toolStatus === 'pending_approval'
  const canExecute = !!savedToolId && toolStatus === 'approved'

  // ── Render ───────────────────────────────────────────────────────────────
  return (
    <div style={{ fontFamily: FONT, color: text, padding: '24px 28px', maxWidth: '960px' }}>

      {/* ── Header ── */}
      <div style={{ marginBottom: '22px' }}>
        <h2 style={{ margin: 0, fontSize: '1.1rem', fontWeight: '700', color: text, letterSpacing: '-0.2px' }}>
          Engine Lab
        </h2>
        <p style={{ margin: '4px 0 0', fontSize: '0.78rem', color: muted }}>
          Dynamic Tool Creation Engine — end-to-end lifecycle testing
        </p>
      </div>

      {/* ── Saved Tools ── */}
      <div style={{ ...card, marginBottom: '14px' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px', flexWrap: 'wrap', gap: '8px' }}>
          <span style={label}>Saved Tools</span>
          <button
            style={btn('default', toolsLoading)}
            disabled={toolsLoading}
            onClick={handleRefreshTools}
          >
            {toolsLoading ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>

        {/* Tool list */}
        {toolsLoading && savedTools.length === 0 ? (
          <p style={{ fontSize: '0.77rem', color: muted, margin: '0 0 12px', fontStyle: 'italic' }}>Loading…</p>
        ) : savedTools.length === 0 ? (
          <p style={{ fontSize: '0.77rem', color: muted, margin: '0 0 12px', fontStyle: 'italic' }}>No saved tools yet — plan and save one below.</p>
        ) : (
          <div style={{ marginBottom: '12px' }}>
            {savedTools.map(t => {
              const isActive  = savedToolId === t.id
              const isLoading = busy === 'load'
              const updated   = t.updated_at
                ? new Date(t.updated_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
                : '—'
              return (
                <div
                  key={t.id}
                  onClick={() => !isLoading && handleLoadTool(t.id)}
                  style={{
                    display: 'flex', alignItems: 'center', gap: '10px',
                    padding: '8px 10px', borderRadius: '7px',
                    cursor: isLoading ? 'not-allowed' : 'pointer',
                    background: isActive ? `${accent}18` : 'transparent',
                    border: `1px solid ${isActive ? accent + '45' : 'transparent'}`,
                    marginBottom: '4px', transition: 'background 0.1s',
                    flexWrap: 'wrap', opacity: isLoading ? 0.6 : 1,
                  }}
                >
                  <span style={{ fontSize: '0.82rem', fontWeight: '600', color: isActive ? accent : text, flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {t.name || t.id}
                  </span>
                  <StatusBadge status={t.status} />
                  <span style={{ fontSize: '0.71rem', color: muted, flexShrink: 0 }}>{updated}</span>
                </div>
              )
            })}
          </div>
        )}

        {/* Manual UUID fallback */}
        <div style={{ borderTop: `1px solid ${border}`, paddingTop: '12px' }}>
          <span style={{ ...label, marginBottom: '8px', textTransform: 'none', letterSpacing: 0, fontSize: '0.72rem' }}>
            Or load by tool ID
          </span>
          <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
            <input
              type="text"
              placeholder="Paste tool_id (UUID)"
              value={loadId}
              onChange={e => setLoadId(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') handleLoadTool() }}
              style={{
                flex: 1,
                background: `${bg}cc`,
                border: `1px solid ${border}`,
                borderRadius: '7px',
                color: text,
                fontFamily: MONO,
                fontSize: '0.8rem',
                padding: '8px 12px',
                outline: 'none',
                boxSizing: 'border-box',
              }}
            />
            <button
              style={btn('default', !loadId.trim() || busy === 'load')}
              disabled={!loadId.trim() || busy === 'load'}
              onClick={() => handleLoadTool()}
            >
              {busy === 'load' ? 'Loading…' : 'Load'}
            </button>
          </div>
        </div>
      </div>

      {/* ── Active tool status bar ── */}
      {savedToolId && (
        <div style={{ ...card, display: 'flex', alignItems: 'center', gap: '14px', padding: '10px 16px', marginBottom: '14px', flexWrap: 'wrap' }}>
          <span style={{ fontSize: '0.74rem', color: muted, fontWeight: '600' }}>TOOL ID</span>
          <code style={{ fontFamily: MONO, fontSize: '0.71rem', color: accent, wordBreak: 'break-all' }}>{savedToolId}</code>
          <StatusBadge status={toolStatus} />
        </div>
      )}

      {/* ═══ STEP 1: PLAN ════════════════════════════════════════════════════ */}
      <div style={card}>
        <span style={label}>Step 1 — Plan</span>
        <textarea
          style={{ ...textareaStyle, minHeight: '76px' }}
          placeholder={'Describe the tool, e.g. "format output and send notification"'}
          value={intent}
          onChange={e => setIntent(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) handlePlan() }}
        />
        <div style={{ marginTop: '10px', display: 'flex', gap: '8px', alignItems: 'center' }}>
          <button
            style={btn('primary', !intent.trim() || busy === 'plan')}
            disabled={!intent.trim() || busy === 'plan'}
            onClick={handlePlan}
          >
            {busy === 'plan' ? 'Planning…' : 'Plan Tool'}
          </button>
          <span style={{ fontSize: '0.72rem', color: muted }}>Ctrl/⌘+Enter to plan</span>
        </div>
      </div>

      {/* ═══ STEP 2: REVIEW & APPROVE ════════════════════════════════════════ */}
      {toolDef && (
        <div style={card}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px', flexWrap: 'wrap', gap: '8px' }}>
            <span style={label}>Step 2 — Review & Approve</span>
            <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
              <button
                style={btn('default', !canSave || busy === 'save')}
                disabled={!canSave || busy === 'save'}
                onClick={handleSave}
              >
                {busy === 'save' ? 'Saving…' : 'Save'}
              </button>
              <button
                style={btn('warn', !canSubmit || busy === 'submit')}
                disabled={!canSubmit || busy === 'submit'}
                onClick={handleSubmit}
              >
                {busy === 'submit' ? 'Submitting…' : 'Submit for Approval'}
              </button>
              <button
                style={btn('success', !canApprove || busy === 'approve')}
                disabled={!canApprove || busy === 'approve'}
                onClick={handleApprove}
              >
                {busy === 'approve' ? 'Approving…' : 'Approve'}
              </button>
            </div>
          </div>

          {/* Node summary */}
          {(toolDef.graph?.nodes ?? []).length > 0 && (
            <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', marginBottom: '10px' }}>
              {(toolDef.graph.nodes ?? []).map(n => (
                <span key={n.id} style={{
                  fontSize: '0.71rem', fontFamily: MONO, padding: '2px 8px',
                  background: `${accent}18`, color: accent, border: `1px solid ${accent}40`,
                  borderRadius: '6px',
                }}>
                  {n.action_type}
                </span>
              ))}
              {(toolDef.inputs ?? []).filter(i => i.required).map(i => (
                <span key={i.name} style={{
                  fontSize: '0.71rem', fontFamily: MONO, padding: '2px 8px',
                  background: `${warn}18`, color: warn, border: `1px solid ${warn}40`,
                  borderRadius: '6px',
                }}>
                  req: {i.name}
                </span>
              ))}
            </div>
          )}

          <pre style={preStyle}>{JSON.stringify(toolDef, null, 2)}</pre>
        </div>
      )}

      {/* ═══ STEP 3: EXECUTE ═════════════════════════════════════════════════ */}
      {savedToolId && (
        <div style={card}>
          <span style={label}>Step 3 — Execute</span>
          {!canExecute && (
            <p style={{ fontSize: '0.77rem', color: muted, margin: '0 0 10px', fontStyle: 'italic' }}>
              {toolStatus === 'draft' ? 'Submit and approve the tool first.' : 'Approve the tool first.'}
            </p>
          )}
          <label style={{ ...label, marginBottom: '6px', textTransform: 'none', letterSpacing: 0, fontSize: '0.76rem' }}>
            Inputs (JSON)
          </label>
          <textarea
            style={{ ...textareaStyle, fontFamily: MONO, fontSize: '0.77rem', minHeight: '80px' }}
            value={inputs}
            onChange={e => setInputs(e.target.value)}
          />
          <div style={{ marginTop: '10px' }}>
            <button
              style={btn('primary', !canExecute || busy === 'execute')}
              disabled={!canExecute || busy === 'execute'}
              onClick={handleExecute}
            >
              {busy === 'execute' ? 'Executing…' : 'Execute Tool'}
            </button>
          </div>
        </div>
      )}

      {/* ═══ STEP 4: RUN HISTORY ═════════════════════════════════════════════ */}
      {savedToolId && (
        <div style={card}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '10px', flexWrap: 'wrap', gap: '8px' }}>
            <span style={label}>Step 4 — Run History</span>
            <button
              style={btn('default', busy === 'runs')}
              disabled={busy === 'runs'}
              onClick={handleLoadRuns}
            >
              {busy === 'runs' ? 'Loading…' : 'Refresh Runs'}
            </button>
          </div>

          {runs.length === 0 ? (
            <p style={{ fontSize: '0.77rem', color: muted, margin: 0, fontStyle: 'italic' }}>
              No runs yet — execute the tool, then refresh.
            </p>
          ) : (
            <div>
              {runs.map(run => {
                const isSelected = selectedRun?.run_id === run.run_id
                const isBusy    = busy === 'run-' + run.run_id
                return (
                  <div
                    key={run.run_id}
                    onClick={() => !isBusy && handleSelectRun(run.run_id)}
                    style={{
                      display: 'flex', alignItems: 'center', gap: '10px',
                      padding: '8px 10px', borderRadius: '7px', cursor: 'pointer',
                      background: isSelected ? `${accent}18` : 'transparent',
                      border: `1px solid ${isSelected ? accent + '45' : 'transparent'}`,
                      marginBottom: '4px', transition: 'background 0.1s',
                      flexWrap: 'wrap',
                    }}
                  >
                    <code style={{ fontFamily: MONO, fontSize: '0.7rem', color: muted }}>
                      {run.run_id.slice(0, 8)}…
                    </code>
                    <StatusBadge status={run.status} />
                    {run.duration_ms != null && (
                      <span style={{ fontSize: '0.72rem', color: muted }}>{run.duration_ms}ms</span>
                    )}
                    {run.started_at && (
                      <span style={{ fontSize: '0.72rem', color: muted, marginLeft: 'auto' }}>
                        {new Date(run.started_at).toLocaleTimeString()}
                      </span>
                    )}
                    {isBusy && <span style={{ fontSize: '0.72rem', color: muted }}>Loading…</span>}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      )}

      {/* ═══ STEP RESULTS DETAIL ═════════════════════════════════════════════ */}
      {selectedRun && (
        <div style={card}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '12px', flexWrap: 'wrap' }}>
            <span style={label}>Run Detail</span>
            <StatusBadge status={selectedRun.status} />
            {selectedRun.duration_ms != null && (
              <span style={{ fontSize: '0.74rem', color: muted }}>{selectedRun.duration_ms}ms</span>
            )}
            <code style={{ fontFamily: MONO, fontSize: '0.7rem', color: muted }}>{selectedRun.run_id}</code>
          </div>

          {selectedRun.error && (
            <p style={{ fontSize: '0.78rem', color: errCol, margin: '0 0 12px',
              background: `${errCol}12`, padding: '8px 12px', borderRadius: '6px',
              border: `1px solid ${errCol}30` }}>
              {selectedRun.error}
            </p>
          )}

          {(selectedRun.step_results ?? []).length === 0 ? (
            <p style={{ fontSize: '0.77rem', color: muted, margin: 0, fontStyle: 'italic' }}>No step results.</p>
          ) : (
            (selectedRun.step_results ?? []).map((sr, i) => (
              <div key={i} style={{
                background: `${bg}bb`, border: `1px solid ${border}`,
                borderRadius: '8px', padding: '10px 13px', marginBottom: '8px',
              }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '9px', marginBottom: sr.error || Object.keys(sr.output ?? {}).length ? '8px' : 0, flexWrap: 'wrap' }}>
                  <code style={{ fontFamily: MONO, fontSize: '0.71rem', color: accent }}>{sr.node_id}</code>
                  <span style={{ fontSize: '0.77rem', color: text, fontWeight: '500' }}>{sr.action_type}</span>
                  <StatusBadge status={sr.status} />
                  {sr.duration_ms != null && (
                    <span style={{ fontSize: '0.7rem', color: muted, marginLeft: 'auto' }}>{sr.duration_ms}ms</span>
                  )}
                </div>
                {sr.error && (
                  <p style={{ margin: '0 0 6px', fontSize: '0.73rem', color: errCol }}>{sr.error}</p>
                )}
                {sr.output && Object.keys(sr.output).length > 0 && (
                  <pre style={{ ...preStyle, maxHeight: '130px', fontSize: '0.69rem' }}>
                    {JSON.stringify(sr.output, null, 2)}
                  </pre>
                )}
              </div>
            ))
          )}
        </div>
      )}

      {/* ── Toast notification ── */}
      {toast && (
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
      )}
    </div>
  )
}
