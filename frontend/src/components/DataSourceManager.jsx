import { useState, useEffect } from 'react'
import { analyzeDomainRefinements, approveDomainRefinement, approveDomainRule, approveEntityRule, createDataSource, deleteDataSource, discoverDataSourceSchema, generateDictionaryForSource, generateDomainRuleSuggestions, generateDomains, generateEntities, generateEntityRuleSuggestions, getDomainRefinements, getDomainRules, getDomainSummary, getDataSourceSchema, getEntityRules, getEntitySummary, getMetadataJob, getProfile, getProfileHistory, listDataSources, listDictionaryTables, listDomainAssignments, listEntityAssignments, rejectDomainRefinement, rejectDomainRule, rejectEntityRule, runMetadataJob, testDataSource } from '../api/client'
import DictionaryReview from './DictionaryReview'

const FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"
const MONO = "'Cascadia Code', 'Fira Code', 'JetBrains Mono', monospace"

const SOURCE_TYPES = [
  { value: 'mssql',      label: 'SQL Server', defaultPort: 1433, available: true  },
  { value: 'postgresql', label: 'PostgreSQL', defaultPort: 5432, available: false },
  { value: 'mysql',      label: 'MySQL',      defaultPort: 3306, available: false },
]

const SOURCE_STATUS_META = {
  ACTIVE:      { label: 'Active',      color: '#10b981' },
  ERROR:       { label: 'Error',       color: '#f87171' },
  DISCOVERING: { label: 'Discovering', color: '#38bdf8' },
  INACTIVE:    { label: 'Inactive',    color: '#94a3b8' },
}

const INITIAL_FORM = {
  display_name: '',
  source_type: 'mssql',
  host: '',
  port: 1433,
  database: '',
  username: '',
  password: '',
  auth_type: 'sql',
  encrypt_connection: true,
  trust_server_certificate: false,
}

function fmtRelative(iso) {
  if (!iso) return null
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 2)  return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24)  return `${hrs}h ago`
  return `${Math.floor(hrs / 24)}d ago`
}

function Toast({ toast }) {
  if (!toast) return null
  return (
    <div style={{
      position: 'fixed', bottom: '24px', right: '24px',
      background: toast.ok ? '#10b98120' : '#f8717120',
      border: `1px solid ${toast.ok ? '#10b98150' : '#f8717150'}`,
      color: toast.ok ? '#10b981' : '#f87171',
      borderRadius: '10px', padding: '10px 18px',
      fontSize: '0.81rem', fontFamily: FONT, fontWeight: '500',
      maxWidth: '380px', zIndex: 9999,
      boxShadow: '0 4px 24px rgba(0,0,0,0.35)',
    }}>
      {toast.text}
    </div>
  )
}

