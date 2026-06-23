import { useState, useEffect } from 'react'
import { analyzeDomainRefinements, approveDomainRefinement, approveDomainRule, approveEntityRule, createDataSource, deleteDataSource, discoverDataSourceSchema, generateDictionaryForSource, generateDomainRuleSuggestions, generateDomains, generateEntities, generateEntityRuleSuggestions, getDomainRefinements, getDomainRules, getDomainSummary, getDataSourceSchema, getEntityRules, getEntitySummary, getMetadataJob, getProfile, listDataSources, listDictionaryTables, rejectDomainRefinement, rejectDomainRule, rejectEntityRule, runMetadataJob, testDataSource } from '../api/client'

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

export default function DataSourceManager({ C = {}, token, setActiveNav }) {
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

  return (
    <div style={{ fontFamily: FONT, color: text }}>

      {/* ── Header ──────────────────────────────────────────────────────────── */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: '20px', gap: '16px' }}>
        <div>
          <h2 style={{ margin: '0 0 4px', fontSize: '1.4rem', fontWeight: '700', color: text, letterSpacing: '-0.4px' }}>Data Sources</h2>
          <p style={{ margin: 0, color: muted, fontSize: '0.75rem' }}>
            Connect external databases. SQL Server is active — PostgreSQL and MySQL coming soon.
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

      {/* ── Saved sources list ───────────────────────────────────────────────── */}
      {loading ? (
        <div style={{ textAlign: 'center', padding: '48px 0', color: muted, fontSize: '0.82rem', fontFamily: FONT }}>
          Loading connections…
        </div>
      ) : sources.length === 0 ? (
        <div style={{ ...card(), textAlign: 'center', padding: '48px 24px' }}>
          <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke={muted} strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" style={{ marginBottom: '12px', display: 'block', margin: '0 auto 12px' }}>
            <ellipse cx="12" cy="5" rx="9" ry="3"/>
            <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/>
            <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>
          </svg>
          <p style={{ margin: '0 0 6px', fontSize: '0.9rem', color: textSec, fontWeight: '500', fontFamily: FONT }}>No connections yet</p>
          <p style={{ margin: 0, fontSize: '0.78rem', color: muted, fontFamily: FONT }}>Add a SQL Server connection to get started.</p>
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
          {sources.map(src => {
            const ts     = testState[src.id]
            const delSt  = deleteState[src.id]
            const canTest = src.source_type === 'mssql'
            const sm = SOURCE_STATUS_META[src.source_status] ?? { label: src.source_status ?? '—', color: '#94a3b8' }
            const stLabel = SOURCE_TYPES.find(t => t.value === src.source_type)?.label ?? src.source_type

            return (
              <div key={src.id} style={card()}>

                {/* ── Source header ── */}
                <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '12px', marginBottom: '10px' }}>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', minWidth: 0 }}>
                    <span style={{ fontSize: '0.97rem', fontWeight: '600', color: text, fontFamily: FONT }}>{src.display_name}</span>
                    <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', alignItems: 'center' }}>
                      {/* Source type badge */}
                      <span style={{ display: 'inline-block', padding: '2px 9px', borderRadius: '10px', fontSize: '0.65rem', fontWeight: '600', letterSpacing: '0.04em', background: `${accent}15`, color: accent, border: `1px solid ${accent}30` }}>
                        {stLabel}
                      </span>
                      {/* Status badge */}
                      <span style={{ display: 'inline-block', padding: '2px 9px', borderRadius: '10px', fontSize: '0.65rem', fontWeight: '700', letterSpacing: '0.05em', textTransform: 'uppercase', background: `${sm.color}20`, color: sm.color, border: `1px solid ${sm.color}50` }}>
                        {sm.label}
                      </span>
                    </div>
                  </div>

                  {/* Actions: Remove (Test Connection lives in pipeline Step 1) */}
                  <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexShrink: 0 }}>
                    {/* Remove button — two-click confirm */}
                    {delSt?.confirming ? (
                      <>
                        <button
                          onClick={() => handleDeleteConfirm(src.id)}
                          style={{ background: danger, color: '#fff', border: 'none', borderRadius: '8px', padding: '7px 14px', fontSize: '0.8rem', fontWeight: '600', cursor: 'pointer', fontFamily: FONT }}
                        >
                          Confirm Remove?
                        </button>
                        <button
                          onClick={() => handleDeleteCancel(src.id)}
                          style={btnGhost({ padding: '7px 12px', fontSize: '0.8rem' })}
                        >
                          Cancel
                        </button>
                      </>
                    ) : (
                      <button
                        onClick={() => !delSt?.deleting && handleDeleteClick(src.id)}
                        disabled={delSt?.deleting}
                        style={{
                          ...btnGhost({ padding: '7px 14px', fontSize: '0.8rem' }),
                          color: delSt?.deleting ? `${muted}50` : danger,
                          borderColor: delSt?.deleting ? `${border}60` : `${danger}50`,
                          cursor: delSt?.deleting ? 'not-allowed' : 'pointer',
                        }}
                      >
                        {delSt?.deleting ? 'Removing…' : 'Remove'}
                      </button>
                    )}
                  </div>
                </div>

                {/* ── Config summary (no credentials) ── */}
                <div style={{ fontFamily: MONO, fontSize: '0.78rem', color: muted, background: bg, border: `1px solid ${border}`, borderRadius: '6px', padding: '6px 10px', marginBottom: '10px', letterSpacing: '0.02em', wordBreak: 'break-all' }}>
                  {src.config_summary ?? '—'}
                </div>

                {/* ── Capabilities + last test info ── */}
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '8px' }}>
                  <div style={{ display: 'flex', gap: '5px', flexWrap: 'wrap' }}>
                    {(src.capabilities ?? []).map(cap => (
                      <span key={cap} style={{ fontSize: '0.63rem', padding: '2px 8px', borderRadius: '6px', background: `${accent}12`, color: `${accent}cc`, border: `1px solid ${accent}25`, letterSpacing: '0.04em', fontWeight: '500', fontFamily: FONT }}>
                        {cap.replace(/_/g, ' ')}
                      </span>
                    ))}
                  </div>
                  {src.last_tested_at && (
                    <span style={{ fontSize: '0.7rem', color: muted, fontFamily: FONT }}>
                      Last tested {fmtRelative(src.last_tested_at)}
                      {src.last_test_status && (
                        <span style={{ marginLeft: '6px', color: src.last_test_status === 'success' ? success : danger }}>
                          {src.last_test_status === 'success' ? '✓' : '✗'}
                        </span>
                      )}
                    </span>
                  )}
                </div>

                {/* ── Live test result ── */}
                {ts && !ts.loading && (
                  <div style={{
                    marginTop: '12px', padding: '10px 14px', borderRadius: '8px',
                    background: ts.status === 'success' ? `${success}12` : `${danger}12`,
                    border: `1px solid ${ts.status === 'success' ? success + '40' : danger + '40'}`,
                  }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '3px' }}>
                      <span style={{ fontWeight: '700', fontSize: '0.78rem', color: ts.status === 'success' ? success : danger, textTransform: 'uppercase', letterSpacing: '0.05em', fontFamily: FONT }}>
                        {ts.status === 'success' ? '✓ Connected' : '✗ Failed'}
                      </span>
                      {ts.latency_ms != null && (
                        <span style={{ fontSize: '0.74rem', color: muted, fontFamily: MONO }}>{ts.latency_ms}ms</span>
                      )}
                    </div>
                    <p style={{ margin: 0, fontSize: '0.78rem', color: textSec, fontFamily: FONT }}>{ts.message}</p>
                  </div>
                )}

                {/* ── Metadata Pipeline ───────────────────────────────── */}
                {canTest && (() => {
                  const ds      = discoverState[src.id]
                  const js      = jobState[src.id]
                  const job     = js?.data
                  const schema  = schemaState[src.id]
                  const prof    = profileState[src.id]
                  const dict    = dictState[src.id]
                  const dom     = domainState[src.id]
                  const ent     = entityState[src.id]

                  const hasSchema    = src.last_snapshot_id != null || !!ds?.result
                  const profSnap     = prof?.data?.snapshot
                  const profComplete = profSnap?.status === 'COMPLETE'
                  const dictTables   = Array.isArray(dict?.tables) ? dict.tables : null
                  const dictCount    = dictTables ? dictTables.length : 0
                  const dictApproved = dictTables ? dictTables.filter(t => t.is_approved === 1 || t.is_approved === true).length : 0
                  const dictPct      = dictCount > 0 ? Math.round((dictApproved / dictCount) * 100) : 0
                  const domSummary   = dom?.summary
                  const entSummary   = ent?.summary
                  const domAssigned  = domSummary?.tables_assigned ?? 0
                  const entAssigned  = entSummary?.entities_assigned ?? 0

                  // ── Step 1: Connect & Verify ──
                  const lastTest = ts?.status ?? src.last_test_status
                  let s1 = 'ready'
                  if (lastTest === 'success') s1 = 'done'
                  else if (lastTest === 'failed') s1 = 'failed'
                  const s1stats = []
                  if (s1 === 'done')   s1stats.push({ value: 'Connected', color: success })
                  if (s1 === 'failed') s1stats.push({ value: 'Connection failed', color: danger })
                  if (ts?.latency_ms != null) s1stats.push({ value: `${ts.latency_ms}ms`, color: muted })

                  // ── Step 2: Discover & Profile (metadata job = discovery + structural profiling) ──
                  const jobRunning = job?.status === 'RUNNING' || job?.status === 'QUEUED'
                  let s2
                  if (src.source_status !== 'ACTIVE') s2 = 'locked'
                  else if (ds?.loading || js?.running || jobRunning) s2 = 'running'
                  else if (job?.status === 'FAILED' || ds?.error) s2 = 'failed'
                  else if (hasSchema) s2 = 'done'
                  else s2 = 'ready'
                  const s2stats = []
                  if (ds?.result) {
                    s2stats.push({ value: (ds.result.table_count ?? 0).toLocaleString(), label: 'tables' })
                    s2stats.push({ value: (ds.result.view_count ?? 0).toLocaleString(), label: 'views' })
                    s2stats.push({ value: (ds.result.column_count ?? 0).toLocaleString(), label: 'columns' })
                  } else if (profSnap) {
                    if (profSnap.tables_total != null)  s2stats.push({ value: profSnap.tables_total.toLocaleString(), label: 'tables' })
                    if (profSnap.columns_total != null) s2stats.push({ value: profSnap.columns_total.toLocaleString(), label: 'columns' })
                  }
                  if (profSnap?.status) {
                    s2stats.push({
                      value: `${(profSnap.tables_profiled ?? 0).toLocaleString()}/${(profSnap.tables_total ?? 0).toLocaleString()}`,
                      label: `profiled · ${profSnap.status}`,
                      color: profComplete ? success : accent,
                    })
                  }

                  // ── Step 3: Generate Dictionary ──
                  let s3
                  if (!hasSchema) s3 = 'locked'
                  else if (dict?.generating) s3 = 'running'
                  else if (dictCount > 0) s3 = 'done'
                  else if (dict?.error) s3 = 'failed'
                  else s3 = 'ready'
                  const s3stats = []
                  if (dictCount > 0) {
                    s3stats.push({ value: dictCount.toLocaleString(), label: 'tables documented' })
                    s3stats.push({ value: `${dictPct}%`, label: 'approved', color: dictPct === 100 ? success : accent })
                  }

                  // ── Step 4: Generate Domains ──
                  let s4
                  if (dictCount === 0) s4 = 'locked'
                  else if (dom?.generating) s4 = 'running'
                  else if (domAssigned > 0) s4 = 'done'
                  else if (dom?.error) s4 = 'failed'
                  else s4 = 'ready'
                  const s4stats = []
                  if (domSummary?.domain_counts) {
                    Object.entries(domSummary.domain_counts)
                      .filter(([k]) => k !== 'Unknown')
                      .sort((a, b) => b[1] - a[1])
                      .slice(0, 4)
                      .forEach(([k, v]) => s4stats.push({ value: k, label: String(v) }))
                  }
                  if (domSummary?.tables_unknown) s4stats.push({ value: 'Unknown', label: String(domSummary.tables_unknown), color: muted })

                  // ── Step 5: Generate Entities ──
                  let s5
                  if (domAssigned === 0) s5 = 'locked'
                  else if (ent?.generating) s5 = 'running'
                  else if (entAssigned > 0) s5 = 'done'
                  else if (ent?.error) s5 = 'failed'
                  else s5 = 'ready'
                  const s5stats = []
                  if (entSummary?.entity_counts) {
                    Object.entries(entSummary.entity_counts)
                      .filter(([k]) => k !== 'Unknown')
                      .sort((a, b) => b[1] - a[1])
                      .slice(0, 4)
                      .forEach(([k, v]) => s5stats.push({ value: k, label: String(v) }))
                  }
                  if (entSummary?.entities_unknown) s5stats.push({ value: 'Unknown', label: String(entSummary.entities_unknown), color: muted })
                  // Optional backend-provided breakdown — rendered only if present.
                  if (entSummary?.learned_matches != null)   s5stats.push({ value: String(entSummary.learned_matches), label: 'learned' })
                  if (entSummary?.heuristic_matches != null) s5stats.push({ value: String(entSummary.heuristic_matches), label: 'heuristic' })

                  return (
                    <div style={{ marginTop: '14px', padding: '14px 16px', borderRadius: '8px', background: bg, border: `1px solid ${border}` }}>
                      <div style={{ fontSize: '0.68rem', fontWeight: '700', color: muted, letterSpacing: '0.08em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '14px' }}>
                        Metadata Pipeline
                      </div>

                      {renderPipelineStep({
                        number: 1,
                        label: 'Connect & Verify',
                        status: s1,
                        description: 'Verifies connectivity and credentials.',
                        stats: s1stats,
                        error: s1 === 'failed' ? (ts?.message || src.last_test_message || null) : null,
                        action: {
                          label: ts?.loading ? 'Testing…' : 'Test Connection',
                          primary: s1 !== 'done',
                          onClick: () => !ts?.loading && handleTest(src.id),
                          disabled: ts?.loading,
                          loading: ts?.loading,
                        },
                      })}

                      {renderPipelineStep({
                        number: 2,
                        label: 'Discover & Profile',
                        status: s2,
                        description: (jobRunning && job?.progress_message)
                          ? job.progress_message
                          : 'Discovers tables, views and columns, then runs structural profiling.',
                        stats: s2stats,
                        warnings: ds?.result?.warnings ?? [],
                        lockReason: 'Run Test Connection first to verify the source is reachable.',
                        error: s2 === 'failed' ? (job?.error_message || ds?.error || null) : null,
                        action: (s2 === 'running')
                          ? {
                              label: js?.loading ? 'Refreshing…' : 'Refresh Status',
                              onClick: () => src.metadata_job_id && loadJobStatus(src.id, src.metadata_job_id),
                              disabled: js?.loading,
                              loading: js?.loading,
                            }
                          : {
                              label: hasSchema ? 'Re-run Discover & Profile' : 'Discover & Profile',
                              primary: !hasSchema,
                              onClick: () => handleDiscoverAndProfile(src),
                              disabled: ds?.loading || js?.running,
                              loading: ds?.loading || js?.running,
                            },
                        secondaryAction: hasSchema
                          ? {
                              label: schema?.loading ? 'Loading…' : (schema?.data ? 'Hide Schema' : 'View Schema'),
                              onClick: () => !schema?.loading && handleViewSchema(src.id),
                              disabled: schema?.loading,
                            }
                          : null,
                      })}

                      {renderPipelineStep({
                        number: 3,
                        label: 'Generate Dictionary',
                        status: s3,
                        description: 'AI-generated business names and descriptions for tables and columns.',
                        stats: s3stats,
                        lockReason: 'Requires schema discovery (Step 2) first.',
                        error: s3 === 'failed' ? (dict?.error || null) : null,
                        action: {
                          label: dict?.generating ? 'Generating…' : (dictCount > 0 ? 'Regenerate' : 'Generate Dictionary'),
                          primary: dictCount === 0,
                          onClick: () => !dict?.generating && handleGenerateDictionary(src.id),
                          disabled: dict?.generating,
                          loading: dict?.generating,
                        },
                        secondaryAction: dictCount > 0
                          ? { label: 'Open Dictionary →', onClick: () => setActiveNav && setActiveNav('dictionary') }
                          : null,
                      })}

                      {renderPipelineStep({
                        number: 4,
                        label: 'Generate Domains',
                        status: s4,
                        description: 'Classifies tables into business domains.',
                        stats: s4stats,
                        lockReason: 'Requires a generated dictionary (Step 3) first.',
                        error: s4 === 'failed' ? (dom?.error || null) : null,
                        action: {
                          label: dom?.generating ? 'Generating…' : (domAssigned > 0 ? 'Regenerate' : 'Generate Domains'),
                          primary: domAssigned === 0,
                          onClick: () => !dom?.generating && handleGenerateDomains(src.id),
                          disabled: dom?.generating,
                          loading: dom?.generating,
                        },
                      })}

                      {renderPipelineStep({
                        number: 5,
                        label: 'Generate Entities',
                        status: s5,
                        description: 'Identifies the primary business entity each table represents.',
                        stats: s5stats,
                        lockReason: 'Requires generated domains (Step 4) first.',
                        error: s5 === 'failed' ? (ent?.error || null) : null,
                        isLast: true,
                        action: {
                          label: ent?.generating ? 'Generating…' : (entAssigned > 0 ? 'Regenerate' : 'Generate Entities'),
                          primary: entAssigned === 0,
                          onClick: () => !ent?.generating && handleGenerateEntities(src.id),
                          disabled: ent?.generating,
                          loading: ent?.generating,
                        },
                      })}

                      {/* ── Schema preview (Step 2 secondary action) ──────── */}
                      {schema?.error && (
                        <div style={{ marginTop: '10px', padding: '8px 12px', borderRadius: '8px', background: `${danger}10`, border: `1px solid ${danger}30` }}>
                          <span style={{ fontSize: '0.78rem', color: danger, fontFamily: FONT }}>{schema.error}</span>
                        </div>
                      )}
                      {schema?.data && (
                        <div style={{ marginTop: '12px', border: `1px solid ${border}`, borderRadius: '8px', overflow: 'hidden' }}>
                          <div style={{ padding: '7px 12px', background: bg, borderBottom: `1px solid ${border}`, fontSize: '0.71rem', color: muted, fontFamily: FONT }}>
                            {schema.data.schemas?.length ?? 0} schema{schema.data.schemas?.length !== 1 ? 's' : ''}
                            {schema.data.database_name ? ` · ${schema.data.database_name}` : ''}
                            {schema.data.discovery_duration_ms != null ? ` · discovered in ${schema.data.discovery_duration_ms}ms` : ''}
                          </div>
                          <div>
                            {schema.data.schemas?.slice(0, 5).map(sc => {
                              const sexp = schemaExpand[src.id]?.schemas.includes(sc.schema_name)
                              const schemaTables = sc.tables?.filter(t => t.table_type === 'TABLE') ?? []
                              return (
                                <div key={sc.schema_name} style={{ borderBottom: `1px solid ${border}30` }}>
                                  <div
                                    onClick={() => toggleSchemaItem(src.id, 'schemas', sc.schema_name)}
                                    style={{ padding: '6px 12px', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '7px', fontSize: '0.8rem', fontFamily: FONT, userSelect: 'none' }}
                                  >
                                    <span style={{ fontSize: '0.6rem', color: muted, width: '8px' }}>{sexp ? '▾' : '▸'}</span>
                                    <span style={{ fontWeight: '600', color: textSec }}>{sc.schema_name}</span>
                                    <span style={{ fontSize: '0.68rem', color: muted }}>{schemaTables.length} tables{sc.tables?.filter(t => t.table_type === 'VIEW').length ? ` · ${sc.tables.filter(t => t.table_type === 'VIEW').length} views` : ''}</span>
                                  </div>
                                  {sexp && (
                                    <div style={{ paddingLeft: '16px', paddingBottom: '4px' }}>
                                      {schemaTables.slice(0, 10).map(t => {
                                        const texp = schemaExpand[src.id]?.tables.includes(t.table_fqn)
                                        return (
                                          <div key={t.table_fqn}>
                                            <div
                                              onClick={() => toggleSchemaItem(src.id, 'tables', t.table_fqn)}
                                              style={{ padding: '4px 12px', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.77rem', fontFamily: MONO, userSelect: 'none' }}
                                            >
                                              <span style={{ fontSize: '0.58rem', color: muted, width: '8px' }}>{texp ? '▾' : '▸'}</span>
                                              <span style={{ color: text }}>{t.table_name}</span>
                                              {t.row_count_estimate != null && (
                                                <span style={{ fontSize: '0.66rem', color: muted }}>~{t.row_count_estimate.toLocaleString()}</span>
                                              )}
                                            </div>
                                            {texp && (
                                              <div style={{ paddingLeft: '24px', paddingBottom: '4px' }}>
                                                {t.columns?.slice(0, 10).map(c => (
                                                  <div key={c.column_name} style={{ padding: '2px 12px', display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.72rem', fontFamily: MONO }}>
                                                    {c.is_primary_key && <span style={{ fontSize: '0.58rem', padding: '1px 5px', borderRadius: '4px', background: `${accent}20`, color: accent, border: `1px solid ${accent}30`, fontFamily: FONT, whiteSpace: 'nowrap' }}>PK</span>}
                                                    <span style={{ color: text }}>{c.column_name}</span>
                                                    <span style={{ color: muted }}>{c.data_type}</span>
                                                    {c.is_nullable && <span style={{ color: `${muted}70`, fontSize: '0.64rem' }}>null</span>}
                                                  </div>
                                                ))}
                                                {(t.columns?.length ?? 0) > 10 && (
                                                  <div style={{ padding: '2px 12px', fontSize: '0.68rem', color: muted, fontFamily: FONT }}>+{t.columns.length - 10} more columns</div>
                                                )}
                                              </div>
                                            )}
                                          </div>
                                        )
                                      })}
                                      {schemaTables.length > 10 && (
                                        <div style={{ padding: '4px 12px', fontSize: '0.68rem', color: muted, fontFamily: FONT }}>+{schemaTables.length - 10} more tables</div>
                                      )}
                                    </div>
                                  )}
                                </div>
                              )
                            })}
                            {(schema.data.schemas?.length ?? 0) > 5 && (
                              <div style={{ padding: '6px 12px', fontSize: '0.68rem', color: muted, fontFamily: FONT }}>+{schema.data.schemas.length - 5} more schemas</div>
                            )}
                          </div>
                        </div>
                      )}
                    </div>
                  )
                })()}

                {/* ── Domain Governance ──────────────────────────────── */}
                {(() => {
                  const gs  = govState[src.id]
                  const refs = gs?.refinements ?? []
                  const refPending  = refs.filter(r => r.approval_status === 'PENDING')
                  const refApproved = refs.filter(r => r.approval_status === 'APPROVED')
                  const refRejected = refs.filter(r => r.approval_status === 'REJECTED')
                  const busy = !!(gs?.loading || gs?.analyzing)

                  const govRules   = rulesState[src.id]?.rules ?? null
                  const ruleApproved = govRules?.filter(r => r.approval_status === 'APPROVED').length ?? null
                  const rulePending  = govRules?.filter(r => r.approval_status === 'PENDING').length ?? null

                  const pTypeBadge = (pt) => {
                    const colors = { PREFIX: accent, SUFFIX: '#38bdf8', TOKEN: '#10b981', SCHEMA: '#a78bfa' }
                    const c = colors[pt] ?? muted
                    return {
                      display: 'inline-block', padding: '1px 7px', borderRadius: '6px',
                      fontSize: '0.58rem', fontWeight: '700', letterSpacing: '0.05em',
                      background: `${c}18`, color: c, border: `1px solid ${c}35`,
                      fontFamily: FONT, textTransform: 'uppercase', flexShrink: 0,
                    }
                  }

                  return (
                    <div style={{ marginTop: '12px' }}>
                      {/* Toggle */}
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <button
                          onClick={() => handleToggleGov(src.id)}
                          style={{ ...btnGhost({ padding: '6px 13px', fontSize: '0.78rem' }), display: 'flex', alignItems: 'center', gap: '5px', color: textSec }}
                        >
                          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
                          </svg>
                          Domain Governance
                          {gs?.refinements != null && (
                            <span style={{ padding: '1px 7px', borderRadius: '8px', fontSize: '0.62rem', fontWeight: '700', background: `${warn}20`, color: warn, border: `1px solid ${warn}40` }}>
                              {refPending.length} pending
                            </span>
                          )}
                          <span style={{ fontSize: '0.65rem', color: muted }}>{gs?.open ? '▾' : '▸'}</span>
                        </button>
                      </div>

                      {gs?.open && (
                        <div style={{ marginTop: '10px', padding: '12px 14px', borderRadius: '8px', background: bg, border: `1px solid ${border}` }}>

                          {/* Rule counts summary (if Domain Rules tab already loaded) */}
                          {govRules != null && (
                            <div style={{ display: 'flex', gap: '14px', fontSize: '0.7rem', fontFamily: FONT, marginBottom: '12px', paddingBottom: '10px', borderBottom: `1px solid ${border}` }}>
                              <span style={{ color: muted, fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.06em' }}>Learned Rules</span>
                              <span style={{ color: success }}>Approved: {ruleApproved}</span>
                              <span style={{ color: accent }}>Pending: {rulePending}</span>
                            </div>
                          )}

                          {/* Action bar */}
                          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '8px', marginBottom: '10px' }}>
                            <div style={{ display: 'flex', gap: '10px', fontSize: '0.7rem', fontFamily: FONT }}>
                              <span style={{ color: muted, fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.06em' }}>Refinements</span>
                              {gs?.refinements != null && (
                                <>
                                  <span style={{ color: accent }}>Pending: {refPending.length}</span>
                                  <span style={{ color: success }}>Approved: {refApproved.length}</span>
                                  <span style={{ color: muted }}>Rejected: {refRejected.length}</span>
                                </>
                              )}
                            </div>
                            <div style={{ display: 'flex', gap: '6px' }}>
                              <button
                                onClick={() => !busy && loadRefinements(src.id)}
                                disabled={busy}
                                style={{ ...btnGhost({ padding: '4px 10px', fontSize: '0.74rem' }), color: !busy ? textSec : `${muted}50`, cursor: !busy ? 'pointer' : 'not-allowed' }}
                              >
                                {gs?.loading ? 'Refreshing…' : 'Refresh'}
                              </button>
                              <button
                                onClick={() => !busy && handleAnalyzeRefinements(src.id)}
                                disabled={busy}
                                style={{
                                  ...btnGhost({ padding: '4px 10px', fontSize: '0.74rem' }),
                                  color: !busy ? warn : `${muted}50`,
                                  borderColor: !busy ? `${warn}55` : `${border}50`,
                                  cursor: !busy ? 'pointer' : 'not-allowed',
                                }}
                              >
                                {gs?.analyzing ? 'Analyzing…' : 'Analyze Refinements'}
                              </button>
                            </div>
                          </div>

                          {/* Analysis summary */}
                          {gs?.analyzeResult && (
                            <div style={{ marginBottom: '10px', padding: '8px 12px', borderRadius: '6px', background: `${warn}0d`, border: `1px solid ${warn}30`, display: 'flex', gap: '16px', flexWrap: 'wrap' }}>
                              <span style={{ fontSize: '0.74rem', color: warn, fontFamily: FONT }}>
                                Flagged Rules: <strong>{gs.analyzeResult.flagged_rules ?? 0}</strong>
                              </span>
                              <span style={{ fontSize: '0.74rem', color: accent, fontFamily: FONT }}>
                                Projected Improvement: <strong>+{gs.analyzeResult.projected_accuracy_improvement ?? 0}%</strong>
                              </span>
                              {gs.analyzeResult.total_refineable_tables != null && (
                                <span style={{ fontSize: '0.74rem', color: muted, fontFamily: FONT }}>
                                  Refineable: {gs.analyzeResult.total_refineable_tables} tables
                                </span>
                              )}
                            </div>
                          )}

                          {/* Loading */}
                          {gs?.loading && !gs?.refinements && (
                            <p style={{ margin: 0, fontSize: '0.75rem', color: muted, fontFamily: FONT }}>Loading refinements…</p>
                          )}

                          {/* Error */}
                          {gs?.error && (
                            <p style={{ margin: 0, fontSize: '0.75rem', color: danger, fontFamily: FONT }}>{gs.error}</p>
                          )}

                          {/* Pending refinements */}
                          {refPending.length > 0 && (
                            <div style={{ marginBottom: '10px' }}>
                              <div style={{ fontSize: '0.62rem', color: accent, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', marginBottom: '6px', fontFamily: FONT }}>
                                Pending ({refPending.length})
                              </div>
                              <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                                {refPending.map(ref => {
                                  const acting = !!refinementActionState[ref.id]
                                  return (
                                    <div key={ref.id} style={{ display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap', padding: '6px 8px', borderRadius: '6px', background: `${warn}08`, border: `1px solid ${border}` }}>
                                      <span style={pTypeBadge(ref.pattern_type)}>{ref.pattern_type}</span>
                                      <span style={{ fontFamily: MONO, fontSize: '0.79rem', color: text, flex: '0 0 auto' }}>{ref.pattern_value}</span>
                                      <span style={{ fontSize: '0.73rem', color: muted }}>→</span>
                                      <span style={{ fontSize: '0.76rem', color: textSec, flex: 1 }}>{ref.suggested_domain}</span>
                                      <span style={{ fontSize: '0.68rem', color: muted, fontFamily: MONO }}>sup:{ref.support_count}</span>
                                      <span style={{ fontSize: '0.68rem', color: muted, fontFamily: MONO }}>{Math.round(ref.confidence * 100)}%</span>
                                      <div style={{ display: 'flex', gap: '4px', marginLeft: 'auto' }}>
                                        <button
                                          onClick={() => !acting && handleApproveRefinement(src.id, ref.id)}
                                          disabled={acting}
                                          style={{ background: acting ? `${success}30` : `${success}20`, color: acting ? `${success}60` : success, border: `1px solid ${success}40`, borderRadius: '6px', padding: '3px 10px', fontSize: '0.74rem', fontWeight: '600', cursor: acting ? 'not-allowed' : 'pointer', fontFamily: FONT }}
                                        >
                                          {acting ? '…' : 'Approve'}
                                        </button>
                                        <button
                                          onClick={() => !acting && handleRejectRefinement(src.id, ref.id)}
                                          disabled={acting}
                                          style={{ background: 'transparent', color: acting ? `${muted}50` : muted, border: `1px solid ${border}`, borderRadius: '6px', padding: '3px 10px', fontSize: '0.74rem', cursor: acting ? 'not-allowed' : 'pointer', fontFamily: FONT }}
                                        >
                                          {acting ? '…' : 'Reject'}
                                        </button>
                                      </div>
                                    </div>
                                  )
                                })}
                              </div>
                            </div>
                          )}

                          {/* Approved refinements */}
                          {refApproved.length > 0 && (
                            <div style={{ marginBottom: '8px' }}>
                              <div style={{ fontSize: '0.62rem', color: success, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', marginBottom: '5px', fontFamily: FONT }}>
                                Approved ({refApproved.length})
                              </div>
                              <div style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
                                {refApproved.map(ref => (
                                  <div key={ref.id} style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.74rem', padding: '3px 6px' }}>
                                    <span style={pTypeBadge(ref.pattern_type)}>{ref.pattern_type}</span>
                                    <span style={{ fontFamily: MONO, color: textSec }}>{ref.pattern_value}</span>
                                    <span style={{ color: muted }}>→</span>
                                    <span style={{ color: success }}>{ref.suggested_domain}</span>
                                    <span style={{ color: muted, fontFamily: MONO, fontSize: '0.66rem' }}>sup:{ref.support_count}</span>
                                  </div>
                                ))}
                              </div>
                            </div>
                          )}

                          {/* Rejected refinements */}
                          {refRejected.length > 0 && (
                            <div>
                              <div style={{ fontSize: '0.62rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', marginBottom: '5px', fontFamily: FONT }}>
                                Rejected ({refRejected.length})
                              </div>
                              <div style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
                                {refRejected.map(ref => (
                                  <div key={ref.id} style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.74rem', padding: '3px 6px', opacity: 0.45 }}>
                                    <span style={pTypeBadge(ref.pattern_type)}>{ref.pattern_type}</span>
                                    <span style={{ fontFamily: MONO, color: muted }}>{ref.pattern_value}</span>
                                    <span style={{ color: muted }}>→</span>
                                    <span style={{ color: muted }}>{ref.suggested_domain}</span>
                                  </div>
                                ))}
                              </div>
                            </div>
                          )}

                          {/* Empty */}
                          {!gs?.loading && gs?.refinements?.length === 0 && (
                            <p style={{ margin: 0, fontSize: '0.75rem', color: muted, fontFamily: FONT }}>
                              No refinement suggestions available. Click Analyze Refinements to generate them.
                            </p>
                          )}
                        </div>
                      )}
                    </div>
                  )
                })()}

                {/* Discovery, View Schema, and the schema preview tree now live
                    in the Metadata Pipeline (Step 2) above — no duplicate controls here. */}

                {/* ── Domain Learning Rules ───────────────────────── */}
                {(() => {
                  const rs       = rulesState[src.id]
                  const allRules = rs?.rules ?? []
                  const pending  = allRules.filter(r => r.approval_status === 'PENDING')
                  const approved = allRules.filter(r => r.approval_status === 'APPROVED')
                  const rejected = allRules.filter(r => r.approval_status === 'REJECTED')
                  const busy     = !!(rs?.loading || rs?.generating)

                  const pTypeBadge = (pt) => {
                    const colors = { PREFIX: accent, SUFFIX: '#38bdf8', TOKEN: '#10b981', SCHEMA: '#a78bfa' }
                    const c = colors[pt] ?? muted
                    return {
                      display: 'inline-block', padding: '1px 7px', borderRadius: '6px',
                      fontSize: '0.58rem', fontWeight: '700', letterSpacing: '0.05em',
                      background: `${c}18`, color: c, border: `1px solid ${c}35`,
                      fontFamily: FONT, textTransform: 'uppercase', flexShrink: 0,
                    }
                  }

                  return (
                    <div style={{ marginTop: '12px' }}>
                      {/* Toggle bar */}
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <button
                          onClick={() => handleToggleRules(src.id)}
                          style={{
                            ...btnGhost({ padding: '6px 13px', fontSize: '0.78rem' }),
                            display: 'flex', alignItems: 'center', gap: '5px', color: textSec,
                          }}
                        >
                          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M12 2a4 4 0 0 1 4 4c0 1.5-.8 2.8-2 3.5V12l2 2-2 2v1.5c1.2.7 2 2 2 3.5a4 4 0 0 1-8 0c0-1.5.8-2.8 2-3.5V16l-2-2 2-2V9.5C10.8 8.8 10 7.5 10 6a4 4 0 0 1 2-3.5"/>
                          </svg>
                          Domain Rules
                          {rs?.rules != null && (
                            <span style={{ padding: '1px 7px', borderRadius: '8px', fontSize: '0.62rem', fontWeight: '700', background: `${accent}20`, color: accent, border: `1px solid ${accent}40` }}>
                              {pending.length} pending
                            </span>
                          )}
                          <span style={{ fontSize: '0.65rem', color: muted }}>{rs?.open ? '▾' : '▸'}</span>
                        </button>
                      </div>

                      {rs?.open && (
                        <div style={{ marginTop: '10px', padding: '12px 14px', borderRadius: '8px', background: bg, border: `1px solid ${border}` }}>
                          {/* Action bar */}
                          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '8px', marginBottom: '10px' }}>
                            <div style={{ display: 'flex', gap: '12px', fontSize: '0.7rem', fontFamily: FONT }}>
                              <span style={{ color: accent, fontWeight: '600' }}>Pending: {pending.length}</span>
                              <span style={{ color: success }}>Approved: {approved.length}</span>
                              <span style={{ color: muted }}>Rejected: {rejected.length}</span>
                            </div>
                            <div style={{ display: 'flex', gap: '6px' }}>
                              <button
                                onClick={() => !busy && loadDomainRules(src.id)}
                                disabled={busy}
                                style={{ ...btnGhost({ padding: '4px 10px', fontSize: '0.74rem' }), color: !busy ? textSec : `${muted}50`, cursor: !busy ? 'pointer' : 'not-allowed' }}
                              >
                                {rs?.loading ? 'Refreshing…' : 'Refresh'}
                              </button>
                              <button
                                onClick={() => !busy && handleGenerateSuggestions(src.id)}
                                disabled={busy}
                                style={{
                                  ...btnGhost({ padding: '4px 10px', fontSize: '0.74rem' }),
                                  color: !busy ? accent : `${muted}50`,
                                  borderColor: !busy ? `${accent}55` : `${border}50`,
                                  cursor: !busy ? 'pointer' : 'not-allowed',
                                }}
                              >
                                {rs?.generating ? 'Generating…' : 'Generate Suggestions'}
                              </button>
                            </div>
                          </div>

                          {/* Loading */}
                          {rs?.loading && !rs?.rules && (
                            <p style={{ margin: 0, fontSize: '0.75rem', color: muted, fontFamily: FONT }}>Loading rules…</p>
                          )}

                          {/* Error */}
                          {rs?.error && (
                            <p style={{ margin: 0, fontSize: '0.75rem', color: danger, fontFamily: FONT }}>{rs.error}</p>
                          )}

                          {/* Pending */}
                          {pending.length > 0 && (
                            <div style={{ marginBottom: '10px' }}>
                              <div style={{ fontSize: '0.62rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', marginBottom: '6px', fontFamily: FONT }}>
                                Pending suggestions
                              </div>
                              <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                                {pending.map(rule => {
                                  const acting = !!ruleActionState[rule.id]
                                  return (
                                    <div key={rule.id} style={{ display: 'flex', alignItems: 'center', gap: '7px', flexWrap: 'wrap', padding: '6px 8px', borderRadius: '6px', background: `${accent}08`, border: `1px solid ${border}` }}>
                                      <span style={pTypeBadge(rule.pattern_type)}>{rule.pattern_type}</span>
                                      <span style={{ fontFamily: MONO, fontSize: '0.79rem', color: text, flex: '0 0 auto' }}>{rule.pattern_value}</span>
                                      <span style={{ fontSize: '0.73rem', color: muted }}>→</span>
                                      <span style={{ fontSize: '0.76rem', color: textSec, flex: 1 }}>{rule.domain}</span>
                                      <span style={{ fontSize: '0.68rem', color: muted, fontFamily: MONO }}>{Math.round(rule.confidence * 100)}%</span>
                                      <div style={{ display: 'flex', gap: '4px', marginLeft: 'auto' }}>
                                        <button
                                          onClick={() => !acting && handleApproveRule(src.id, rule.id)}
                                          disabled={acting}
                                          style={{ background: acting ? `${success}30` : `${success}20`, color: acting ? `${success}60` : success, border: `1px solid ${success}40`, borderRadius: '6px', padding: '3px 10px', fontSize: '0.74rem', fontWeight: '600', cursor: acting ? 'not-allowed' : 'pointer', fontFamily: FONT }}
                                        >
                                          {acting ? '…' : 'Approve'}
                                        </button>
                                        <button
                                          onClick={() => !acting && handleRejectRule(src.id, rule.id)}
                                          disabled={acting}
                                          style={{ background: 'transparent', color: acting ? `${muted}50` : muted, border: `1px solid ${border}`, borderRadius: '6px', padding: '3px 10px', fontSize: '0.74rem', cursor: acting ? 'not-allowed' : 'pointer', fontFamily: FONT }}
                                        >
                                          {acting ? '…' : 'Reject'}
                                        </button>
                                      </div>
                                    </div>
                                  )
                                })}
                              </div>
                            </div>
                          )}

                          {/* Approved */}
                          {approved.length > 0 && (
                            <div style={{ marginBottom: '8px' }}>
                              <div style={{ fontSize: '0.62rem', color: success, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', marginBottom: '5px', fontFamily: FONT }}>
                                Approved ({approved.length})
                              </div>
                              <div style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
                                {approved.map(rule => (
                                  <div key={rule.id} style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.74rem', padding: '3px 6px' }}>
                                    <span style={pTypeBadge(rule.pattern_type)}>{rule.pattern_type}</span>
                                    <span style={{ fontFamily: MONO, color: textSec }}>{rule.pattern_value}</span>
                                    <span style={{ color: muted }}>→</span>
                                    <span style={{ color: success }}>{rule.domain}</span>
                                  </div>
                                ))}
                              </div>
                            </div>
                          )}

                          {/* Rejected */}
                          {rejected.length > 0 && (
                            <div>
                              <div style={{ fontSize: '0.62rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', marginBottom: '5px', fontFamily: FONT }}>
                                Rejected ({rejected.length})
                              </div>
                              <div style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
                                {rejected.map(rule => (
                                  <div key={rule.id} style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.74rem', padding: '3px 6px', opacity: 0.5 }}>
                                    <span style={pTypeBadge(rule.pattern_type)}>{rule.pattern_type}</span>
                                    <span style={{ fontFamily: MONO, color: muted }}>{rule.pattern_value}</span>
                                    <span style={{ color: muted }}>→</span>
                                    <span style={{ color: muted }}>{rule.domain}</span>
                                  </div>
                                ))}
                              </div>
                            </div>
                          )}

                          {/* Empty */}
                          {!rs?.loading && rs?.rules?.length === 0 && (
                            <p style={{ margin: 0, fontSize: '0.75rem', color: muted, fontFamily: FONT }}>
                              No domain rules yet. Click Generate Suggestions to analyse Unknown tables.
                            </p>
                          )}
                        </div>
                      )}
                    </div>
                  )
                })()}

                {/* ── Entity Learning Rules ───────────────────────── */}
                {(() => {
                  const ers       = entityRulesState[src.id]
                  const allRules  = ers?.rules ?? []
                  const pending   = allRules.filter(r => r.approval_status === 'PENDING')
                  const approved  = allRules.filter(r => r.approval_status === 'APPROVED')
                  const rejected  = allRules.filter(r => r.approval_status === 'REJECTED')
                  const busy      = !!(ers?.loading || ers?.generating)

                  const pTypeBadge = (pt) => {
                    const colors = { PREFIX: accent, SUFFIX: '#38bdf8', TOKEN: '#10b981', SCHEMA: '#a78bfa' }
                    const c = colors[pt] ?? muted
                    return {
                      display: 'inline-block', padding: '1px 7px', borderRadius: '6px',
                      fontSize: '0.58rem', fontWeight: '700', letterSpacing: '0.05em',
                      background: `${c}18`, color: c, border: `1px solid ${c}35`,
                      fontFamily: FONT, textTransform: 'uppercase', flexShrink: 0,
                    }
                  }

                  return (
                    <div style={{ marginTop: '12px' }}>
                      {/* Toggle bar */}
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <button
                          onClick={() => handleToggleEntityRules(src.id)}
                          style={{
                            ...btnGhost({ padding: '6px 13px', fontSize: '0.78rem' }),
                            display: 'flex', alignItems: 'center', gap: '5px', color: textSec,
                          }}
                        >
                          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                            <rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/>
                            <rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/>
                          </svg>
                          Entity Rules
                          {ers?.rules != null && (
                            <span style={{ padding: '1px 7px', borderRadius: '8px', fontSize: '0.62rem', fontWeight: '700', background: `${accent}20`, color: accent, border: `1px solid ${accent}40` }}>
                              {pending.length} pending
                            </span>
                          )}
                          <span style={{ fontSize: '0.65rem', color: muted }}>{ers?.open ? '▾' : '▸'}</span>
                        </button>
                      </div>

                      {ers?.open && (
                        <div style={{ marginTop: '10px', padding: '12px 14px', borderRadius: '8px', background: bg, border: `1px solid ${border}` }}>
                          {/* Action bar */}
                          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '8px', marginBottom: '10px' }}>
                            <div style={{ display: 'flex', gap: '12px', fontSize: '0.7rem', fontFamily: FONT }}>
                              <span style={{ color: accent, fontWeight: '600' }}>Pending: {pending.length}</span>
                              <span style={{ color: success }}>Approved: {approved.length}</span>
                              <span style={{ color: muted }}>Rejected: {rejected.length}</span>
                            </div>
                            <div style={{ display: 'flex', gap: '6px' }}>
                              <button
                                onClick={() => !busy && loadEntityRules(src.id)}
                                disabled={busy}
                                style={{ ...btnGhost({ padding: '4px 10px', fontSize: '0.74rem' }), color: !busy ? textSec : `${muted}50`, cursor: !busy ? 'pointer' : 'not-allowed' }}
                              >
                                {ers?.loading ? 'Refreshing…' : 'Refresh'}
                              </button>
                              <button
                                onClick={() => !busy && handleGenerateEntitySuggestions(src.id)}
                                disabled={busy}
                                style={{
                                  ...btnGhost({ padding: '4px 10px', fontSize: '0.74rem' }),
                                  color: !busy ? accent : `${muted}50`,
                                  borderColor: !busy ? `${accent}55` : `${border}50`,
                                  cursor: !busy ? 'pointer' : 'not-allowed',
                                }}
                              >
                                {ers?.generating ? 'Generating…' : 'Generate Suggestions'}
                              </button>
                            </div>
                          </div>

                          {/* Loading */}
                          {ers?.loading && !ers?.rules && (
                            <p style={{ margin: 0, fontSize: '0.75rem', color: muted, fontFamily: FONT }}>Loading entity rules…</p>
                          )}

                          {/* Error */}
                          {ers?.error && (
                            <p style={{ margin: 0, fontSize: '0.75rem', color: danger, fontFamily: FONT }}>{ers.error}</p>
                          )}

                          {/* Pending */}
                          {pending.length > 0 && (
                            <div style={{ marginBottom: '10px' }}>
                              <div style={{ fontSize: '0.62rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', marginBottom: '6px', fontFamily: FONT }}>
                                Pending suggestions
                              </div>
                              <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                                {pending.map(rule => {
                                  const acting = !!entityRuleActionState[rule.id]
                                  return (
                                    <div key={rule.id} style={{ display: 'flex', alignItems: 'center', gap: '7px', flexWrap: 'wrap', padding: '6px 8px', borderRadius: '6px', background: `${accent}08`, border: `1px solid ${border}` }}>
                                      <span style={pTypeBadge(rule.pattern_type)}>{rule.pattern_type}</span>
                                      <span style={{ fontFamily: MONO, fontSize: '0.79rem', color: text, flex: '0 0 auto' }}>{rule.pattern_value}</span>
                                      <span style={{ fontSize: '0.73rem', color: muted }}>→</span>
                                      <span style={{ fontSize: '0.76rem', color: textSec, flex: 1 }}>{rule.entity}</span>
                                      <span style={{ fontSize: '0.68rem', color: muted, fontFamily: MONO }}>{Math.round(rule.confidence * 100)}%</span>
                                      <div style={{ display: 'flex', gap: '4px', marginLeft: 'auto' }}>
                                        <button
                                          onClick={() => !acting && handleApproveEntityRule(src.id, rule.id)}
                                          disabled={acting}
                                          style={{ background: acting ? `${success}30` : `${success}20`, color: acting ? `${success}60` : success, border: `1px solid ${success}40`, borderRadius: '6px', padding: '3px 10px', fontSize: '0.74rem', fontWeight: '600', cursor: acting ? 'not-allowed' : 'pointer', fontFamily: FONT }}
                                        >
                                          {acting ? '…' : 'Approve'}
                                        </button>
                                        <button
                                          onClick={() => !acting && handleRejectEntityRule(src.id, rule.id)}
                                          disabled={acting}
                                          style={{ background: 'transparent', color: acting ? `${muted}50` : muted, border: `1px solid ${border}`, borderRadius: '6px', padding: '3px 10px', fontSize: '0.74rem', cursor: acting ? 'not-allowed' : 'pointer', fontFamily: FONT }}
                                        >
                                          {acting ? '…' : 'Reject'}
                                        </button>
                                      </div>
                                    </div>
                                  )
                                })}
                              </div>
                            </div>
                          )}

                          {/* Approved */}
                          {approved.length > 0 && (
                            <div style={{ marginBottom: '8px' }}>
                              <div style={{ fontSize: '0.62rem', color: success, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', marginBottom: '5px', fontFamily: FONT }}>
                                Approved ({approved.length})
                              </div>
                              <div style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
                                {approved.map(rule => (
                                  <div key={rule.id} style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.74rem', padding: '3px 6px' }}>
                                    <span style={pTypeBadge(rule.pattern_type)}>{rule.pattern_type}</span>
                                    <span style={{ fontFamily: MONO, color: textSec }}>{rule.pattern_value}</span>
                                    <span style={{ color: muted }}>→</span>
                                    <span style={{ color: success }}>{rule.entity}</span>
                                  </div>
                                ))}
                              </div>
                            </div>
                          )}

                          {/* Rejected */}
                          {rejected.length > 0 && (
                            <div>
                              <div style={{ fontSize: '0.62rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', marginBottom: '5px', fontFamily: FONT }}>
                                Rejected ({rejected.length})
                              </div>
                              <div style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
                                {rejected.map(rule => (
                                  <div key={rule.id} style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.74rem', padding: '3px 6px', opacity: 0.5 }}>
                                    <span style={pTypeBadge(rule.pattern_type)}>{rule.pattern_type}</span>
                                    <span style={{ fontFamily: MONO, color: muted }}>{rule.pattern_value}</span>
                                    <span style={{ color: muted }}>→</span>
                                    <span style={{ color: muted }}>{rule.entity}</span>
                                  </div>
                                ))}
                              </div>
                            </div>
                          )}

                          {/* Empty */}
                          {!ers?.loading && ers?.rules?.length === 0 && (
                            <p style={{ margin: 0, fontSize: '0.75rem', color: muted, fontFamily: FONT }}>
                              No entity rules yet. Click Generate Suggestions to analyse Unknown entity tables.
                            </p>
                          )}
                        </div>
                      )}
                    </div>
                  )
                })()}
              </div>
            )
          })}
        </div>
      )}

      <Toast toast={toast} />

      <style>{`@keyframes dsm-spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  )
}
