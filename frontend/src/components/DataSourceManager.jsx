import { useEffect, useState } from 'react'
import { createDataSource, deleteDataSource, disconnectDataSource, listDataSources, reconnectDataSource, removeDataSourceCredentials, testDataSource } from '../api/client'

const FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"
const TYPES = [
  { value: 'mssql', label: 'SQL Server', port: 1433, enabled: true },
  { value: 'postgresql', label: 'PostgreSQL', port: 5432 },
  { value: 'mysql', label: 'MySQL', port: 3306 },
]
const EMPTY = { display_name: '', source_type: 'mssql', host: '', port: 1433, database: '', username: '', password: '', auth_type: 'sql', encrypt_connection: true, trust_server_certificate: false }
const COPY = {
  disconnect: 'Temporary and reversible — metadata and credentials are preserved. Reconnect at any time.',
  credentials: 'Metadata is preserved; credentials are removed. Reconnecting requires credentials.',
  reconnect: 'Verifies the stable database identity and reuses compatible existing intelligence — never starts over.',
  remove: 'Destructive and permanent. Removes all source metadata for this connection. This cannot be undone.',
}

function Spinner() { return <span aria-label="Loading" style={{ display: 'inline-block', width: 11, height: 11, border: '2px solid currentColor', borderRightColor: 'transparent', borderRadius: '50%', animation: 'dsm-spin .8s linear infinite' }} /> }
function Toast({ value }) { return value ? <div style={{ position: 'fixed', right: 24, bottom: 24, zIndex: 9999, padding: '10px 16px', borderRadius: 10, background: value.ok ? '#052e2b' : '#3f1720', color: value.ok ? '#34d399' : '#fca5a5', fontFamily: FONT, fontSize: '.8rem' }}>{value.text}</div> : null }

