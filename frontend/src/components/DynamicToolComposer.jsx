import { useState, useEffect } from 'react'
import { getDynamicTools, createDynamicTool, updateDynamicTool, approveDynamicTool } from '../api/client'

const FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"
const MONO = "'Cascadia Code', 'Fira Code', 'JetBrains Mono', monospace"

const PRIMITIVE_TYPES = [
  'format_output',
  'transform_json',
  'send_email',
  'send_notification',
  'http_request',
]

const BUILTIN_NAMES = new Set(['email_sender', 'data_fetcher', 'notifier'])

const EMPTY_FORM = {
  name: '',
  slug: '',
  primitiveType: 'format_output',
  config: '{}',
  operations: '',
  requiredParams: '{}',
}

const is401 = err => err?.message?.startsWith('401')

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function isBuiltin(tool) {
  return BUILTIN_NAMES.has(tool.name) || !tool.created_by
}

function SmallBadge({ label, color, bg }) {
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: '4px',
      background: bg, color, border: `1px solid ${color}40`,
      borderRadius: '20px', padding: '1px 8px',
      fontSize: '0.62rem', fontWeight: '600', whiteSpace: 'nowrap',
    }}>
      <span style={{ width: '5px', height: '5px', borderRadius: '50%', background: color, flexShrink: 0 }} />
      {label}
    </span>
  )
}

function IconBtn({ onClick, disabled, title, color, dangerSoft, children }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={title}
      style={{
        display: 'flex', alignItems: 'center', gap: '4px',
        background: 'transparent', border: `1px solid ${color}40`,
        borderRadius: '7px', padding: '4px 10px', cursor: disabled ? 'not-allowed' : 'pointer',
        fontSize: '0.7rem', color, fontFamily: FONT, fontWeight: '600',
        opacity: disabled ? 0.5 : 1,
      }}
    >
      {children}
    </button>
  )
}

// ---------------------------------------------------------------------------
// DynamicToolComposer component
// ---------------------------------------------------------------------------