export default function DataSourceManager({ C = {}, token, setActiveNav, openSource, dsSelectedSourceId, dsActiveTab, setDsSelectedSourceId, setDsActiveTab }) {
  const bg         = C.bg        ?? '#07091a'
  const surface    = C.surface   ?? '#0d1128'
  const border     = C.border    ?? '#1e2b52'
  const text       = C.text      ?? '#eef0ff'
  const textSec    = C.textSec   ?? '#dde1ff'
  const muted      = C.textMuted ?? '#7880a8'
  const accent     = C.accent    ?? '#6366f1'
  const accentSoft = C.accentSoft ?? '#6366f11a'
  const success    = C.success   ?? '#10b981'
  const danger     = C.danger    ?? '#f87171'
  const warn       = C.warn      ?? '#f59e0b'

  const [sources,       setSources]       = useState([])
  const [loading,       setLoading]       = useState(true)
  const [showForm,      setShowForm]      = useState(false)
  const [form,          setForm]          = useState(INITIAL_FORM)
  const [saving,        setSaving]        = useState(false)
  const [formError,     setFormError]     = useState(null)
  const [testState,     setTestState]     = useState({})  // { [id]: { loading, status, message, latency_ms } }
  const [discoverState, setDiscoverState] = useState({})  // { [id]: { loading, result, error } }
  const [schemaState,   setSchemaState]   = useState({})  // { [id]: { loading, data, error } }
  const [schemaExpand,  setSchemaExpand]  = useState({})  // { [id]: { schemas: [], tables: [] } }
  const [profileState,  setProfileState]  = useState({})  // { [id]: { loading, data, error } }
  const [dictState,     setDictState]     = useState({})  // { [id]: { loading, generating, tables, error } }
  const [domainState,   setDomainState]   = useState({})  // { [id]: { loading, generating, summary, error } }
  const [entityState,   setEntityState]   = useState({})  // { [id]: { loading, generating, summary, error } }
  const [toast,         setToast]         = useState(null)
  const [jobState,      setJobState]      = useState({})  // { [sourceId]: { loading, running, data, error } }
  const [deleteState,     setDeleteState]     = useState({})  // { [id]: { confirming, deleting } }
  const [rulesState,            setRulesState]            = useState({})  // { [srcId]: { open, loading, generating, rules, error } }
  const [ruleActionState,       setRuleActionState]       = useState({})  // { [ruleId]: bool }
  const [govState,              setGovState]              = useState({})  // { [srcId]: { open, loading, analyzing, refinements, analyzeResult, error } }
  const [refinementActionState, setRefinementActionState] = useState({})  // { [refId]: bool }
  const [entityRulesState,      setEntityRulesState]      = useState({})  // { [srcId]: { open, loading, generating, rules, error } }
  const [entityRuleActionState, setEntityRuleActionState] = useState({})  // { [ruleId]: bool }
  const [domAssignState,  setDomAssignState]  = useState({})  // { [srcId]: { loading, data, error } }
  const [entAssignState,  setEntAssignState]  = useState({})  // { [srcId]: { loading, data, error } }
  const [profileHistState,setProfileHistState]= useState({})  // { [srcId]: { loading, data, error } }
  const [srcMenu,          setSrcMenu]         = useState({})  // { [id]: bool } per-source three-dot menu
  const [landingSearch,    setLandingSearch]    = useState('')    // landing-page search filter

  function notify(msg, ok = true) {
    setToast({ text: msg, ok })
    setTimeout(() => setToast(null), 4000)
  }

  async function loadJobStatus(sourceId, jobId) {
    setJobState(s => ({ ...s, [sourceId]: { ...(s[sourceId] ?? {}), loading: true, error: null } }))
    try {
      const resp = await getMetadataJob(jobId, token)
      setJobState(s => ({ ...s, [sourceId]: { ...(s[sourceId] ?? {}), loading: false, data: resp?.data ?? resp } }))
    } catch (e) {
      setJobState(s => ({ ...s, [sourceId]: { ...(s[sourceId] ?? {}), loading: false, error: e?.message ?? 'Failed to fetch job status.' } }))
    }
  }

  async function handleRunJob(sourceId, jobId) {
    setJobState(s => ({ ...s, [sourceId]: { ...(s[sourceId] ?? {}), running: true } }))
    try {
      const resp = await runMetadataJob(jobId, token)
      setJobState(s => ({ ...s, [sourceId]: { ...(s[sourceId] ?? {}), running: false, data: resp?.data ?? resp } }))
      notify('Metadata job completed.')
    } catch (e) {
      setJobState(s => ({ ...s, [sourceId]: { ...(s[sourceId] ?? {}), running: false, error: e?.message ?? 'Job execution failed.' } }))
      notify(e?.message ?? 'Metadata job failed.', false)
    }
  }

  async function loadSources() {
    setLoading(true)
    try {
      const data = await listDataSources(token)
      const list = data?.data ?? []
      setSources(list)
      list.forEach(src => {
        if (src.metadata_job_id) loadJobStatus(src.id, src.metadata_job_id)
        // Fire-and-forget pipeline status loads — only when a schema snapshot
        // exists. Each is independent; one failing never blocks the others.
        if (src.last_snapshot_id != null) {
          loadProfile(src.id)
          loadDictSummary(src.id)
          loadDomainSummary(src.id)
          loadEntitySummary(src.id)
        }
      })
    } catch (e) {
      notify(e?.message ?? 'Failed to load data sources.', false)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { loadSources() }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-load schema for discovered sources so landing page can show real table/column counts
  useEffect(() => {
    sources.forEach(src => {
      if (src.last_snapshot_id != null && !schemaState[src.id]?.data && !schemaState[src.id]?.loading) {
        loadSchema(src.id)
      }
    })
  }, [sources]) // eslint-disable-line react-hooks/exhaustive-deps

  // Lazy-load workspace tab data when tab or selected source changes
  useEffect(() => {
    if (dsSelectedSourceId == null) return
    const id = dsSelectedSourceId
    const src = sources.find(s => s.id === id)
    if (dsActiveTab === 'schema' && !schemaState[id]?.data && !schemaState[id]?.loading && src?.last_snapshot_id != null) {
      loadSchema(id)
    }
    if (dsActiveTab === 'domains' && !domAssignState[id]?.data && !domAssignState[id]?.loading) {
      loadDomainAssignments(id)
    }
    if (dsActiveTab === 'entities' && !entAssignState[id]?.data && !entAssignState[id]?.loading) {
      loadEntityAssignments(id)
    }
    if (dsActiveTab === 'runs' && !profileHistState[id]?.data && !profileHistState[id]?.loading) {
      loadProfileHistory(id)
    }
  }, [dsActiveTab, dsSelectedSourceId]) // eslint-disable-line react-hooks/exhaustive-deps

  function handleTypeChange(value) {
    const st = SOURCE_TYPES.find(t => t.value === value)
    setForm(f => ({ ...f, source_type: value, port: st?.defaultPort ?? f.port }))
  }

  function handleField(key, value) {
    setForm(f => ({ ...f, [key]: value }))
  }

  async function handleSave(e) {
    e.preventDefault()
    setFormError(null)
    if (!form.display_name.trim()) { setFormError('Display name is required.'); return }
    if (!form.host.trim())         { setFormError('Host is required.'); return }
    if (!form.database.trim())     { setFormError('Database name is required.'); return }
    if (form.auth_type === 'sql' && !form.username.trim()) {
      setFormError('Username is required for SQL authentication.'); return
    }
    if (form.auth_type === 'sql' && !form.password) {
      setFormError('Password is required for SQL authentication.'); return
    }

    setSaving(true)
    try {
      const config = {
        host: form.host.trim(),
        port: Number(form.port) || 1433,
        database: form.database.trim(),
        auth_type: form.auth_type,
        encrypt_connection: form.encrypt_connection,
        trust_server_certificate: form.trust_server_certificate,
      }
      if (form.auth_type === 'sql') {
        config.username = form.username.trim()
        config.password = form.password
      }

      await createDataSource({
        display_name: form.display_name.trim(),
        source_type: form.source_type,
        config,
        metadata: { environment: 'user-configured' },
      }, token)

      setForm(INITIAL_FORM)  // clears password
      setShowForm(false)
      notify('Connection saved.')
      await loadSources()
    } catch (e) {
      setFormError(e?.message ?? 'Failed to save connection.')
    } finally {
      setSaving(false)
    }
  }

  async function handleTest(id) {
    setTestState(s => ({ ...s, [id]: { loading: true } }))
    try {
      const data = await testDataSource(id, token)
      setTestState(s => ({
        ...s,
        [id]: { loading: false, status: data.status, message: data.message, latency_ms: data.latency_ms },
      }))
    } catch (e) {
      setTestState(s => ({
        ...s,
        [id]: { loading: false, status: 'failed', message: e?.message ?? 'Test request failed.', latency_ms: null },
      }))
    } finally {
      await loadSources()
    }
  }

  async function handleDiscover(id) {
    setDiscoverState(s => ({ ...s, [id]: { loading: true } }))
    try {
      const resp = await discoverDataSourceSchema(id, token)
      setDiscoverState(s => ({ ...s, [id]: { loading: false, result: resp.data ?? resp } }))
      await loadSources()
    } catch (e) {
      setDiscoverState(s => ({ ...s, [id]: { loading: false, error: e?.message ?? 'Discovery failed.' } }))
    }
  }

  async function handleViewSchema(id) {
    if (schemaState[id]?.data) {
      setSchemaState(s => ({ ...s, [id]: { ...s[id], data: null } }))
      return
    }
    setSchemaState(s => ({ ...s, [id]: { loading: true } }))
    try {
      const resp = await getDataSourceSchema(id, token)
      setSchemaState(s => ({ ...s, [id]: { loading: false, data: resp.data ?? resp } }))
      setSchemaExpand(s => ({ ...s, [id]: { schemas: [], tables: [] } }))
    } catch (e) {
      setSchemaState(s => ({ ...s, [id]: { loading: false, error: e?.message ?? 'Failed to load schema.' } }))
    }
  }

  // ── Pipeline status loaders (lazy, per-source, non-blocking) ────────────────
  function _is404(e) {
    return /(^|\D)404(\D|$)/.test(e?.message ?? '')
  }

  async function loadProfile(id) {
    setProfileState(s => ({ ...s, [id]: { ...(s[id] ?? {}), loading: true, error: null } }))
    try {
      const resp = await getProfile(id, token)
      setProfileState(s => ({ ...s, [id]: { ...(s[id] ?? {}), loading: false, data: resp?.data ?? null } }))
    } catch (e) {
      // 404 = source has no profiling snapshot yet — not an error condition.
      setProfileState(s => ({ ...s, [id]: { ...(s[id] ?? {}), loading: false, data: null, error: _is404(e) ? null : (e?.message ?? 'Failed to load profile.') } }))
    }
  }

  async function loadDictSummary(id) {
    setDictState(s => ({ ...s, [id]: { ...(s[id] ?? {}), loading: true, error: null } }))
    try {
      const resp = await listDictionaryTables(id, token)
      setDictState(s => ({ ...s, [id]: { ...(s[id] ?? {}), loading: false, tables: resp?.data ?? [] } }))
    } catch (e) {
      setDictState(s => ({ ...s, [id]: { ...(s[id] ?? {}), loading: false, tables: null, error: e?.message ?? 'Failed to load dictionary status.' } }))
    }
  }

  async function loadDomainSummary(id) {
    setDomainState(s => ({ ...s, [id]: { ...(s[id] ?? {}), loading: true, error: null } }))
    try {
      const resp = await getDomainSummary(id, token)
      setDomainState(s => ({ ...s, [id]: { ...(s[id] ?? {}), loading: false, summary: resp?.data ?? null } }))
    } catch (e) {
      setDomainState(s => ({ ...s, [id]: { ...(s[id] ?? {}), loading: false, error: e?.message ?? 'Failed to load domain status.' } }))
    }
  }

  async function loadEntitySummary(id) {
    setEntityState(s => ({ ...s, [id]: { ...(s[id] ?? {}), loading: true, error: null } }))
    try {
      const resp = await getEntitySummary(id, token)
      setEntityState(s => ({ ...s, [id]: { ...(s[id] ?? {}), loading: false, summary: resp?.data ?? null } }))
    } catch (e) {
      setEntityState(s => ({ ...s, [id]: { ...(s[id] ?? {}), loading: false, error: e?.message ?? 'Failed to load entity status.' } }))
    }
  }

  async function loadDomainAssignments(id) {
    setDomAssignState(s => ({ ...s, [id]: { loading: true, error: null } }))
    try {
      const resp = await listDomainAssignments(id, token)
      setDomAssignState(s => ({ ...s, [id]: { loading: false, data: resp?.data ?? [] } }))
    } catch (e) {
      setDomAssignState(s => ({ ...s, [id]: { loading: false, error: e?.message ?? 'Failed to load domain assignments.' } }))
    }
  }

  async function loadEntityAssignments(id) {
    setEntAssignState(s => ({ ...s, [id]: { loading: true, error: null } }))
    try {
      const resp = await listEntityAssignments(id, token)
      setEntAssignState(s => ({ ...s, [id]: { loading: false, data: resp?.data ?? [] } }))
    } catch (e) {
      setEntAssignState(s => ({ ...s, [id]: { loading: false, error: e?.message ?? 'Failed to load entity assignments.' } }))
    }
  }

  async function loadProfileHistory(id) {
    setProfileHistState(s => ({ ...s, [id]: { loading: true, error: null } }))
    try {
      const resp = await getProfileHistory(id, token)
      setProfileHistState(s => ({ ...s, [id]: { loading: false, data: resp?.data ?? [] } }))
    } catch (e) {
      setProfileHistState(s => ({ ...s, [id]: { loading: false, error: e?.message ?? 'Failed to load profile history.' } }))
    }
  }

  async function loadSchema(id) {
    setSchemaState(s => ({ ...s, [id]: { loading: true } }))
    try {
      const resp = await getDataSourceSchema(id, token)
      setSchemaState(s => ({ ...s, [id]: { loading: false, data: resp.data ?? resp } }))
      setSchemaExpand(s => ({ ...s, [id]: { schemas: [], tables: [] } }))
    } catch (e) {
      setSchemaState(s => ({ ...s, [id]: { loading: false, error: e?.message ?? 'Failed to load schema.' } }))
    }
  }

  // ── Pipeline action handlers ────────────────────────────────────────────────
  async function handleDiscoverAndProfile(src) {
    // The metadata job runs Discover Schema + Structural Profiling together.
    // Fall back to discovery-only if no job exists for this source.
    if (src.metadata_job_id) {
      await handleRunJob(src.id, src.metadata_job_id)
    } else {
      await handleDiscover(src.id)
    }
    await loadSources()           // refresh last_snapshot_id / source_status
    loadProfile(src.id)
    loadDictSummary(src.id)
    loadDomainSummary(src.id)
    loadEntitySummary(src.id)
  }

  async function handleGenerateDictionary(id) {
    setDictState(s => ({ ...s, [id]: { ...(s[id] ?? {}), generating: true, error: null } }))
    try {
      await generateDictionaryForSource(id, token)
      await loadDictSummary(id)
      notify('Dictionary generated.')
    } catch (e) {
      setDictState(s => ({ ...s, [id]: { ...(s[id] ?? {}), generating: false, error: e?.message ?? 'Dictionary generation failed.' } }))
      notify(e?.message ?? 'Dictionary generation failed.', false)
      return
    }
    setDictState(s => ({ ...s, [id]: { ...(s[id] ?? {}), generating: false } }))
  }

  async function handleGenerateDomains(id) {
    setDomainState(s => ({ ...s, [id]: { ...(s[id] ?? {}), generating: true, error: null } }))
    try {
      await generateDomains(id, token)
      await loadDomainSummary(id)
      notify('Domains generated.')
    } catch (e) {
      setDomainState(s => ({ ...s, [id]: { ...(s[id] ?? {}), generating: false, error: e?.message ?? 'Domain generation failed.' } }))
      notify(e?.message ?? 'Domain generation failed.', false)
      return
    }
    setDomainState(s => ({ ...s, [id]: { ...(s[id] ?? {}), generating: false } }))
  }

  async function handleGenerateEntities(id) {
    setEntityState(s => ({ ...s, [id]: { ...(s[id] ?? {}), generating: true, error: null } }))
    try {
      await generateEntities(id, token)
      await loadEntitySummary(id)
      notify('Entities generated.')
    } catch (e) {
      setEntityState(s => ({ ...s, [id]: { ...(s[id] ?? {}), generating: false, error: e?.message ?? 'Entity generation failed.' } }))
      notify(e?.message ?? 'Entity generation failed.', false)
      return
    }
    setEntityState(s => ({ ...s, [id]: { ...(s[id] ?? {}), generating: false } }))
  }

  function handleDeleteClick(id) {
    setDeleteState(s => ({ ...s, [id]: { confirming: true, deleting: false } }))
  }

  function handleDeleteCancel(id) {
    setDeleteState(s => ({ ...s, [id]: { confirming: false, deleting: false } }))
  }

  async function handleDeleteConfirm(id) {
    setDeleteState(s => ({ ...s, [id]: { confirming: false, deleting: true } }))
    try {
      await deleteDataSource(id, token)
      setSources(s => s.filter(src => src.id !== id))
      notify('Data source removed.')
    } catch (e) {
      setDeleteState(s => ({ ...s, [id]: { confirming: false, deleting: false } }))
      notify(e?.message ?? 'Failed to remove data source.', false)
    }
  }

  async function loadDomainRules(srcId) {
    setRulesState(s => ({ ...s, [srcId]: { ...(s[srcId] ?? {}), loading: true, error: null } }))
    try {
      const resp = await getDomainRules(srcId, token)
      setRulesState(s => ({ ...s, [srcId]: { ...(s[srcId] ?? {}), loading: false, rules: resp?.data ?? [] } }))
    } catch (e) {
      setRulesState(s => ({ ...s, [srcId]: { ...(s[srcId] ?? {}), loading: false, error: e?.message ?? 'Failed to load rules.' } }))
    }
  }

  async function handleToggleRules(srcId) {
    const cur = rulesState[srcId]
    if (cur?.open) {
      setRulesState(s => ({ ...s, [srcId]: { ...s[srcId], open: false } }))
      return
    }
    setRulesState(s => ({ ...s, [srcId]: { ...(s[srcId] ?? {}), open: true } }))
    await loadDomainRules(srcId)
  }

  async function handleGenerateSuggestions(srcId) {
    setRulesState(s => ({ ...s, [srcId]: { ...(s[srcId] ?? {}), generating: true, error: null } }))
    try {
      await generateDomainRuleSuggestions(srcId, token)
      await loadDomainRules(srcId)
      notify('Domain rule suggestions generated.')
    } catch (e) {
      setRulesState(s => ({ ...s, [srcId]: { ...(s[srcId] ?? {}), generating: false, error: e?.message ?? 'Generation failed.' } }))
      notify(e?.message ?? 'Failed to generate suggestions.', false)
    } finally {
      setRulesState(s => ({ ...s, [srcId]: { ...(s[srcId] ?? {}), generating: false } }))
    }
  }

  async function handleApproveRule(srcId, ruleId) {
    setRuleActionState(s => ({ ...s, [ruleId]: true }))
    try {
      await approveDomainRule(ruleId, token)
      setRulesState(s => ({
        ...s,
        [srcId]: {
          ...s[srcId],
          rules: s[srcId]?.rules?.map(r => r.id === ruleId ? { ...r, approval_status: 'APPROVED', active: 1 } : r) ?? null,
        },
      }))
      notify('Rule approved.')
    } catch (e) {
      notify(e?.message ?? 'Approval failed.', false)
    } finally {
      setRuleActionState(s => ({ ...s, [ruleId]: false }))
    }
  }

  async function handleRejectRule(srcId, ruleId) {
    setRuleActionState(s => ({ ...s, [ruleId]: true }))
    try {
      await rejectDomainRule(ruleId, token)
      setRulesState(s => ({
        ...s,
        [srcId]: {
          ...s[srcId],
          rules: s[srcId]?.rules?.map(r => r.id === ruleId ? { ...r, approval_status: 'REJECTED', active: 0 } : r) ?? null,
        },
      }))
      notify('Rule rejected.')
    } catch (e) {
      notify(e?.message ?? 'Rejection failed.', false)
    } finally {
      setRuleActionState(s => ({ ...s, [ruleId]: false }))
    }
  }

  async function loadEntityRules(srcId) {
    setEntityRulesState(s => ({ ...s, [srcId]: { ...(s[srcId] ?? {}), loading: true, error: null } }))
    try {
      const resp = await getEntityRules(srcId, token)
      setEntityRulesState(s => ({ ...s, [srcId]: { ...(s[srcId] ?? {}), loading: false, rules: resp?.data ?? [] } }))
    } catch (e) {
      setEntityRulesState(s => ({ ...s, [srcId]: { ...(s[srcId] ?? {}), loading: false, error: e?.message ?? 'Failed to load entity rules.' } }))
    }
  }

  async function handleToggleEntityRules(srcId) {
    const cur = entityRulesState[srcId]
    if (cur?.open) {
      setEntityRulesState(s => ({ ...s, [srcId]: { ...s[srcId], open: false } }))
      return
    }
    setEntityRulesState(s => ({ ...s, [srcId]: { ...(s[srcId] ?? {}), open: true } }))
    await loadEntityRules(srcId)
  }

  async function handleGenerateEntitySuggestions(srcId) {
    setEntityRulesState(s => ({ ...s, [srcId]: { ...(s[srcId] ?? {}), generating: true, error: null } }))
    try {
      await generateEntityRuleSuggestions(srcId, token)
      await loadEntityRules(srcId)
      notify('Entity rule suggestions generated.')
    } catch (e) {
      setEntityRulesState(s => ({ ...s, [srcId]: { ...(s[srcId] ?? {}), generating: false, error: e?.message ?? 'Generation failed.' } }))
      notify(e?.message ?? 'Failed to generate entity suggestions.', false)
    } finally {
      setEntityRulesState(s => ({ ...s, [srcId]: { ...(s[srcId] ?? {}), generating: false } }))
    }
  }

  async function handleApproveEntityRule(srcId, ruleId) {
    setEntityRuleActionState(s => ({ ...s, [ruleId]: true }))
    try {
      await approveEntityRule(ruleId, token)
      setEntityRulesState(s => ({
        ...s,
        [srcId]: {
          ...s[srcId],
          rules: s[srcId]?.rules?.map(r => r.id === ruleId ? { ...r, approval_status: 'APPROVED', active: 1 } : r) ?? null,
        },
      }))
      notify('Entity rule approved.')
    } catch (e) {
      notify(e?.message ?? 'Approval failed.', false)
    } finally {
      setEntityRuleActionState(s => ({ ...s, [ruleId]: false }))
    }
  }

  async function handleRejectEntityRule(srcId, ruleId) {
    setEntityRuleActionState(s => ({ ...s, [ruleId]: true }))
    try {
      await rejectEntityRule(ruleId, token)
      setEntityRulesState(s => ({
        ...s,
        [srcId]: {
          ...s[srcId],
          rules: s[srcId]?.rules?.map(r => r.id === ruleId ? { ...r, approval_status: 'REJECTED', active: 0 } : r) ?? null,
        },
      }))
      notify('Entity rule rejected.')
    } catch (e) {
      notify(e?.message ?? 'Rejection failed.', false)
    } finally {
      setEntityRuleActionState(s => ({ ...s, [ruleId]: false }))
    }
  }

  async function loadRefinements(srcId) {
    setGovState(s => ({ ...s, [srcId]: { ...(s[srcId] ?? {}), loading: true, error: null } }))
    try {
      const resp = await getDomainRefinements(srcId, token)
      setGovState(s => ({ ...s, [srcId]: { ...(s[srcId] ?? {}), loading: false, refinements: resp?.data ?? [] } }))
    } catch (e) {
      setGovState(s => ({ ...s, [srcId]: { ...(s[srcId] ?? {}), loading: false, error: e?.message ?? 'Failed to load refinements.' } }))
    }
  }

  async function handleToggleGov(srcId) {
    const cur = govState[srcId]
    if (cur?.open) {
      setGovState(s => ({ ...s, [srcId]: { ...s[srcId], open: false } }))
      return
    }
    setGovState(s => ({ ...s, [srcId]: { ...(s[srcId] ?? {}), open: true } }))
    await loadRefinements(srcId)
  }

  async function handleAnalyzeRefinements(srcId) {
    setGovState(s => ({ ...s, [srcId]: { ...(s[srcId] ?? {}), analyzing: true, error: null } }))
    try {
      const resp = await analyzeDomainRefinements(srcId, token)
      setGovState(s => ({
        ...s,
        [srcId]: {
          ...(s[srcId] ?? {}),
          analyzing: false,
          analyzeResult: resp?.data ?? null,
        },
      }))
      await loadRefinements(srcId)
      notify('Refinement analysis complete.')
    } catch (e) {
      setGovState(s => ({ ...s, [srcId]: { ...(s[srcId] ?? {}), analyzing: false, error: e?.message ?? 'Analysis failed.' } }))
      notify(e?.message ?? 'Refinement analysis failed.', false)
    }
  }

  async function handleApproveRefinement(srcId, refId) {
    setRefinementActionState(s => ({ ...s, [refId]: true }))
    try {
      await approveDomainRefinement(refId, token)
      setGovState(s => ({
        ...s,
        [srcId]: {
          ...s[srcId],
          refinements: s[srcId]?.refinements?.map(r => r.id === refId ? { ...r, approval_status: 'APPROVED', active: 1 } : r) ?? null,
        },
      }))
      notify('Refinement approved.')
    } catch (e) {
      notify(e?.message ?? 'Approval failed.', false)
    } finally {
      setRefinementActionState(s => ({ ...s, [refId]: false }))
    }
  }

  async function handleRejectRefinement(srcId, refId) {
    setRefinementActionState(s => ({ ...s, [refId]: true }))
    try {
      await rejectDomainRefinement(refId, token)
      setGovState(s => ({
        ...s,
        [srcId]: {
          ...s[srcId],
          refinements: s[srcId]?.refinements?.map(r => r.id === refId ? { ...r, approval_status: 'REJECTED', active: 0 } : r) ?? null,
        },
      }))
      notify('Refinement rejected.')
    } catch (e) {
      notify(e?.message ?? 'Rejection failed.', false)
    } finally {
      setRefinementActionState(s => ({ ...s, [refId]: false }))
    }
  }

  function toggleSchemaItem(sourceId, type, name) {
    setSchemaExpand(s => {
      const cur = s[sourceId] || { schemas: [], tables: [] }
      const list = cur[type] || []
      const next = list.includes(name) ? list.filter(x => x !== name) : [...list, name]
      return { ...s, [sourceId]: { ...cur, [type]: next } }
    })
  }

  // ── Shared style helpers ───────────────────────────────────────────────────
  const card    = (x = {}) => ({ background: surface, border: `1px solid ${border}`, borderRadius: '12px', padding: '16px 18px', ...x })
  const inp     = (x = {}) => ({ width: '100%', boxSizing: 'border-box', background: bg, border: `1px solid ${border}`, borderRadius: '8px', color: text, fontSize: '0.88rem', padding: '9px 13px', outline: 'none', fontFamily: MONO, letterSpacing: '0.02em', ...x })
  const lbl     = { display: 'block', fontSize: '0.7rem', color: textSec, fontWeight: '600', marginBottom: '6px', letterSpacing: '0.06em', textTransform: 'uppercase', fontFamily: FONT }
  const btnMain = { background: accent, color: '#fff', border: 'none', borderRadius: '8px', padding: '9px 20px', fontSize: '0.86rem', fontWeight: '600', cursor: 'pointer', fontFamily: FONT }
  const btnGhost = (x = {}) => ({ background: 'transparent', color: muted, border: `1px solid ${border}`, borderRadius: '8px', padding: '7px 16px', fontSize: '0.82rem', cursor: 'pointer', fontFamily: FONT, ...x })

  // ── Reusable pipeline step renderer ─────────────────────────────────────────
  // Drives all five Metadata Pipeline steps from a single declarative shape so
  // there is no copy-pasted per-step UI. Status is supplied by the caller and is
  // always derived from real backend data — never hardcoded here.
  const STEP_META = {
    done:    { color: success, text: 'Done',    glyph: '✓' },
    ready:   { color: accent,  text: 'Ready',   glyph: null },
    running: { color: accent,  text: 'Running', glyph: null },
    locked:  { color: muted,   text: 'Locked',  glyph: '🔒' },
    failed:  { color: danger,  text: 'Failed',  glyph: '!' },
  }
  const Spinner = ({ size = 10 }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ animation: 'dsm-spin 1s linear infinite' }}>
      <path d="M21 12a9 9 0 1 1-6.219-8.56" />
    </svg>
  )

  function renderPipelineStep({ number, label, status, description, stats = [], warnings = [], action, secondaryAction, lockReason, error, isLast }) {
    const m = STEP_META[status] ?? STEP_META.ready
    const dim = status === 'locked'
    return (
      <div key={number} style={{ display: 'flex', gap: '12px', opacity: dim ? 0.6 : 1 }}>
        {/* Rail: numbered node + connector */}
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', flexShrink: 0 }}>
          <div style={{
            width: '26px', height: '26px', borderRadius: '50%', flexShrink: 0,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            fontSize: '0.72rem', fontWeight: '700', fontFamily: FONT,
            background: `${m.color}18`, color: m.color, border: `1px solid ${m.color}55`,
          }}>
            {status === 'done' ? '✓' : status === 'failed' ? '!' : number}
          </div>
          {!isLast && <div style={{ flex: 1, width: '1px', minHeight: '14px', background: border, marginTop: '4px' }} />}
        </div>

        {/* Body */}
        <div style={{ flex: 1, minWidth: 0, paddingBottom: isLast ? '2px' : '16px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
            <span style={{ fontSize: '0.85rem', fontWeight: '600', color: text, fontFamily: FONT }}>{label}</span>
            <span style={{
              padding: '1px 8px', borderRadius: '8px', fontSize: '0.6rem', fontWeight: '700',
              letterSpacing: '0.05em', textTransform: 'uppercase', fontFamily: FONT,
              background: `${m.color}18`, color: m.color, border: `1px solid ${m.color}40`,
              display: 'inline-flex', alignItems: 'center', gap: '5px',
            }}>
              {status === 'running' && <Spinner size={9} />}
              {m.text}
            </span>
          </div>

          {description && (
            <p style={{ margin: '3px 0 0', fontSize: '0.72rem', color: muted, fontFamily: FONT }}>{description}</p>
          )}

          {stats.length > 0 && (
            <div style={{ display: 'flex', gap: '12px', flexWrap: 'wrap', marginTop: '6px' }}>
              {stats.map((s, i) => (
                <span key={i} style={{ fontSize: '0.74rem', color: textSec, fontFamily: FONT }}>
                  <span style={{ fontWeight: '600', color: s.color ?? text }}>{s.value}</span>
                  {s.label ? <span style={{ color: muted }}> {s.label}</span> : null}
                </span>
              ))}
            </div>
          )}

          {warnings.length > 0 && (
            <div style={{ marginTop: '5px', display: 'flex', flexDirection: 'column', gap: '2px' }}>
              {warnings.map((w, i) => (
                <span key={i} style={{ fontSize: '0.71rem', color: warn, fontFamily: FONT }}>⚠ {w}</span>
              ))}
            </div>
          )}

          {status === 'locked' && lockReason && (
            <p style={{ margin: '6px 0 0', fontSize: '0.71rem', color: muted, fontFamily: FONT, fontStyle: 'italic' }}>{lockReason}</p>
          )}

          {error && (
            <p style={{ margin: '6px 0 0', fontSize: '0.72rem', color: danger, fontFamily: FONT }}>{error}</p>
          )}

          {status !== 'locked' && (action || secondaryAction) && (
            <div style={{ display: 'flex', gap: '8px', marginTop: '8px', flexWrap: 'wrap' }}>
              {action && (
                <button
                  onClick={action.onClick}
                  disabled={action.disabled}
                  style={{
                    ...btnGhost({ padding: '5px 13px', fontSize: '0.76rem' }),
                    color: action.disabled ? `${muted}50` : (action.primary ? accent : textSec),
                    borderColor: action.primary && !action.disabled ? `${accent}55` : border,
                    cursor: action.disabled ? 'not-allowed' : 'pointer',
                    display: 'inline-flex', alignItems: 'center', gap: '6px',
                  }}
                >
                  {action.loading && <Spinner />}
                  {action.label}
                </button>
              )}
              {secondaryAction && (
                <button
                  onClick={secondaryAction.onClick}
                  disabled={secondaryAction.disabled}
                  style={{
                    ...btnGhost({ padding: '5px 13px', fontSize: '0.76rem' }),
                    color: secondaryAction.disabled ? `${muted}50` : textSec,
                    cursor: secondaryAction.disabled ? 'not-allowed' : 'pointer',
                  }}
                >
                  {secondaryAction.label}
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    )
  }

  // ── Source Workspace ────────────────────────────────────────────────────
  if (dsSelectedSourceId != null && setDsSelectedSourceId) {
    const src = sources.find(s => s.id === dsSelectedSourceId)

    // Pull all state slices for this source
    const discSt   = discoverState[dsSelectedSourceId]   ?? {}
    const ts       = testState[dsSelectedSourceId]       ?? {}
    const js       = jobState[dsSelectedSourceId]        ?? {}
    const job      = js.data
    const sc       = schemaState[dsSelectedSourceId]     ?? {}
    const prof     = profileState[dsSelectedSourceId]    ?? {}
    const dict     = dictState[dsSelectedSourceId]       ?? {}
    const dom      = domainState[dsSelectedSourceId]     ?? {}
    const ent      = entityState[dsSelectedSourceId]     ?? {}
    const rs       = rulesState[dsSelectedSourceId]      ?? {}
    const ers      = entityRulesState[dsSelectedSourceId]?? {}
    const gs       = govState[dsSelectedSourceId]        ?? {}
    const domAsgn  = domAssignState[dsSelectedSourceId]  ?? {}
    const entAsgn  = entAssignState[dsSelectedSourceId]  ?? {}
    const profHist = profileHistState[dsSelectedSourceId]?? {}

    // Pipeline computed values (mirrors existing logic exactly)
    const hasSchema    = src?.last_snapshot_id != null || !!discSt.result
    const profSnap     = prof.data?.snapshot
    const profComplete = profSnap?.status === 'COMPLETE'
    const dictTables   = Array.isArray(dict.tables) ? dict.tables : null
    const dictCount    = dictTables?.length ?? 0
    const dictApproved = dictTables?.filter(t => t.is_approved === 1 || t.is_approved === true).length ?? 0
    const dictPct      = dictCount > 0 ? Math.round((dictApproved / dictCount) * 100) : 0
    const domSummary   = dom.summary
    const entSummary   = ent.summary
    const domAssigned  = domSummary?.tables_assigned ?? 0
    const entAssigned  = entSummary?.entities_assigned ?? 0

    const lastTest = ts.status ?? src?.last_test_status
    let s1 = 'ready'
    if (lastTest === 'success') s1 = 'done'
    else if (lastTest === 'failed') s1 = 'failed'

    const jobRunning = job?.status === 'RUNNING' || job?.status === 'QUEUED'
    let s2
    if (src?.source_status !== 'ACTIVE') s2 = 'locked'
    else if (discSt.loading || js.running || jobRunning) s2 = 'running'
    else if (job?.status === 'FAILED' || discSt.error) s2 = 'failed'
    else if (hasSchema) s2 = 'done'
    else s2 = 'ready'

    let s3
    if (!hasSchema) s3 = 'locked'
    else if (dict.generating) s3 = 'running'
    else if (dictCount > 0) s3 = 'done'
    else if (dict.error) s3 = 'failed'
    else s3 = 'ready'

    let s4
    if (dictCount === 0) s4 = 'locked'
    else if (dom.generating) s4 = 'running'
    else if (domAssigned > 0) s4 = 'done'
    else if (dom.error) s4 = 'failed'
    else s4 = 'ready'

    let s5
    if (domAssigned === 0) s5 = 'locked'
    else if (ent.generating) s5 = 'running'
    else if (entAssigned > 0) s5 = 'done'
    else if (ent.error) s5 = 'failed'
    else s5 = 'ready'

    const pendingRules    = (rs.rules  ?? []).filter(r => r.approval_status === 'PENDING').length
    const pendingEntRules = (ers.rules ?? []).filter(r => r.approval_status === 'PENDING').length
    const pendingRefs     = (gs.refinements ?? []).filter(r => r.approval_status === 'PENDING').length
    const totalPending    = pendingRules + pendingEntRules + pendingRefs
    let s6 = 'locked'
    if (entAssigned > 0 || s5 === 'done') s6 = totalPending > 0 ? 'ready' : 'done'

    const smBadge = SOURCE_STATUS_META[src?.source_status] ?? { label: src?.source_status ?? '—', color: '#94a3b8' }
    const stLabel = SOURCE_TYPES.find(t => t.value === src?.source_type)?.label ?? src?.source_type ?? '—'

    const WORKSPACE_TABS = [
      { id: 'overview',   label: 'Overview'   },
      { id: 'schema',     label: 'Schema'     },
      { id: 'profile',    label: 'Profile'    },
      { id: 'dictionary', label: 'Dictionary' },
      { id: 'domains',    label: 'Domains'    },
      { id: 'entities',   label: 'Entities'   },
      { id: 'governance', label: 'Governance' },
      { id: 'lineage',    label: 'Lineage'    },
      { id: 'runs',       label: 'Runs'       },
    ]
    const activeTab = dsActiveTab ?? 'overview'

    // ── Local render helpers (called as functions, not JSX components) ────

    const stepCol = s => ({ done: success, ready: accent, running: accent, locked: muted, failed: danger }[s] ?? muted)
    const stepLabel = { done: 'Complete', ready: 'Ready', running: 'Running', locked: 'Locked', failed: 'Failed' }

    const matBadge = (label, status) => {
      const color = status === 'done' ? success : status === 'partial' ? warn : muted
      if (status === 'none') return null
      return (
        <span key={label} style={{ display: 'inline-flex', alignItems: 'center', gap: '3px', padding: '1px 9px', borderRadius: '10px', fontSize: '0.6rem', fontWeight: '700', letterSpacing: '0.05em', textTransform: 'uppercase', background: `${color}18`, color, border: `1px solid ${color}40`, fontFamily: FONT }}>
          {status === 'done' ? '✓ ' : ''}{label}
        </span>
      )
    }

    const pipCard = ({ num, title, desc, status, stat, action, viewTab, err }) => {
      const col = stepCol(status)
      return (
        <div style={{ ...card({ padding: '14px 16px' }), display: 'flex', flexDirection: 'column', gap: '8px' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '8px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <div style={{ width: '22px', height: '22px', borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '0.65rem', fontWeight: '700', background: `${col}18`, color: col, border: `1px solid ${col}40`, flexShrink: 0 }}>
                {status === 'done' ? '✓' : status === 'failed' ? '!' : num}
              </div>
              <span style={{ fontSize: '0.83rem', fontWeight: '600', color: text, fontFamily: FONT }}>{title}</span>
            </div>
            <span style={{ padding: '1px 8px', borderRadius: '8px', fontSize: '0.6rem', fontWeight: '700', letterSpacing: '0.05em', textTransform: 'uppercase', background: `${col}18`, color: col, border: `1px solid ${col}40`, fontFamily: FONT, display: 'inline-flex', alignItems: 'center', gap: '4px', flexShrink: 0 }}>
              {status === 'running' && <Spinner size={8} />}
              {stepLabel[status] ?? status}
            </span>
          </div>
          <p style={{ margin: 0, fontSize: '0.73rem', color: muted, fontFamily: FONT, lineHeight: 1.4 }}>{desc}</p>
          {stat && <div style={{ fontSize: '0.73rem', color: textSec, fontFamily: FONT }}>{stat}</div>}
          {err  && <div style={{ fontSize: '0.72rem', color: danger,  fontFamily: FONT }}>{err}</div>}
          {(action || viewTab) && (
            <div style={{ display: 'flex', gap: '8px', marginTop: '2px', flexWrap: 'wrap' }}>
              {action && (
                <button onClick={action.onClick} disabled={action.disabled} style={{ ...btnGhost({ padding: '5px 12px', fontSize: '0.74rem' }), color: action.disabled ? `${muted}50` : (action.primary ? accent : textSec), borderColor: action.primary && !action.disabled ? `${accent}55` : border, cursor: action.disabled ? 'not-allowed' : 'pointer', display: 'inline-flex', alignItems: 'center', gap: '5px' }}>
                  {action.loading && <Spinner />}{action.label}
                </button>
              )}
              {viewTab && (
                <button onClick={() => setDsActiveTab(viewTab)} style={{ ...btnGhost({ padding: '5px 12px', fontSize: '0.74rem' }), color: accent }}>View details →</button>
              )}
            </div>
          )}
        </div>
      )
    }

    const pTypBadge = pt => {
      const colors = { PREFIX: accent, SUFFIX: '#38bdf8', TOKEN: '#10b981', SCHEMA: '#a78bfa' }
      const c = colors[pt] ?? muted
      return { display: 'inline-block', padding: '1px 7px', borderRadius: '6px', fontSize: '0.58rem', fontWeight: '700', letterSpacing: '0.05em', background: `${c}18`, color: c, border: `1px solid ${c}35`, fontFamily: FONT, textTransform: 'uppercase', flexShrink: 0 }
    }

    const govSectionLabel = (label, n, color) => (
      <div style={{ fontSize: '0.62rem', color: color ?? muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', marginBottom: '6px', fontFamily: FONT }}>
        {label}{n != null ? ` (${n})` : ''}
      </div>
    )

    const approvalRows = (items, onApprove, onReject, actState, entityMode = false) => items.map(item => (
      <div key={item.id} style={{ display: 'flex', alignItems: 'center', gap: '7px', flexWrap: 'wrap', padding: '7px 10px', borderRadius: '6px', background: `${accent}06`, border: `1px solid ${border}` }}>
        <span style={pTypBadge(item.pattern_type)}>{item.pattern_type}</span>
        <span style={{ fontFamily: MONO, fontSize: '0.79rem', color: text, flex: '0 0 auto' }}>{item.pattern_value}</span>
        <span style={{ fontSize: '0.73rem', color: muted }}>→</span>
        <span style={{ fontSize: '0.76rem', color: textSec, flex: 1 }}>{entityMode ? item.entity : (item.suggested_domain ?? item.domain)}</span>
        {item.confidence   != null && <span style={{ fontSize: '0.67rem', color: muted, fontFamily: MONO }}>{Math.round(item.confidence * 100)}%</span>}
        {item.support_count!= null && <span style={{ fontSize: '0.67rem', color: muted, fontFamily: MONO }}>sup:{item.support_count}</span>}
        <div style={{ display: 'flex', gap: '4px', marginLeft: 'auto' }}>
          <button onClick={() => !actState[item.id] && onApprove(item.id)} disabled={!!actState[item.id]} style={{ background: actState[item.id] ? `${success}30` : `${success}20`, color: actState[item.id] ? `${success}60` : success, border: `1px solid ${success}40`, borderRadius: '6px', padding: '3px 10px', fontSize: '0.74rem', fontWeight: '600', cursor: actState[item.id] ? 'not-allowed' : 'pointer', fontFamily: FONT }}>{actState[item.id] ? '…' : 'Approve'}</button>
          <button onClick={() => !actState[item.id] && onReject(item.id)}  disabled={!!actState[item.id]} style={{ background: 'transparent', color: actState[item.id] ? `${muted}50` : muted, border: `1px solid ${border}`, borderRadius: '6px', padding: '3px 10px', fontSize: '0.74rem', cursor: actState[item.id] ? 'not-allowed' : 'pointer', fontFamily: FONT }}>{actState[item.id] ? '…' : 'Reject'}</button>
        </div>
      </div>
    ))

    const approvedRows = (items, entityMode = false) => items.map(item => (
      <div key={item.id} style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.74rem', padding: '3px 8px' }}>
        <span style={pTypBadge(item.pattern_type)}>{item.pattern_type}</span>
        <span style={{ fontFamily: MONO, color: textSec }}>{item.pattern_value}</span>
        <span style={{ color: muted }}>→</span>
        <span style={{ color: success }}>{entityMode ? item.entity : (item.suggested_domain ?? item.domain)}</span>
      </div>
    ))

    // ── Tab content ────────────────────────────────────────────────────────

    const overviewTab = (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
        {/* Connection row */}
        <div style={{ ...card({ padding: '14px 18px' }), display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '12px' }}>
          <div>
            <div style={{ fontSize: '0.7rem', fontWeight: '700', color: muted, letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '4px' }}>Connection</div>
            <div style={{ fontSize: '0.85rem', color: textSec, fontFamily: MONO, wordBreak: 'break-all' }}>{src?.config_summary ?? '—'}</div>
          </div>
          <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' }}>
            {src?.last_tested_at && (
              <span style={{ fontSize: '0.71rem', color: muted, fontFamily: FONT }}>
                Tested {fmtRelative(src.last_tested_at)}
                <span style={{ marginLeft: '5px', color: src.last_test_status === 'success' ? success : danger }}>{src.last_test_status === 'success' ? '✓' : '✗'}</span>
              </span>
            )}
            <button onClick={() => !ts.loading && handleTest(dsSelectedSourceId)} disabled={ts.loading} style={{ ...btnGhost({ padding: '6px 14px', fontSize: '0.78rem' }), color: ts.loading ? `${muted}50` : accent, borderColor: `${accent}50`, cursor: ts.loading ? 'not-allowed' : 'pointer', display: 'inline-flex', alignItems: 'center', gap: '5px' }}>
              {ts.loading && <Spinner />}{ts.loading ? 'Testing…' : 'Test Connection'}
            </button>
          </div>
        </div>
        {ts.status && !ts.loading && (
          <div style={{ padding: '10px 14px', borderRadius: '8px', background: ts.status === 'success' ? `${success}12` : `${danger}12`, border: `1px solid ${ts.status === 'success' ? success + '40' : danger + '40'}` }}>
            <span style={{ fontWeight: '700', fontSize: '0.78rem', color: ts.status === 'success' ? success : danger, fontFamily: FONT }}>{ts.status === 'success' ? '✓ Connected' : '✗ Failed'}</span>
            {ts.latency_ms != null && <span style={{ fontSize: '0.74rem', color: muted, fontFamily: MONO, marginLeft: '10px' }}>{ts.latency_ms}ms</span>}
            {ts.message && <p style={{ margin: '4px 0 0', fontSize: '0.77rem', color: textSec, fontFamily: FONT }}>{ts.message}</p>}
          </div>
        )}

        {/* Pipeline grid */}
        <div>
          <div style={{ fontSize: '0.68rem', fontWeight: '700', color: muted, letterSpacing: '0.08em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '10px' }}>Metadata Pipeline</div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: '10px' }}>
            {pipCard({ num: 1, title: 'Connect & Verify', status: s1, desc: 'Verify connectivity and credentials to the database.', stat: s1 === 'done' ? (ts.latency_ms ? `${ts.latency_ms}ms response` : 'Connected') : null, err: s1 === 'failed' ? (ts.message || src?.last_test_message || null) : null, action: { label: ts.loading ? 'Testing…' : 'Test Connection', primary: s1 !== 'done', onClick: () => !ts.loading && handleTest(dsSelectedSourceId), disabled: ts.loading, loading: ts.loading } })}
            {pipCard({ num: 2, title: 'Discover & Profile', status: s2, desc: 'Crawl schemas, tables, and columns, then run structural profiling.', stat: hasSchema && profSnap ? `${profSnap.tables_profiled ?? '?'} / ${profSnap.tables_total ?? '?'} assets profiled · ${profSnap.status}` : hasSchema ? 'Schema snapshot available' : null, err: s2 === 'failed' ? (job?.error_message || discSt.error || null) : null, action: s2 === 'running' ? { label: js.loading ? 'Refreshing…' : 'Refresh Status', onClick: () => src?.metadata_job_id && loadJobStatus(dsSelectedSourceId, src.metadata_job_id), disabled: js.loading, loading: js.loading } : { label: hasSchema ? 'Re-run' : 'Discover & Profile', primary: !hasSchema, onClick: () => src && handleDiscoverAndProfile(src), disabled: discSt.loading || js.running, loading: discSt.loading || js.running }, viewTab: hasSchema ? 'schema' : null })}
            {pipCard({ num: 3, title: 'Generate Dictionary', status: s3, desc: 'AI-generate business names, descriptions, and column semantics.', stat: dictCount > 0 ? `${dictCount} tables · ${dictPct}% approved` : null, err: s3 === 'failed' ? dict.error : null, action: { label: dict.generating ? 'Generating…' : dictCount > 0 ? 'Regenerate' : 'Generate', primary: dictCount === 0 && hasSchema, onClick: () => !dict.generating && handleGenerateDictionary(dsSelectedSourceId), disabled: dict.generating || !hasSchema, loading: dict.generating }, viewTab: dictCount > 0 ? 'dictionary' : null })}
            {pipCard({ num: 4, title: 'Generate Domains', status: s4, desc: 'Classify tables into business domains (Sales, Finance, Product, etc.).', stat: domAssigned > 0 ? `${domAssigned} tables assigned` : null, err: s4 === 'failed' ? dom.error : null, action: { label: dom.generating ? 'Generating…' : domAssigned > 0 ? 'Regenerate' : 'Generate', primary: domAssigned === 0 && dictCount > 0, onClick: () => !dom.generating && handleGenerateDomains(dsSelectedSourceId), disabled: dom.generating || dictCount === 0, loading: dom.generating }, viewTab: domAssigned > 0 ? 'domains' : null })}
            {pipCard({ num: 5, title: 'Generate Entities', status: s5, desc: 'Identify the primary business entity each table represents.', stat: entAssigned > 0 ? `${entAssigned} entities assigned` : null, err: s5 === 'failed' ? ent.error : null, action: { label: ent.generating ? 'Generating…' : entAssigned > 0 ? 'Regenerate' : 'Generate', primary: entAssigned === 0 && domAssigned > 0, onClick: () => !ent.generating && handleGenerateEntities(dsSelectedSourceId), disabled: ent.generating || domAssigned === 0, loading: ent.generating }, viewTab: entAssigned > 0 ? 'entities' : null })}
            {pipCard({ num: 6, title: 'Govern & Refine', status: s6, desc: 'Review domain rules, entity rules, and refinement suggestions.', stat: totalPending > 0 ? `${totalPending} items pending review` : s6 === 'done' ? 'No pending items' : null, viewTab: s6 !== 'locked' ? 'governance' : null })}
          </div>
        </div>

        {/* Intelligence summary metrics */}
        {(hasSchema || dictCount > 0 || domAssigned > 0 || entAssigned > 0) && (
          <div>
            <div style={{ fontSize: '0.68rem', fontWeight: '700', color: muted, letterSpacing: '0.08em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '10px' }}>Intelligence Summary</div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(160px, 1fr))', gap: '10px' }}>
              {profSnap?.tables_total != null && (<div style={card({ padding: '12px 14px' })}><div style={{ fontSize: '0.62rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '4px' }}>Tables</div><div style={{ fontSize: '1.4rem', fontWeight: '700', color: text, fontFamily: FONT, lineHeight: 1 }}>{profSnap.tables_total.toLocaleString()}</div><div style={{ fontSize: '0.72rem', color: muted, fontFamily: FONT }}>discovered</div></div>)}
              {dictCount > 0 && (<div style={card({ padding: '12px 14px' })}><div style={{ fontSize: '0.62rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '4px' }}>Dictionary</div><div style={{ fontSize: '1.4rem', fontWeight: '700', color: text, fontFamily: FONT, lineHeight: 1 }}>{dictPct}%</div><div style={{ fontSize: '0.72rem', color: muted, fontFamily: FONT }}>{dictApproved} / {dictCount} approved</div></div>)}
              {domSummary?.total_domains != null && (<div style={card({ padding: '12px 14px' })}><div style={{ fontSize: '0.62rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '4px' }}>Domains</div><div style={{ fontSize: '1.4rem', fontWeight: '700', color: text, fontFamily: FONT, lineHeight: 1 }}>{domSummary.total_domains}</div><div style={{ fontSize: '0.72rem', color: muted, fontFamily: FONT }}>{domAssigned} tables assigned</div></div>)}
              {entSummary?.total_entities != null && (<div style={card({ padding: '12px 14px' })}><div style={{ fontSize: '0.62rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '4px' }}>Entities</div><div style={{ fontSize: '1.4rem', fontWeight: '700', color: text, fontFamily: FONT, lineHeight: 1 }}>{entSummary.total_entities}</div><div style={{ fontSize: '0.72rem', color: muted, fontFamily: FONT }}>{entAssigned} tables assigned</div></div>)}
            </div>
          </div>
        )}
      </div>
    )

    const schemaTab = (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
        {!hasSchema && (<div style={{ ...card({ padding: '40px 24px' }), textAlign: 'center' }}><p style={{ margin: '0 0 8px', fontSize: '0.88rem', color: textSec, fontWeight: '500', fontFamily: FONT }}>Schema not discovered yet</p><p style={{ margin: '0 0 16px', fontSize: '0.78rem', color: muted, fontFamily: FONT }}>Run Discover & Profile to crawl schemas, tables, and columns.</p><button onClick={() => src && handleDiscoverAndProfile(src)} disabled={!src || discSt.loading || js.running} style={{ ...btnGhost({ padding: '7px 16px', fontSize: '0.8rem' }), color: accent, borderColor: `${accent}50` }}>Discover & Profile</button></div>)}
        {hasSchema && sc.loading  && (<div style={{ ...card({ padding: '36px 24px' }), textAlign: 'center', color: muted, fontSize: '0.82rem', fontFamily: FONT, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px' }}><Spinner size={12} /> Loading schema…</div>)}
        {hasSchema && !sc.data && !sc.loading && (<div style={{ ...card({ padding: '24px' }), textAlign: 'center' }}><p style={{ margin: '0 0 8px', fontSize: '0.82rem', color: textSec, fontFamily: FONT }}>Schema not loaded for this session.</p><button onClick={() => loadSchema(dsSelectedSourceId)} style={{ ...btnGhost({ padding: '7px 16px', fontSize: '0.8rem' }), color: accent, borderColor: `${accent}50` }}>Load Schema</button></div>)}
        {sc.error && (<div style={{ padding: '10px 14px', borderRadius: '8px', background: `${danger}10`, border: `1px solid ${danger}30` }}><span style={{ fontSize: '0.78rem', color: danger, fontFamily: FONT }}>{sc.error}</span></div>)}
        {sc.data && (
          <div style={card({ overflow: 'hidden' })}>
            <div style={{ padding: '10px 14px', borderBottom: `1px solid ${border}`, display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '8px' }}>
              <div style={{ fontSize: '0.78rem', color: textSec, fontFamily: FONT }}>
                <span style={{ fontWeight: '600' }}>{sc.data.database_name ?? 'Database'}</span>
                <span style={{ color: muted, marginLeft: '8px' }}>{sc.data.schemas?.length ?? 0} schemas</span>
                {sc.data.discovery_duration_ms != null && <span style={{ color: muted, marginLeft: '8px' }}>discovered in {sc.data.discovery_duration_ms}ms</span>}
              </div>
              <button onClick={() => loadSchema(dsSelectedSourceId)} disabled={sc.loading} style={{ ...btnGhost({ padding: '4px 10px', fontSize: '0.74rem' }), color: textSec }}>Refresh</button>
            </div>
            {sc.data.schemas?.map(schema => {
              const sexp   = schemaExpand[dsSelectedSourceId]?.schemas.includes(schema.schema_name)
              const tables = schema.tables?.filter(t => t.table_type === 'TABLE') ?? []
              const views  = schema.tables?.filter(t => t.table_type === 'VIEW')  ?? []
              return (
                <div key={schema.schema_name} style={{ borderBottom: `1px solid ${border}30` }}>
                  <div onClick={() => toggleSchemaItem(dsSelectedSourceId, 'schemas', schema.schema_name)} style={{ padding: '9px 14px', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '8px', fontSize: '0.82rem', fontFamily: FONT, userSelect: 'none', background: sexp ? `${accent}06` : 'transparent' }}>
                    <span style={{ fontSize: '0.6rem', color: muted, width: '10px', flexShrink: 0 }}>{sexp ? '▾' : '▸'}</span>
                    <span style={{ fontWeight: '600', color: textSec }}>{schema.schema_name}</span>
                    <span style={{ fontSize: '0.68rem', color: muted }}>{tables.length} table{tables.length !== 1 ? 's' : ''}{views.length > 0 ? ` · ${views.length} view${views.length !== 1 ? 's' : ''}` : ''}</span>
                  </div>
                  {sexp && (
                    <div style={{ paddingLeft: '8px', paddingBottom: '4px' }}>
                      {schema.tables?.map(t => {
                        const texp   = schemaExpand[dsSelectedSourceId]?.tables.includes(t.table_fqn)
                        const isView = t.table_type === 'VIEW'
                        return (
                          <div key={t.table_fqn}>
                            <div onClick={() => toggleSchemaItem(dsSelectedSourceId, 'tables', t.table_fqn)} style={{ padding: '5px 14px', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '7px', fontSize: '0.78rem', fontFamily: MONO, userSelect: 'none' }}>
                              <span style={{ fontSize: '0.58rem', color: muted, width: '10px' }}>{texp ? '▾' : '▸'}</span>
                              {isView && <span style={{ fontSize: '0.6rem', padding: '1px 5px', borderRadius: '4px', background: `${muted}18`, color: muted, border: `1px solid ${muted}30`, fontFamily: FONT }}>VIEW</span>}
                              <span style={{ color: text }}>{t.table_name}</span>
                              {t.row_count_estimate != null && <span style={{ fontSize: '0.67rem', color: muted }}>~{t.row_count_estimate.toLocaleString()} rows</span>}
                              <span style={{ fontSize: '0.67rem', color: muted, marginLeft: 'auto' }}>{t.columns?.length ?? 0} cols</span>
                            </div>
                            {texp && (
                              <div style={{ paddingLeft: '28px', paddingBottom: '6px' }}>
                                <div style={{ display: 'grid', gridTemplateColumns: '1fr 110px 70px 50px', padding: '3px 14px', fontSize: '0.6rem', fontWeight: '700', color: muted, letterSpacing: '0.06em', textTransform: 'uppercase', fontFamily: FONT }}>
                                  <span>Column</span><span>Type</span><span>Nullable</span><span>Key</span>
                                </div>
                                {t.columns?.map(c => (
                                  <div key={c.column_name} style={{ display: 'grid', gridTemplateColumns: '1fr 110px 70px 50px', padding: '3px 14px', fontSize: '0.74rem', fontFamily: MONO, borderTop: `1px solid ${border}15` }}>
                                    <span style={{ color: text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{c.column_name}</span>
                                    <span style={{ color: muted }}>{c.data_type}</span>
                                    <span style={{ color: c.is_nullable ? `${muted}80` : muted }}>{c.is_nullable ? 'yes' : 'no'}</span>
                                    <span>{c.is_primary_key ? <span style={{ fontSize: '0.6rem', padding: '1px 5px', borderRadius: '4px', background: `${accent}20`, color: accent, border: `1px solid ${accent}30`, fontFamily: FONT }}>PK</span> : null}</span>
                                  </div>
                                ))}
                              </div>
                            )}
                          </div>
                        )
                      })}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </div>
    )

    const profileTab = (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
        {!hasSchema && (<div style={{ ...card({ padding: '40px 24px' }), textAlign: 'center' }}><p style={{ margin: '0 0 6px', fontSize: '0.88rem', color: textSec, fontWeight: '500', fontFamily: FONT }}>No profiling data yet</p><p style={{ margin: 0, fontSize: '0.78rem', color: muted, fontFamily: FONT }}>Run Discover & Profile to generate a structural profile.</p></div>)}
        {hasSchema && !profSnap && !prof.loading && (<div style={{ ...card({ padding: '40px 24px' }), textAlign: 'center' }}><p style={{ margin: '0 0 6px', fontSize: '0.88rem', color: textSec, fontWeight: '500', fontFamily: FONT }}>Profile not available</p><p style={{ margin: '0 0 16px', fontSize: '0.78rem', color: muted, fontFamily: FONT }}>Schema was discovered but structural profiling has not run yet.</p><button onClick={() => src && handleDiscoverAndProfile(src)} disabled={!src || js.running || discSt.loading} style={{ ...btnGhost({ padding: '7px 16px', fontSize: '0.8rem' }), color: accent, borderColor: `${accent}50` }}>Run Profile</button></div>)}
        {prof.loading && (<div style={{ ...card({ padding: '24px' }), textAlign: 'center', color: muted, fontSize: '0.82rem', fontFamily: FONT, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px' }}><Spinner size={12} /> Loading profile…</div>)}
        {prof.error  && (<div style={{ padding: '10px 14px', borderRadius: '8px', background: `${danger}10`, border: `1px solid ${danger}30` }}><span style={{ fontSize: '0.78rem', color: danger, fontFamily: FONT }}>{prof.error}</span></div>)}
        {profSnap && (
          <>
            <div style={{ ...card({ padding: '14px 18px' }), display: 'flex', gap: '24px', flexWrap: 'wrap', alignItems: 'center' }}>
              <div><div style={{ fontSize: '0.62rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '4px' }}>Status</div><span style={{ padding: '2px 10px', borderRadius: '8px', fontSize: '0.72rem', fontWeight: '700', background: profComplete ? `${success}18` : `${accent}18`, color: profComplete ? success : accent, border: `1px solid ${profComplete ? success : accent}40`, fontFamily: FONT }}>{profSnap.status}</span></div>
              {profSnap.tables_total != null && (<div><div style={{ fontSize: '0.62rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '4px' }}>Assets Profiled</div><div style={{ fontSize: '1.1rem', fontWeight: '700', color: text, fontFamily: FONT }}>{profSnap.tables_profiled ?? '—'} <span style={{ fontSize: '0.78rem', color: muted, fontWeight: '400' }}>/ {profSnap.tables_total}</span></div></div>)}
              {profSnap.columns_total != null && profSnap.columns_total > 0 && (<div><div style={{ fontSize: '0.62rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '4px' }}>Columns</div><div style={{ fontSize: '1.1rem', fontWeight: '700', color: text, fontFamily: FONT }}>{profSnap.columns_total.toLocaleString()}</div></div>)}
              {profSnap.profiling_snapshot_id != null && (<div style={{ marginLeft: 'auto' }}><div style={{ fontSize: '0.62rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '4px' }}>Snapshot ID</div><div style={{ fontSize: '0.74rem', color: muted, fontFamily: MONO }}>{profSnap.profiling_snapshot_id}</div></div>)}
            </div>
            <div style={card({ padding: '12px 18px' })}>
              <div style={{ fontSize: '0.7rem', fontWeight: '700', color: muted, letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '8px' }}>About This Profile</div>
              <p style={{ margin: 0, fontSize: '0.78rem', color: textSec, fontFamily: FONT, lineHeight: 1.5 }}>Structural profiling captures schema layout, table row estimates, and column types. Column-level quality metrics (null rates, uniqueness, distributions) require a full profile run. Business metadata including PII classification is in the Dictionary tab.</p>
            </div>
          </>
        )}
      </div>
    )

    const domainsTab = (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
        <p style={{ margin: 0, fontSize: '0.78rem', color: muted, fontFamily: FONT }}>Domains group tables by business area so users can understand ownership and purpose.</p>
        {dictCount === 0 && (<div style={{ ...card({ padding: '40px 24px' }), textAlign: 'center' }}><p style={{ margin: '0 0 6px', fontSize: '0.88rem', color: textSec, fontWeight: '500', fontFamily: FONT }}>Domains not generated yet</p><p style={{ margin: 0, fontSize: '0.78rem', color: muted, fontFamily: FONT }}>Generate a dictionary first, then generate domains.</p></div>)}
        {dictCount > 0 && domAssigned === 0 && !dom.generating && (<div style={{ ...card({ padding: '32px 24px' }), textAlign: 'center' }}><p style={{ margin: '0 0 6px', fontSize: '0.88rem', color: textSec, fontWeight: '500', fontFamily: FONT }}>No domains assigned yet</p><p style={{ margin: '0 0 16px', fontSize: '0.78rem', color: muted, fontFamily: FONT }}>Classify tables into business areas like Sales, Finance, or Product.</p><button onClick={() => handleGenerateDomains(dsSelectedSourceId)} disabled={dom.generating} style={{ ...btnGhost({ padding: '7px 16px', fontSize: '0.8rem' }), color: accent, borderColor: `${accent}50` }}>Generate Domains</button></div>)}
        {dom.generating && (<div style={{ ...card({ padding: '24px' }), textAlign: 'center', color: muted, fontSize: '0.82rem', fontFamily: FONT, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px' }}><Spinner size={12} /> Generating domain assignments…</div>)}
        {dom.error && (<div style={{ padding: '10px 14px', borderRadius: '8px', background: `${danger}10`, border: `1px solid ${danger}30` }}><span style={{ fontSize: '0.78rem', color: danger, fontFamily: FONT }}>{dom.error}</span></div>)}
        {domSummary && (
          <>
            <div style={{ ...card({ padding: '14px 18px' }), display: 'flex', gap: '24px', flexWrap: 'wrap', alignItems: 'center', justifyContent: 'space-between' }}>
              <div style={{ display: 'flex', gap: '24px', flexWrap: 'wrap' }}>
                {domSummary.total_domains    != null && (<div><div style={{ fontSize: '0.62rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '4px' }}>Domains</div><div style={{ fontSize: '1.3rem', fontWeight: '700', color: text, fontFamily: FONT, lineHeight: 1 }}>{domSummary.total_domains}</div></div>)}
                {domSummary.tables_assigned  != null && (<div><div style={{ fontSize: '0.62rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '4px' }}>Assigned</div><div style={{ fontSize: '1.3rem', fontWeight: '700', color: text, fontFamily: FONT, lineHeight: 1 }}>{domSummary.tables_assigned}</div></div>)}
                {domSummary.tables_unknown   != null && domSummary.tables_unknown > 0 && (<div><div style={{ fontSize: '0.62rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '4px' }}>Unclassified</div><div style={{ fontSize: '1.3rem', fontWeight: '700', color: warn, fontFamily: FONT, lineHeight: 1 }}>{domSummary.tables_unknown}</div></div>)}
              </div>
              <button onClick={() => handleGenerateDomains(dsSelectedSourceId)} disabled={dom.generating} style={{ ...btnGhost({ padding: '6px 14px', fontSize: '0.78rem' }), color: textSec }}>{dom.generating ? 'Regenerating…' : 'Regenerate'}</button>
            </div>
            {domSummary.domain_counts && (
              <div style={card({ padding: '14px 18px' })}>
                <div style={{ fontSize: '0.7rem', fontWeight: '700', color: muted, letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '10px' }}>Domain Breakdown</div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '7px' }}>
                  {Object.entries(domSummary.domain_counts).sort((a, b) => b[1] - a[1]).map(([domain, count]) => {
                    const pct = Math.round((count / (domSummary.tables_assigned || 1)) * 100)
                    const isUnknown = domain === 'Unknown'
                    return (
                      <div key={domain} style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                        <span style={{ fontSize: '0.78rem', color: isUnknown ? muted : textSec, fontFamily: FONT, width: '130px', flexShrink: 0 }}>{domain}</span>
                        <div style={{ flex: 1, height: '6px', borderRadius: '3px', background: border }}><div style={{ height: '100%', borderRadius: '3px', background: isUnknown ? muted : accent, width: `${pct}%` }} /></div>
                        <span style={{ fontSize: '0.74rem', color: muted, fontFamily: MONO, width: '28px', textAlign: 'right', flexShrink: 0 }}>{count}</span>
                      </div>
                    )
                  })}
                </div>
              </div>
            )}
          </>
        )}
        {domAssigned > 0 && !domAsgn.data && !domAsgn.loading && (<div style={{ ...card({ padding: '12px 16px' }), display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px' }}><span style={{ fontSize: '0.78rem', color: muted, fontFamily: FONT }}>Load per-table domain assignments for full detail.</span><button onClick={() => loadDomainAssignments(dsSelectedSourceId)} style={{ ...btnGhost({ padding: '5px 12px', fontSize: '0.76rem' }), color: accent, borderColor: `${accent}50` }}>Load Assignments</button></div>)}
        {domAsgn.loading && (<div style={{ ...card({ padding: '16px' }), textAlign: 'center', color: muted, fontSize: '0.8rem', fontFamily: FONT, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px' }}><Spinner size={11} /> Loading…</div>)}
        {domAsgn.error && (<div style={{ padding: '10px 14px', borderRadius: '8px', background: `${danger}10`, border: `1px solid ${danger}30` }}><span style={{ fontSize: '0.78rem', color: danger, fontFamily: FONT }}>{domAsgn.error}</span></div>)}
        {domAsgn.data && Array.isArray(domAsgn.data) && (
          <div style={card({ overflow: 'hidden' })}>
            <div style={{ padding: '8px 14px', borderBottom: `1px solid ${border}`, display: 'flex', gap: '8px', alignItems: 'center' }}><span style={{ fontSize: '0.7rem', fontWeight: '700', color: muted, letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT }}>Table Assignments</span><span style={{ fontSize: '0.7rem', color: muted }}>{domAsgn.data.length} tables</span></div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 140px 80px', padding: '6px 14px', fontSize: '0.6rem', fontWeight: '700', color: muted, letterSpacing: '0.06em', textTransform: 'uppercase', fontFamily: FONT, borderBottom: `1px solid ${border}` }}><span>Table</span><span>Domain</span><span>Confidence</span></div>
            <div style={{ maxHeight: '420px', overflowY: 'auto' }}>
              {domAsgn.data.map((row, i) => (
                <div key={row.table_fqn ?? i} style={{ display: 'grid', gridTemplateColumns: '1fr 140px 80px', padding: '7px 14px', fontSize: '0.78rem', borderBottom: `1px solid ${border}20`, background: i % 2 === 0 ? 'transparent' : `${bg}60` }}>
                  <span style={{ color: text, fontFamily: MONO, fontSize: '0.74rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{row.table_fqn ?? row.table_name}</span>
                  <span style={{ color: row.domain === 'Unknown' ? muted : accent, fontFamily: FONT }}>{row.domain ?? '—'}</span>
                  <span style={{ color: muted, fontFamily: MONO }}>{row.confidence != null ? `${Math.round(row.confidence * 100)}%` : '—'}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    )

    const entitiesTab = (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
        <p style={{ margin: 0, fontSize: '0.78rem', color: muted, fontFamily: FONT }}>Entities identify the primary business object each table represents.</p>
        {domAssigned === 0 && (<div style={{ ...card({ padding: '40px 24px' }), textAlign: 'center' }}><p style={{ margin: '0 0 6px', fontSize: '0.88rem', color: textSec, fontWeight: '500', fontFamily: FONT }}>Entities not generated yet</p><p style={{ margin: 0, fontSize: '0.78rem', color: muted, fontFamily: FONT }}>Generate domains first, then generate entities.</p></div>)}
        {domAssigned > 0 && entAssigned === 0 && !ent.generating && (<div style={{ ...card({ padding: '32px 24px' }), textAlign: 'center' }}><p style={{ margin: '0 0 6px', fontSize: '0.88rem', color: textSec, fontWeight: '500', fontFamily: FONT }}>No entities assigned yet</p><p style={{ margin: '0 0 16px', fontSize: '0.78rem', color: muted, fontFamily: FONT }}>Identify what each table represents — Customer, Order, Product, etc.</p><button onClick={() => handleGenerateEntities(dsSelectedSourceId)} disabled={ent.generating} style={{ ...btnGhost({ padding: '7px 16px', fontSize: '0.8rem' }), color: accent, borderColor: `${accent}50` }}>Generate Entities</button></div>)}
        {ent.generating && (<div style={{ ...card({ padding: '24px' }), textAlign: 'center', color: muted, fontSize: '0.82rem', fontFamily: FONT, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px' }}><Spinner size={12} /> Generating entity assignments…</div>)}
        {ent.error && (<div style={{ padding: '10px 14px', borderRadius: '8px', background: `${danger}10`, border: `1px solid ${danger}30` }}><span style={{ fontSize: '0.78rem', color: danger, fontFamily: FONT }}>{ent.error}</span></div>)}
        {entSummary && (
          <>
            <div style={{ ...card({ padding: '14px 18px' }), display: 'flex', gap: '24px', flexWrap: 'wrap', alignItems: 'center', justifyContent: 'space-between' }}>
              <div style={{ display: 'flex', gap: '24px', flexWrap: 'wrap' }}>
                {entSummary.total_entities    != null && (<div><div style={{ fontSize: '0.62rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '4px' }}>Entities</div><div style={{ fontSize: '1.3rem', fontWeight: '700', color: text, fontFamily: FONT, lineHeight: 1 }}>{entSummary.total_entities}</div></div>)}
                {entSummary.entities_assigned != null && (<div><div style={{ fontSize: '0.62rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '4px' }}>Assigned</div><div style={{ fontSize: '1.3rem', fontWeight: '700', color: text, fontFamily: FONT, lineHeight: 1 }}>{entSummary.entities_assigned}</div></div>)}
                {entSummary.entities_unknown  != null && entSummary.entities_unknown > 0 && (<div><div style={{ fontSize: '0.62rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '4px' }}>Unclassified</div><div style={{ fontSize: '1.3rem', fontWeight: '700', color: warn, fontFamily: FONT, lineHeight: 1 }}>{entSummary.entities_unknown}</div></div>)}
              </div>
              <button onClick={() => handleGenerateEntities(dsSelectedSourceId)} disabled={ent.generating} style={{ ...btnGhost({ padding: '6px 14px', fontSize: '0.78rem' }), color: textSec }}>{ent.generating ? 'Regenerating…' : 'Regenerate'}</button>
            </div>
            {entSummary.entity_counts && (
              <div style={card({ padding: '14px 18px' })}>
                <div style={{ fontSize: '0.7rem', fontWeight: '700', color: muted, letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '10px' }}>Entity Breakdown</div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '7px' }}>
                  {Object.entries(entSummary.entity_counts).sort((a, b) => b[1] - a[1]).map(([entity, count]) => {
                    const pct = Math.round((count / (entSummary.entities_assigned || 1)) * 100)
                    const isUnknown = entity === 'Unknown'
                    return (
                      <div key={entity} style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                        <span style={{ fontSize: '0.78rem', color: isUnknown ? muted : textSec, fontFamily: FONT, width: '150px', flexShrink: 0 }}>{entity}</span>
                        <div style={{ flex: 1, height: '6px', borderRadius: '3px', background: border }}><div style={{ height: '100%', borderRadius: '3px', background: isUnknown ? muted : success, width: `${pct}%` }} /></div>
                        <span style={{ fontSize: '0.74rem', color: muted, fontFamily: MONO, width: '28px', textAlign: 'right', flexShrink: 0 }}>{count}</span>
                      </div>
                    )
                  })}
                </div>
              </div>
            )}
          </>
        )}
        {entAssigned > 0 && !entAsgn.data && !entAsgn.loading && (<div style={{ ...card({ padding: '12px 16px' }), display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px' }}><span style={{ fontSize: '0.78rem', color: muted, fontFamily: FONT }}>Load per-table entity assignments for full detail.</span><button onClick={() => loadEntityAssignments(dsSelectedSourceId)} style={{ ...btnGhost({ padding: '5px 12px', fontSize: '0.76rem' }), color: accent, borderColor: `${accent}50` }}>Load Assignments</button></div>)}
        {entAsgn.loading && (<div style={{ ...card({ padding: '16px' }), textAlign: 'center', color: muted, fontSize: '0.8rem', fontFamily: FONT, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px' }}><Spinner size={11} /> Loading…</div>)}
        {entAsgn.error && (<div style={{ padding: '10px 14px', borderRadius: '8px', background: `${danger}10`, border: `1px solid ${danger}30` }}><span style={{ fontSize: '0.78rem', color: danger, fontFamily: FONT }}>{entAsgn.error}</span></div>)}
        {entAsgn.data && Array.isArray(entAsgn.data) && (
          <div style={card({ overflow: 'hidden' })}>
            <div style={{ padding: '8px 14px', borderBottom: `1px solid ${border}`, display: 'flex', gap: '8px', alignItems: 'center' }}><span style={{ fontSize: '0.7rem', fontWeight: '700', color: muted, letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT }}>Table Assignments</span><span style={{ fontSize: '0.7rem', color: muted }}>{entAsgn.data.length} tables</span></div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 160px 80px', padding: '6px 14px', fontSize: '0.6rem', fontWeight: '700', color: muted, letterSpacing: '0.06em', textTransform: 'uppercase', fontFamily: FONT, borderBottom: `1px solid ${border}` }}><span>Table</span><span>Entity</span><span>Confidence</span></div>
            <div style={{ maxHeight: '420px', overflowY: 'auto' }}>
              {entAsgn.data.map((row, i) => (
                <div key={row.table_fqn ?? i} style={{ display: 'grid', gridTemplateColumns: '1fr 160px 80px', padding: '7px 14px', fontSize: '0.78rem', borderBottom: `1px solid ${border}20`, background: i % 2 === 0 ? 'transparent' : `${bg}60` }}>
                  <span style={{ color: text, fontFamily: MONO, fontSize: '0.74rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{row.table_fqn ?? row.table_name}</span>
                  <span style={{ color: row.entity === 'Unknown' ? muted : success, fontFamily: FONT }}>{row.entity ?? '—'}</span>
                  <span style={{ color: muted, fontFamily: MONO }}>{row.confidence != null ? `${Math.round(row.confidence * 100)}%` : '—'}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    )

    const govBusy = !!(gs.loading || gs.analyzing)
    const rsBusy  = !!(rs.loading  || rs.generating)
    const ersBusy = !!(ers.loading || ers.generating)

    const govSection = ({ title, pendingCount, loadFn, busyFlag, analyzeBtn, analyzeResult, error, rules, approveFn, rejectFn, actState, entityMode = false, generateBtn }) => (
      <div style={card({ padding: '14px 16px' })}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px', flexWrap: 'wrap', gap: '8px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <span style={{ fontSize: '0.82rem', fontWeight: '600', color: text, fontFamily: FONT }}>{title}</span>
            {rules != null && pendingCount > 0 && <span style={{ padding: '1px 7px', borderRadius: '8px', fontSize: '0.62rem', fontWeight: '700', background: `${accent}20`, color: accent, border: `1px solid ${accent}40`, fontFamily: FONT }}>{pendingCount} pending</span>}
          </div>
          <div style={{ display: 'flex', gap: '6px' }}>
            <button onClick={loadFn} disabled={busyFlag} style={{ ...btnGhost({ padding: '4px 10px', fontSize: '0.74rem' }), color: !busyFlag ? textSec : `${muted}50`, cursor: !busyFlag ? 'pointer' : 'not-allowed' }}>{rules == null ? 'Load' : 'Refresh'}</button>
            {analyzeBtn}
            {generateBtn}
          </div>
        </div>
        {analyzeResult && (<div style={{ marginBottom: '10px', padding: '8px 12px', borderRadius: '6px', background: `${warn}0d`, border: `1px solid ${warn}30`, display: 'flex', gap: '16px', flexWrap: 'wrap' }}><span style={{ fontSize: '0.74rem', color: warn, fontFamily: FONT }}>Flagged: <strong>{analyzeResult.flagged_rules ?? 0}</strong></span><span style={{ fontSize: '0.74rem', color: accent, fontFamily: FONT }}>Improvement: <strong>+{analyzeResult.projected_accuracy_improvement ?? 0}%</strong></span></div>)}
        {error && <p style={{ margin: 0, fontSize: '0.75rem', color: danger, fontFamily: FONT }}>{error}</p>}
        {rules != null && (() => {
          const pending  = rules.filter(r => r.approval_status === 'PENDING')
          const approved = rules.filter(r => r.approval_status === 'APPROVED')
          const rejected = rules.filter(r => r.approval_status === 'REJECTED')
          return (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
              {pending.length  > 0 && (<div>{govSectionLabel('Pending',  pending.length,  accent)}<div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>{approvalRows(pending, approveFn, rejectFn, actState, entityMode)}</div></div>)}
              {approved.length > 0 && (<div>{govSectionLabel('Approved', approved.length, success)}<div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>{approvedRows(approved, entityMode)}</div></div>)}
              {rejected.length > 0 && (<div style={{ opacity: 0.5 }}>{govSectionLabel('Rejected', rejected.length)}<div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>{approvedRows(rejected, entityMode)}</div></div>)}
              {rules.length === 0 && <p style={{ margin: 0, fontSize: '0.75rem', color: muted, fontFamily: FONT }}>No items found. Use the buttons above to load or generate suggestions.</p>}
            </div>
          )
        })()}
      </div>
    )

    const governanceTab = (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
        {s6 === 'locked' && (<div style={{ ...card({ padding: '32px 24px' }), textAlign: 'center' }}><p style={{ margin: '0 0 6px', fontSize: '0.88rem', color: textSec, fontWeight: '500', fontFamily: FONT }}>Governance not available yet</p><p style={{ margin: 0, fontSize: '0.78rem', color: muted, fontFamily: FONT }}>Complete the pipeline through Entities before governance rules can be generated.</p></div>)}
        {s6 !== 'locked' && (
          <>
            {govSection({ title: 'Domain Refinements', pendingCount: pendingRefs, loadFn: () => loadRefinements(dsSelectedSourceId), busyFlag: govBusy, analyzeBtn: <button onClick={() => handleAnalyzeRefinements(dsSelectedSourceId)} disabled={govBusy} style={{ ...btnGhost({ padding: '4px 10px', fontSize: '0.74rem' }), color: !govBusy ? warn : `${muted}50`, borderColor: !govBusy ? `${warn}55` : `${border}50`, cursor: !govBusy ? 'pointer' : 'not-allowed' }}>{gs.analyzing ? 'Analyzing…' : 'Analyze'}</button>, analyzeResult: gs.analyzeResult, error: gs.error, rules: gs.refinements, approveFn: id => handleApproveRefinement(dsSelectedSourceId, id), rejectFn: id => handleRejectRefinement(dsSelectedSourceId, id), actState: refinementActionState })}
            {govSection({ title: 'Domain Rules',       pendingCount: pendingRules,    loadFn: () => loadDomainRules(dsSelectedSourceId), busyFlag: rsBusy,  generateBtn: <button onClick={() => handleGenerateSuggestions(dsSelectedSourceId)} disabled={rsBusy} style={{ ...btnGhost({ padding: '4px 10px', fontSize: '0.74rem' }), color: !rsBusy ? accent : `${muted}50`, borderColor: !rsBusy ? `${accent}55` : `${border}50`, cursor: !rsBusy ? 'pointer' : 'not-allowed' }}>{rs.generating ? 'Generating…' : 'Generate Suggestions'}</button>, error: rs.error, rules: rs.rules, approveFn: id => handleApproveRule(dsSelectedSourceId, id), rejectFn: id => handleRejectRule(dsSelectedSourceId, id), actState: ruleActionState })}
            {govSection({ title: 'Entity Rules',       pendingCount: pendingEntRules, loadFn: () => loadEntityRules(dsSelectedSourceId), busyFlag: ersBusy, generateBtn: <button onClick={() => handleGenerateEntitySuggestions(dsSelectedSourceId)} disabled={ersBusy} style={{ ...btnGhost({ padding: '4px 10px', fontSize: '0.74rem' }), color: !ersBusy ? accent : `${muted}50`, borderColor: !ersBusy ? `${accent}55` : `${border}50`, cursor: !ersBusy ? 'pointer' : 'not-allowed' }}>{ers.generating ? 'Generating…' : 'Generate Suggestions'}</button>, error: ers.error, rules: ers.rules, approveFn: id => handleApproveEntityRule(dsSelectedSourceId, id), rejectFn: id => handleRejectEntityRule(dsSelectedSourceId, id), actState: entityRuleActionState, entityMode: true })}
          </>
        )}
      </div>
    )

    const lineageTab = (
      <div style={{ ...card({ padding: '52px 24px' }), textAlign: 'center' }}>
        <svg width="38" height="38" viewBox="0 0 24 24" fill="none" stroke={muted} strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" style={{ display: 'block', margin: '0 auto 14px' }}>
          <circle cx="5" cy="12" r="2"/><circle cx="12" cy="5" r="2"/><circle cx="19" cy="12" r="2"/><circle cx="12" cy="19" r="2"/>
          <line x1="7" y1="12" x2="10" y2="12"/><line x1="12" y1="7" x2="12" y2="10"/><line x1="14" y1="12" x2="17" y2="12"/><line x1="12" y1="14" x2="12" y2="17"/>
        </svg>
        <p style={{ margin: '0 0 6px', fontSize: '0.9rem', color: textSec, fontWeight: '600', fontFamily: FONT }}>Lineage not available yet</p>
        <p style={{ margin: 0, fontSize: '0.78rem', color: muted, fontFamily: FONT, lineHeight: 1.6, maxWidth: '380px', marginLeft: 'auto', marginRight: 'auto' }}>
          Lineage will show upstream and downstream relationships — which processes write to this source and which consume from it. This requires lineage capture to be configured on your data platform.
        </p>
      </div>
    )

    const runsTab = (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
        {src?.metadata_job_id && (
          <div style={card({ padding: '14px 16px' })}>
            <div style={{ fontSize: '0.7rem', fontWeight: '700', color: muted, letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '10px' }}>Current Metadata Job</div>
            {js.loading && <p style={{ margin: 0, fontSize: '0.78rem', color: muted, fontFamily: FONT }}>Loading…</p>}
            {job && (
              <div style={{ display: 'flex', gap: '20px', flexWrap: 'wrap', alignItems: 'center' }}>
                <div><div style={{ fontSize: '0.62rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '4px' }}>Status</div><span style={{ padding: '2px 9px', borderRadius: '8px', fontSize: '0.72rem', fontWeight: '700', background: jobRunning ? `${accent}18` : job.status === 'COMPLETE' ? `${success}18` : `${danger}18`, color: jobRunning ? accent : job.status === 'COMPLETE' ? success : danger, border: `1px solid ${jobRunning ? accent : job.status === 'COMPLETE' ? success : danger}40`, fontFamily: FONT, display: 'inline-flex', alignItems: 'center', gap: '5px' }}>{jobRunning && <Spinner size={9} />}{job.status}</span></div>
                {job.progress_message && (<div><div style={{ fontSize: '0.62rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '4px' }}>Progress</div><div style={{ fontSize: '0.78rem', color: textSec, fontFamily: FONT }}>{job.progress_message}</div></div>)}
                {job.error_message    && (<div><div style={{ fontSize: '0.62rem', color: danger, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '4px' }}>Error</div><div style={{ fontSize: '0.78rem', color: danger, fontFamily: FONT }}>{job.error_message}</div></div>)}
                <div style={{ marginLeft: 'auto', display: 'flex', gap: '8px' }}>
                  {jobRunning && <button onClick={() => loadJobStatus(dsSelectedSourceId, src.metadata_job_id)} disabled={js.loading} style={{ ...btnGhost({ padding: '5px 12px', fontSize: '0.76rem' }), color: textSec }}>Refresh</button>}
                  <button onClick={() => src && handleDiscoverAndProfile(src)} disabled={js.running || discSt.loading} style={{ ...btnGhost({ padding: '5px 12px', fontSize: '0.76rem' }), color: accent, borderColor: `${accent}50` }}>{js.running ? 'Running…' : 'Re-run'}</button>
                </div>
              </div>
            )}
          </div>
        )}
        {profHist.loading && (<div style={{ ...card({ padding: '16px' }), textAlign: 'center', color: muted, fontSize: '0.8rem', fontFamily: FONT, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px' }}><Spinner size={11} /> Loading run history…</div>)}
        {profHist.error   && (<div style={{ padding: '10px 14px', borderRadius: '8px', background: `${danger}10`, border: `1px solid ${danger}30` }}><span style={{ fontSize: '0.78rem', color: danger, fontFamily: FONT }}>{profHist.error}</span></div>)}
        {!profHist.data && !profHist.loading && hasSchema && (<div style={{ ...card({ padding: '12px 16px' }), display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px' }}><span style={{ fontSize: '0.78rem', color: muted, fontFamily: FONT }}>Load profiling run history for this source.</span><button onClick={() => loadProfileHistory(dsSelectedSourceId)} style={{ ...btnGhost({ padding: '5px 12px', fontSize: '0.76rem' }), color: accent, borderColor: `${accent}50` }}>Load History</button></div>)}
        {profHist.data && Array.isArray(profHist.data) && profHist.data.length === 0 && (<div style={{ ...card({ padding: '32px 24px' }), textAlign: 'center' }}><p style={{ margin: 0, fontSize: '0.82rem', color: muted, fontFamily: FONT }}>No profiling run history found for this source.</p></div>)}
        {profHist.data && Array.isArray(profHist.data) && profHist.data.length > 0 && (
          <div style={card({ overflow: 'hidden' })}>
            <div style={{ padding: '8px 14px', borderBottom: `1px solid ${border}` }}><span style={{ fontSize: '0.7rem', fontWeight: '700', color: muted, letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT }}>Profiling History</span></div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 100px 120px 110px', padding: '6px 14px', fontSize: '0.6rem', fontWeight: '700', color: muted, letterSpacing: '0.06em', textTransform: 'uppercase', fontFamily: FONT, borderBottom: `1px solid ${border}` }}><span>Run ID</span><span>Status</span><span>Tables</span><span>Started</span></div>
            <div style={{ maxHeight: '360px', overflowY: 'auto' }}>
              {profHist.data.map((run, i) => {
                const runColor = run.status === 'COMPLETE' ? success : run.status === 'RUNNING' ? accent : danger
                return (
                  <div key={run.id ?? i} style={{ display: 'grid', gridTemplateColumns: '1fr 100px 120px 110px', padding: '8px 14px', fontSize: '0.78rem', borderBottom: `1px solid ${border}20`, background: i % 2 === 0 ? 'transparent' : `${bg}60` }}>
                    <span style={{ color: muted, fontFamily: MONO, fontSize: '0.72rem' }}>{run.id ?? '—'}</span>
                    <span style={{ color: runColor, fontFamily: FONT, fontWeight: '600' }}>{run.status ?? '—'}</span>
                    <span style={{ color: textSec }}>{run.tables_profiled != null ? `${run.tables_profiled} / ${run.tables_total ?? '?'} assets` : '—'}</span>
                    <span style={{ color: muted, fontSize: '0.72rem' }}>{run.created_at ? fmtRelative(run.created_at) : '—'}</span>
                  </div>
                )
              })}
            </div>
          </div>
        )}
        {!hasSchema && !src?.metadata_job_id && (<div style={{ ...card({ padding: '40px 24px' }), textAlign: 'center' }}><p style={{ margin: '0 0 6px', fontSize: '0.88rem', color: textSec, fontWeight: '500', fontFamily: FONT }}>No runs yet</p><p style={{ margin: 0, fontSize: '0.78rem', color: muted, fontFamily: FONT }}>Run Discover & Profile to start building a run history.</p></div>)}
      </div>
    )

    const TAB_CONTENT = { overview: overviewTab, schema: schemaTab, profile: profileTab, domains: domainsTab, entities: entitiesTab, governance: governanceTab, lineage: lineageTab, runs: runsTab }

    return (
      <div style={{ fontFamily: FONT, color: text }}>

        {/* ── Breadcrumb ── */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '20px', fontSize: '0.74rem', fontFamily: FONT }}>
          <button onClick={() => setDsSelectedSourceId(null)} style={{ background: 'none', border: 'none', color: muted, cursor: 'pointer', fontFamily: FONT, fontSize: '0.74rem', padding: 0, display: 'flex', alignItems: 'center', gap: '5px' }} onMouseEnter={e => { e.currentTarget.style.color = accent }} onMouseLeave={e => { e.currentTarget.style.color = muted }}>
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>
            Data Sources
          </button>
          <span style={{ color: border }}>/</span>
          <span style={{ color: textSec }}>{src?.display_name ?? `Source #${dsSelectedSourceId}`}</span>
        </div>

        {/* ── Workspace header ── */}
        <div style={{ marginBottom: '20px' }}>
          <h2 style={{ margin: '0 0 8px', fontSize: '1.4rem', fontWeight: '700', color: text, letterSpacing: '-0.4px' }}>{src?.display_name ?? `Source #${dsSelectedSourceId}`}</h2>
          <div style={{ display: 'flex', gap: '6px', alignItems: 'center', flexWrap: 'wrap' }}>
            <span style={{ display: 'inline-block', padding: '2px 9px', borderRadius: '10px', fontSize: '0.65rem', fontWeight: '600', letterSpacing: '0.04em', background: `${accent}15`, color: accent, border: `1px solid ${accent}30` }}>{stLabel}</span>
            <span style={{ display: 'inline-block', padding: '2px 9px', borderRadius: '10px', fontSize: '0.65rem', fontWeight: '700', letterSpacing: '0.05em', textTransform: 'uppercase', background: `${smBadge.color}20`, color: smBadge.color, border: `1px solid ${smBadge.color}50` }}>{smBadge.label}</span>
            {matBadge('Schema',     s2 === 'done' ? 'done' : s2 === 'running' ? 'running' : 'none')}
            {matBadge('Profile',    profComplete ? 'done' : profSnap ? 'partial' : 'none')}
            {dictCount > 0  && matBadge(dictPct === 100 ? 'Dictionary' : 'Needs Review', dictPct === 100 ? 'done' : 'partial')}
            {domAssigned > 0 && matBadge('Domains',   s4 === 'done' ? 'done' : 'partial')}
            {entAssigned > 0 && matBadge('Entities',  s5 === 'done' ? 'done' : 'partial')}
          </div>
        </div>

        {/* ── Tab bar ── */}
        <div style={{ display: 'flex', marginBottom: '20px', borderBottom: `1px solid ${border}` }}>
          {WORKSPACE_TABS.map(t => {
            const isActive = activeTab === t.id
            return (
              <button key={t.id} onClick={() => setDsActiveTab && setDsActiveTab(t.id)} style={{ background: 'none', border: 'none', borderBottom: isActive ? `2px solid ${accent}` : '2px solid transparent', color: isActive ? accent : muted, fontFamily: FONT, fontSize: '0.8rem', fontWeight: isActive ? '600' : '400', padding: '8px 16px', cursor: 'pointer', marginBottom: '-1px', letterSpacing: '0.01em', whiteSpace: 'nowrap' }}>
                {t.label}
              </button>
            )
          })}
        </div>

        {/* ── Tab content ── */}
        {activeTab === 'dictionary'
          ? <DictionaryReview C={C} token={token} sourceId={dsSelectedSourceId} embedded hideSourceSelector />
          : (TAB_CONTENT[activeTab] ?? null)
        }

        <Toast toast={toast} />
        <style>{`@keyframes dsm-spin { to { transform: rotate(360deg); } }`}</style>
      </div>
    )
  }

  return (
    <div style={{ fontFamily: FONT, color: text }}>

      {/* ── Header ──────────────────────────────────────────────────────────── */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: '20px', gap: '16px' }}>
        <div>
          <h2 style={{ margin: '0 0 4px', fontSize: '1.4rem', fontWeight: '700', color: text, letterSpacing: '-0.4px' }}>Data Sources</h2>
          <p style={{ margin: 0, color: muted, fontSize: '0.75rem' }}>
            Connect, scan, and govern enterprise data sources for metadata intelligence.
          </p>
        </div>
        <button
          onClick={() => { setShowForm(f => !f); setFormError(null) }}
          style={{ ...btnMain, display: 'flex', alignItems: 'center', gap: '7px', flexShrink: 0 }}
        >
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            {showForm
              ? <><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></>
              : <><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></>}
          </svg>
          {showForm ? 'Cancel' : 'Add Connection'}
        </button>
      </div>

      {/* ── Add connection form ──────────────────────────────────────────────── */}
      {showForm && (
        <div style={{ ...card(), marginBottom: '20px' }}>
          <h3 style={{ margin: '0 0 16px', fontSize: '0.82rem', fontWeight: '600', color: textSec, letterSpacing: '0.01em', fontFamily: FONT }}>
            New Connection
          </h3>

          {/* Source type picker */}
          <div style={{ display: 'flex', gap: '8px', marginBottom: '20px' }}>
            {SOURCE_TYPES.map(st => {
              const active = form.source_type === st.value
              return (
                <button
                  key={st.value}
                  type="button"
                  disabled={!st.available}
                  onClick={() => st.available && handleTypeChange(st.value)}
                  style={{
                    flex: 1, padding: '10px 8px', borderRadius: '10px',
                    border: `1px solid ${active ? accent : border}`,
                    background: active ? accentSoft : bg,
                    color: st.available ? (active ? accent : text) : muted,
                    cursor: st.available ? 'pointer' : 'not-allowed',
                    fontFamily: FONT, fontSize: '0.82rem', fontWeight: active ? '600' : '400',
                    display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '4px',
                  }}
                >
                  <span>{st.label}</span>
                  {!st.available && (
                    <span style={{ fontSize: '0.6rem', color: muted, letterSpacing: '0.06em', textTransform: 'uppercase' }}>
                      Coming Soon
                    </span>
                  )}
                </button>
              )
            })}
          </div>

          <form onSubmit={handleSave} autoComplete="off">
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '14px', marginBottom: '14px' }}>

              <div style={{ gridColumn: '1 / -1' }}>
                <label style={lbl}>Display Name</label>
                <input style={inp()} value={form.display_name} onChange={e => handleField('display_name', e.target.value)} placeholder="e.g. Production Analytics DB" />
              </div>

              <div>
                <label style={lbl}>Host / Server</label>
                <input style={inp()} value={form.host} onChange={e => handleField('host', e.target.value)} placeholder="server.domain.com" />
              </div>

              <div>
                <label style={lbl}>Port</label>
                <input style={inp()} type="number" value={form.port} onChange={e => handleField('port', e.target.value)} min="1" max="65535" />
              </div>

              <div style={{ gridColumn: '1 / -1' }}>
                <label style={lbl}>Database Name</label>
                <input style={inp()} value={form.database} onChange={e => handleField('database', e.target.value)} placeholder="database_name" />
              </div>

              <div style={{ gridColumn: '1 / -1' }}>
                <label style={lbl}>Authentication</label>
                <select style={{ ...inp(), cursor: 'pointer' }} value={form.auth_type} onChange={e => handleField('auth_type', e.target.value)}>
                  <option value="sql">SQL Authentication</option>
                  <option value="windows">Windows Authentication</option>
                </select>
              </div>

              {form.auth_type === 'sql' && (
                <>
                  <div>
                    <label style={lbl}>Username</label>
                    <input style={inp()} value={form.username} onChange={e => handleField('username', e.target.value)} autoComplete="off" />
                  </div>
                  <div>
                    <label style={lbl}>Password</label>
                    <input style={inp()} type="password" value={form.password} onChange={e => handleField('password', e.target.value)} autoComplete="new-password" />
                  </div>
                </>
              )}
            </div>

            {/* TLS options */}
            <div style={{ display: 'flex', gap: '24px', marginBottom: '16px', flexWrap: 'wrap' }}>
              {[
                { key: 'encrypt_connection',      label: 'Encrypt connection'       },
                { key: 'trust_server_certificate', label: 'Trust server certificate' },
              ].map(({ key, label }) => (
                <label key={key} style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer', fontSize: '0.82rem', color: textSec, fontFamily: FONT }}>
                  <input
                    type="checkbox"
                    checked={form[key]}
                    onChange={e => handleField(key, e.target.checked)}
                    style={{ accentColor: accent }}
                  />
                  {label}
                </label>
              ))}
            </div>

            {formError && (
              <div style={{ marginBottom: '14px', padding: '10px 14px', background: `${danger}15`, border: `1px solid ${danger}40`, borderRadius: '8px', color: danger, fontSize: '0.82rem', fontFamily: FONT }}>
                {formError}
              </div>
            )}

            <div style={{ display: 'flex', gap: '10px', justifyContent: 'flex-end' }}>
              <button type="button" onClick={() => { setShowForm(false); setForm(INITIAL_FORM); setFormError(null) }} style={btnGhost()}>
                Cancel
              </button>
              <button type="submit" disabled={saving} style={{ ...btnMain, opacity: saving ? 0.65 : 1 }}>
                {saving ? 'Saving…' : 'Save Connection'}
              </button>
            </div>
          </form>
        </div>
      )}

      {/* ── Source portfolio ─────────────────────────────────────────── */}
      {loading ? (
        <div style={{ textAlign: 'center', padding: '64px 0', color: muted, fontSize: '0.82rem', fontFamily: FONT, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '10px' }}>
          <Spinner size={14} /> Loading data sources…
        </div>
      ) : sources.length === 0 ? (
        <div style={{ ...card({ padding: '56px 24px' }), textAlign: 'center' }}>
          <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke={muted} strokeWidth="1.1" strokeLinecap="round" strokeLinejoin="round" style={{ display: 'block', margin: '0 auto 16px' }}>
            <ellipse cx="12" cy="5" rx="9" ry="3"/>
            <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/>
            <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>
          </svg>
          <p style={{ margin: '0 0 8px', fontSize: '1rem', color: textSec, fontWeight: '600', fontFamily: FONT }}>No data sources connected yet</p>
          <p style={{ margin: '0 0 20px', fontSize: '0.82rem', color: muted, fontFamily: FONT, lineHeight: 1.6, maxWidth: '420px', marginLeft: 'auto', marginRight: 'auto' }}>
            Connect a database to discover its schema, generate a business dictionary, assign domains and entities, and surface governance rules — all in one place.
          </p>
          <button onClick={() => { setShowForm(true); setFormError(null) }} style={{ ...btnMain, display: 'inline-flex', alignItems: 'center', gap: '7px' }}>
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
            Add Connection
          </button>
        </div>
      ) : (() => {
        const connectedSrcs = sources.filter(s => s.source_status === 'ACTIVE').length
        const scannedSrcs   = sources.filter(s => s.last_snapshot_id != null).length
        const dictSrcs      = sources.filter(s => (dictState[s.id]?.tables?.length ?? 0) > 0).length
        const domainSrcs    = sources.filter(s => (domainState[s.id]?.summary?.tables_assigned ?? 0) > 0).length
        const entitySrcs    = sources.filter(s => (entityState[s.id]?.summary?.entities_assigned ?? 0) > 0).length
        const n             = sources.length

        // Loading flags: true while async summaries are still in-flight for any scanned source
        const scannedList   = sources.filter(s => s.last_snapshot_id != null)
        const dictLoading   = scannedList.some(s => dictState[s.id]?.loading === true)
        const domainLoading = scannedList.some(s => domainState[s.id]?.loading === true)
        const entityLoading = scannedList.some(s => entityState[s.id]?.loading === true)

        // Table column template and search-filtered source list
        const TABLE_COL = '1fr 100px 132px 110px 80px 90px 130px'
        const filteredSources = landingSearch.trim()
          ? sources.filter(src => {
              const q = landingSearch.toLowerCase()
              const typeLabel = SOURCE_TYPES.find(t => t.value === src.source_type)?.label ?? ''
              return (
                (src.display_name ?? '').toLowerCase().includes(q) ||
                (src.source_type ?? '').toLowerCase().includes(q) ||
                typeLabel.toLowerCase().includes(q) ||
                (src.config_summary ?? '').toLowerCase().includes(q)
              )
            })
          : sources

        return (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>

            {/* ── Metric cards ── */}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(6, 1fr)', gap: '10px' }}>
              {[
                { label: 'Total Sources', value: n,             sub: 'registered',         col: text,    top: border,          loading: false        },
                { label: 'Connected',     value: connectedSrcs, sub: 'active connections',  col: success, top: `${success}70`,  loading: false        },
                { label: 'Scanned',       value: scannedSrcs,   sub: 'schema discovered',   col: text,    top: `${accent}70`,   loading: false        },
                { label: 'Dictionaries',  value: dictSrcs,      sub: 'business metadata',   col: text,    top: `${accent}70`,   loading: dictLoading  },
                { label: 'Domains',       value: domainSrcs,    sub: 'classified',          col: text,    top: `${accent}70`,   loading: domainLoading },
                { label: 'Entities',      value: entitySrcs,    sub: 'mapped',              col: text,    top: `${accent}70`,   loading: entityLoading },
              ].map(m => (
                <div key={m.label} style={{ background: surface, border: `1px solid ${border}`, borderTop: `2px solid ${m.top}`, borderRadius: '10px', padding: '14px 16px', display: 'flex', flexDirection: 'column', gap: '2px' }}>
                  <div style={{ fontSize: '2rem', fontWeight: '800', color: m.col, fontFamily: FONT, lineHeight: 1, letterSpacing: '-1px' }}>
                    {m.loading
                      ? <span style={{ fontSize: '1rem', color: muted, fontWeight: '500', opacity: 0.5 }}>…</span>
                      : m.value}
                  </div>
                  <div style={{ fontSize: '0.78rem', fontWeight: '600', color: textSec, fontFamily: FONT }}>{m.label}</div>
                  <div style={{ fontSize: '0.64rem', color: muted, fontFamily: FONT }}>{m.sub}</div>
                </div>
              ))}
            </div>



            {/* ── Data Sources enterprise table ── */}
            <div style={{ background: surface, border: `1px solid ${border}`, borderRadius: '12px', overflow: 'hidden' }}>

              {/* Card header: title + subtitle + search */}
              <div style={{ padding: '16px 20px 14px' }}>
                <h3 style={{ margin: '0 0 2px', fontSize: '0.92rem', fontWeight: '700', color: text, fontFamily: FONT }}>Source Inventory</h3>
                <p style={{ margin: '0 0 12px', fontSize: '0.73rem', color: muted, fontFamily: FONT }}>Monitor and manage all connected data sources.</p>
                <div style={{ position: 'relative' }}>
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke={muted} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ position: 'absolute', left: '10px', top: '50%', transform: 'translateY(-50%)', pointerEvents: 'none' }}>
                    <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
                  </svg>
                  <input
                    value={landingSearch}
                    onChange={e => setLandingSearch(e.target.value)}
                    placeholder="Search data sources…"
                    style={{ width: '100%', boxSizing: 'border-box', background: bg, border: `1px solid ${border}`, borderRadius: '8px', color: text, fontSize: '0.8rem', padding: '7px 10px 7px 30px', outline: 'none', fontFamily: FONT }}
                  />
                </div>
              </div>

              {/* Empty state or table */}
              {filteredSources.length === 0 ? (
                <div style={{ padding: '36px 20px', textAlign: 'center', color: muted, fontSize: '0.8rem', fontFamily: FONT, borderTop: `1px solid ${border}` }}>
                  No sources match the search.
                </div>
              ) : (
                <>
                  {/* Column headers */}
                  <div style={{ display: 'grid', gridTemplateColumns: TABLE_COL, padding: '8px 20px', borderTop: `1px solid ${border}`, borderBottom: `1px solid ${border}`, background: bg, gap: '8px', alignItems: 'center' }}>
                    {[
                      { label: 'Name',      align: 'left'  },
                      { label: 'Type',      align: 'left'  },
                      { label: 'Status',    align: 'left'  },
                      { label: 'Last Activity', align: 'left'  },
                      { label: 'Tables',    align: 'right' },
                      { label: 'Columns',   align: 'right' },
                      { label: 'Actions',   align: 'right' },
                    ].map(h => (
                      <span key={h.label} style={{ fontSize: '0.63rem', fontWeight: '700', color: textSec, letterSpacing: '0.06em', textTransform: 'uppercase', fontFamily: FONT, textAlign: h.align }}>{h.label}</span>
                    ))}
                  </div>

                  {/* Source rows */}
                  {filteredSources.map((src, rowIdx) => {
                    const ts     = testState[src.id]     ?? {}
                    const discSt = discoverState[src.id] ?? {}
                    const js     = jobState[src.id]      ?? {}
                    const delSt  = deleteState[src.id]   ?? {}
                    const sc     = schemaState[src.id]   ?? {}

                    const lastTest  = ts.status ?? src.last_test_status
                    const isActive  = src.source_status === 'ACTIVE'
                    const stLabel   = SOURCE_TYPES.find(t => t.value === src.source_type)?.label ?? src.source_type
                    const scanBusy  = discSt.loading || js.running
                    const menuOpen  = srcMenu[src.id] ?? false
                    const notLast   = rowIdx < filteredSources.length - 1

                    const tableCount = sc.data?.schemas != null
                      ? sc.data.schemas.reduce((acc, s) => acc + (s.tables?.filter(t => t.table_type === 'TABLE').length ?? 0), 0)
                      : null
                    const colCount = sc.data?.schemas != null
                      ? sc.data.schemas.reduce((acc, s) => acc + (s.tables?.reduce((a2, t) => a2 + (t.columns?.length ?? 0), 0) ?? 0), 0)
                      : null

                    const connected = lastTest === 'success' || isActive
                    const failed    = lastTest === 'failed'
                    const stCol     = connected ? success : failed ? danger : muted
                    const stLbl     = connected ? '✓ Connected' : failed ? '✗ Failed' : '— Unknown'

                    const evenRow = rowIdx % 2 === 0

                    return (
                      <div key={src.id} style={{ borderBottom: notLast ? `1px solid ${border}20` : 'none' }}>

                        {/* Main row */}
                        <div
                          style={{ display: 'grid', gridTemplateColumns: TABLE_COL, padding: '15px 20px', gap: '8px', alignItems: 'center', background: evenRow ? 'transparent' : `${bg}50`, transition: 'background 0.1s' }}
                          onMouseEnter={e => { e.currentTarget.style.background = `${accent}08` }}
                          onMouseLeave={e => { e.currentTarget.style.background = evenRow ? 'transparent' : `${bg}50` }}
                        >
                          {/* Name + connection summary */}
                          <div style={{ minWidth: 0 }}>
                            <div
                              onClick={() => openSource && openSource(src.id, 'overview')}
                              style={{ fontSize: '0.86rem', fontWeight: '600', color: openSource ? accent : text, fontFamily: FONT, cursor: openSource ? 'pointer' : 'default', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', letterSpacing: '-0.1px' }}
                            >
                              {src.display_name}
                            </div>
                            <div style={{ fontSize: '0.67rem', color: muted, fontFamily: MONO, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', marginTop: '1px' }}>
                              {src.config_summary ?? '—'}
                            </div>
                          </div>

                          {/* Type */}
                          <div style={{ fontSize: '0.76rem', color: textSec, fontFamily: FONT, whiteSpace: 'nowrap' }}>{stLabel}</div>

                          {/* Status badge */}
                          <div>
                            <span style={{ display: 'inline-flex', alignItems: 'center', padding: '2px 9px', borderRadius: '8px', fontSize: '0.67rem', fontWeight: '600', background: `${stCol}15`, color: stCol, border: `1px solid ${stCol}35`, fontFamily: FONT, whiteSpace: 'nowrap' }}>
                              {stLbl}
                            </span>
                          </div>

                          {/* Last Activity — prefer snapshot/scan ts when available, fall back to last_tested_at */}
                          <div style={{ fontSize: '0.72rem', color: muted, fontFamily: FONT, whiteSpace: 'nowrap' }}>
                            {(() => {
                              const scanTs = profileState[src.id]?.data?.snapshot?.created_at ?? src.last_snapshot_at ?? null
                              const ts_val = scanTs ?? src.last_tested_at ?? null
                              return ts_val ? fmtRelative(ts_val) : '—'
                            })()}
                          </div>

                          {/* Tables count */}
                          <div style={{ textAlign: 'right' }}>
                            {sc.loading ? (
                              <Spinner size={10} />
                            ) : tableCount != null ? (
                              <span style={{ fontSize: '0.82rem', fontWeight: '600', color: text, fontFamily: FONT }}>{tableCount.toLocaleString()}</span>
                            ) : (
                              <span style={{ fontSize: '0.82rem', color: muted, fontFamily: FONT }}>—</span>
                            )}
                          </div>

                          {/* Columns count */}
                          <div style={{ textAlign: 'right' }}>
                            {sc.loading ? (
                              <Spinner size={10} />
                            ) : colCount != null ? (
                              <span style={{ fontSize: '0.82rem', fontWeight: '600', color: text, fontFamily: FONT }}>{colCount.toLocaleString()}</span>
                            ) : (
                              <span style={{ fontSize: '0.82rem', color: muted, fontFamily: FONT }}>—</span>
                            )}
                          </div>

                          {/* Actions */}
                          <div style={{ display: 'flex', gap: '5px', alignItems: 'center', justifyContent: 'flex-end' }}>
                            {!delSt.confirming ? (
                              <>
                                {openSource && (
                                  <button
                                    onClick={() => openSource(src.id, 'overview')}
                                    title="Explore source"
                                    style={{ background: accent, color: '#fff', border: 'none', borderRadius: '7px', padding: '6px 12px', fontSize: '0.76rem', fontWeight: '600', cursor: 'pointer', fontFamily: FONT, display: 'inline-flex', alignItems: 'center', gap: '4px' }}
                                  >
                                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12h14"/><path d="m12 5 7 7-7 7"/></svg>
                                    Explore
                                  </button>
                                )}
                                <button
                                  onClick={() => setSrcMenu(s => ({ ...s, [src.id]: !s[src.id] }))}
                                  style={{ ...btnGhost({ padding: '5px 8px', fontSize: '1rem' }), color: menuOpen ? accent : muted, borderColor: menuOpen ? `${accent}50` : border, lineHeight: 1 }}
                                  title="More actions"
                                >
                                  ⋮
                                </button>
                              </>
                            ) : (
                              <>
                                <button onClick={() => handleDeleteConfirm(src.id)} style={{ background: danger, color: '#fff', border: 'none', borderRadius: '7px', padding: '5px 11px', fontSize: '0.75rem', fontWeight: '600', cursor: 'pointer', fontFamily: FONT }}>Confirm?</button>
                                <button onClick={() => handleDeleteCancel(src.id)} style={btnGhost({ padding: '5px 8px', fontSize: '0.75rem' })}>Cancel</button>
                              </>
                            )}
                          </div>
                        </div>

                        {/* Expandable ⋮ secondary actions */}
                        {menuOpen && !delSt.confirming && (
                          <div style={{ display: 'flex', gap: '6px', alignItems: 'center', padding: '9px 20px 11px 22px', background: `${accent}05`, borderTop: `1px solid ${border}30`, borderLeft: `3px solid ${accent}45`, flexWrap: 'wrap' }}>
                            <span style={{ fontSize: '0.58rem', fontWeight: '700', color: muted, letterSpacing: '0.08em', textTransform: 'uppercase', fontFamily: FONT, marginRight: '6px', flexShrink: 0 }}>Actions</span>
                            <button
                              onClick={() => { if (!ts.loading) { handleTest(src.id); setSrcMenu(s => ({ ...s, [src.id]: false })) } }}
                              disabled={ts.loading}
                              style={{ ...btnGhost({ padding: '5px 11px', fontSize: '0.74rem' }), color: ts.loading ? `${muted}50` : textSec, cursor: ts.loading ? 'not-allowed' : 'pointer', display: 'inline-flex', alignItems: 'center', gap: '4px' }}
                            >
                              {ts.loading && <Spinner size={8} />}{ts.loading ? 'Testing…' : 'Test Connection'}
                            </button>
                            <button
                              onClick={() => { if (!scanBusy && src.source_status === 'ACTIVE') { handleDiscoverAndProfile(src); setSrcMenu(s => ({ ...s, [src.id]: false })) } }}
                              disabled={scanBusy || src.source_status !== 'ACTIVE'}
                              style={{ ...btnGhost({ padding: '5px 11px', fontSize: '0.74rem' }), color: scanBusy || src.source_status !== 'ACTIVE' ? `${muted}50` : textSec, cursor: scanBusy || src.source_status !== 'ACTIVE' ? 'not-allowed' : 'pointer', display: 'inline-flex', alignItems: 'center', gap: '4px' }}
                            >
                              {scanBusy && <Spinner size={8} />}{scanBusy ? 'Scanning…' : 'Scan / Profile'}
                            </button>
                            <button
                              onClick={() => { handleDeleteClick(src.id); setSrcMenu(s => ({ ...s, [src.id]: false })) }}
                              style={{ ...btnGhost({ padding: '5px 11px', fontSize: '0.74rem' }), color: `${danger}99`, borderColor: `${danger}30` }}
                            >
                              Remove
                            </button>
                            {ts.status && !ts.loading && (
                              <span style={{ marginLeft: '4px', fontSize: '0.74rem', fontWeight: '600', color: ts.status === 'success' ? success : danger, fontFamily: FONT }}>
                                {ts.status === 'success'
                                  ? `✓ Connected${ts.latency_ms != null ? ` · ${ts.latency_ms}ms` : ''}`
                                  : `✗ ${ts.message ?? 'Failed'}`}
                              </span>
                            )}
                          </div>
                        )}
                      </div>
                    )
                  })}
                </>
              )}
            </div>
          </div>
        )
      })()}

      <Toast toast={toast} />

      <style>{`@keyframes dsm-spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  )
}