export default function DataSourceManager({ C = {}, token, onBack }) {
  const bg = C.bg ?? '#07091a', surface = C.surface ?? '#0d1128', border = C.border ?? '#1e2b52'
  const text = C.text ?? '#eef0ff', secondary = C.textSec ?? '#dde1ff', muted = C.textMuted ?? '#7880a8'
  const accent = C.accent ?? '#6366f1', accentSoft = C.accentSoft ?? '#6366f11a'
  const success = C.success ?? '#10b981', danger = C.danger ?? '#f87171', warn = C.warn ?? '#f59e0b'
  const [sources, setSources] = useState([]), [loading, setLoading] = useState(true)
  const [showForm, setShowForm] = useState(false), [form, setForm] = useState(EMPTY), [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState(null), [reconnectId, setReconnectId] = useState(null)
  const [menuId, setMenuId] = useState(null), [deleteId, setDeleteId] = useState(null), [infoId, setInfoId] = useState(null)
  const [busy, setBusy] = useState({}), [tests, setTests] = useState({}), [toast, setToast] = useState(null)
  const card = extra => ({ background: surface, border: `1px solid ${border}`, borderRadius: 12, ...extra })
  const input = { width: '100%', boxSizing: 'border-box', padding: '9px 11px', borderRadius: 8, border: `1px solid ${border}`, background: bg, color: text, font: `400 .82rem ${FONT}` }
  const label = { display: 'block', marginBottom: 5, color: muted, fontSize: '.7rem', fontWeight: 600 }
  const primary = { border: 0, borderRadius: 8, padding: '8px 14px', background: accent, color: '#fff', fontFamily: FONT, fontSize: '.78rem', fontWeight: 600, cursor: 'pointer' }
  const ghost = { border: `1px solid ${border}`, borderRadius: 8, padding: '8px 14px', background: 'transparent', color: secondary, fontFamily: FONT, fontSize: '.78rem', cursor: 'pointer' }
  const menuStyle = color => ({ display: 'block', width: '100%', border: 0, borderRadius: 7, padding: '9px 11px', background: 'transparent', color, textAlign: 'left', fontFamily: FONT, fontSize: '.78rem', cursor: 'pointer' })

  function formatDate(value) {
    if (!value) return 'Not available'
    const parsed = new Date(value)
    return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString()
  }

  function message(textValue, ok = true) { setToast({ text: textValue, ok }); window.setTimeout(() => setToast(null), 3500) }
  async function refresh() { setLoading(true); try { const result = await listDataSources(token); setSources(result?.data ?? []) } catch (e) { message(e?.message ?? 'Could not load data sources.', false) } finally { setLoading(false) } }
  useEffect(() => { refresh() }, []) // eslint-disable-line react-hooks/exhaustive-deps

  function closeForm() { setShowForm(false); setReconnectId(null); setForm(EMPTY); setFormError(null) }
  function startReconnect(source) { setReconnectId(source.id); setForm({ ...EMPTY, display_name: source.display_name ?? '', source_type: source.source_type ?? 'mssql', port: source.port ?? 1433, database: source.database ?? '' }); setShowForm(true); setFormError(null) }
  async function save(event) {
    event.preventDefault(); setSaving(true); setFormError(null)
    try {
      if (!form.display_name.trim()) throw new Error('Display name is required.')
      if (!form.host.trim()) throw new Error('Host is required.')
      if (!form.database.trim()) throw new Error('Database name is required.')
      if (form.auth_type === 'sql' && !form.username.trim()) throw new Error('Username is required for SQL authentication.')
      if (form.auth_type === 'sql' && !form.password) throw new Error('Password is required for SQL authentication.')
      const config = {
        host: form.host.trim(), port: Number(form.port) || 1433, database: form.database.trim(),
        auth_type: form.auth_type, encrypt_connection: form.encrypt_connection,
        trust_server_certificate: form.trust_server_certificate,
      }
      if (form.auth_type === 'sql') { config.username = form.username.trim(); config.password = form.password }
      const payload = { display_name: form.display_name.trim(), source_type: form.source_type, config, metadata: { environment: 'user-configured' } }
      if (reconnectId != null) { await reconnectDataSource(payload, token); message('Data source reconnected. Existing intelligence was preserved.') }
      else { await createDataSource(payload, token); message('Connection saved. Automatic metadata preparation has started.') }
      closeForm(); await refresh()
    } catch (e) { setFormError(e?.message ?? 'Could not save the connection.') } finally { setSaving(false) }
  }
  async function test(sourceId) {
    setTests(v => ({ ...v, [sourceId]: { loading: true } }))
    try { const response = await testDataSource(sourceId, token); const data = response?.data ?? response ?? {}; setTests(v => ({ ...v, [sourceId]: { loading: false, status: data.status ?? 'success', latency_ms: data.latency_ms, message: data.message } })) }
    catch (e) { setTests(v => ({ ...v, [sourceId]: { loading: false, status: 'failed', message: e?.message ?? 'Connection failed.' } })) }
  }
  async function lifecycle(sourceId, action) {
    setBusy(v => ({ ...v, [sourceId]: action }))
    try {
      if (action === 'disconnect') await disconnectDataSource(sourceId, null, token)
      else await removeDataSourceCredentials(sourceId, null, token)
      message(action === 'disconnect' ? 'Data source disconnected. Metadata and credentials were preserved.' : 'Credentials removed. Metadata was preserved.'); await refresh()
    } catch (e) { message(e?.message ?? 'The action could not be completed.', false) } finally { setBusy(v => ({ ...v, [sourceId]: null })) }
  }
  async function remove(sourceId) {
    setBusy(v => ({ ...v, [sourceId]: 'delete' }))
    try { await deleteDataSource(sourceId, token); setDeleteId(null); message('Data source and its metadata were permanently deleted.'); await refresh() }
    catch (e) { message(e?.message ?? 'The source could not be deleted.', false) } finally { setBusy(v => ({ ...v, [sourceId]: null })) }
  }

  return <div style={{ color: text, fontFamily: FONT }}>
    <style>{`@keyframes dsm-spin { to { transform: rotate(360deg) } }`}</style>
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 16, alignItems: 'flex-start', marginBottom: 20 }}>
      <div>
        {onBack && <button type="button" onClick={onBack} style={{ padding: 0, margin: '0 0 12px', border: 0, background: 'transparent', color: accent, fontFamily: FONT, fontSize: '.74rem', fontWeight: 650, cursor: 'pointer' }}>← All data sources</button>}
        <h2 style={{ margin: '0 0 4px', fontSize: '1.4rem' }}>Database Connections</h2><p style={{ margin: 0, color: muted, fontSize: '.76rem' }}>Connect databases for automatic profiling and AI-powered questions.</p>
      </div>
      <button onClick={() => showForm && reconnectId == null ? closeForm() : (setReconnectId(null), setForm(EMPTY), setShowForm(true))} style={primary}>{showForm && reconnectId == null ? 'Cancel' : '+ Add Connection'}</button>
    </div>

    {showForm && <form onSubmit={save} autoComplete="off" style={{ ...card({ padding: 20 }), marginBottom: 20 }}>
      <h3 style={{ margin: '0 0 6px', fontSize: '.95rem' }}>{reconnectId != null ? `Reconnect "${form.display_name}"` : 'New Connection'}</h3>
      {reconnectId != null && <p style={{ margin: '0 0 16px', color: muted, fontSize: '.74rem' }}>{COPY.reconnect}</p>}
      <div style={{ display: 'flex', gap: 8, marginBottom: 18 }}>{TYPES.map(type => <button key={type.value} type="button" disabled={!type.enabled} onClick={() => type.enabled && setForm(v => ({ ...v, source_type: type.value, port: type.port }))} style={{ flex: 1, padding: 10, borderRadius: 9, border: `1px solid ${form.source_type === type.value ? accent : border}`, background: form.source_type === type.value ? accentSoft : bg, color: type.enabled ? text : muted, cursor: type.enabled ? 'pointer' : 'not-allowed' }}>{type.label}{!type.enabled && <small style={{ display: 'block' }}>Coming soon</small>}</button>)}</div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
        <Field wide name="Display Name"><input style={input} value={form.display_name} onChange={e => setForm(v => ({ ...v, display_name: e.target.value }))} /></Field>
        <Field name="Host / Server"><input style={input} placeholder="server.domain.com" value={form.host} onChange={e => setForm(v => ({ ...v, host: e.target.value }))} /></Field>
        <Field name="Port"><input style={input} type="number" value={form.port} onChange={e => setForm(v => ({ ...v, port: Number(e.target.value) }))} /></Field>
        <Field wide name="Database Name"><input style={input} placeholder="database_name" value={form.database} onChange={e => setForm(v => ({ ...v, database: e.target.value }))} /></Field>
        <Field wide name="Authentication"><select style={input} value={form.auth_type} onChange={e => setForm(v => ({ ...v, auth_type: e.target.value }))}><option value="sql">SQL Authentication</option><option value="windows">Windows Authentication</option></select></Field>
        {form.auth_type === 'sql' && <><Field name="Username"><input style={input} value={form.username} onChange={e => setForm(v => ({ ...v, username: e.target.value }))} /></Field><Field name="Password"><input style={input} type="password" value={form.password} onChange={e => setForm(v => ({ ...v, password: e.target.value }))} /></Field></>}
      </div>
      <div style={{ display: 'flex', gap: 20, marginTop: 16, color: secondary, fontSize: '.78rem' }}><label><input type="checkbox" checked={form.encrypt_connection} onChange={e => setForm(v => ({ ...v, encrypt_connection: e.target.checked }))} /> Encrypt connection</label><label><input type="checkbox" checked={form.trust_server_certificate} onChange={e => setForm(v => ({ ...v, trust_server_certificate: e.target.checked }))} /> Trust server certificate</label></div>
      {formError && <div style={{ marginTop: 14, color: danger, fontSize: '.78rem' }}>{formError}</div>}
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 18 }}><button type="button" onClick={closeForm} style={ghost}>Cancel</button><button type="submit" disabled={saving} style={{ ...primary, opacity: saving ? .6 : 1 }}>{saving ? 'Saving…' : reconnectId != null ? 'Reconnect' : 'Save Connection'}</button></div>
    </form>}

    <div style={card({ overflow: 'visible' })}>
      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(220px,1.5fr) .65fr .8fr 1fr auto', gap: 12, padding: '10px 18px', borderBottom: `1px solid ${border}`, color: muted, fontSize: '.65rem', fontWeight: 700, textTransform: 'uppercase' }}><span>Name</span><span>Type</span><span>Status</span><span>Last test</span><span>Actions</span></div>
      {loading ? <div style={{ padding: 48, textAlign: 'center', color: muted }}><Spinner /> Loading data sources…</div> : sources.length === 0 ? <div style={{ padding: 48, textAlign: 'center', color: muted }}>No data sources connected yet.</div> : sources.map(source => {
        const state = source.connection_state ?? 'ACTIVE', disconnected = state !== 'ACTIVE', testValue = tests[source.id] ?? {}, working = busy[source.id]
        const schemaSummary = source.schema_summary ?? {}
        const statusText = state === 'DISCONNECTED' ? 'Disconnected' : state === 'CREDENTIALS_REMOVED' ? 'Credentials removed' : source.source_status === 'ERROR' ? 'Error' : 'Connected'
        const statusColor = disconnected ? warn : source.source_status === 'ERROR' ? danger : success
        return <div key={source.id}>
          <div role="button" tabIndex={0} aria-expanded={infoId === source.id} aria-label={`View database summary for ${source.display_name}`}
            onClick={() => setInfoId(current => current === source.id ? null : source.id)}
            onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setInfoId(current => current === source.id ? null : source.id) } }}
            onMouseEnter={event => { event.currentTarget.style.background = accentSoft; event.currentTarget.style.borderColor = `${accent}55` }}
            onMouseLeave={event => { event.currentTarget.style.background = 'transparent'; event.currentTarget.style.borderColor = `${border}55` }}
            style={{ display: 'grid', gridTemplateColumns: 'minmax(220px,1.5fr) .65fr .8fr 1fr auto', gap: 12, alignItems: 'center', padding: '15px 18px', borderBottom: `1px solid ${border}55`, cursor: 'pointer', transition: 'background .15s, border-color .15s', outlineOffset: -2 }}>
            <div style={{ minWidth: 0 }}><div style={{ fontWeight: 650, fontSize: '.86rem' }}>{source.display_name}</div><div style={{ color: muted, fontSize: '.66rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{source.config_summary ?? '—'}</div><div style={{ color: accent, fontSize: '.64rem', fontWeight: 650, marginTop: 4 }}>{infoId === source.id ? 'Summary open' : 'Click to view database summary'} →</div></div>
            <span style={{ color: secondary, fontSize: '.76rem' }}>{TYPES.find(type => type.value === source.source_type)?.label ?? source.source_type}</span>
            <span style={{ width: 'fit-content', padding: '3px 9px', borderRadius: 10, color: statusColor, background: `${statusColor}15`, border: `1px solid ${statusColor}40`, fontSize: '.68rem', fontWeight: 650 }}>{statusText}</span>
            <span style={{ color: testValue.status === 'failed' ? danger : testValue.status === 'success' ? success : muted, fontSize: '.72rem' }}>{testValue.loading ? 'Testing…' : testValue.status === 'success' ? `Connected${testValue.latency_ms != null ? ` · ${testValue.latency_ms}ms` : ''}` : testValue.status === 'failed' ? testValue.message : source.last_test_status === 'success' ? 'Connected' : '—'}</span>
            <div onClick={event => event.stopPropagation()} onKeyDown={event => event.stopPropagation()} style={{ position: 'relative' }}><button title="More actions" aria-label={`More actions for ${source.display_name}`} onClick={() => setMenuId(v => v === source.id ? null : source.id)} style={{ ...ghost, padding: '5px 10px', fontSize: '1rem' }}>⋮</button>
              {menuId === source.id && <><div onClick={() => setMenuId(null)} style={{ position: 'fixed', inset: 0, zIndex: 90 }} /><div style={{ position: 'absolute', right: 0, top: 38, zIndex: 100, minWidth: 190, ...card({ padding: 5, boxShadow: '0 12px 36px rgba(0,0,0,.4)' }) }}>
                <button onClick={() => { test(source.id); setMenuId(null) }} style={menuStyle(secondary)}>Test Connection</button>
                {!disconnected ? <><button title={COPY.disconnect} disabled={!!working} onClick={() => { lifecycle(source.id, 'disconnect'); setMenuId(null) }} style={menuStyle(secondary)}>{working === 'disconnect' ? 'Disconnecting…' : 'Disconnect'}</button><button title={COPY.credentials} disabled={!!working} onClick={() => { lifecycle(source.id, 'credentials'); setMenuId(null) }} style={menuStyle(secondary)}>{working === 'credentials' ? 'Removing…' : 'Remove Credentials'}</button></> : <button title={COPY.reconnect} onClick={() => { startReconnect(source); setMenuId(null) }} style={menuStyle(accent)}>Reconnect</button>}
                <div style={{ height: 1, background: border, margin: '4px 0' }} /><button title={COPY.remove} onClick={() => { setDeleteId(source.id); setMenuId(null) }} style={menuStyle(danger)}>Permanently Delete</button>
              </div></>}
            </div>
          </div>
          {infoId === source.id && <div aria-label={`Database information for ${source.display_name}`} style={{ padding: '20px', background: C.surfaceAlt ?? bg, borderBottom: `1px solid ${border}` }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, marginBottom: 14 }}>
              <div><strong style={{ display: 'block', fontSize: '.92rem' }}>Database Summary</strong><span style={{ color: muted, fontSize: '.7rem' }}>Useful information from ToolSmithAI’s latest automatic discovery.</span></div>
              <button type="button" onClick={() => setInfoId(null)} style={{ ...ghost, padding: '5px 10px' }}>Close</button>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(100px, 1fr))', gap: 10, marginBottom: 16 }}>
              {[
                ['Schemas', schemaSummary.schema_count ?? 0],
                ['Tables', schemaSummary.table_count ?? 0],
                ['Views', schemaSummary.view_count ?? 0],
                ['Columns', schemaSummary.column_count ?? 0],
              ].map(([name, value]) => <div key={name} style={{ padding: '14px', borderRadius: 10, textAlign: 'center', border: `1px solid ${accent}35`, background: accentSoft }}><strong style={{ display: 'block', color: accent, fontSize: '1.2rem', marginBottom: 3 }}>{Number(value).toLocaleString()}</strong><span style={{ color: muted, fontSize: '.68rem', fontWeight: 650 }}>{name}</span></div>)}
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(185px, 1fr))', gap: 10 }}>
              {[
                ['Source ID', source.id],
                ['Database', source.display_name],
                ['Database type', TYPES.find(type => type.value === source.source_type)?.label ?? source.source_type],
                ['Server / database', source.config_summary ?? 'Not available'],
                ['Connection state', statusText],
                ['Live questions', source.live_query_enabled === false ? 'Unavailable' : disconnected ? 'Available after reconnecting' : 'Available'],
                ['Last connection test', testValue.status === 'success' ? `Successful${testValue.latency_ms != null ? ` (${testValue.latency_ms} ms)` : ''}` : source.last_test_status === 'success' ? `Successful · ${formatDate(source.last_tested_at)}` : source.last_test_status ? `${source.last_test_status} · ${formatDate(source.last_tested_at)}` : 'Not tested yet'],
                ['AI preparation', schemaSummary.status === 'READY' ? 'Ready for questions' : 'Preparing database information'],
                ['Last discovery', formatDate(schemaSummary.discovered_at)],
                ['Added', formatDate(source.created_at)],
                ['Last updated', formatDate(source.updated_at)],
              ].map(([name, value]) => <div key={name} style={{ minWidth: 0, padding: '11px 12px', borderRadius: 9, border: `1px solid ${border}`, background: surface }}><span style={{ display: 'block', marginBottom: 5, color: muted, fontSize: '.63rem', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.04em' }}>{name}</span><span style={{ display: 'block', color: secondary, fontSize: '.76rem', overflowWrap: 'anywhere' }}>{String(value ?? 'Not available')}</span></div>)}
            </div>
            <p style={{ margin: '14px 0 0', color: muted, fontSize: '.68rem' }}>Counts come from the latest saved discovery and remain available while the database is disconnected. Credentials and secrets are never displayed.</p>
          </div>}
          {deleteId === source.id && <div style={{ padding: '13px 18px', background: `${danger}0d`, borderBottom: `1px solid ${danger}35`, display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 16 }}><div><strong style={{ display: 'block', color: danger, fontSize: '.78rem' }}>This permanently deletes all metadata for “{source.display_name}” and cannot be undone.</strong><span style={{ color: muted, fontSize: '.7rem' }}>Disconnect instead if you may use this source again.</span></div><div style={{ display: 'flex', gap: 8 }}><button onClick={() => setDeleteId(null)} style={ghost}>Cancel</button><button onClick={() => remove(source.id)} disabled={working === 'delete'} style={{ ...primary, background: danger }}>{working === 'delete' ? 'Deleting…' : 'Permanently Delete?'}</button></div></div>}
        </div>
      })}
    </div>
    <p style={{ color: muted, fontSize: '.72rem', marginTop: 12 }}>Schema discovery, profiling, view intelligence, and AI preparation run automatically during connection setup. Use AI Workspace to ask questions.</p>
    <Toast value={toast} />
  </div>

  function Field({ name, wide, children }) { return <div style={wide ? { gridColumn: '1 / -1' } : undefined}><label style={label}>{name}</label>{children}</div> }
}