export default function DynamicToolComposer({ C, S, token, onSessionExpired }) {
  const [toolList,        setToolList]        = useState([])
  const [listLoading,     setListLoading]     = useState(false)
  const [listError,       setListError]       = useState(null)
  const [form,            setForm]            = useState(EMPTY_FORM)
  const [editId,          setEditId]          = useState(null)   // null = create mode
  const [saving,          setSaving]          = useState(false)
  const [saveError,       setSaveError]       = useState(null)
  const [saveSuccess,     setSaveSuccess]     = useState(null)
  const [approvingId,     setApprovingId]     = useState(null)
  const [approveError,    setApproveError]    = useState(null)

  useEffect(() => { refreshTools() }, [])  // eslint-disable-line react-hooks/exhaustive-deps

  // ── Data loading ──────────────────────────────────────────────────────────

  function refreshTools() {
    setListLoading(true)
    setListError(null)
    getDynamicTools(token)
      .then(d => setToolList(d?.data ?? []))
      .catch(err => {
        if (is401(err)) { onSessionExpired(); return }
        setListError('Could not load tools. Check that the server is running.')
      })
      .finally(() => setListLoading(false))
  }

  // ── Form helpers ──────────────────────────────────────────────────────────

  function setField(key, value) {
    setForm(prev => ({ ...prev, [key]: value }))
    setSaveError(null)
    setSaveSuccess(null)
  }

  function startEdit(tool) {
    const cfg = tool.config_json || {}
    const primConfig = cfg.config || {}
    const ops = (cfg.operations || []).join(', ')
    const rp = cfg.required_params || {}
    setForm({
      name:           tool.name || '',
      slug:           tool.slug || '',
      primitiveType:  cfg.primitive_type || 'format_output',
      config:         JSON.stringify(primConfig, null, 2),
      operations:     ops,
      requiredParams: JSON.stringify(rp, null, 2),
    })
    setEditId(tool.id)
    setSaveError(null)
    setSaveSuccess(null)
  }

  function cancelEdit() {
    setForm(EMPTY_FORM)
    setEditId(null)
    setSaveError(null)
    setSaveSuccess(null)
  }

  // ── Save ──────────────────────────────────────────────────────────────────

  async function handleSave() {
    const name = form.name.trim()
    if (!name) { setSaveError('Name is required.'); return }

    const ops = form.operations.split(',').map(s => s.trim()).filter(Boolean)
    if (!ops.length) { setSaveError('Enter at least one operation name.'); return }

    let parsedConfig, parsedRequiredParams
    try { parsedConfig = JSON.parse(form.config) }
    catch { setSaveError('Config must be valid JSON (e.g. {"template":"Hello {name}","output_key":"greeting"}).'); return }
    try { parsedRequiredParams = JSON.parse(form.requiredParams) }
    catch { setSaveError('Required params must be valid JSON (e.g. {"greet":["name"]}).'); return }

    const configJson = {
      primitive_type: form.primitiveType,
      config: parsedConfig,
      operations: ops,
      required_params: parsedRequiredParams,
    }
    const payload = { name, config_json: configJson }
    if (form.slug.trim()) payload.slug = form.slug.trim()

    setSaving(true)
    setSaveError(null)
    setSaveSuccess(null)
    try {
      if (editId) {
        await updateDynamicTool(editId, payload, token)
        setSaveSuccess('Tool updated.')
      } else {
        await createDynamicTool(payload, token)
        setSaveSuccess('Tool draft created. Approve it to make it active.')
        setForm(EMPTY_FORM)
      }
      refreshTools()
    } catch (err) {
      if (is401(err)) { onSessionExpired(); return }
      setSaveError(err.message.replace(/^\d+:\s*/, ''))
    } finally {
      setSaving(false)
    }
  }

  // ── Approve ───────────────────────────────────────────────────────────────

  async function handleApprove(id) {
    setApprovingId(id)
    setApproveError(null)
    try {
      await approveDynamicTool(id, token)
      refreshTools()
    } catch (err) {
      if (is401(err)) { onSessionExpired(); return }
      setApproveError(err.message.replace(/^\d+:\s*/, ''))
    } finally {
      setApprovingId(null)
    }
  }

  // ── Render ────────────────────────────────────────────────────────────────

  const label = (text, sub) => (
    <div style={{ marginBottom: '6px' }}>
      <div style={{ fontSize: '0.67rem', fontWeight: '600', color: C.textSec, textTransform: 'uppercase', letterSpacing: '0.06em' }}>{text}</div>
      {sub && <div style={{ fontSize: '0.65rem', color: C.textMuted, marginTop: '2px' }}>{sub}</div>}
    </div>
  )

  const input = (key, placeholder, opts = {}) => (
    <input
      type="text"
      placeholder={placeholder}
      value={form[key]}
      onChange={e => setField(key, e.target.value)}
      style={{ ...S.input, marginBottom: '14px', fontFamily: opts.mono ? MONO : FONT, ...opts.style }}
    />
  )

  const textarea = (key, placeholder, rows = 4) => (
    <textarea
      rows={rows}
      placeholder={placeholder}
      value={form[key]}
      onChange={e => setField(key, e.target.value)}
      style={{ ...S.textarea, fontFamily: MONO, fontSize: '0.78rem', marginBottom: '14px', resize: 'vertical' }}
    />
  )

  return (
    <div>
      {/* ── No-execute notice ────────────────────────────────────────────── */}
      <div style={{ background: C.warnSoft, border: `1px solid ${C.warn}40`, borderRadius: '9px', padding: '10px 14px', marginBottom: '18px', display: 'flex', alignItems: 'flex-start', gap: '10px' }}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={C.warn} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, marginTop: '1px' }}><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
        <div style={{ fontSize: '0.73rem', color: C.warn, lineHeight: 1.55 }}>
          <strong>No execution from this panel.</strong> Tools created here only execute when triggered by the workflow runner after approval. Dynamic tool execution requires <code style={{ fontFamily: MONO, fontSize: '0.7rem' }}>ENABLE_DYNAMIC_TOOL_EXECUTION=true</code> on the server.
        </div>
      </div>

      {/* ── Tool list ────────────────────────────────────────────────────── */}
      <div style={{ ...S.card, marginBottom: '18px' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px' }}>
          <h3 style={{ margin: 0, fontSize: '0.75rem', fontWeight: '600', color: C.textSec, letterSpacing: '0.01em' }}>Tool Registry</h3>
          <button onClick={refreshTools} style={{ background: 'none', border: 'none', cursor: 'pointer', color: C.textMuted, fontSize: '0.68rem', fontFamily: FONT, display: 'flex', alignItems: 'center', gap: '4px' }}>
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
            Refresh
          </button>
        </div>

        {listLoading ? (
          <div style={{ padding: '24px', textAlign: 'center', color: C.textMuted, fontSize: '0.8rem' }}>Loading…</div>
        ) : listError ? (
          <div style={{ padding: '16px', color: C.danger, fontSize: '0.8rem' }}>{listError}</div>
        ) : toolList.length === 0 ? (
          <div style={{ padding: '24px', textAlign: 'center', color: C.textMuted, fontSize: '0.8rem' }}>No tools yet. Create the first one below.</div>
        ) : (
          <div>
            {/* Column headers */}
            <div style={{ display: 'grid', gridTemplateColumns: 'minmax(140px,1fr) 130px 140px auto', gap: '8px', padding: '6px 14px', borderBottom: `1px solid ${C.border}` }}>
              {['Name', 'Primitive', 'Status', 'Actions'].map(h => (
                <div key={h} style={{ fontSize: '0.6rem', fontWeight: '700', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.08em' }}>{h}</div>
              ))}
            </div>

            {toolList.map((tool, idx) => {
              const builtin  = isBuiltin(tool)
              const approved = !!tool.approved
              const enabled  = !!tool.enabled
              const approving = approvingId === tool.id
              const primType = tool.config_json?.primitive_type

              return (
                <div key={tool.id} style={{ display: 'grid', gridTemplateColumns: 'minmax(140px,1fr) 130px 140px auto', gap: '8px', alignItems: 'center', padding: '10px 14px', borderBottom: idx < toolList.length - 1 ? `1px solid ${C.border}` : 'none', background: editId === tool.id ? C.accentSoft : 'transparent' }}>

                  {/* Name + slug */}
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: '0.78rem', fontWeight: '500', color: editId === tool.id ? C.accent : C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{tool.name}</div>
                    {tool.slug && tool.slug !== tool.name && (
                      <div style={{ fontSize: '0.63rem', color: C.textMuted, marginTop: '1px', fontFamily: MONO, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{tool.slug}</div>
                    )}
                  </div>

                  {/* Primitive type */}
                  <div>
                    {primType ? (
                      <span style={{ fontSize: '0.65rem', color: C.accent, background: C.accentSoft, border: `1px solid ${C.accent}30`, borderRadius: '20px', padding: '2px 9px', fontWeight: '500', fontFamily: MONO, whiteSpace: 'nowrap' }}>{primType}</span>
                    ) : (
                      <span style={{ fontSize: '0.65rem', color: C.textMuted }}>—</span>
                    )}
                  </div>

                  {/* Status badges */}
                  <div style={{ display: 'flex', gap: '5px', flexWrap: 'wrap' }}>
                    {builtin  && <SmallBadge label="Built-in" color={C.textSec}  bg={C.borderAlt} />}
                    {!builtin && approved  && <SmallBadge label="Approved" color={C.success} bg={C.successSoft} />}
                    {!builtin && !approved && <SmallBadge label="Draft"    color={C.warn}    bg={C.warnSoft}    />}
                    {!builtin && <SmallBadge label={enabled ? 'Enabled' : 'Disabled'} color={enabled ? C.success : C.textMuted} bg={enabled ? C.successSoft : C.bg} />}
                  </div>

                  {/* Actions */}
                  <div style={{ display: 'flex', gap: '6px', justifyContent: 'flex-end' }}>
                    {!builtin && !approved && (
                      <>
                        <IconBtn
                          onClick={() => startEdit(tool)}
                          color={C.accent}
                          title="Edit draft"
                        >
                          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                          Edit
                        </IconBtn>
                        <IconBtn
                          onClick={() => handleApprove(tool.id)}
                          disabled={approving}
                          color={C.success}
                          title="Validate config and approve for execution"
                        >
                          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                          {approving ? 'Approving…' : 'Approve'}
                        </IconBtn>
                      </>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        )}

        {approveError && (
          <div style={{ marginTop: '10px', padding: '10px 14px', background: C.dangerSoft, border: `1px solid ${C.danger}40`, borderRadius: '8px', fontSize: '0.78rem', color: C.danger }}>{approveError}</div>
        )}
      </div>

      {/* ── Create / Edit form ───────────────────────────────────────────── */}
      <div style={{ ...S.card }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px' }}>
          <h3 style={{ margin: 0, fontSize: '0.75rem', fontWeight: '600', color: C.textSec, letterSpacing: '0.01em' }}>
            {editId ? `Editing Tool #${editId}` : 'Create New Tool'}
          </h3>
          {editId && (
            <button onClick={cancelEdit} style={{ background: 'none', border: `1px solid ${C.border}`, borderRadius: '7px', padding: '4px 12px', cursor: 'pointer', fontSize: '0.72rem', color: C.textSec, fontFamily: FONT }}>
              Cancel
            </button>
          )}
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 18px' }}>
          <div>
            {label('Name', 'Unique identifier, e.g. weather_checker')}
            {input('name', 'e.g. weather_checker', { mono: true })}
          </div>
          <div>
            {label('Slug', 'Optional URL-safe alias (defaults to name)')}
            {input('slug', 'e.g. weather', { mono: true })}
          </div>
        </div>

        {label('Primitive Type', 'The built-in execution primitive this tool uses')}
        <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', marginBottom: '14px' }}>
          {PRIMITIVE_TYPES.map(pt => {
            const active = form.primitiveType === pt
            return (
              <button key={pt} onClick={() => setField('primitiveType', pt)} style={{ padding: '4px 13px', borderRadius: '20px', fontSize: '0.72rem', cursor: 'pointer', fontFamily: MONO, fontWeight: active ? '600' : '400', border: `1px solid ${active ? C.accent : C.border}`, background: active ? C.accentSoft : 'transparent', color: active ? C.accent : C.textSec, transition: 'border-color 0.12s, background 0.12s, color 0.12s' }}>
                {pt}
              </button>
            )
          })}
        </div>

        {label('Config', 'Primitive-specific settings as JSON. Must be a JSON object.')}
        {textarea('config', '{"template":"Hello {name}","output_key":"greeting"}', 5)}

        {label('Operations', 'Comma-separated list of operation names this tool supports')}
        {input('operations', 'e.g. greet, send', { mono: true })}

        {label('Required Params', 'JSON object mapping each operation to its required parameter names')}
        {textarea('requiredParams', '{"greet":["name"]}', 3)}

        <button
          onClick={handleSave}
          disabled={saving || !form.name.trim()}
          style={{ ...S.btnPrimary, opacity: (saving || !form.name.trim()) ? 0.6 : 1 }}
        >
          {saving ? 'Saving…' : editId ? 'Update Tool' : 'Create Draft'}
        </button>

        {saveError && (
          <div style={{ marginTop: '12px', background: C.dangerSoft, border: `1px solid ${C.danger}40`, borderRadius: '8px', padding: '10px 14px', fontSize: '0.8rem', color: C.danger }}>{saveError}</div>
        )}
        {saveSuccess && (
          <div style={{ marginTop: '12px', background: C.successSoft, border: `1px solid ${C.success}40`, borderRadius: '8px', padding: '10px 14px', fontSize: '0.8rem', color: C.success }}>{saveSuccess}</div>
        )}
      </div>
    </div>
  )
}
