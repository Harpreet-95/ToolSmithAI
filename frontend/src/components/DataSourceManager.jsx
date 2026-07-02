import { useState, useEffect } from 'react'
import { analyzeDomainRefinements, approveDomainRefinement, approveDomainRule, approveEntityRule, bulkGovernanceApprove, bulkGovernanceDryRun, bulkGovernanceReject, continueBatchProfile, createDataSource, deleteDataSource, discoverDataSourceSchema, executeDataSourceQuery, explainTable, generateDictionaryForSource, generateDomainRuleSuggestions, generateDomains, generateEntities, generateEntityRuleSuggestions, getDomainRefinements, getDomainRules, getDomainSummary, getDataSourceSchema, getDownstreamLineage, getEntityRules, getEntitySummary, getGovernanceAssignments, getGovernanceAssignmentSummary, getGovernanceBottlenecks, getGovernanceDashboard, getGovernanceExplanation, getGovernancePolicies, getGovernanceRecommendations, getImpactAnalysis, getKnowledgeGraphSummary, getMetadataJob, getProfile, getProfileHistory, getProfileReviewTasks, getRelatedTables, getSemanticSummary, getSemanticTableProfile, getUpstreamLineage, listDataSources, listDictionaryTables, listDomainAssignments, listEntityAssignments, rejectDomainRefinement, rejectDomainRule, rejectEntityRule, runMetadataJob, startBatchProfile, testDataSource } from '../api/client'
import DictionaryReview from './DictionaryReview'
import ColumnProfileExplorer from './ColumnProfileExplorer'

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

const _REVIEW_PAGE_SIZE = 50

function fmtRelative(iso) {
  if (!iso) return null
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 2)  return 'just now'
  if (mins < 60) return `${mins} min ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24)  return hrs === 1 ? '1 hour ago' : `${hrs} hours ago`
  const days = Math.floor(hrs / 24)
  if (days === 1) return 'Yesterday'
  return `${days} days ago`
}

function fmtDateTime(iso) {
  if (!iso) return null
  const d = new Date(iso)
  if (isNaN(d.getTime())) return null
  const date = d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
  const time = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
  return date + ' · ' + time
}

function fmtDate(iso) {
  if (!iso) return null
  try { return new Date(iso).toLocaleString('en-US', { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' }) }
  catch { return null }
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

export default function DataSourceManager({ C = {}, token, setActiveNav, openSource, dsSelectedSourceId, dsActiveTab, setDsSelectedSourceId, setDsActiveTab, setDsSourceName }) {
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
  const [fullProfState,    setFullProfState]    = useState({})  // { [id]: { loading, snapshotId, progress, total, error } }
  const [reviewTaskState,  setReviewTaskState]  = useState({})  // { [id]: { loading, data, error } }
  const [reviewPage,       setReviewPage]       = useState({})  // { [id]: number } 0-indexed current page
  const [srcMenu,          setSrcMenu]          = useState({})  // { [id]: bool } per-source three-dot menu
  const [landingSearch,    setLandingSearch]    = useState('')    // landing-page search filter
  const [bkSelectedTable,  setBkSelectedTable]  = useState({})  // { [srcId]: table_fqn } — Business Knowledge tab's table picker
  const [bkExpanded,       setBkExpanded]       = useState({})  // { [sectionKey]: bool } — collapsed/expanded per BK card (overview defaults open)
  const [bkSectionState,   setBkSectionState]   = useState({})  // { [cacheKey]: { loading, data, error } } — lazy-loaded + cached per (source, table, section)

  // ── Governance Command Center ──────────────────────────────────────────────
  const [cmdCenterState,    setCmdCenterState]    = useState({})  // { [`${srcId}::dashboard`|`${srcId}::assignments`|`${srcId}::policies`]: { loading, data, error } }
  const [cmdCenterExpanded, setCmdCenterExpanded] = useState({ exec: true })  // { [section]: bool } — Executive Dashboard open by default
  const [recExpanded,       setRecExpanded]       = useState({})  // { [recommendationId]: bool } — expanded recommendation detail
  const [assignFilters,     setAssignFilters]     = useState({})  // { [srcId]: { priority, status, steward } }
  const [bulkForm,          setBulkForm]          = useState({})  // { [srcId]: { object_type, confidence_min, confidence_max, approval_state, domain, entity, schema_name, exclude_pii } }
  const [bulkResult,        setBulkResult]        = useState({})  // { [srcId]: { loading, dryRun, approveResult, rejectResult, error, confirmAction } }
  const [explainForm,       setExplainForm]       = useState({})  // { [srcId]: { object_type, table_fqn, column_name, rule_id, suggestion_id, tool_id } }
  const [explainState,      setExplainState]      = useState({})  // { [srcId]: { loading, data, error } }
  const [queryState,        setQueryState]        = useState({})  // { [srcId]: { question, loading, result, error } }

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

  // Push selected source name up to App header breadcrumb
  useEffect(() => {
    if (!setDsSourceName) return
    const src = sources.find(s => s.id === dsSelectedSourceId)
    setDsSourceName(src?.display_name ?? null)
  }, [dsSelectedSourceId, sources]) // eslint-disable-line react-hooks/exhaustive-deps

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
    if (dsActiveTab === 'governance' && !reviewTaskState[id]?.data && !reviewTaskState[id]?.loading) {
      loadReviewTasks(id, 0)
    }
    if (dsActiveTab === 'governance' && cmdCenterExpanded.exec) {
      ensureCmdDashboard(id)
    }
    if (dsActiveTab === 'business-knowledge') {
      if (!schemaState[id]?.data && !schemaState[id]?.loading && src?.last_snapshot_id != null) {
        loadSchema(id)
      }
      const bkFqn = bkSelectedTable[id]
      if (bkFqn) ensureBkSection(id, bkFqn, 'overview')
    }
  }, [dsActiveTab, dsSelectedSourceId]) // eslint-disable-line react-hooks/exhaustive-deps

  // Clear query results whenever the user navigates to a different source.
  // Satisfies the "do not cache result rows" requirement.
  useEffect(() => {
    if (dsSelectedSourceId == null) return
    setQueryState(s => {
      const cur = s[dsSelectedSourceId]
      if (!cur?.result && !cur?.error) return s
      return { ...s, [dsSelectedSourceId]: { question: cur.question ?? '', loading: false, result: null, error: null } }
    })
  }, [dsSelectedSourceId]) // eslint-disable-line react-hooks/exhaustive-deps

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

  async function loadReviewTasks(id, page = 0) {
    // Preserve existing data during page navigation so the list stays visible
    // while the next page loads. For a first load, s[id] is undefined so data
    // stays absent, which correctly triggers the loading spinner.
    setReviewTaskState(s => ({ ...s, [id]: { ...(s[id] ?? {}), loading: true, error: null } }))
    setReviewPage(s => ({ ...s, [id]: page }))
    try {
      const resp = await getProfileReviewTasks(id, token, {
        limit:  _REVIEW_PAGE_SIZE,
        offset: page * _REVIEW_PAGE_SIZE,
      })
      setReviewTaskState(s => ({ ...s, [id]: { loading: false, data: resp?.data ?? null, error: null } }))
    } catch (e) {
      if (_is404(e)) {
        setReviewTaskState(s => ({ ...s, [id]: { loading: false, data: null, error: null } }))
      } else {
        setReviewTaskState(s => ({ ...s, [id]: { loading: false, data: null, error: e?.message ?? 'Failed to load review tasks.' } }))
      }
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

  // ── Business Knowledge tab: generic per-section lazy loader + cache ─────────
  // Each section's data is fetched once per (source, table) and reused across
  // sections that need the same underlying call (e.g. Overview + Business
  // Explanation both reuse the same explain_table() fetch).
  function _bkTableKey(sourceId, tableFqn, dataKey) {
    return `${sourceId}::${tableFqn}::${dataKey}`
  }
  function _bkSourceKey(sourceId, dataKey) {
    return `${sourceId}::${dataKey}`
  }
  const BK_FETCHERS = {
    explain:         (id, fqn) => explainTable(id, fqn, token),
    semanticProfile: (id, fqn) => getSemanticTableProfile(id, fqn, token),
    related:         (id, fqn) => getRelatedTables(id, fqn, token),
    upstream:        (id, fqn) => getUpstreamLineage(id, fqn, token),
    downstream:      (id, fqn) => getDownstreamLineage(id, fqn, token),
    impact:          (id, fqn) => getImpactAnalysis(id, fqn, token),
  }
  const BK_SOURCE_FETCHERS = {
    kgSummary:       id => getKnowledgeGraphSummary(id, token),
    semanticSummary: id => getSemanticSummary(id, token),
  }
  const BK_SECTION_DEPS = {
    overview:    ['explain', 'semanticProfile'],
    graph:       ['related'],
    lineage:     ['upstream', 'downstream', 'impact'],
    semantic:    ['semanticProfile'],
    explanation: ['explain'],
  }
  const BK_SECTION_SOURCE_DEPS = {
    summary: ['kgSummary', 'semanticSummary'],
  }

  async function _loadBkData(key, fetchFn) {
    setBkSectionState(s => ({ ...s, [key]: { ...(s[key] ?? {}), loading: true, error: null } }))
    try {
      const resp = await fetchFn()
      setBkSectionState(s => ({ ...s, [key]: { loading: false, data: resp?.data ?? resp, error: null } }))
    } catch (e) {
      setBkSectionState(s => ({ ...s, [key]: { loading: false, data: null, error: _is404(e) ? null : (e?.message ?? 'Failed to load.') } }))
    }
  }

  // Fetches only the data a section needs, only if not already cached/in-flight.
  function ensureBkSection(sourceId, tableFqn, sectionId) {
    for (const dataKey of BK_SECTION_DEPS[sectionId] ?? []) {
      const key = _bkTableKey(sourceId, tableFqn, dataKey)
      if (!bkSectionState[key]?.data && !bkSectionState[key]?.loading) {
        _loadBkData(key, () => BK_FETCHERS[dataKey](sourceId, tableFqn))
      }
    }
    for (const dataKey of BK_SECTION_SOURCE_DEPS[sectionId] ?? []) {
      const key = _bkSourceKey(sourceId, dataKey)
      if (!bkSectionState[key]?.data && !bkSectionState[key]?.loading) {
        _loadBkData(key, () => BK_SOURCE_FETCHERS[dataKey](sourceId))
      }
    }
  }

  function toggleBkSection(sourceId, tableFqn, sectionId) {
    const opening = !bkExpanded[sectionId]
    setBkExpanded(s => ({ ...s, [sectionId]: opening }))
    if (opening && tableFqn) ensureBkSection(sourceId, tableFqn, sectionId)
  }

  // Selecting a table (directly, or by clicking a related/lineage node) refreshes
  // only the sections currently expanded — collapsed sections fetch on next expand.
  function handleBkSelectTable(sourceId, tableFqn) {
    setBkSelectedTable(s => ({ ...s, [sourceId]: tableFqn }))
    for (const sectionId of Object.keys(BK_SECTION_DEPS)) {
      const isOpen = sectionId === 'overview' ? (bkExpanded.overview !== false) : !!bkExpanded[sectionId]
      if (isOpen) ensureBkSection(sourceId, tableFqn, sectionId)
    }
  }

  // ── Governance Command Center: lazy-load + cache ────────────────────────────
  // Executive Dashboard / Recommendations / Bottlenecks all read from the same
  // /governance/dashboard payload (it already embeds kpis, bottlenecks, and
  // recommendations) so expanding all three never issues more than one fetch.
  function _cmdKey(sourceId, section) {
    return `${sourceId}::${section}`
  }

  async function _loadCmd(key, fetchFn) {
    setCmdCenterState(s => ({ ...s, [key]: { ...(s[key] ?? {}), loading: true, error: null } }))
    try {
      const resp = await fetchFn()
      setCmdCenterState(s => ({ ...s, [key]: { loading: false, data: resp?.data ?? resp, error: null } }))
    } catch (e) {
      setCmdCenterState(s => ({ ...s, [key]: { loading: false, data: null, error: e?.message ?? 'Failed to load.' } }))
    }
  }

  function ensureCmdDashboard(sourceId, force = false) {
    const key = _cmdKey(sourceId, 'dashboard')
    if (force || (!cmdCenterState[key]?.data && !cmdCenterState[key]?.loading)) {
      _loadCmd(key, () => getGovernanceDashboard(sourceId, token))
    }
  }

  function ensureCmdRecommendations(sourceId) {
    const dashKey = _cmdKey(sourceId, 'dashboard')
    if (cmdCenterState[dashKey]?.data) return
    const key = _cmdKey(sourceId, 'recommendations')
    if (!cmdCenterState[key]?.data && !cmdCenterState[key]?.loading) {
      _loadCmd(key, () => getGovernanceRecommendations(sourceId, token))
    }
  }

  function ensureCmdBottlenecks(sourceId) {
    const dashKey = _cmdKey(sourceId, 'dashboard')
    if (cmdCenterState[dashKey]?.data) return
    const key = _cmdKey(sourceId, 'bottlenecks')
    if (!cmdCenterState[key]?.data && !cmdCenterState[key]?.loading) {
      _loadCmd(key, () => getGovernanceBottlenecks(sourceId, token))
    }
  }

  function ensureCmdAssignments(sourceId, force = false) {
    const key = _cmdKey(sourceId, 'assignments')
    if (force || (!cmdCenterState[key]?.data && !cmdCenterState[key]?.loading)) {
      _loadCmd(key, async () => {
        const [listResp, summaryResp] = await Promise.all([
          getGovernanceAssignments(sourceId, token, { limit: 200 }),
          getGovernanceAssignmentSummary(sourceId, token),
        ])
        return { data: { items: listResp?.data ?? [], summary: summaryResp?.data ?? null } }
      })
    }
  }

  function ensureCmdPolicies(force = false) {
    const key = 'global::policies'
    if (force || (!cmdCenterState[key]?.data && !cmdCenterState[key]?.loading)) {
      _loadCmd(key, () => getGovernancePolicies(token))
    }
  }

  function toggleCmdSection(sourceId, section) {
    const opening = !cmdCenterExpanded[section]
    setCmdCenterExpanded(s => ({ ...s, [section]: opening }))
    if (!opening) return
    if (section === 'exec')            ensureCmdDashboard(sourceId)
    if (section === 'recommendations') ensureCmdRecommendations(sourceId)
    if (section === 'bottlenecks')     ensureCmdBottlenecks(sourceId)
    if (section === 'assignments')     ensureCmdAssignments(sourceId)
    if (section === 'policies')        ensureCmdPolicies()
  }

  function updateBulkForm(sourceId, patch) {
    setBulkForm(s => ({ ...s, [sourceId]: { ...(s[sourceId] ?? { object_type: 'dict.table', exclude_pii: true }), ...patch } }))
  }

  async function handleBulkDryRun(sourceId) {
    const filter = { source_id: sourceId, object_type: 'dict.table', exclude_pii: true, ...(bulkForm[sourceId] ?? {}) }
    setBulkResult(s => ({ ...s, [sourceId]: { ...(s[sourceId] ?? {}), loading: true, error: null, confirmAction: null } }))
    try {
      const resp = await bulkGovernanceDryRun(filter, token)
      setBulkResult(s => ({ ...s, [sourceId]: { loading: false, dryRun: resp?.data ?? resp, approveResult: null, rejectResult: null, error: null, confirmAction: null } }))
    } catch (e) {
      setBulkResult(s => ({ ...s, [sourceId]: { ...(s[sourceId] ?? {}), loading: false, error: e?.message ?? 'Dry run failed.' } }))
    }
  }

  function handleBulkRequestConfirm(sourceId, action) {
    setBulkResult(s => ({ ...s, [sourceId]: { ...(s[sourceId] ?? {}), confirmAction: action } }))
  }

  function handleBulkCancelConfirm(sourceId) {
    setBulkResult(s => ({ ...s, [sourceId]: { ...(s[sourceId] ?? {}), confirmAction: null } }))
  }

  async function handleBulkExecute(sourceId, action) {
    const filter = { source_id: sourceId, object_type: 'dict.table', exclude_pii: true, ...(bulkForm[sourceId] ?? {}) }
    setBulkResult(s => ({ ...s, [sourceId]: { ...(s[sourceId] ?? {}), loading: true, error: null, confirmAction: null } }))
    try {
      const fn = action === 'approve' ? bulkGovernanceApprove : bulkGovernanceReject
      const resp = await fn(filter, token)
      setBulkResult(s => ({
        ...s,
        [sourceId]: {
          ...(s[sourceId] ?? {}),
          loading: false,
          error: null,
          [action === 'approve' ? 'approveResult' : 'rejectResult']: resp?.data ?? resp,
        },
      }))
      notify(`Bulk ${action} complete.`)
      ensureCmdDashboard(sourceId, true) // refresh KPIs/backlog after a write
    } catch (e) {
      setBulkResult(s => ({ ...s, [sourceId]: { ...(s[sourceId] ?? {}), loading: false, error: e?.message ?? `Bulk ${action} failed.` } }))
      notify(e?.message ?? `Bulk ${action} failed.`, false)
    }
  }

  function updateExplainForm(sourceId, patch) {
    setExplainForm(s => ({ ...s, [sourceId]: { ...(s[sourceId] ?? { object_type: 'dict.table' }), ...patch } }))
  }

  async function handleExplain(sourceId) {
    const f = explainForm[sourceId] ?? { object_type: 'dict.table' }
    setExplainState(s => ({ ...s, [sourceId]: { loading: true, data: null, error: null } }))
    try {
      const resp = await getGovernanceExplanation({ source_id: sourceId, ...f }, token)
      setExplainState(s => ({ ...s, [sourceId]: { loading: false, data: resp?.data ?? resp, error: null } }))
    } catch (e) {
      setExplainState(s => ({ ...s, [sourceId]: { loading: false, data: null, error: e?.message ?? 'Failed to load explanation.' } }))
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
    loadReviewTasks(src.id)
  }

  async function handleRunDataProfile(src) {
    const id = src.id
    setFullProfState(s => ({ ...s, [id]: { loading: true, snapshotId: null, progress: 0, total: 0, error: null } }))
    try {
      const startResp = await startBatchProfile(id, token)
      const snap = startResp?.data
      const snapshotId = snap?.profiling_snapshot_id
      const total = snap?.total_tables ?? 0
      setFullProfState(s => ({ ...s, [id]: { loading: true, snapshotId, progress: 0, total, error: null } }))
      let isComplete = total === 0 || (snap?.next_table_index != null && snap.next_table_index >= total)
      while (!isComplete) {
        // eslint-disable-next-line no-await-in-loop
        const contResp = await continueBatchProfile(id, snapshotId, token)
        const cont = contResp?.data
        isComplete = cont?.is_complete ?? false
        setFullProfState(s => ({ ...s, [id]: { loading: !isComplete, snapshotId, progress: cont?.completed_tables ?? 0, total: cont?.total_tables ?? total, error: null } }))
      }
      await loadProfile(id)
      await loadProfileHistory(id)
      loadReviewTasks(id)
      notify('Data profile complete.')
    } catch (e) {
      const msg = e?.message ?? 'Data profiling failed.'
      setFullProfState(s => ({ ...s, [id]: { ...(s[id] ?? {}), loading: false, error: msg } }))
      notify(msg, false)
    }
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

  async function handleQueryRun(sourceId) {
    const q = (queryState[sourceId]?.question ?? '').trim()
    if (!q || queryState[sourceId]?.loading) return
    setQueryState(s => ({ ...s, [sourceId]: { ...(s[sourceId] ?? {}), loading: true, result: null, error: null } }))
    try {
      const resp = await executeDataSourceQuery(sourceId, q, token)
      setQueryState(s => ({ ...s, [sourceId]: { ...(s[sourceId] ?? {}), loading: false, result: resp?.data ?? null, error: null } }))
    } catch (err) {
      const msg = (err?.message ?? '').replace(/^\d+:\s*/, '') || 'Query failed. Please try again.'
      setQueryState(s => ({ ...s, [sourceId]: { ...(s[sourceId] ?? {}), loading: false, result: null, error: msg } }))
    }
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
    const profHist     = profileHistState[dsSelectedSourceId] ?? {}
    const fullProfSt      = fullProfState[dsSelectedSourceId]   ?? {}
    const reviewTaskSt    = reviewTaskState[dsSelectedSourceId] ?? {}
    const reviewCurrentPage = reviewPage[dsSelectedSourceId] ?? 0

    // Pipeline computed values (mirrors existing logic exactly)
    const hasSchema    = src?.last_snapshot_id != null || !!discSt.result
    const profSnap     = prof.data?.snapshot
    const profComplete          = profSnap?.status === 'COMPLETE'
    const profTables            = Array.isArray(prof.data?.tables) ? prof.data.tables : []
    const profTablesStatistical = profTables.filter(t => t.profiling_depth === 'STATISTICAL' || t.profiling_depth === 'FULL').length
    const profTablesStructOnly  = profTables.filter(t => t.profiling_depth === 'STRUCTURAL_ONLY').length
    const hasStatisticalData    = profTablesStatistical > 0
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
      { id: 'business-knowledge', label: 'Knowledge' },
      { id: 'lineage',    label: 'Lineage'    },
      { id: 'runs',       label: 'Runs'       },
      { id: 'query',      label: 'Query'      },
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

    const overviewTab = (() => {
      // ── Derived values ────────────────────────────────────────────────────────
      const kpiSchemas = sc.data?.schemas?.length ?? null
      const kpiTables  = sc.data?.schemas != null
        ? sc.data.schemas.reduce((a, s) => a + (s.tables?.filter(t => t.table_type === 'TABLE').length ?? 0), 0)
        : (profSnap?.tables_total ?? null)
      const kpiViews   = sc.data?.schemas != null
        ? sc.data.schemas.reduce((a, s) => a + (s.tables?.filter(t => t.table_type === 'VIEW').length ?? 0), 0)
        : null
      const kpiCols    = profSnap?.columns_total ?? null

      const domUnassigned = domSummary != null ? (domSummary.tables_unknown ?? 0) : null
      const domTotal      = domAssigned + (domUnassigned ?? 0)
      const domPct        = domTotal > 0 ? Math.round(domAssigned / domTotal * 100) : null

      const entUnassigned = entSummary != null ? (entSummary.entities_unknown ?? 0) : null
      const entTotal      = entAssigned + (entUnassigned ?? 0)
      const entPct        = entTotal > 0 ? Math.round(entAssigned / entTotal * 100) : null

      // ── Executive readiness KPIs ──────────────────────────────────────────────
      const rtTasks           = reviewTaskSt.data?.tasks   ?? []
      const rtSummary         = reviewTaskSt.data?.summary ?? null
      const criticalTaskCount = rtSummary?.critical ?? rtTasks.filter(t => t.severity === 'CRITICAL').length
      const openTaskCount     = rtSummary?.open     ?? rtTasks.length
      const piiPendingCount   = rtSummary?.pii_pending ?? rtTasks.filter(t => t.task_type === 'Review PII Classification').length
      const profTablesProfiled = profSnap?.tables_profiled ?? 0
      const profTablesTotal    = profSnap?.tables_total ?? (kpiTables ?? 0)
      const profCoveragePct    = profTablesTotal > 0 ? Math.round(profTablesProfiled / profTablesTotal * 100) : null
      const isStructuralOnly   = profSnap?.mode?.toLowerCase() === 'structural_only'

      // ── Stage statuses ────────────────────────────────────────────────────────
      let sProfile = 'locked'
      if (hasSchema) {
        if (prof.loading)   sProfile = 'running'
        else if (profComplete)  sProfile = 'done'
        else if (prof.error)    sProfile = 'failed'
        else sProfile = 'ready'
      }

      let sDictStage = 'locked'
      if (hasSchema) {
        if (dict.generating) sDictStage = 'running'
        else if (dictCount > 0 && dictApproved < dictCount) sDictStage = 'review'
        else if (dictCount > 0) sDictStage = 'done'
        else if (dict.error)    sDictStage = 'failed'
        else sDictStage = 'ready'
      }

      const stgCol = st => ({ done: success, review: warn, ready: accent, running: accent, locked: `${muted}50`, failed: danger }[st] ?? muted)
      const stgLbl = st => ({ done: 'Completed', review: 'Awaiting Approval', ready: 'Ready', running: 'In Progress', locked: 'Locked', failed: 'Failed' }[st] ?? st)

      // ── Pipeline stages with real metrics ─────────────────────────────────────
      const SI = (...paths) => (
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">{paths}</svg>
      )
      const pipelineStages = [
        { num: 1, label: 'Connect & Verify', status: s1, ts: src?.last_tested_at ?? null,
          metrics: s1 === 'done' && ts.latency_ms != null ? [`${ts.latency_ms}ms`] : [],
          icon: SI(<path key="a" d="M12 22V12"/>,<path key="b" d="M5 12H2a10 10 0 0 0 20 0h-3"/>,<rect key="c" x="8" y="2" width="2" height="6" rx="1"/>,<rect key="d" x="14" y="2" width="2" height="6" rx="1"/>) },
        { num: 2, label: 'Discover Schema', status: s2, ts: src?.last_snapshot_at ?? null,
          metrics: kpiSchemas != null ? [`${kpiSchemas} schema${kpiSchemas !== 1 ? 's' : ''}`, kpiTables != null ? `${kpiTables.toLocaleString()} tables` : null].filter(Boolean) : [],
          icon: SI(<ellipse key="a" cx="12" cy="5" rx="9" ry="3"/>,<path key="b" d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/>,<path key="c" d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>) },
        { num: 3, label: 'Profile Data', status: sProfile, ts: null,
          metrics: profSnap != null ? [profSnap.tables_profiled != null ? `${profSnap.tables_profiled}/${profSnap.tables_total} assets` : null, kpiCols != null && kpiCols > 0 ? `${kpiCols.toLocaleString()} cols` : null].filter(Boolean) : [],
          icon: SI(<polyline key="a" points="22 12 18 12 15 21 9 3 6 12 2 12"/>) },
        { num: 4, label: 'Business Dictionary', status: sDictStage, ts: null,
          metrics: dictCount > 0 ? [`${dictCount.toLocaleString()} terms`, `${dictApproved} approved`] : [],
          icon: SI(<path key="a" d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>,<path key="b" d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>) },
        { num: 5, label: 'Assign Domains', status: s4, ts: null,
          metrics: domAssigned > 0 ? [`${domAssigned} assigned`, domPct != null ? `${domPct}% coverage` : null].filter(Boolean) : [],
          icon: SI(<circle key="a" cx="18" cy="5" r="3"/>,<circle key="b" cx="6" cy="12" r="3"/>,<circle key="c" cx="18" cy="19" r="3"/>,<line key="d" x1="8.59" y1="13.51" x2="15.42" y2="17.49"/>,<line key="e" x1="15.41" y1="6.51" x2="8.59" y2="10.49"/>) },
        { num: 6, label: 'Map Entities', status: s5, ts: null,
          metrics: entAssigned > 0 ? [`${entAssigned} assigned`, entPct != null ? `${entPct}% coverage` : null].filter(Boolean) : [],
          icon: SI(<path key="a" d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>,<circle key="b" cx="9" cy="7" r="4"/>) },
        { num: 7, label: 'Govern & Refine', status: s6, ts: null,
          metrics: totalPending > 0 ? [`${totalPending} pending`] : (s6 === 'done' ? ['All reviewed'] : []),
          icon: SI(<path key="a" d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>,<polyline key="b" points="9 12 11 14 15 10"/>) },
      ]

      // ── Next Best Action ──────────────────────────────────────────────────────
      const nba = (() => {
        // Prerequisites — cannot be bypassed
        if (s1 !== 'done') return { urgency: 'ACTION REQUIRED', urgencyColor: danger,
          title: 'Test Connection',
          desc: 'Verify connectivity and credentials before the metadata pipeline can proceed.',
          cta: 'Test Connection', tab: null, act: () => !ts.loading && handleTest(dsSelectedSourceId) }
        if (!hasSchema) return { urgency: 'NEXT STEP', urgencyColor: accent,
          title: 'Discover Schema',
          desc: 'Scan all schemas, tables, and columns to build the metadata foundation.',
          cta: 'Discover Schema', tab: 'schema', act: () => src && handleDiscoverAndProfile(src) }
        // Priority 1: Critical review tasks block certification
        if (criticalTaskCount > 0) return { urgency: 'ACTION REQUIRED', urgencyColor: danger,
          title: 'Resolve Critical Review Tasks',
          desc: `${criticalTaskCount} critical issue${criticalTaskCount !== 1 ? 's' : ''} require immediate attention before this source can be certified for governed use.`,
          cta: 'Review Tasks', tab: 'governance', act: null }
        // Priority 2: Dictionary must be generated and fully approved
        if (dictCount === 0) return { urgency: 'NEXT STEP', urgencyColor: accent,
          title: 'Generate Business Dictionary',
          desc: 'AI-generate business names and descriptions for all discovered tables.',
          cta: 'Generate Dictionary', tab: 'dictionary', act: () => handleGenerateDictionary(dsSelectedSourceId) }
        if (dictApproved === 0) return { urgency: 'ACTION REQUIRED', urgencyColor: danger,
          title: 'Approve Business Dictionary',
          desc: `${dictCount.toLocaleString()} business terms are awaiting approval. Governance cannot proceed until dictionary review is complete.`,
          cta: 'Review Dictionary', tab: 'dictionary', act: null }
        if (dictApproved < dictCount) return { urgency: 'ATTENTION NEEDED', urgencyColor: warn,
          title: 'Complete Dictionary Review',
          desc: `${(dictCount - dictApproved).toLocaleString()} of ${dictCount.toLocaleString()} business terms are still pending approval.`,
          cta: 'Review Dictionary', tab: 'dictionary', act: null }
        // Priority 3: Domain coverage gaps
        if (domAssigned === 0) return { urgency: 'NEXT STEP', urgencyColor: accent,
          title: 'Assign Business Domains',
          desc: 'Classify tables into business domains to enable ownership and governance.',
          cta: 'Generate Domains', tab: 'domains', act: () => handleGenerateDomains(dsSelectedSourceId) }
        if (domPct != null && domPct < 100 && domTotal > 0) return { urgency: 'ATTENTION NEEDED', urgencyColor: warn,
          title: 'Complete Domain Coverage',
          desc: `${(domTotal - domAssigned).toLocaleString()} table${domTotal - domAssigned !== 1 ? 's' : ''} ${domTotal - domAssigned !== 1 ? 'have' : 'has'} no domain assignment. Current coverage: ${domPct}%.`,
          cta: 'Assign Domains', tab: 'domains', act: null }
        // Priority 4: Entity coverage gaps
        if (entAssigned === 0) return { urgency: 'NEXT STEP', urgencyColor: accent,
          title: 'Map Business Entities',
          desc: 'Identify the primary business object each table represents — Customer, Order, Product.',
          cta: 'Generate Entities', tab: 'entities', act: () => handleGenerateEntities(dsSelectedSourceId) }
        if (entPct != null && entPct < 100 && entTotal > 0) return { urgency: 'ATTENTION NEEDED', urgencyColor: warn,
          title: 'Complete Entity Coverage',
          desc: `${(entTotal - entAssigned).toLocaleString()} table${entTotal - entAssigned !== 1 ? 's' : ''} ${entTotal - entAssigned !== 1 ? 'lack' : 'lacks'} entity classification. Current coverage: ${entPct}%.`,
          cta: 'Map Entities', tab: 'entities', act: null }
        // Priority 5: Full profile not yet run
        if (!profComplete) return { urgency: 'NEXT STEP', urgencyColor: accent,
          title: 'Run Data Profile',
          desc: 'Profile data assets to uncover column statistics, PII risk, and data quality issues.',
          cta: 'Run Profile', tab: 'profile', act: null }
        if (isStructuralOnly) return { urgency: 'ATTENTION NEEDED', urgencyColor: warn,
          title: 'Run Full Data Profile',
          desc: 'Current profile is structural only. Run full profiling to capture column metrics and enable PII detection.',
          cta: 'Run Full Profile', tab: 'profile', act: null }
        // Remaining open tasks and governance
        if (openTaskCount > 0) return { urgency: 'ATTENTION NEEDED', urgencyColor: warn,
          title: 'Review Open Tasks',
          desc: `${openTaskCount} review task${openTaskCount !== 1 ? 's' : ''} ${openTaskCount !== 1 ? 'are' : 'is'} open. Resolve before certifying this source.`,
          cta: 'Review Tasks', tab: 'governance', act: null }
        if (totalPending > 0) return { urgency: 'ATTENTION NEEDED', urgencyColor: warn,
          title: 'Review Governance Items',
          desc: `${totalPending} governance item${totalPending !== 1 ? 's' : ''} require review before this source can be certified.`,
          cta: 'Review Governance', tab: 'governance', act: null }
        return { urgency: 'COMPLETE', urgencyColor: success,
          title: 'Source Ready for Governed Use',
          desc: 'All metadata intelligence and governance steps are complete. This source is certified for governed use.',
          cta: null, tab: null, act: null }
      })()

      // ── Recent Activity ───────────────────────────────────────────────────────
      const actItems = []
      if (src?.last_tested_at) actItems.push({ label: src.last_test_status === 'success' ? 'Connection verified' : 'Connection test failed', ts: src.last_tested_at, col: src.last_test_status === 'success' ? success : danger })
      if (src?.last_snapshot_at && hasSchema) actItems.push({ label: kpiTables != null ? `Schema discovered · ${kpiTables.toLocaleString()} tables` : 'Schema discovered', ts: src.last_snapshot_at, col: success })
      if (profComplete) actItems.push({ label: profSnap?.tables_profiled != null ? `Profile completed · ${profSnap.tables_profiled} assets` : 'Profile completed', ts: profSnap?.created_at ?? null, col: success })
      if (dictCount > 0) actItems.push({ label: `Dictionary generated · ${dictCount.toLocaleString()} terms`, ts: dictTables?.reduce((max, t) => (t.created_at ?? '') > (max ?? '') ? t.created_at : max, null) ?? null, col: accent })
      if (domAssigned > 0) actItems.push({ label: `Domains assigned · ${domAssigned} tables`, ts: domSummary?.last_generated_at ?? null, col: accent })
      if (entAssigned > 0) actItems.push({ label: `Entities mapped · ${entAssigned} tables`, ts: entSummary?.last_generated_at ?? null, col: accent })
      actItems.sort((a, b) => {
        if (a.ts && b.ts) return new Date(b.ts) - new Date(a.ts)
        return a.ts ? -1 : b.ts ? 1 : 0
      })

      // ── KPI card definitions (Executive readiness view) ───────────────────────
      const KI = (...paths) => (
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">{paths}</svg>
      )
      const kpiDefs = [
        // 1. Structural Coverage — tables_profiled/tables_total (any depth); statistical breakdown from per-table data
        { label: 'Structural Coverage',
          value: profTablesTotal > 0 ? `${profCoveragePct ?? 0}%` : (hasSchema && profSnap == null ? '—' : null),
          valueColor: profTablesTotal > 0 && !profComplete ? warn : null,
          lines: profTablesTotal > 0 ? [
            { text: `${profTablesProfiled.toLocaleString()} / ${profTablesTotal.toLocaleString()} tables have profile records`, color: muted },
            ...(profComplete && profTables.length > 0
              ? [{ text: `${profTablesStatistical} with statistics · ${profTablesStructOnly} structural-only`, color: hasStatisticalData ? success : warn }]
              : []),
            { text: isStructuralOnly ? 'Schema-only — no row statistics captured' : profComplete ? (hasStatisticalData ? 'Full profile run · statistics available' : 'Full profile run · no statistics captured') : 'Profiling in progress', color: isStructuralOnly ? warn : profComplete ? (hasStatisticalData ? success : warn) : accent },
          ] : hasSchema ? [{ text: 'Not yet profiled', color: warn }] : [],
          iconCol: '#38bdf8', tab: 'profile',
          icon: KI(<polyline key="a" points="22 12 18 12 15 21 9 3 6 12 2 12"/>) },
        // 2. PII Pending Review — tasks where task_type === 'Review PII Classification'
        { label: 'PII Pending Review',
          value: reviewTaskSt.data != null ? piiPendingCount : null,
          valueColor: piiPendingCount > 0 ? danger : null,
          lines: reviewTaskSt.data != null ? [
            { text: piiPendingCount > 0 ? 'Classification review required' : 'No PII tasks pending', color: piiPendingCount > 0 ? danger : success },
          ] : [],
          iconCol: '#ef4444', tab: piiPendingCount > 0 ? 'governance' : null,
          icon: KI(<path key="a" d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>,<line key="b" x1="12" y1="8" x2="12" y2="12"/>,<line key="c" x1="12" y1="16" x2="12.01" y2="16"/>) },
        // 3. Assets Requiring Review — total open review tasks from summary.open
        { label: 'Assets Requiring Review',
          value: reviewTaskSt.data != null ? openTaskCount : null,
          valueColor: openTaskCount > 0 ? (criticalTaskCount > 0 ? danger : warn) : null,
          lines: reviewTaskSt.data != null ? [
            ...(criticalTaskCount > 0 ? [{ text: `${criticalTaskCount} critical · action required`, color: danger }] : []),
            { text: openTaskCount > 0 ? 'Review in Governance tab' : 'All tasks resolved', color: openTaskCount > 0 ? warn : success },
          ] : [],
          iconCol: '#f59e0b', tab: openTaskCount > 0 ? 'governance' : null,
          icon: KI(<path key="a" d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>,<line key="b" x1="12" y1="9" x2="12" y2="13"/>,<line key="c" x1="12" y1="17" x2="12.01" y2="17"/>) },
        // 4. Dictionary Approval — approved / total and %
        { label: 'Dictionary Approval',
          value: dictCount > 0 ? `${dictPct}%` : null,
          valueColor: dictCount > 0 && dictPct < 100 ? (dictPct === 0 ? danger : warn) : null,
          lines: dictCount > 0 ? [
            { text: `${dictApproved.toLocaleString()} / ${dictCount.toLocaleString()} approved`, color: muted },
            { text: dictApproved === dictCount ? 'All terms approved' : `${(dictCount - dictApproved).toLocaleString()} pending approval`, color: dictApproved === dictCount ? success : warn },
          ] : [],
          iconCol: '#f59e0b', tab: 'dictionary',
          icon: KI(<path key="a" d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>,<path key="b" d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>) },
        // 5. Domain Coverage — assigned / total and %
        { label: 'Domain Coverage',
          value: domSummary != null ? (domPct != null ? `${domPct}%` : '0%') : null,
          valueColor: domSummary != null && (domPct == null || domPct < 80) ? warn : null,
          lines: domSummary != null ? [
            { text: `${domAssigned.toLocaleString()} / ${domTotal.toLocaleString()} tables assigned`, color: muted },
            { text: domPct === 100 ? 'Full coverage' : `${(domTotal - domAssigned).toLocaleString()} tables unassigned`, color: domPct === 100 ? success : warn },
          ] : [],
          iconCol: '#6366f1', tab: 'domains',
          icon: KI(<circle key="a" cx="18" cy="5" r="3"/>,<circle key="b" cx="6" cy="12" r="3"/>,<circle key="c" cx="18" cy="19" r="3"/>,<line key="d" x1="8.59" y1="13.51" x2="15.42" y2="17.49"/>,<line key="e" x1="15.41" y1="6.51" x2="8.59" y2="10.49"/>) },
        // 6. Entity Coverage — assigned / total and %
        { label: 'Entity Coverage',
          value: entSummary != null ? (entPct != null ? `${entPct}%` : '0%') : null,
          valueColor: entSummary != null && (entPct == null || entPct < 80) ? warn : null,
          lines: entSummary != null ? [
            { text: `${entAssigned.toLocaleString()} / ${entTotal.toLocaleString()} tables mapped`, color: muted },
            { text: entPct === 100 ? 'Full coverage' : `${(entTotal - entAssigned).toLocaleString()} tables unmapped`, color: entPct === 100 ? success : warn },
          ] : [],
          iconCol: '#ec4899', tab: 'entities',
          icon: KI(<path key="a" d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>,<circle key="b" cx="9" cy="7" r="4"/>,<path key="c" d="M23 21v-2a4 4 0 0 0-3-3.87"/>,<path key="d" d="M16 3.13a4 4 0 0 1 0 7.75"/>) },
      ].filter(k => k.value != null)

      // ── Source config rows ────────────────────────────────────────────────────
      const truncate = (str, n) => str && str.length > n ? str.slice(0, n) + '…' : str
      const cfgRows = [
        { label: 'Type',        value: stLabel,                                   color: null,
          icon: <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg> },
        { label: 'Connection',  value: truncate(src?.config_summary ?? null, 38), color: null,
          icon: <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg> },
        { label: 'Environment', value: src?.metadata?.environment ?? null,         color: null,
          icon: <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg> },
        { label: 'Status',      value: smBadge.label,                             color: smBadge.color,
          icon: <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg> },
      ].filter(r => r.value != null)

      // ── Governance Readiness checks ───────────────────────────────────────────
      const rcStatusCol = { ok: success, warn, error: danger, locked: muted }
      const readinessChecks = [
        { key: 'profile', label: 'Profile',
          status: !hasSchema ? 'locked' : profComplete && !isStructuralOnly ? 'ok' : profComplete ? 'warn' : 'warn',
          detail: !hasSchema ? 'No schema' : profComplete && !isStructuralOnly ? `${profCoveragePct ?? 100}%` : profComplete ? 'Structural only' : 'Not run',
          tab: 'profile' },
        { key: 'pii', label: 'PII',
          status: reviewTaskSt.data == null ? 'locked' : piiPendingCount === 0 ? 'ok' : 'error',
          detail: reviewTaskSt.data == null ? '—' : piiPendingCount === 0 ? 'Clear' : `${piiPendingCount} pending`,
          tab: 'governance' },
        { key: 'tasks', label: 'Open Tasks',
          status: reviewTaskSt.data == null ? 'locked' : openTaskCount === 0 ? 'ok' : criticalTaskCount > 0 ? 'error' : 'warn',
          detail: reviewTaskSt.data == null ? '—' : openTaskCount === 0 ? 'All clear' : `${openTaskCount}${criticalTaskCount > 0 ? ` (${criticalTaskCount} crit)` : ''}`,
          tab: 'governance' },
        { key: 'dict', label: 'Dictionary',
          status: dictCount === 0 ? 'locked' : dictApproved === dictCount ? 'ok' : dictApproved === 0 ? 'error' : 'warn',
          detail: dictCount === 0 ? 'Not generated' : `${dictPct}%`,
          tab: 'dictionary' },
        { key: 'domains', label: 'Domains',
          status: domSummary == null ? 'locked' : domPct === 100 ? 'ok' : domPct != null && domPct >= 50 ? 'warn' : 'error',
          detail: domSummary == null ? '—' : domPct != null ? `${domPct}%` : '0%',
          tab: 'domains' },
        { key: 'entities', label: 'Entities',
          status: entSummary == null ? 'locked' : entPct === 100 ? 'ok' : entPct != null && entPct >= 50 ? 'warn' : 'error',
          detail: entSummary == null ? '—' : entPct != null ? `${entPct}%` : '0%',
          tab: 'entities' },
      ]
      const readinessOk      = readinessChecks.filter(c => c.status === 'ok').length
      const readinessBlocked = readinessChecks.some(c => c.status === 'error')
      const readinessOverall = readinessBlocked ? 'blocked' : readinessOk === readinessChecks.length ? 'ready' : 'in-progress'
      const readinessColor   = readinessOverall === 'ready' ? success : readinessOverall === 'blocked' ? danger : warn
      const readinessLabel   = readinessOverall === 'ready' ? 'Source Ready' : readinessOverall === 'blocked' ? 'Blocked' : 'In Progress'
      const rcStatusIcon = {
        ok:     <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>,
        warn:   <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>,
        error:  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>,
        locked: <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>,
      }

      return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '10px', width: '100%', minWidth: 0 }}>

            {/* KPI Row */}
            {kpiDefs.length > 0 && (
              <div style={{ display: 'grid', gridTemplateColumns: `repeat(${Math.min(kpiDefs.length, 6)}, minmax(0, 1fr))`, gap: '10px' }}>
                {kpiDefs.map(k => (
                  <div key={k.label}
                    onClick={k.tab ? () => setDsActiveTab(k.tab) : undefined}
                    style={{ background: surface, border: `1px solid ${border}`, borderRadius: '10px', padding: '10px 12px 8px', cursor: k.tab ? 'pointer' : 'default' }}>
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '6px' }}>
                      <span style={{ fontSize: '0.58rem', fontWeight: '700', color: muted, letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: 'calc(100% - 28px)' }}>{k.label}</span>
                      <div style={{ width: '22px', height: '22px', borderRadius: '6px', background: `${k.iconCol}18`, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
                        <span style={{ color: k.iconCol, display: 'flex', alignItems: 'center' }}>{k.icon}</span>
                      </div>
                    </div>
                    <div style={{ fontSize: '1.5rem', fontWeight: '800', color: k.valueColor ?? text, fontFamily: FONT, lineHeight: 1, letterSpacing: '-0.5px' }}>{k.value}</div>
                    {(k.lines ?? []).map((line, i) => (
                      <div key={i} style={{ fontSize: '0.62rem', color: line.color, fontFamily: FONT, marginTop: i === 0 ? '3px' : '2px', lineHeight: 1.3 }}>{line.text}</div>
                    ))}
                  </div>
                ))}
              </div>
            )}

            {/* Governance Readiness */}
            <div style={{ background: surface, border: `1px solid ${readinessColor}40`, borderLeft: `3px solid ${readinessColor}`, borderRadius: '10px', padding: '10px 16px' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '8px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '7px' }}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={readinessColor} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
                  <span style={{ fontSize: '0.8rem', fontWeight: '700', color: text, fontFamily: FONT }}>Governance Readiness</span>
                  <span style={{ fontSize: '0.65rem', color: muted, fontFamily: FONT }}>{readinessOk} / {readinessChecks.length} complete</span>
                </div>
                <span style={{ padding: '3px 12px', borderRadius: '20px', fontSize: '0.62rem', fontWeight: '800', letterSpacing: '0.08em', textTransform: 'uppercase', background: `${readinessColor}18`, color: readinessColor, border: `1px solid ${readinessColor}40`, fontFamily: FONT }}>
                  {readinessLabel}
                </span>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(6, minmax(0, 1fr))', gap: '6px' }}>
                {readinessChecks.map(check => {
                  const col = rcStatusCol[check.status] ?? muted
                  return (
                    <div key={check.key}
                      onClick={check.tab ? () => setDsActiveTab(check.tab) : undefined}
                      style={{ background: `${col}08`, border: `1px solid ${col}30`, borderRadius: '8px', padding: '7px 8px 6px', cursor: check.tab ? 'pointer' : 'default' }}>
                      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '4px' }}>
                        <span style={{ fontSize: '0.58rem', fontWeight: '700', color: muted, letterSpacing: '0.06em', textTransform: 'uppercase', fontFamily: FONT }}>{check.label}</span>
                        <span style={{ color: col, display: 'flex', alignItems: 'center' }}>{rcStatusIcon[check.status]}</span>
                      </div>
                      <div style={{ fontSize: '0.68rem', color: col, fontWeight: '600', fontFamily: FONT, lineHeight: 1.3 }}>{check.detail}</div>
                    </div>
                  )
                })}
              </div>
            </div>

            {/* Metadata Intelligence Pipeline */}
            <div style={{ background: surface, border: `1px solid ${border}`, borderRadius: '10px', padding: '12px 16px 10px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '7px', marginBottom: '10px' }}>
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke={accent} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>
                <span style={{ fontSize: '0.8rem', fontWeight: '700', color: text, fontFamily: FONT }}>Metadata Intelligence Pipeline</span>
              </div>
              <div style={{ display: 'flex', alignItems: 'flex-start' }}>
                {pipelineStages.map((stage, idx) => {
                  const col    = stgCol(stage.status)
                  const isLast = idx === pipelineStages.length - 1
                  const isDone = stage.status === 'done'
                  const isRun  = stage.status === 'running'
                  const isLock = stage.status === 'locked'
                  return (
                    <div key={stage.num} style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', position: 'relative', opacity: isLock ? 0.4 : 1 }}>
                      {!isLast && (
                        <div style={{ position: 'absolute', top: '14px', left: 'calc(50% + 15px)', width: 'calc(100% - 30px)', height: '2px', background: isDone ? `${success}70` : border, zIndex: 0 }} />
                      )}
                      <div style={{ width: '30px', height: '30px', borderRadius: '50%', background: `${col}18`, border: `2px solid ${col}`, display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1, position: 'relative', flexShrink: 0 }}>
                        {isRun ? <Spinner size={14} /> : <span style={{ color: col, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>{stage.icon}</span>}
                      </div>
                      <div style={{ marginTop: '5px', textAlign: 'center', padding: '0 2px', width: '100%' }}>
                        <div style={{ fontSize: '0.6rem', fontWeight: '600', color: isLock ? muted : text, lineHeight: 1.3, fontFamily: FONT }}>{stage.label}</div>
                        <div style={{ marginTop: '3px', fontSize: '0.55rem', fontWeight: '700', color: col, fontFamily: FONT, textTransform: 'uppercase', letterSpacing: '0.04em' }}>{stgLbl(stage.status)}</div>
                        {stage.ts && <div style={{ marginTop: '2px', fontSize: '0.54rem', color: muted, fontFamily: FONT }}>{fmtDate(stage.ts)}</div>}
                        {stage.metrics.length > 0 && (
                          <div style={{ marginTop: '3px', display: 'flex', flexDirection: 'column', gap: '1px', alignItems: 'center' }}>
                            {stage.metrics.map((m, i) => (
                              <span key={i} style={{ fontSize: '0.54rem', color: muted, fontFamily: FONT, lineHeight: 1.2 }}>{m}</span>
                            ))}
                          </div>
                        )}
                      </div>
                    </div>
                  )
                })}
              </div>
            </div>

            {/* Recent Pipeline Runs */}
            {profHist.data && Array.isArray(profHist.data) && profHist.data.length > 0 && (
              <div style={{ background: surface, border: `1px solid ${border}`, borderRadius: '10px', overflow: 'hidden' }}>
                <div style={{ padding: '8px 16px', borderBottom: `1px solid ${border}`, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                  <span style={{ fontSize: '0.8rem', fontWeight: '700', color: text, fontFamily: FONT }}>Recent Pipeline Runs</span>
                  <button onClick={() => setDsActiveTab('runs')} style={{ background: 'none', border: 'none', color: accent, fontSize: '0.72rem', fontFamily: FONT, cursor: 'pointer', padding: 0 }}>View all →</button>
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 90px 150px 110px', padding: '6px 16px', borderBottom: `1px solid ${border}`, background: bg }}>
                  {['Run Type', 'Status', 'Started', 'Assets'].map(h => (
                    <span key={h} style={{ fontSize: '0.58rem', fontWeight: '700', color: muted, letterSpacing: '0.06em', textTransform: 'uppercase', fontFamily: FONT }}>{h}</span>
                  ))}
                </div>
                {profHist.data.slice(0, 4).map((run, i) => {
                  const runCol = run.status === 'COMPLETE' ? success : run.status === 'RUNNING' ? accent : danger
                  return (
                    <div key={run.id ?? i} style={{ display: 'grid', gridTemplateColumns: '1fr 90px 150px 110px', padding: '6px 16px', borderBottom: i < Math.min(profHist.data.length, 4) - 1 ? `1px solid ${border}20` : 'none', background: i % 2 === 0 ? 'transparent' : `${bg}50` }}>
                      <span style={{ fontSize: '0.72rem', color: textSec, fontFamily: FONT }}>Full Pipeline Run</span>
                      <span style={{ display: 'inline-flex', alignItems: 'center', width: 'fit-content', gap: '3px', padding: '1px 7px', borderRadius: '5px', fontSize: '0.62rem', fontWeight: '700', background: `${runCol}15`, color: runCol, border: `1px solid ${runCol}35`, fontFamily: FONT }}>{run.status ?? '—'}</span>
                      <span style={{ fontSize: '0.73rem', color: muted, fontFamily: FONT }}>{run.created_at ? fmtDate(run.created_at) : '—'}</span>
                      <span style={{ fontSize: '0.73rem', color: textSec, fontFamily: FONT }}>{run.tables_profiled != null ? `${run.tables_profiled} / ${run.tables_total ?? '?'}` : '—'}</span>
                    </div>
                  )
                })}
              </div>
            )}

            {/* Action Required · Recent Activity · Source Configuration */}
            <div style={{ display: 'grid', gridTemplateColumns: '1.5fr 1fr 1fr', gap: '10px', alignItems: 'start' }}>

            {/* Next Best Action */}
            <div style={{ background: surface, border: `1px solid ${nba.urgencyColor}50`, borderLeft: `3px solid ${nba.urgencyColor}`, borderRadius: '10px', padding: '12px' }}>
              <div style={{ marginBottom: '7px' }}>
                <span style={{ fontSize: '0.58rem', fontWeight: '800', color: nba.urgencyColor, letterSpacing: '0.09em', textTransform: 'uppercase', fontFamily: FONT }}>{nba.urgency}</span>
              </div>
              <div style={{ fontSize: '0.8rem', fontWeight: '700', color: text, fontFamily: FONT, marginBottom: '6px', lineHeight: 1.3 }}>{nba.title}</div>
              <p style={{ margin: '0 0 10px', fontSize: '0.7rem', color: muted, fontFamily: FONT, lineHeight: 1.5 }}>{nba.desc}</p>
              {nba.cta && (
                <button
                  onClick={() => { if (nba.act) nba.act(); if (nba.tab) setDsActiveTab(nba.tab) }}
                  style={{ width: '100%', background: nba.urgencyColor === danger ? danger : accent, color: '#fff', border: 'none', borderRadius: '7px', padding: '7px 12px', fontSize: '0.76rem', fontWeight: '600', cursor: 'pointer', fontFamily: FONT, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '6px' }}
                >
                  {nba.cta} →
                </button>
              )}
            </div>

            {/* Recent Activity */}
            <div style={{ background: surface, border: `1px solid ${border}`, borderRadius: '10px', padding: '12px' }}>
              <div style={{ marginBottom: '8px' }}>
                <span style={{ fontSize: '0.62rem', fontWeight: '700', color: muted, letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT }}>Recent Activity</span>
              </div>
              {actItems.length === 0 ? (
                <p style={{ margin: 0, fontSize: '0.7rem', color: muted, fontFamily: FONT }}>No activity recorded yet.</p>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                  {actItems.slice(0, 6).map((item, i) => (
                    <div key={i} style={{ display: 'flex', alignItems: 'center', gap: '8px', minWidth: 0 }}>
                      <span style={{ width: '6px', height: '6px', borderRadius: '50%', background: item.col, flexShrink: 0 }} />
                      <span style={{ flex: 1, minWidth: 0, fontSize: '0.7rem', color: textSec, fontFamily: FONT, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{item.label}</span>
                      <span style={{ fontSize: '0.61rem', color: muted, fontFamily: FONT, flexShrink: 0, whiteSpace: 'nowrap' }}>
                        {item.ts ? fmtRelative(item.ts) : 'Unknown'}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* Source Configuration */}
            <div style={{ background: surface, border: `1px solid ${border}`, borderRadius: '10px', padding: '12px' }}>
              <div style={{ marginBottom: '8px' }}>
                <span style={{ fontSize: '0.62rem', fontWeight: '700', color: muted, letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT }}>Source Configuration</span>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                {cfgRows.map(row => (
                  <div key={row.label} style={{ display: 'grid', gridTemplateColumns: '78px 1fr', gap: '8px', alignItems: 'flex-start' }}>
                    <span style={{ fontSize: '0.64rem', color: muted, fontFamily: FONT, paddingTop: '1px', display: 'flex', alignItems: 'center', gap: '4px' }}>
                      <span style={{ color: muted, display: 'flex', flexShrink: 0 }}>{row.icon}</span>
                      {row.label}
                    </span>
                    <span style={{ fontSize: '0.7rem', color: row.color ?? textSec, fontFamily: FONT, wordBreak: 'break-word' }}>{row.value}</span>
                  </div>
                ))}
              </div>
            </div>

          </div>
        </div>
      )
    })()

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

    const profileDepth   = profSnap?.mode
    const reviewSummary  = reviewTaskSt.data?.summary ?? null
    const reviewTasks    = reviewTaskSt.data?.tasks   ?? []
    const reviewTotal    = reviewTaskSt.data?.total_count ?? 0
    const SEV_COLOR      = { CRITICAL: danger, HIGH: warn, MEDIUM: '#38bdf8', LOW: muted }
    const SEV_BG         = { CRITICAL: `${danger}12`, HIGH: `${warn}12`, MEDIUM: '#38bdf812', LOW: `${muted}10` }

    function handleOpenReview(task) {
      const tab = task.nav_target?.tab
      if (tab && tab !== 'governance' && setDsActiveTab) setDsActiveTab(tab)
    }

    const profileTab = (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>

        {/* ── Profile mode badge ── */}
        {profSnap && (() => {
          const modeIsFull      = profSnap?.mode?.toLowerCase() === 'full'
          const tableDataLoaded = profTables.length > 0
          if (modeIsFull && tableDataLoaded && profTablesStatistical > 0 && profTablesStructOnly > 0) {
            return (
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '9px 14px', borderRadius: '8px', background: `${warn}10`, border: `1px solid ${warn}30` }}>
                <span style={{ fontSize: '0.65rem', fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, color: warn, flexShrink: 0 }}>Partial Statistics</span>
                <span style={{ fontSize: '0.75rem', color: textSec, fontFamily: FONT }}>
                  Full profiling run exists, but only {profTablesStatistical} table{profTablesStatistical !== 1 ? 's' : ''} have row-level statistics. {profTablesStructOnly} table{profTablesStructOnly !== 1 ? 's' : ''} are structural-only.
                </span>
              </div>
            )
          }
          if (modeIsFull && tableDataLoaded && profTablesStatistical === 0) {
            return (
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '9px 14px', borderRadius: '8px', background: `${warn}10`, border: `1px solid ${warn}30` }}>
                <span style={{ fontSize: '0.65rem', fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, color: warn, flexShrink: 0 }}>No Statistics Captured</span>
                <span style={{ fontSize: '0.75rem', color: textSec, fontFamily: FONT }}>
                  Full profile mode was used, but no tables have row-level statistics. Tables may have exceeded row count limits.
                </span>
              </div>
            )
          }
          if (modeIsFull) {
            return (
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '9px 14px', borderRadius: '8px', background: `${success}10`, border: `1px solid ${success}30` }}>
                <span style={{ fontSize: '0.65rem', fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, color: success, flexShrink: 0 }}>Full Profile</span>
                <span style={{ fontSize: '0.75rem', color: textSec, fontFamily: FONT }}>
                  Includes statistical analysis: row counts, null rates, distributions, and PII detection.
                </span>
              </div>
            )
          }
          return (
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '9px 14px', borderRadius: '8px', background: `${warn}10`, border: `1px solid ${warn}30` }}>
              <span style={{ fontSize: '0.65rem', fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, color: warn, flexShrink: 0 }}>Structural Only</span>
              <span style={{ fontSize: '0.75rem', color: textSec, fontFamily: FONT }}>
                Schema structure only — no row-level statistics. Use Run Data Profile for full analysis.
              </span>
            </div>
          )
        })()
        }


        <ColumnProfileExplorer
          C={C}
          token={token}
          sourceId={dsSelectedSourceId}
          profileData={prof.data}
          profLoading={prof.loading}
          profError={prof.error}
          hasSchema={hasSchema}
          onRunProfile={() => src && handleDiscoverAndProfile(src)}
          profileRunning={js.running || discSt.loading}
        />
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

    // ── Governance Command Center: collapsible card wrapper ─────────────────────
    const cmdCard = (id, title, badge, body) => (
      <div style={card({ padding: '0' })}>
        <button
          onClick={() => toggleCmdSection(dsSelectedSourceId, id)}
          style={{ width: '100%', display: 'flex', alignItems: 'center', gap: '8px', padding: '12px 16px', background: 'transparent', border: 'none', cursor: 'pointer', textAlign: 'left', fontFamily: FONT }}
        >
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke={muted} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{ transform: cmdCenterExpanded[id] ? 'rotate(90deg)' : 'none', transition: 'transform 0.15s', flexShrink: 0 }}><polyline points="9 18 15 12 9 6" /></svg>
          <span style={{ fontSize: '0.84rem', fontWeight: '700', color: text, fontFamily: FONT }}>{title}</span>
          {badge}
        </button>
        {cmdCenterExpanded[id] && <div style={{ padding: '0 16px 16px' }}>{body}</div>}
      </div>
    )

    const cmdStat = (label, value, color) => (
      <div key={label}>
        <div style={{ fontSize: '0.6rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '3px' }}>{label}</div>
        <div style={{ fontSize: '1.1rem', fontWeight: '700', color: color ?? text, fontFamily: FONT, lineHeight: 1 }}>{value}</div>
      </div>
    )

    const cmdPriorityBadge = p => (
      <span style={{ padding: '1px 7px', borderRadius: '6px', fontSize: '0.6rem', fontWeight: '700', letterSpacing: '0.04em', background: SEV_BG[p] ?? `${muted}10`, color: SEV_COLOR[p] ?? muted, border: `1px solid ${(SEV_COLOR[p] ?? muted)}35`, fontFamily: FONT, textTransform: 'uppercase' }}>{p}</span>
    )

    // ── Section data (Executive Dashboard / Recommendations / Bottlenecks share one fetch) ──
    const cmdDash      = cmdCenterState[_cmdKey(dsSelectedSourceId, 'dashboard')] ?? {}
    const cmdRecFb      = cmdCenterState[_cmdKey(dsSelectedSourceId, 'recommendations')] ?? {}
    const cmdBotFb      = cmdCenterState[_cmdKey(dsSelectedSourceId, 'bottlenecks')] ?? {}
    const cmdAssign     = cmdCenterState[_cmdKey(dsSelectedSourceId, 'assignments')] ?? {}
    const cmdPolicies   = cmdCenterState['global::policies'] ?? {}
    const execSummary   = cmdDash.data?.executive_summary ?? null
    const execKpis      = cmdDash.data?.kpis ?? null
    const recommendations = cmdDash.data?.recommendations ?? cmdRecFb.data ?? null
    const recLoading       = cmdDash.loading || cmdRecFb.loading
    const recError         = cmdRecFb.error
    const bottlenecks      = cmdDash.data?.bottlenecks ?? cmdBotFb.data ?? null
    const botLoading       = cmdDash.loading || cmdBotFb.loading
    const botError         = cmdBotFb.error
    const bulkF   = bulkForm[dsSelectedSourceId]   ?? { object_type: 'dict.table', exclude_pii: true }
    const bulkR   = bulkResult[dsSelectedSourceId] ?? {}
    const explainF = explainForm[dsSelectedSourceId] ?? { object_type: 'dict.table' }
    const explainR = explainState[dsSelectedSourceId] ?? {}
    const assignF  = assignFilters[dsSelectedSourceId] ?? { priority: '', status: '', steward: '' }

    // ── Section 1: Executive Dashboard ──────────────────────────────────────────
    const cmdExecBody = cmdDash.loading ? (
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: muted, fontSize: '0.8rem', fontFamily: FONT, padding: '4px 0' }}><Spinner size={12} /> Loading dashboard…</div>
    ) : cmdDash.error ? (
      <p style={{ margin: 0, fontSize: '0.78rem', color: danger, fontFamily: FONT }}>{cmdDash.error}</p>
    ) : (!execSummary || !execKpis) ? (
      <p style={{ margin: 0, fontSize: '0.78rem', color: muted, fontFamily: FONT }}>No governance data yet.</p>
    ) : (
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '22px' }}>
        {cmdStat('Governance Score', `${execSummary.governance_score}/100`, execSummary.governance_score >= 70 ? success : execSummary.governance_score >= 40 ? warn : danger)}
        {cmdStat('Readiness Score',  `${execSummary.governance_score}/100`, execSummary.governance_score >= 70 ? success : execSummary.governance_score >= 40 ? warn : danger)}
        {cmdStat('Human Approved %', `${execKpis.human_approved_pct}%`)}
        {cmdStat('Auto Approved %',  `${execKpis.auto_approved_pct}%`)}
        {cmdStat('Pending',          `${execKpis.pending_pct}%`, execKpis.pending_pct > 0 ? warn : muted)}
        {cmdStat('Blocked',          `${execKpis.blocked_pct}%`, execKpis.blocked_pct > 0 ? danger : muted)}
        {cmdStat('Critical Backlog', execKpis.critical_backlog, execKpis.critical_backlog > 0 ? danger : muted)}
        {cmdStat('Avg Confidence',   execKpis.avg_confidence != null ? `${Math.round(execKpis.avg_confidence * 100)}%` : '—')}
        {cmdStat('Avg Risk',         execKpis.avg_risk_score ?? '—')}
        {cmdStat('Open Assignments', execKpis.open_assignments)}
        {cmdStat('Overdue Assignments', execKpis.overdue_assignments, execKpis.overdue_assignments > 0 ? danger : muted)}
      </div>
    )

    // ── Section 2: Recommendations ──────────────────────────────────────────────
    const cmdRecBody = recLoading ? (
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: muted, fontSize: '0.8rem', fontFamily: FONT, padding: '4px 0' }}><Spinner size={12} /> Loading recommendations…</div>
    ) : recError ? (
      <p style={{ margin: 0, fontSize: '0.78rem', color: danger, fontFamily: FONT }}>{recError}</p>
    ) : !recommendations || recommendations.length === 0 ? (
      <p style={{ margin: 0, fontSize: '0.78rem', color: muted, fontFamily: FONT }}>No active recommendations — governance is in good shape.</p>
    ) : (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
        {recommendations.map(r => (
          <div key={r.id} style={{ border: `1px solid ${border}`, borderRadius: '8px', background: `${accent}06` }}>
            <button
              onClick={() => setRecExpanded(s => ({ ...s, [r.id]: !s[r.id] }))}
              style={{ width: '100%', display: 'flex', alignItems: 'center', gap: '8px', padding: '8px 12px', background: 'transparent', border: 'none', cursor: 'pointer', textAlign: 'left', fontFamily: FONT }}
            >
              {cmdPriorityBadge(r.priority)}
              <span style={{ fontSize: '0.8rem', fontWeight: '600', color: text, flex: 1 }}>{r.title}</span>
              <span style={{ fontSize: '0.68rem', color: muted, fontFamily: MONO }}>{r.affected_count} item{r.affected_count !== 1 ? 's' : ''}</span>
            </button>
            {recExpanded[r.id] && (
              <div style={{ padding: '0 12px 10px', display: 'flex', flexDirection: 'column', gap: '4px' }}>
                <div style={{ fontSize: '0.74rem', color: textSec, fontFamily: FONT, lineHeight: 1.5 }}>{r.description}</div>
                <div style={{ fontSize: '0.68rem', color: muted, fontFamily: MONO }}>Expected impact: {r.affected_count} item{r.affected_count !== 1 ? 's' : ''}</div>
                {r.action_endpoint && <div style={{ fontSize: '0.66rem', color: muted, fontFamily: MONO }}>{r.action_endpoint}</div>}
              </div>
            )}
          </div>
        ))}
      </div>
    )

    // ── Section 3: Assignments ──────────────────────────────────────────────────
    const cmdAssignItems = cmdAssign.data?.items ?? []
    const cmdAssignFiltered = cmdAssignItems.filter(a =>
      (!assignF.priority || a.priority === assignF.priority) &&
      (!assignF.status   || a.status   === assignF.status) &&
      (!assignF.steward  || a.assigned_to === assignF.steward)
    )
    const cmdStewards = [...new Set(cmdAssignItems.map(a => a.assigned_to).filter(Boolean))]
    const cmdAssignBody = cmdAssign.loading ? (
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: muted, fontSize: '0.8rem', fontFamily: FONT, padding: '4px 0' }}><Spinner size={12} /> Loading assignments…</div>
    ) : cmdAssign.error ? (
      <p style={{ margin: 0, fontSize: '0.78rem', color: danger, fontFamily: FONT }}>{cmdAssign.error}</p>
    ) : (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
        {cmdAssign.data?.summary && (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '18px' }}>
            {cmdStat('Open', cmdAssign.data.summary.open)}
            {cmdStat('Overdue', cmdAssign.data.summary.overdue, cmdAssign.data.summary.overdue > 0 ? danger : muted)}
            {cmdStat('Critical Backlog', cmdAssign.data.summary.critical_backlog, cmdAssign.data.summary.critical_backlog > 0 ? danger : muted)}
            {cmdStat('Completed Today', cmdAssign.data.summary.completed_today)}
            {cmdStat('Avg Resolution (days)', cmdAssign.data.summary.avg_resolution_days ?? '—')}
          </div>
        )}
        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
          <select value={assignF.priority} onChange={e => setAssignFilters(s => ({ ...s, [dsSelectedSourceId]: { ...assignF, priority: e.target.value } }))} style={{ ...inp({ width: 'auto' }), padding: '5px 10px', fontSize: '0.76rem' }}>
            <option value="">All priorities</option>
            {['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'].map(p => <option key={p} value={p}>{p}</option>)}
          </select>
          <select value={assignF.status} onChange={e => setAssignFilters(s => ({ ...s, [dsSelectedSourceId]: { ...assignF, status: e.target.value } }))} style={{ ...inp({ width: 'auto' }), padding: '5px 10px', fontSize: '0.76rem' }}>
            <option value="">All statuses</option>
            <option value="OPEN">OPEN</option>
            <option value="COMPLETED">COMPLETED</option>
          </select>
          <select value={assignF.steward} onChange={e => setAssignFilters(s => ({ ...s, [dsSelectedSourceId]: { ...assignF, steward: e.target.value } }))} style={{ ...inp({ width: 'auto' }), padding: '5px 10px', fontSize: '0.76rem' }}>
            <option value="">All stewards</option>
            {cmdStewards.map(st => <option key={st} value={st}>{st}</option>)}
          </select>
          <button onClick={() => ensureCmdAssignments(dsSelectedSourceId, true)} style={{ ...btnGhost({ padding: '5px 12px', fontSize: '0.76rem' }) }}>Refresh</button>
        </div>
        {cmdAssignFiltered.length === 0 ? (
          <p style={{ margin: 0, fontSize: '0.76rem', color: muted, fontFamily: FONT }}>No assignments match the current filters.</p>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
            {cmdAssignFiltered.map(a => (
              <div key={a.id} style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap', padding: '7px 10px', borderRadius: '6px', border: `1px solid ${border}`, background: a.sla?.sla_status === 'OVERDUE' ? `${danger}08` : 'transparent' }}>
                {cmdPriorityBadge(a.priority)}
                <span style={{ fontSize: '0.76rem', color: text, fontFamily: MONO }}>{a.object_type}#{a.object_id}</span>
                <span style={{ fontSize: '0.73rem', color: textSec }}>{a.assigned_to}</span>
                <span style={{ fontSize: '0.68rem', color: muted, fontFamily: FONT }}>{a.status}</span>
                <span style={{ fontSize: '0.68rem', color: muted, fontFamily: MONO }}>due {fmtDate(a.due_date) ?? '—'}</span>
                {a.sla?.sla_status === 'OVERDUE' && <span style={{ padding: '1px 6px', borderRadius: '6px', fontSize: '0.58rem', fontWeight: '700', background: `${danger}20`, color: danger, fontFamily: FONT }}>OVERDUE</span>}
              </div>
            ))}
          </div>
        )}
      </div>
    )

    // ── Section 4: Policies ──────────────────────────────────────────────────────
    const cmdPolicyList = cmdPolicies.data ?? null
    const cmdPolicyBody = cmdPolicies.loading ? (
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: muted, fontSize: '0.8rem', fontFamily: FONT, padding: '4px 0' }}><Spinner size={12} /> Loading policies…</div>
    ) : cmdPolicies.error ? (
      <p style={{ margin: 0, fontSize: '0.78rem', color: danger, fontFamily: FONT }}>{cmdPolicies.error}</p>
    ) : !cmdPolicyList || cmdPolicyList.length === 0 ? (
      <p style={{ margin: 0, fontSize: '0.78rem', color: muted, fontFamily: FONT }}>No governance policies configured.</p>
    ) : (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
        <p style={{ margin: '0 0 4px', fontSize: '0.68rem', color: muted, fontFamily: FONT, fontStyle: 'italic' }}>Read-only — no editing in this phase.</p>
        {cmdPolicyList.map(p => (
          <div key={p.id} style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap', padding: '7px 10px', borderRadius: '6px', border: `1px solid ${border}` }}>
            <span style={{ fontSize: '0.78rem', fontWeight: '600', color: text, fontFamily: FONT, flex: '1 1 auto' }}>{p.policy_name}</span>
            <span style={{ padding: '1px 7px', borderRadius: '6px', fontSize: '0.6rem', fontWeight: '700', background: p.enabled ? `${success}20` : `${muted}20`, color: p.enabled ? success : muted, fontFamily: FONT }}>{p.enabled ? 'ENABLED' : 'DISABLED'}</span>
            <span style={{ fontSize: '0.68rem', color: muted, fontFamily: MONO }}>priority {p.priority}</span>
            <span style={{ fontSize: '0.7rem', color: accent, fontFamily: MONO }}>{p.action}</span>
            <span style={{ fontSize: '0.68rem', color: textSec, fontFamily: MONO }}>{(p.object_types ?? []).join(', ') || 'all types'}</span>
          </div>
        ))}
      </div>
    )

    // ── Section 5: Bulk Governance ───────────────────────────────────────────────
    const cmdBulkBody = (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
          <select value={bulkF.object_type} onChange={e => updateBulkForm(dsSelectedSourceId, { object_type: e.target.value })} style={{ ...inp({ width: 'auto' }), padding: '5px 10px', fontSize: '0.76rem' }}>
            {['dict.table', 'dict.column', 'domain.rule', 'entity.rule', 'domain.refinement'].map(t => <option key={t} value={t}>{t}</option>)}
          </select>
          <input type="number" placeholder="Min confidence" min="0" max="1" step="0.05" value={bulkF.confidence_min ?? ''} onChange={e => updateBulkForm(dsSelectedSourceId, { confidence_min: e.target.value === '' ? null : Number(e.target.value) })} style={{ ...inp({ width: '120px' }), padding: '5px 10px', fontSize: '0.76rem' }} />
          <label style={{ display: 'flex', alignItems: 'center', gap: '5px', fontSize: '0.74rem', color: textSec, fontFamily: FONT }}>
            <input type="checkbox" checked={bulkF.exclude_pii !== false} onChange={e => updateBulkForm(dsSelectedSourceId, { exclude_pii: e.target.checked })} /> Exclude PII
          </label>
          <button onClick={() => handleBulkDryRun(dsSelectedSourceId)} disabled={bulkR.loading} style={{ ...btnGhost({ padding: '5px 12px', fontSize: '0.76rem' }), color: !bulkR.loading ? accent : `${muted}50`, borderColor: !bulkR.loading ? `${accent}55` : border }}>{bulkR.loading ? 'Running…' : 'Preview (Dry Run)'}</button>
        </div>
        {bulkR.error && <p style={{ margin: 0, fontSize: '0.76rem', color: danger, fontFamily: FONT }}>{bulkR.error}</p>}
        {bulkR.dryRun && (
          <div style={{ padding: '10px 12px', borderRadius: '8px', border: `1px solid ${border}`, background: `${accent}06`, display: 'flex', flexDirection: 'column', gap: '8px' }}>
            <div style={{ display: 'flex', gap: '20px', flexWrap: 'wrap' }}>
              {cmdStat('Candidates', bulkR.dryRun.total_candidates)}
              {cmdStat('Affected', bulkR.dryRun.affected_count, success)}
              {cmdStat('Blocked', bulkR.dryRun.blocked_count, bulkR.dryRun.blocked_count > 0 ? danger : muted)}
            </div>
            {bulkR.dryRun.blocked_items?.length > 0 && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                {bulkR.dryRun.blocked_items.slice(0, 10).map((b, i) => (
                  <div key={i} style={{ fontSize: '0.68rem', color: muted, fontFamily: MONO }}>{b.object_type_id}#{b.object_id} — {b.blocking_policy}: {b.reason}</div>
                ))}
              </div>
            )}
            <div style={{ display: 'flex', gap: '8px' }}>
              <button onClick={() => handleBulkRequestConfirm(dsSelectedSourceId, 'approve')} disabled={bulkR.loading || bulkR.dryRun.affected_count === 0} style={{ background: `${success}20`, color: success, border: `1px solid ${success}40`, borderRadius: '6px', padding: '5px 14px', fontSize: '0.76rem', fontWeight: '600', cursor: bulkR.dryRun.affected_count === 0 ? 'not-allowed' : 'pointer', fontFamily: FONT }}>Approve {bulkR.dryRun.affected_count} Item{bulkR.dryRun.affected_count !== 1 ? 's' : ''}</button>
              <button onClick={() => handleBulkRequestConfirm(dsSelectedSourceId, 'reject')} disabled={bulkR.loading || bulkR.dryRun.affected_count === 0} style={{ background: 'transparent', color: danger, border: `1px solid ${danger}40`, borderRadius: '6px', padding: '5px 14px', fontSize: '0.76rem', fontWeight: '600', cursor: bulkR.dryRun.affected_count === 0 ? 'not-allowed' : 'pointer', fontFamily: FONT }}>Reject {bulkR.dryRun.affected_count} Item{bulkR.dryRun.affected_count !== 1 ? 's' : ''}</button>
            </div>
          </div>
        )}
        {bulkR.confirmAction && (
          <div style={{ padding: '12px 14px', borderRadius: '8px', border: `1px solid ${warn}40`, background: `${warn}0d`, display: 'flex', flexDirection: 'column', gap: '8px' }}>
            <span style={{ fontSize: '0.8rem', color: text, fontFamily: FONT, fontWeight: '600' }}>
              Confirm bulk {bulkR.confirmAction}: {bulkR.dryRun?.affected_count ?? 0} {bulkF.object_type} item{(bulkR.dryRun?.affected_count ?? 0) !== 1 ? 's' : ''}?
            </span>
            <span style={{ fontSize: '0.72rem', color: muted, fontFamily: FONT }}>This action writes to governance state and cannot be undone from this screen.</span>
            <div style={{ display: 'flex', gap: '8px' }}>
              <button onClick={() => handleBulkExecute(dsSelectedSourceId, bulkR.confirmAction)} style={{ background: bulkR.confirmAction === 'approve' ? success : danger, color: '#fff', border: 'none', borderRadius: '6px', padding: '5px 14px', fontSize: '0.76rem', fontWeight: '600', cursor: 'pointer', fontFamily: FONT }}>Confirm</button>
              <button onClick={() => handleBulkCancelConfirm(dsSelectedSourceId)} style={btnGhost({ padding: '5px 14px', fontSize: '0.76rem' })}>Cancel</button>
            </div>
          </div>
        )}
        {(bulkR.approveResult || bulkR.rejectResult) && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
            {bulkR.approveResult && <span style={{ fontSize: '0.74rem', color: success, fontFamily: FONT }}>Approved {bulkR.approveResult.affected_count} item(s), {bulkR.approveResult.blocked_count} blocked.</span>}
            {bulkR.rejectResult && <span style={{ fontSize: '0.74rem', color: danger, fontFamily: FONT }}>Rejected {bulkR.rejectResult.affected_count} item(s), {bulkR.rejectResult.blocked_count} blocked.</span>}
          </div>
        )}
      </div>
    )

    // ── Section 6: Decision Intelligence ─────────────────────────────────────────
    const _GOV_TYPES = ['dict.table', 'dict.column', 'domain.rule', 'entity.rule', 'domain.refinement', 'tool.engine', 'pii.confirmation']
    const cmdDecisionBody = (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
          <select value={explainF.object_type} onChange={e => updateExplainForm(dsSelectedSourceId, { object_type: e.target.value })} style={{ ...inp({ width: 'auto' }), padding: '5px 10px', fontSize: '0.76rem' }}>
            {_GOV_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
          </select>
          {['dict.table', 'dict.column', 'pii.confirmation'].includes(explainF.object_type) && (
            <input placeholder="table_fqn" value={explainF.table_fqn ?? ''} onChange={e => updateExplainForm(dsSelectedSourceId, { table_fqn: e.target.value })} style={{ ...inp({ width: '180px' }), padding: '5px 10px', fontSize: '0.76rem' }} />
          )}
          {['dict.column', 'pii.confirmation'].includes(explainF.object_type) && (
            <input placeholder="column_name" value={explainF.column_name ?? ''} onChange={e => updateExplainForm(dsSelectedSourceId, { column_name: e.target.value })} style={{ ...inp({ width: '140px' }), padding: '5px 10px', fontSize: '0.76rem' }} />
          )}
          {['domain.rule', 'entity.rule'].includes(explainF.object_type) && (
            <input type="number" placeholder="rule_id" value={explainF.rule_id ?? ''} onChange={e => updateExplainForm(dsSelectedSourceId, { rule_id: e.target.value === '' ? null : Number(e.target.value) })} style={{ ...inp({ width: '100px' }), padding: '5px 10px', fontSize: '0.76rem' }} />
          )}
          {explainF.object_type === 'domain.refinement' && (
            <input type="number" placeholder="suggestion_id" value={explainF.suggestion_id ?? ''} onChange={e => updateExplainForm(dsSelectedSourceId, { suggestion_id: e.target.value === '' ? null : Number(e.target.value) })} style={{ ...inp({ width: '120px' }), padding: '5px 10px', fontSize: '0.76rem' }} />
          )}
          {explainF.object_type === 'tool.engine' && (
            <input placeholder="tool_id" value={explainF.tool_id ?? ''} onChange={e => updateExplainForm(dsSelectedSourceId, { tool_id: e.target.value })} style={{ ...inp({ width: '140px' }), padding: '5px 10px', fontSize: '0.76rem' }} />
          )}
          <button onClick={() => handleExplain(dsSelectedSourceId)} disabled={explainR.loading} style={{ ...btnGhost({ padding: '5px 12px', fontSize: '0.76rem' }), color: !explainR.loading ? accent : `${muted}50`, borderColor: !explainR.loading ? `${accent}55` : border }}>{explainR.loading ? 'Explaining…' : 'Explain'}</button>
        </div>
        {explainR.error && <p style={{ margin: 0, fontSize: '0.76rem', color: danger, fontFamily: FONT }}>{explainR.error}</p>}
        {explainR.data && (
          <div style={{ padding: '10px 12px', borderRadius: '8px', border: `1px solid ${border}`, background: `${accent}06`, display: 'flex', flexDirection: 'column', gap: '8px' }}>
            <span style={{ fontSize: '0.82rem', fontWeight: '600', color: text, fontFamily: FONT }}>{explainR.data.decision}</span>
            <div style={{ display: 'flex', gap: '20px', flexWrap: 'wrap' }}>
              {cmdStat('Risk Score', explainR.data.risk_score, explainR.data.risk_score >= 70 ? danger : explainR.data.risk_score >= 40 ? warn : success)}
              {cmdStat('Confidence', explainR.data.confidence_score != null ? `${Math.round(explainR.data.confidence_score * 100)}%` : '—')}
              {cmdStat('Est. Review Time', `${explainR.data.estimated_review_minutes}m`)}
            </div>
            <div style={{ fontSize: '0.74rem', color: textSec, fontFamily: FONT }}><strong>Recommended action:</strong> {explainR.data.recommended_action}</div>
            {explainR.data.recommended_steward && <div style={{ fontSize: '0.74rem', color: textSec, fontFamily: FONT }}><strong>Recommended steward:</strong> {explainR.data.recommended_steward}</div>}
            <div style={{ fontSize: '0.74rem', color: textSec, fontFamily: FONT }}><strong>Priority reason:</strong> {explainR.data.priority_reason}</div>
            {explainR.data.matched_policies?.length > 0 && <div style={{ fontSize: '0.72rem', color: muted, fontFamily: MONO }}>Matched policies: {explainR.data.matched_policies.join(', ')}</div>}
            {explainR.data.blocking_policies?.length > 0 && <div style={{ fontSize: '0.72rem', color: danger, fontFamily: MONO }}>Blocking policies: {explainR.data.blocking_policies.join(', ')}</div>}
            {explainR.data.evidence?.length > 0 && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                <span style={{ fontSize: '0.66rem', color: muted, fontWeight: '700', letterSpacing: '0.06em', textTransform: 'uppercase', fontFamily: FONT }}>Evidence</span>
                {explainR.data.evidence.map((ev, i) => <div key={i} style={{ fontSize: '0.7rem', color: textSec, fontFamily: MONO }}>{JSON.stringify(ev)}</div>)}
              </div>
            )}
          </div>
        )}
      </div>
    )

    // ── Section 7: Bottlenecks ───────────────────────────────────────────────────
    const cmdBotBody = botLoading ? (
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: muted, fontSize: '0.8rem', fontFamily: FONT, padding: '4px 0' }}><Spinner size={12} /> Loading bottlenecks…</div>
    ) : botError ? (
      <p style={{ margin: 0, fontSize: '0.78rem', color: danger, fontFamily: FONT }}>{botError}</p>
    ) : !bottlenecks ? (
      <p style={{ margin: 0, fontSize: '0.78rem', color: muted, fontFamily: FONT }}>No bottleneck data yet.</p>
    ) : (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
        {[
          { label: 'Largest Pending Queues', items: bottlenecks.pending_by_type, render: x => `${x.object_type}: ${x.pending_count} pending` },
          { label: 'Most Overdue Stewards',  items: bottlenecks.overdue_stewards, render: x => `${x.assigned_to}: ${x.overdue_count} overdue (oldest ${x.oldest_days_overdue}d)` },
          { label: 'Lowest-Confidence Areas', items: bottlenecks.low_confidence_areas, render: x => `${x.object_type}: ${x.low_conf_count} low-confidence` },
          { label: 'Policy Blocks', items: bottlenecks.active_blocking_policies, render: x => `${x.policy_name} (${x.action}, priority ${x.priority})` },
          { label: 'Pending Domain Queues', items: bottlenecks.pending_domains, render: x => `${x.domain}: ${x.pending_count} pending` },
        ].filter(g => g.items && g.items.length > 0).map(g => (
          <div key={g.label}>
            {govSectionLabel(g.label, g.items.length)}
            <div style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
              {g.items.map((x, i) => <div key={i} style={{ fontSize: '0.74rem', color: textSec, fontFamily: FONT }}>{g.render(x)}</div>)}
            </div>
          </div>
        ))}
        {['pending_by_type', 'overdue_stewards', 'low_confidence_areas', 'active_blocking_policies', 'pending_domains'].every(k => !bottlenecks[k] || bottlenecks[k].length === 0) && (
          <p style={{ margin: 0, fontSize: '0.78rem', color: muted, fontFamily: FONT }}>No bottlenecks detected.</p>
        )}
      </div>
    )

    const governanceTab = (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>

        {/* ── Governance Command Center ── */}
        {cmdCard('exec',            'Executive Dashboard',     null, cmdExecBody)}
        {cmdCard('recommendations', 'Recommendations',         recommendations?.length > 0 ? <span style={{ padding: '1px 7px', borderRadius: '8px', fontSize: '0.62rem', fontWeight: '700', background: `${accent}20`, color: accent, fontFamily: FONT }}>{recommendations.length}</span> : null, cmdRecBody)}
        {cmdCard('assignments',     'Assignments',              null, cmdAssignBody)}
        {cmdCard('policies',        'Policies',                 null, cmdPolicyBody)}
        {cmdCard('bulk',            'Bulk Governance',          null, cmdBulkBody)}
        {cmdCard('decision',        'Decision Intelligence',    null, cmdDecisionBody)}
        {cmdCard('bottlenecks',     'Bottlenecks',               null, cmdBotBody)}

        {cmdCard('workqueue', 'Governance Work Queue', null, (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>

        {/* ── Quality Review Tasks (from profiling) ── */}
        {reviewSummary && (
          <div style={{ ...card({ padding: '14px 18px' }) }}>
            <div style={{ fontSize: '0.68rem', fontWeight: '700', color: muted, letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '10px' }}>Quality Review Summary</div>
            <div style={{ display: 'flex', gap: '28px', flexWrap: 'wrap', alignItems: 'flex-end' }}>
              {[
                { label: 'Open Tasks', value: reviewSummary.open,      color: reviewSummary.open > 0 ? warn : success },
                { label: 'Critical',   value: reviewSummary.critical,  color: reviewSummary.critical > 0 ? danger : muted },
                { label: 'High',       value: reviewSummary.high,      color: reviewSummary.high > 0 ? warn : muted },
                { label: 'Medium',     value: reviewSummary.medium,    color: reviewSummary.medium > 0 ? '#38bdf8' : muted },
                { label: 'Low',        value: reviewSummary.low,       color: muted },
                { label: 'Completed',  value: reviewSummary.completed, color: success },
              ].map(({ label, value, color }) => (
                <div key={label}>
                  <div style={{ fontSize: '0.6rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '3px' }}>{label}</div>
                  <div style={{ fontSize: '1.25rem', fontWeight: '700', color, fontFamily: FONT, lineHeight: 1 }}>{value}</div>
                </div>
              ))}
            </div>
          </div>
        )}
        {reviewTaskSt.loading && !reviewSummary && (
          <div style={{ ...card({ padding: '14px 18px' }), display: 'flex', alignItems: 'center', gap: '8px', color: muted, fontSize: '0.8rem', fontFamily: FONT }}>
            <Spinner size={12} /> Loading review tasks…
          </div>
        )}

        {(reviewTaskSt.data != null) && (
          <div style={card({ padding: '0' })}>
            <div style={{ padding: '12px 18px', borderBottom: `1px solid ${border}`, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <span style={{ fontSize: '0.68rem', fontWeight: '700', color: muted, letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT }}>Quality Review Tasks</span>
              {reviewTotal > 0 && (
                <span style={{ fontSize: '0.72rem', color: muted, fontFamily: FONT }}>{reviewTotal.toLocaleString()} task{reviewTotal !== 1 ? 's' : ''}</span>
              )}
            </div>

            {reviewTasks.length === 0 && (
              <div style={{ padding: '28px 18px', textAlign: 'center', color: muted, fontSize: '0.82rem', fontFamily: FONT }}>
                No review tasks generated.
              </div>
            )}

            {reviewTasks.length > 0 && (
              <div>
                {['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'].map(sev => {
                  const sevTasks = reviewTasks.filter(t => t.severity === sev)
                  if (sevTasks.length === 0) return null
                  return (
                    <div key={sev}>
                      <div style={{ padding: '6px 18px', background: SEV_BG[sev], borderBottom: `1px solid ${border}20`, display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <span style={{ fontSize: '0.62rem', fontWeight: '800', color: SEV_COLOR[sev], letterSpacing: '0.08em', textTransform: 'uppercase', fontFamily: FONT }}>{sev}</span>
                        <span style={{ fontSize: '0.65rem', color: muted, fontFamily: FONT }}>{sevTasks.length} task{sevTasks.length !== 1 ? 's' : ''}</span>
                      </div>
                      {sevTasks.map((task, i) => (
                        <div key={task.id} style={{ display: 'grid', gridTemplateColumns: '140px 1fr 1fr auto', gap: '0', padding: '10px 18px', borderBottom: `1px solid ${border}15`, background: i % 2 === 0 ? 'transparent' : `${bg}50`, alignItems: 'start' }}>
                          <div>
                            <span style={{ fontSize: '0.72rem', fontWeight: '600', color: SEV_COLOR[sev], fontFamily: FONT }}>{task.task_type}</span>
                          </div>
                          <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                            <span style={{ fontSize: '0.78rem', color: text, fontFamily: MONO, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                              {task.column_name ? `${task.table_fqn}.${task.column_name}` : task.table_fqn}
                            </span>
                            <span style={{ fontSize: '0.68rem', color: muted, fontFamily: FONT, textTransform: 'capitalize' }}>{task.asset_type}</span>
                          </div>
                          <div style={{ fontSize: '0.74rem', color: textSec, fontFamily: FONT, paddingRight: '12px', lineHeight: 1.45 }}>
                            {task.reason}
                          </div>
                          <button
                            onClick={() => handleOpenReview(task)}
                            style={{ padding: '4px 12px', borderRadius: '6px', background: 'transparent', border: `1px solid ${border}`, color: accent, fontSize: '0.73rem', fontWeight: '600', cursor: 'pointer', fontFamily: FONT, whiteSpace: 'nowrap' }}
                            onMouseEnter={e => e.currentTarget.style.background = accentSoft}
                            onMouseLeave={e => e.currentTarget.style.background = 'transparent'}
                          >
                            Open Review →
                          </button>
                        </div>
                      ))}
                    </div>
                  )
                })}
                {reviewTotal > _REVIEW_PAGE_SIZE && (
                  <div style={{ padding: '10px 18px', borderTop: `1px solid ${border}`, display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px' }}>
                    <button
                      onClick={() => loadReviewTasks(dsSelectedSourceId, reviewCurrentPage - 1)}
                      disabled={reviewCurrentPage === 0 || reviewTaskSt.loading}
                      style={{ ...btnGhost({ padding: '5px 14px', fontSize: '0.76rem' }), color: reviewCurrentPage === 0 ? `${muted}50` : textSec, cursor: reviewCurrentPage === 0 ? 'not-allowed' : 'pointer' }}
                    >
                      ← Previous
                    </button>
                    <span style={{ fontSize: '0.72rem', color: muted, fontFamily: FONT }}>
                      {reviewCurrentPage * _REVIEW_PAGE_SIZE + 1}–{Math.min((reviewCurrentPage + 1) * _REVIEW_PAGE_SIZE, reviewTotal)} of {reviewTotal.toLocaleString()}
                      {reviewTaskSt.loading && <Spinner size={10} style={{ marginLeft: '6px' }} />}
                    </span>
                    <button
                      onClick={() => loadReviewTasks(dsSelectedSourceId, reviewCurrentPage + 1)}
                      disabled={(reviewCurrentPage + 1) * _REVIEW_PAGE_SIZE >= reviewTotal || reviewTaskSt.loading}
                      style={{ ...btnGhost({ padding: '5px 14px', fontSize: '0.76rem' }), color: (reviewCurrentPage + 1) * _REVIEW_PAGE_SIZE >= reviewTotal ? `${muted}50` : textSec, cursor: (reviewCurrentPage + 1) * _REVIEW_PAGE_SIZE >= reviewTotal ? 'not-allowed' : 'pointer' }}
                    >
                      Next →
                    </button>
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {s6 === 'locked' && (<div style={{ ...card({ padding: '32px 24px' }), textAlign: 'center' }}><p style={{ margin: '0 0 6px', fontSize: '0.88rem', color: textSec, fontWeight: '500', fontFamily: FONT }}>Governance rules not available yet</p><p style={{ margin: 0, fontSize: '0.78rem', color: muted, fontFamily: FONT }}>Complete the pipeline through Entities before governance rules can be generated.</p></div>)}
        {s6 !== 'locked' && (
          <>
            {govSection({ title: 'Domain Refinements', pendingCount: pendingRefs, loadFn: () => loadRefinements(dsSelectedSourceId), busyFlag: govBusy, analyzeBtn: <button onClick={() => handleAnalyzeRefinements(dsSelectedSourceId)} disabled={govBusy} style={{ ...btnGhost({ padding: '4px 10px', fontSize: '0.74rem' }), color: !govBusy ? warn : `${muted}50`, borderColor: !govBusy ? `${warn}55` : `${border}50`, cursor: !govBusy ? 'pointer' : 'not-allowed' }}>{gs.analyzing ? 'Analyzing…' : 'Analyze'}</button>, analyzeResult: gs.analyzeResult, error: gs.error, rules: gs.refinements, approveFn: id => handleApproveRefinement(dsSelectedSourceId, id), rejectFn: id => handleRejectRefinement(dsSelectedSourceId, id), actState: refinementActionState })}
            {govSection({ title: 'Domain Rules',       pendingCount: pendingRules,    loadFn: () => loadDomainRules(dsSelectedSourceId), busyFlag: rsBusy,  generateBtn: <button onClick={() => handleGenerateSuggestions(dsSelectedSourceId)} disabled={rsBusy} style={{ ...btnGhost({ padding: '4px 10px', fontSize: '0.74rem' }), color: !rsBusy ? accent : `${muted}50`, borderColor: !rsBusy ? `${accent}55` : `${border}50`, cursor: !rsBusy ? 'pointer' : 'not-allowed' }}>{rs.generating ? 'Generating…' : 'Generate Suggestions'}</button>, error: rs.error, rules: rs.rules, approveFn: id => handleApproveRule(dsSelectedSourceId, id), rejectFn: id => handleRejectRule(dsSelectedSourceId, id), actState: ruleActionState })}
            {govSection({ title: 'Entity Rules',       pendingCount: pendingEntRules, loadFn: () => loadEntityRules(dsSelectedSourceId), busyFlag: ersBusy, generateBtn: <button onClick={() => handleGenerateEntitySuggestions(dsSelectedSourceId)} disabled={ersBusy} style={{ ...btnGhost({ padding: '4px 10px', fontSize: '0.74rem' }), color: !ersBusy ? accent : `${muted}50`, borderColor: !ersBusy ? `${accent}55` : `${border}50`, cursor: !ersBusy ? 'pointer' : 'not-allowed' }}>{ers.generating ? 'Generating…' : 'Generate Suggestions'}</button>, error: ers.error, rules: ers.rules, approveFn: id => handleApproveEntityRule(dsSelectedSourceId, id), rejectFn: id => handleRejectEntityRule(dsSelectedSourceId, id), actState: entityRuleActionState, entityMode: true })}
          </>
        )}
      </div>
        ))}
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

    // ── Business Knowledge tab: enterprise knowledge center for the selected table ──
    // Composes Relationship/Business Knowledge/Knowledge Graph/Lineage/Semantic
    // services already used elsewhere in this workspace. No new backend calls
    // beyond thin GET wrappers over existing routes; no data duplicated from
    // the Profile/Governance/Dictionary/Schema tabs.
    const bkAllTables    = (sc.data?.schemas ?? []).flatMap(s => (s.tables ?? []).map(t => t.table_fqn)).sort()
    const bkTableFqn     = bkSelectedTable[dsSelectedSourceId] ?? ''
    const bkOverviewOpen    = bkExpanded.overview !== false
    const bkGraphOpen       = !!bkExpanded.graph
    const bkLineageOpen     = !!bkExpanded.lineage
    const bkSemanticOpen    = !!bkExpanded.semantic
    const bkExplanationOpen = !!bkExpanded.explanation
    const bkSummaryOpen     = !!bkExpanded.summary

    const bkExplain    = bkTableFqn ? bkSectionState[_bkTableKey(dsSelectedSourceId, bkTableFqn, 'explain')]          : null
    const bkSemantic   = bkTableFqn ? bkSectionState[_bkTableKey(dsSelectedSourceId, bkTableFqn, 'semanticProfile')] : null
    const bkRelated    = bkTableFqn ? bkSectionState[_bkTableKey(dsSelectedSourceId, bkTableFqn, 'related')]         : null
    const bkUpstream   = bkTableFqn ? bkSectionState[_bkTableKey(dsSelectedSourceId, bkTableFqn, 'upstream')]        : null
    const bkDownstream = bkTableFqn ? bkSectionState[_bkTableKey(dsSelectedSourceId, bkTableFqn, 'downstream')]      : null
    const bkImpact     = bkTableFqn ? bkSectionState[_bkTableKey(dsSelectedSourceId, bkTableFqn, 'impact')]          : null
    const bkKgSummary  = dsSelectedSourceId != null ? bkSectionState[_bkSourceKey(dsSelectedSourceId, 'kgSummary')]       : null
    const bkSemSummary = dsSelectedSourceId != null ? bkSectionState[_bkSourceKey(dsSelectedSourceId, 'semanticSummary')] : null

    const bkSectionHeader = (label, open, onClick, count) => (
      <div onClick={onClick} style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer', userSelect: 'none' }}>
        <span style={{ fontSize: '0.6rem', color: muted, width: '10px', flexShrink: 0 }}>{open ? '▾' : '▸'}</span>
        <span style={{ fontSize: '0.68rem', fontWeight: '700', color: muted, letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT }}>{label}</span>
        {count != null && <span style={{ fontSize: '0.68rem', color: muted, fontFamily: FONT }}>({count})</span>}
      </div>
    )
    const bkStat = (label, value, color = text) => (
      <div>
        <div style={{ fontSize: '0.6rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '3px' }}>{label}</div>
        <div style={{ fontSize: '0.88rem', fontWeight: '600', color, fontFamily: FONT }}>{value ?? '—'}</div>
      </div>
    )
    const bkPct = v => (v == null ? '—' : `${Math.min(100, Math.round(v * 100))}%`)
    const bkClassLabel = (v, fallback) => (!v || v === 'Unknown' ? fallback : v)
    const BK_REL_LABEL = { FK_OUTBOUND: 'Foreign Key', FK_INBOUND: 'Foreign Key', SAME_DOMAIN: 'Same Domain', SAME_ENTITY: 'Same Entity' }
    const BK_REL_COLOR = { FK_OUTBOUND: accent, FK_INBOUND: accent, SAME_DOMAIN: '#38bdf8', SAME_ENTITY: warn }

    const bkTab = (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
        <div>
          <h3 style={{ margin: '0 0 4px', fontSize: '1.05rem', fontWeight: '700', color: text, fontFamily: FONT }}>Business Knowledge Graph</h3>
          <p style={{ margin: 0, fontSize: '0.78rem', color: muted, fontFamily: FONT }}>
            Summarize, connect, navigate, and explain this source's business metadata — composed from the existing Relationship, Business Knowledge, Knowledge Graph, Lineage, and Semantic services.
          </p>
        </div>

        <div style={{ ...card({ padding: '12px 16px' }), display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
          <label style={{ fontSize: '0.7rem', color: textSec, fontWeight: '600', letterSpacing: '0.06em', textTransform: 'uppercase', fontFamily: FONT }}>Table</label>
          {!hasSchema && (
            <span style={{ fontSize: '0.78rem', color: muted, fontFamily: FONT }}>Discover schema first to select a table.</span>
          )}
          {hasSchema && sc.loading && !sc.data && (
            <span style={{ fontSize: '0.78rem', color: muted, fontFamily: FONT, display: 'flex', alignItems: 'center', gap: '6px' }}><Spinner size={11} /> Loading tables…</span>
          )}
          {hasSchema && sc.data && (
            <select
              value={bkTableFqn}
              onChange={e => handleBkSelectTable(dsSelectedSourceId, e.target.value)}
              style={inp({ width: 'auto', minWidth: '260px', padding: '7px 12px', fontSize: '0.82rem' })}
            >
              <option value="">Select a table…</option>
              {bkAllTables.map(fqn => <option key={fqn} value={fqn}>{fqn}</option>)}
            </select>
          )}
        </div>

        {!bkTableFqn && hasSchema && sc.data && (
          <div style={{ ...card({ padding: '32px 24px' }), textAlign: 'center' }}>
            <p style={{ margin: 0, fontSize: '0.85rem', color: textSec, fontFamily: FONT }}>Select a table above to view its business knowledge.</p>
          </div>
        )}

        {bkTableFqn && (
          <>
            {/* ── Section 1: Business Overview ── */}
            <div style={card({ padding: '14px 18px' })}>
              {bkSectionHeader('Business Overview', bkOverviewOpen, () => toggleBkSection(dsSelectedSourceId, bkTableFqn, 'overview'))}
              {bkOverviewOpen && (
                <div style={{ marginTop: '12px' }}>
                  {(bkExplain?.loading || bkSemantic?.loading) && !bkExplain?.data && (
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: muted, fontSize: '0.8rem', fontFamily: FONT }}><Spinner size={12} /> Loading overview…</div>
                  )}
                  {bkExplain?.error && <div style={{ fontSize: '0.78rem', color: danger, fontFamily: FONT }}>{bkExplain.error}</div>}
                  {bkExplain?.data && (
                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: '14px' }}>
                      {bkStat('Business Name', bkExplain.data.business_name ?? bkExplain.data.table_name)}
                      {bkStat('Description', bkExplain.data.business_purpose)}
                      {bkStat('Domain', bkClassLabel(bkExplain.data.business_domain, 'Not yet classified'))}
                      {bkStat('Entity', bkClassLabel(bkExplain.data.business_entity, 'Not yet classified'))}
                      {bkStat('Importance', bkExplain.data.business_importance?.label, SEV_COLOR[bkExplain.data.business_importance?.label] ?? text)}
                      {bkStat('Confidence', bkPct(bkExplain.data.domain_confidence ?? bkExplain.data.entity_confidence))}
                      {bkStat('Governance Status', bkPct(bkExplain.data.governance_score), bkExplain.data.governance_score >= 0.75 ? success : bkExplain.data.governance_score >= 0.4 ? warn : danger)}
                      {bkStat('Business Role', bkSemantic?.data ? bkClassLabel(bkSemantic.data.semantic_role, 'Awaiting semantic classification') : '—')}
                    </div>
                  )}
                </div>
              )}
            </div>

            {/* ── Section 2: Knowledge Graph ── */}
            <div style={card({ padding: '14px 18px' })}>
              {bkSectionHeader('Knowledge Graph', bkGraphOpen, () => toggleBkSection(dsSelectedSourceId, bkTableFqn, 'graph'), bkRelated?.data?.total_related)}
              {bkGraphOpen && (
                <div style={{ marginTop: '12px' }}>
                  {bkRelated?.loading && !bkRelated?.data && (
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: muted, fontSize: '0.8rem', fontFamily: FONT }}><Spinner size={12} /> Loading related tables…</div>
                  )}
                  {bkRelated?.error && <div style={{ fontSize: '0.78rem', color: danger, fontFamily: FONT }}>{bkRelated.error}</div>}
                  {bkRelated?.data && bkRelated.data.related_tables.length === 0 && (
                    <div style={{ fontSize: '0.8rem', color: muted, fontFamily: FONT }}>No related tables found.</div>
                  )}
                  {bkRelated?.data && bkRelated.data.related_tables.length > 0 && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                      {bkRelated.data.related_tables.map(rt => (
                        <div
                          key={rt.table_fqn}
                          onClick={() => handleBkSelectTable(dsSelectedSourceId, rt.table_fqn)}
                          style={{ padding: '8px 12px', borderRadius: '8px', border: `1px solid ${border}`, cursor: 'pointer', display: 'flex', flexDirection: 'column', gap: '4px' }}
                          onMouseEnter={e => e.currentTarget.style.background = accentSoft}
                          onMouseLeave={e => e.currentTarget.style.background = 'transparent'}
                        >
                          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '8px' }}>
                            <span style={{ fontSize: '0.82rem', color: accent, fontFamily: MONO, fontWeight: '600' }}>{rt.table_fqn} →</span>
                            <span style={{ fontSize: '0.7rem', color: muted, fontFamily: FONT }}>{bkPct(rt.confidence)} confidence</span>
                          </div>
                          <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
                            {rt.relationship_types.map(t => {
                              const relColor = BK_REL_COLOR[t] ?? accent
                              return (
                                <span key={t} style={{ fontSize: '0.6rem', padding: '1px 7px', borderRadius: '8px', background: `${relColor}18`, color: relColor, border: `1px solid ${relColor}30`, fontFamily: FONT }}>{BK_REL_LABEL[t] ?? t}</span>
                              )
                            })}
                          </div>
                          {rt.evidence?.length > 0 && (
                            <span style={{ fontSize: '0.72rem', color: textSec, fontFamily: FONT }}>{rt.evidence[0]}</span>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>

            {/* ── Section 3: Business Lineage ── */}
            <div style={card({ padding: '14px 18px' })}>
              {bkSectionHeader('Business Lineage', bkLineageOpen, () => toggleBkSection(dsSelectedSourceId, bkTableFqn, 'lineage'))}
              {bkLineageOpen && (
                <div style={{ marginTop: '12px', display: 'flex', flexDirection: 'column', gap: '14px' }}>
                  {(bkUpstream?.loading || bkDownstream?.loading || bkImpact?.loading) && !bkImpact?.data && (
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: muted, fontSize: '0.8rem', fontFamily: FONT }}><Spinner size={12} /> Loading lineage…</div>
                  )}
                  {bkImpact?.data && (
                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: '14px' }}>
                      {bkStat('Critical Asset', bkImpact.data.impact_label, SEV_COLOR[bkImpact.data.impact_label] ?? text)}
                      {bkStat('Impact Score', bkPct(bkImpact.data.impact_score))}
                      {bkStat('Affected Domains', bkImpact.data.affected_domains.length ? bkImpact.data.affected_domains.join(', ') : '—')}
                      {bkStat('Affected Entities', bkImpact.data.affected_entities.length ? bkImpact.data.affected_entities.join(', ') : '—')}
                    </div>
                  )}
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '14px' }}>
                    <div>
                      <div style={{ fontSize: '0.65rem', fontWeight: '700', color: muted, letterSpacing: '0.06em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '6px' }}>Upstream ({bkUpstream?.data?.total_upstream ?? 0})</div>
                      <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                        {(bkUpstream?.data?.upstream ?? []).map(n => (
                          <span key={n.table_fqn} onClick={() => handleBkSelectTable(dsSelectedSourceId, n.table_fqn)} style={{ fontSize: '0.76rem', color: accent, fontFamily: MONO, cursor: 'pointer' }}>
                            {n.table_fqn} <span style={{ color: muted, fontFamily: FONT }}>(dist {n.distance})</span>
                          </span>
                        ))}
                        {bkUpstream?.data && bkUpstream.data.upstream.length === 0 && <span style={{ fontSize: '0.76rem', color: muted, fontFamily: FONT }}>None</span>}
                      </div>
                    </div>
                    <div>
                      <div style={{ fontSize: '0.65rem', fontWeight: '700', color: muted, letterSpacing: '0.06em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '6px' }}>Downstream ({bkDownstream?.data?.total_downstream ?? 0})</div>
                      <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                        {(bkDownstream?.data?.downstream ?? []).map(n => (
                          <span key={n.table_fqn} onClick={() => handleBkSelectTable(dsSelectedSourceId, n.table_fqn)} style={{ fontSize: '0.76rem', color: accent, fontFamily: MONO, cursor: 'pointer' }}>
                            {n.table_fqn} <span style={{ color: muted, fontFamily: FONT }}>(dist {n.distance})</span>
                          </span>
                        ))}
                        {bkDownstream?.data && bkDownstream.data.downstream.length === 0 && <span style={{ fontSize: '0.76rem', color: muted, fontFamily: FONT }}>None</span>}
                      </div>
                    </div>
                  </div>
                </div>
              )}
            </div>

            {/* ── Section 4: Semantic Intelligence ── */}
            <div style={card({ padding: '14px 18px' })}>
              {bkSectionHeader('Semantic Intelligence', bkSemanticOpen, () => toggleBkSection(dsSelectedSourceId, bkTableFqn, 'semantic'))}
              {bkSemanticOpen && (
                <div style={{ marginTop: '12px' }}>
                  {bkSemantic?.loading && !bkSemantic?.data && (
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: muted, fontSize: '0.8rem', fontFamily: FONT }}><Spinner size={12} /> Loading semantic profile…</div>
                  )}
                  {bkSemantic?.data && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: '14px' }}>
                        {bkStat('Role', bkClassLabel(bkSemantic.data.semantic_role, 'Awaiting semantic classification'), accent)}
                        {bkStat('Role Confidence', bkPct(bkSemantic.data.semantic_role_confidence))}
                        {bkStat('Business Process', (bkSemantic.data.related_business_processes ?? []).join(', ') || '—')}
                        {bkStat('Trusted', bkSemantic.data.trusted ? 'Yes' : 'No', bkSemantic.data.trusted ? success : muted)}
                      </div>
                      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '14px' }}>
                        <div>
                          <div style={{ fontSize: '0.65rem', fontWeight: '700', color: muted, letterSpacing: '0.06em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '6px' }}>Typical Joins</div>
                          {(bkSemantic.data.typical_joins ?? []).map((j, i) => (
                            <div key={i} style={{ fontSize: '0.76rem', color: text, fontFamily: MONO, marginBottom: '3px' }}>→ {j.to_table_fqn} <span style={{ color: muted, fontFamily: FONT }}>({bkPct(j.confidence)})</span></div>
                          ))}
                          {(bkSemantic.data.typical_joins ?? []).length === 0 && <span style={{ fontSize: '0.76rem', color: muted, fontFamily: FONT }}>None</span>}
                        </div>
                        <div>
                          <div style={{ fontSize: '0.65rem', fontWeight: '700', color: muted, letterSpacing: '0.06em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '6px' }}>Typical Consumers</div>
                          {(bkSemantic.data.typical_consumers ?? []).map((j, i) => (
                            <div key={i} style={{ fontSize: '0.76rem', color: text, fontFamily: MONO, marginBottom: '3px' }}>← {j.from_table_fqn} <span style={{ color: muted, fontFamily: FONT }}>({bkPct(j.confidence)})</span></div>
                          ))}
                          {(bkSemantic.data.typical_consumers ?? []).length === 0 && <span style={{ fontSize: '0.76rem', color: muted, fontFamily: FONT }}>None</span>}
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              )}
            </div>

            {/* ── Section 5: Business Explanation ── */}
            <div style={card({ padding: '14px 18px' })}>
              {bkSectionHeader('Business Explanation', bkExplanationOpen, () => toggleBkSection(dsSelectedSourceId, bkTableFqn, 'explanation'))}
              {bkExplanationOpen && (
                <div style={{ marginTop: '12px' }}>
                  {bkExplain?.loading && !bkExplain?.data && (
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: muted, fontSize: '0.8rem', fontFamily: FONT }}><Spinner size={12} /> Loading explanation…</div>
                  )}
                  {bkExplain?.data && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                      <div>
                        <div style={{ fontSize: '0.65rem', fontWeight: '700', color: muted, letterSpacing: '0.06em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '4px' }}>Purpose</div>
                        <div style={{ fontSize: '0.82rem', color: text, fontFamily: FONT }}>{bkExplain.data.business_purpose}</div>
                      </div>
                      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: '14px' }}>
                        {bkStat('Metadata Completeness', bkPct(bkExplain.data.profiling?.completeness_score))}
                        {bkStat('Governance Score', bkPct(bkExplain.data.governance_score))}
                        {bkStat('Business Importance', bkExplain.data.business_importance?.label, SEV_COLOR[bkExplain.data.business_importance?.label] ?? text)}
                      </div>
                      <div>
                        <div style={{ fontSize: '0.65rem', fontWeight: '700', color: muted, letterSpacing: '0.06em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '4px' }}>Evidence</div>
                        {bkExplain.data.evidence.length === 0 && <span style={{ fontSize: '0.78rem', color: muted, fontFamily: FONT }}>None</span>}
                        <ul style={{ margin: 0, paddingLeft: '18px' }}>
                          {bkExplain.data.evidence.map((e, i) => <li key={i} style={{ fontSize: '0.78rem', color: textSec, fontFamily: FONT, marginBottom: '2px' }}>{e}</li>)}
                        </ul>
                      </div>
                      {bkExplain.data.gaps.length > 0 && (
                        <div>
                          <div style={{ fontSize: '0.65rem', fontWeight: '700', color: warn, letterSpacing: '0.06em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '4px' }}>Missing Metadata</div>
                          <ul style={{ margin: 0, paddingLeft: '18px' }}>
                            {bkExplain.data.gaps.map((g, i) => <li key={i} style={{ fontSize: '0.78rem', color: warn, fontFamily: FONT, marginBottom: '2px' }}>{g}</li>)}
                          </ul>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>

            {/* ── Section 6: Knowledge Summary ── */}
            <div style={card({ padding: '14px 18px' })}>
              {bkSectionHeader('Knowledge Summary', bkSummaryOpen, () => toggleBkSection(dsSelectedSourceId, bkTableFqn, 'summary'))}
              {bkSummaryOpen && (
                <div style={{ marginTop: '12px' }}>
                  {(bkKgSummary?.loading || bkSemSummary?.loading) && !bkKgSummary?.data && (
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: muted, fontSize: '0.8rem', fontFamily: FONT }}><Spinner size={12} /> Loading knowledge summary…</div>
                  )}
                  {bkKgSummary?.data && (
                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: '14px' }}>
                      {bkKgSummary.data.nodes?.total_tables != null && bkStat('Total Tables', bkKgSummary.data.nodes.total_tables)}
                      {bkStat('Relationship Count', bkKgSummary.data.edges.total_relationships)}
                      {bkStat('Domain Coverage', bkPct(bkKgSummary.data.domain_coverage.coverage_rate))}
                      {bkStat('Entity Coverage', bkPct(bkKgSummary.data.entity_coverage.coverage_rate))}
                      {bkStat('Dictionary Coverage', bkPct(bkKgSummary.data.dictionary_coverage.coverage_rate))}
                      {bkStat('Business Completeness', bkSemSummary?.data ? bkPct(bkSemSummary.data.metrics.business_completeness) : '—')}
                      {bkStat('Knowledge Confidence', `${bkKgSummary.data.confidence_distribution.high} high / ${bkKgSummary.data.confidence_distribution.medium} med / ${bkKgSummary.data.confidence_distribution.low} low`)}
                    </div>
                  )}
                </div>
              )}
            </div>
          </>
        )}
      </div>
    )

    // ── Query tab ─────────────────────────────────────────────────────────────
    // Safe fields only. sql_generation.sql is never accessed or rendered.
    const qryState     = queryState[dsSelectedSourceId] ?? { question: '', loading: false, result: null, error: null }
    const qryResult    = qryState.result                                     // { query_plan, sql_plan, sql_generation, execution }
    const qryExec      = qryResult?.execution ?? null
    const qryStatus    = qryExec?.status ?? null
    const qryRows      = qryExec?.rows ?? []
    const qryCols      = qryRows.length > 0 ? Object.keys(qryRows[0]) : []
    const qryExpl      = qryResult?.sql_generation?.explanation ?? null      // natural-language explanation, not SQL
    const qryGovWarns  = [
      ...(qryResult?.sql_generation?.governance_warnings ?? []),
      ...(qryResult?.sql_plan?.warnings ?? []),
    ]
    const qryExecWarns = qryExec?.warnings ?? []
    const _QSTATUS = {
      governance_block: { col: warn,   title: 'Access Denied',      body: 'This query requires access to governed or restricted data. Contact your data administrator to request access.' },
      rate_limited:     { col: warn,   title: 'Rate Limit Reached', body: 'You are querying too quickly. Please wait a moment before running another query.' },
      failed:           { col: danger, title: 'Query Failed',       body: qryExec?.error_code ? `Query could not be completed (${qryExec.error_code}). Try rephrasing your question or verify the data source is reachable.` : 'Query could not be completed. Try rephrasing your question.' },
      timeout:          { col: danger, title: 'Query Timed Out',    body: 'The query took too long to complete. Try a more specific question or add filters to narrow your result set.' },
    }
    const qrySm = qryStatus ? _QSTATUS[qryStatus] : null

    const queryTab = (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>

        {/* ── Discovery gate ── */}
        {src?.last_snapshot_id == null ? (
          <div style={{ ...card({ padding: '48px 24px' }), textAlign: 'center' }}>
            <svg width="38" height="38" viewBox="0 0 24 24" fill="none" stroke={muted} strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" style={{ display: 'block', margin: '0 auto 14px' }}>
              <ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>
            </svg>
            <p style={{ margin: '0 0 6px', fontSize: '0.9rem', color: textSec, fontWeight: '600', fontFamily: FONT }}>Schema discovery required</p>
            <p style={{ margin: '0 0 18px', fontSize: '0.78rem', color: muted, fontFamily: FONT, lineHeight: 1.6, maxWidth: '420px', marginLeft: 'auto', marginRight: 'auto' }}>
              Run Scan Metadata first to discover this source's tables and columns. The Query panel uses that schema to plan safe, governance-aware queries.
            </p>
            <button
              onClick={() => src && handleDiscoverAndProfile(src)}
              style={{ ...btnMain, display: 'inline-flex', alignItems: 'center', gap: '7px' }}
            >
              Scan Metadata
            </button>
          </div>
        ) : (
          <>
            {/* ── Input card ── */}
            <div style={card()}>
              <label style={lbl}>Ask a question about your data</label>
              <textarea
                value={qryState.question}
                onChange={e => setQueryState(s => ({ ...s, [dsSelectedSourceId]: { ...(s[dsSelectedSourceId] ?? {}), question: e.target.value } }))}
                onKeyDown={e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); handleQueryRun(dsSelectedSourceId) } }}
                disabled={qryState.loading}
                placeholder="e.g. What are the top 10 customers by total revenue this year?"
                rows={3}
                style={{ ...inp({ fontFamily: FONT, lineHeight: 1.6, resize: 'vertical', minHeight: '72px', marginBottom: '12px', letterSpacing: '0' }) }}
              />
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px', flexWrap: 'wrap' }}>
                <span style={{ fontSize: '0.7rem', color: muted, fontFamily: FONT }}>
                  Ctrl+Enter to run · Natural language only · SQL is never shown
                </span>
                <button
                  onClick={() => handleQueryRun(dsSelectedSourceId)}
                  disabled={qryState.loading || !(qryState.question ?? '').trim()}
                  style={{
                    ...btnMain,
                    display: 'inline-flex', alignItems: 'center', gap: '7px',
                    opacity: (qryState.loading || !(qryState.question ?? '').trim()) ? 0.6 : 1,
                    cursor:  (qryState.loading || !(qryState.question ?? '').trim()) ? 'not-allowed' : 'pointer',
                  }}
                >
                  {qryState.loading && <Spinner size={12} />}
                  {qryState.loading ? 'Querying…' : 'Run Query'}
                </button>
              </div>
            </div>

            {/* ── Loading state ── */}
            {qryState.loading && (
              <div style={{ ...card({ padding: '24px' }), display: 'flex', alignItems: 'center', gap: '14px' }}>
                <Spinner size={18} />
                <div>
                  <div style={{ fontSize: '0.84rem', fontWeight: '600', color: text, fontFamily: FONT, marginBottom: '3px' }}>
                    Analyzing your question…
                  </div>
                  <div style={{ fontSize: '0.73rem', color: muted, fontFamily: FONT }}>
                    Planning query, checking governance rules, and retrieving results
                  </div>
                </div>
              </div>
            )}

            {/* ── Thrown-exception error ── */}
            {!qryState.loading && qryState.error && (
              <div style={{ ...card({ padding: '16px 18px' }), borderColor: `${danger}50`, background: `${danger}08` }}>
                <div style={{ display: 'flex', alignItems: 'flex-start', gap: '10px' }}>
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke={danger} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, marginTop: '1px' }}>
                    <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
                  </svg>
                  <div>
                    <div style={{ fontSize: '0.84rem', fontWeight: '600', color: danger, fontFamily: FONT, marginBottom: '4px' }}>
                      Query Error
                    </div>
                    <div style={{ fontSize: '0.78rem', color: textSec, fontFamily: FONT, lineHeight: 1.5 }}>
                      {qryState.error}
                    </div>
                  </div>
                </div>
              </div>
            )}

            {/* ── Non-success execution status ── */}
            {!qryState.loading && !qryState.error && qryExec && qryStatus !== 'success' && qrySm && (
              <div style={{ ...card({ padding: '16px 18px' }), borderColor: `${qrySm.col}50`, background: `${qrySm.col}08` }}>
                <div style={{ display: 'flex', alignItems: 'flex-start', gap: '10px' }}>
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke={qrySm.col} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, marginTop: '1px' }}>
                    <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
                    <line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>
                  </svg>
                  <div>
                    <div style={{ fontSize: '0.84rem', fontWeight: '600', color: qrySm.col, fontFamily: FONT, marginBottom: '4px' }}>
                      {qrySm.title}
                    </div>
                    <div style={{ fontSize: '0.78rem', color: textSec, fontFamily: FONT, lineHeight: 1.5 }}>
                      {qrySm.body}
                    </div>
                  </div>
                </div>
              </div>
            )}

            {/* ── Success result ── */}
            {!qryState.loading && !qryState.error && qryExec && qryStatus === 'success' && (
              <>
                {/* AI Interpretation block — explanation only, never SQL */}
                {qryExpl && (
                  <div style={{ ...card({ padding: '14px 18px' }), borderColor: `${accent}35`, background: `${accent}07` }}>
                    <div style={{ fontSize: '0.6rem', fontWeight: '800', color: accent, letterSpacing: '0.12em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '7px' }}>
                      AI Interpretation
                    </div>
                    <p style={{ margin: 0, fontSize: '0.82rem', color: textSec, fontFamily: FONT, lineHeight: 1.6, fontStyle: 'italic' }}>
                      {qryExpl}
                    </p>
                  </div>
                )}

                {/* Governance / trust warning chips */}
                {qryGovWarns.length > 0 && (
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px' }}>
                    {qryGovWarns.map((w, i) => (
                      <span key={i} style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', padding: '4px 12px', borderRadius: '20px', fontSize: '0.72rem', fontWeight: '600', background: `${warn}14`, color: warn, border: `1px solid ${warn}40`, fontFamily: FONT }}>
                        ⚠ {typeof w === 'string' ? w : (w.message ?? w.warning ?? String(w))}
                      </span>
                    ))}
                  </div>
                )}

                {/* Execution warnings (e.g. repeated_query) */}
                {qryExecWarns.length > 0 && (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                    {qryExecWarns.map((w, i) => {
                      const sev = (w.severity ?? '').toUpperCase()
                      const wc  = (sev === 'HIGH' || sev === 'MEDIUM') ? warn : '#38bdf8'
                      return (
                        <div key={i} style={{ ...card({ padding: '10px 14px' }), borderColor: `${wc}35`, background: `${wc}08`, fontSize: '0.78rem', color: textSec, fontFamily: FONT }}>
                          {w.message ?? String(w)}
                        </div>
                      )
                    })}
                  </div>
                )}

                {/* Metadata row: row count · duration · truncation */}
                <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', padding: '3px 10px', borderRadius: '20px', fontSize: '0.72rem', fontWeight: '600', background: `${success}14`, color: success, border: `1px solid ${success}35`, fontFamily: FONT }}>
                    {(qryExec.row_count ?? qryRows.length).toLocaleString()} row{(qryExec.row_count ?? qryRows.length) !== 1 ? 's' : ''}
                  </span>
                  {qryExec.duration_ms != null && (
                    <span style={{ fontSize: '0.72rem', color: muted, fontFamily: FONT }}>
                      {qryExec.duration_ms < 1000 ? `${qryExec.duration_ms}ms` : `${(qryExec.duration_ms / 1000).toFixed(1)}s`}
                    </span>
                  )}
                  {qryExec.truncated && (
                    <span style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', padding: '3px 10px', borderRadius: '20px', fontSize: '0.72rem', fontWeight: '600', background: `${warn}14`, color: warn, border: `1px solid ${warn}35`, fontFamily: FONT }}>
                      ⚠ Truncated at 500 rows · refine your question to narrow results
                    </span>
                  )}
                </div>

                {/* Result table */}
                {qryRows.length === 0 ? (
                  <div style={{ ...card({ padding: '36px 24px' }), textAlign: 'center' }}>
                    <p style={{ margin: 0, fontSize: '0.84rem', color: muted, fontFamily: FONT }}>No rows returned for this question.</p>
                  </div>
                ) : (
                  <div style={{ ...card({ padding: 0 }), overflow: 'hidden' }}>
                    <div style={{ overflowX: 'auto', maxHeight: '480px', overflowY: 'auto' }}>
                      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.8rem' }}>
                        <thead>
                          <tr style={{ background: bg }}>
                            {qryCols.map(h => (
                              <th key={h} style={{ position: 'sticky', top: 0, background: bg, padding: '9px 14px', textAlign: 'left', fontSize: '0.67rem', fontWeight: '700', color: muted, letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT, borderBottom: `1px solid ${border}`, whiteSpace: 'nowrap', zIndex: 1 }}>
                                {h}
                              </th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {qryRows.map((row, ri) => (
                            <tr key={ri} style={{ background: ri % 2 === 0 ? 'transparent' : `${bg}80` }}>
                              {qryCols.map(h => (
                                <td key={h} style={{ padding: '7px 14px', color: textSec, fontFamily: MONO, borderBottom: `1px solid ${border}18`, verticalAlign: 'top', maxWidth: '300px', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                                  {row[h] == null
                                    ? <span style={{ color: muted, fontStyle: 'italic', fontFamily: FONT }}>null</span>
                                    : String(row[h])
                                  }
                                </td>
                              ))}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}
              </>
            )}
          </>
        )}
      </div>
    )

    const TAB_CONTENT = { overview: overviewTab, schema: schemaTab, profile: profileTab, domains: domainsTab, entities: entitiesTab, governance: governanceTab, 'business-knowledge': bkTab, lineage: lineageTab, runs: runsTab, query: queryTab }

    return (
      <div style={{ fontFamily: FONT, color: text }}>

        {/* ── Workspace header ── */}
        <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '16px', marginBottom: '20px', flexWrap: 'wrap' }}>
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '8px', flexWrap: 'wrap' }}>
              <h2 style={{ margin: 0, fontSize: '1.5rem', fontWeight: '700', color: text, letterSpacing: '-0.5px', fontFamily: FONT }}>{src?.display_name ?? `Source #${dsSelectedSourceId}`}</h2>
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', padding: '3px 10px', borderRadius: '12px', fontSize: '0.7rem', fontWeight: '700', background: `${smBadge.color}20`, color: smBadge.color, border: `1px solid ${smBadge.color}50`, fontFamily: FONT }}>
                <span style={{ width: '6px', height: '6px', borderRadius: '50%', background: smBadge.color, display: 'inline-block', flexShrink: 0 }} />
                {smBadge.label}
              </span>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '0.77rem', color: muted, fontFamily: FONT, flexWrap: 'wrap' }}>
              <span style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
                {stLabel}
              </span>
              {src?.config_summary && (
                <><span style={{ color: border }}>·</span><span>{src.config_summary}</span></>
              )}
              {src?.metadata?.environment && (
                <><span style={{ color: border }}>·</span><span style={{ textTransform: 'capitalize' }}>{src.metadata.environment}</span></>
              )}
            </div>
          </div>
          <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexShrink: 0, flexWrap: 'wrap' }}>
            <button
              onClick={() => !ts.loading && handleTest(dsSelectedSourceId)}
              disabled={ts.loading}
              style={{ display: 'inline-flex', alignItems: 'center', gap: '6px', padding: '8px 16px', borderRadius: '8px', background: 'transparent', border: `1px solid ${border}`, color: ts.loading ? `${muted}60` : textSec, fontSize: '0.82rem', fontWeight: '500', cursor: ts.loading ? 'not-allowed' : 'pointer', fontFamily: FONT }}
            >
              {ts.loading
                ? <Spinner size={11} />
                : <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>}
              {ts.loading ? 'Testing…' : 'Test Connection'}
            </button>
            <button
              onClick={() => src && !discSt.loading && !js.running && handleDiscoverAndProfile(src)}
              disabled={discSt.loading || js.running}
              style={{ display: 'inline-flex', alignItems: 'center', gap: '6px', padding: '8px 16px', borderRadius: '8px', background: accent, border: 'none', color: '#fff', fontSize: '0.82rem', fontWeight: '600', cursor: (discSt.loading || js.running) ? 'not-allowed' : 'pointer', fontFamily: FONT, opacity: (discSt.loading || js.running) ? 0.7 : 1 }}
            >
              {(discSt.loading || js.running) && <Spinner size={11} />}
              {(discSt.loading || js.running) ? 'Scanning…' : 'Scan Metadata'}
            </button>
            <button
              onClick={() => src && !fullProfSt.loading && hasSchema && handleRunDataProfile(src)}
              disabled={fullProfSt.loading || !hasSchema}
              style={{ display: 'inline-flex', alignItems: 'center', gap: '6px', padding: '8px 16px', borderRadius: '8px', background: 'transparent', border: `1px solid ${border}`, color: (fullProfSt.loading || !hasSchema) ? muted : textSec, fontSize: '0.82rem', fontWeight: '500', cursor: (fullProfSt.loading || !hasSchema) ? 'not-allowed' : 'pointer', fontFamily: FONT, opacity: (fullProfSt.loading || !hasSchema) ? 0.6 : 1 }}
            >
              {fullProfSt.loading && <Spinner size={11} />}
              {fullProfSt.loading
                ? (fullProfSt.total > 0 ? `Profiling ${fullProfSt.progress}/${fullProfSt.total}…` : 'Profiling…')
                : 'Run Data Profile'}
            </button>
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
        const connectedSrcs      = sources.filter(s => s.source_status === 'ACTIVE').length
        const n                   = sources.length

        // Loading flags
        const scannedList         = sources.filter(s => s.last_snapshot_id != null)
        const dictLoading         = scannedList.some(s => dictState[s.id]?.loading === true)
        const domainLoading       = scannedList.some(s => domainState[s.id]?.loading === true)
        const entityLoading       = scannedList.some(s => entityState[s.id]?.loading === true)
        const schemaLoadingAny    = scannedList.some(s => schemaState[s.id]?.loading === true)

        // Assets Discovered — total tables from real schema data
        const totalAssets = sources.reduce((acc, s) => {
          const sc = schemaState[s.id]
          if (!sc?.data?.schemas) return acc
          return acc + sc.data.schemas.reduce((a, schema) => a + (schema.tables?.filter(t => t.table_type === 'TABLE').length ?? 0), 0)
        }, 0)
        const assetsAvailable = scannedList.some(s => schemaState[s.id]?.data?.schemas != null)

        // Dictionary Review — portfolio-level approval rate
        const totalDictTables    = sources.reduce((acc, s) => acc + (dictState[s.id]?.tables?.length ?? 0), 0)
        const approvedDictTables = sources.reduce((acc, s) => {
          const tables = dictState[s.id]?.tables ?? []
          return acc + tables.filter(t => t.is_approved === 1 || t.is_approved === true).length
        }, 0)
        const pendingReviews  = totalDictTables - approvedDictTables
        const dictApprovalPct = totalDictTables > 0 ? Math.round((approvedDictTables / totalDictTables) * 100) : null
        const dictNavSrc      = openSource ? sources.find(s => (dictState[s.id]?.tables?.length ?? 0) > 0) : null

        // Domain & Entity Coverage
        const totalDomainAssigned = sources.reduce((acc, s) => acc + (domainState[s.id]?.summary?.tables_assigned ?? 0), 0)
        const domNavSrc           = openSource ? sources.find(s => (domainState[s.id]?.summary?.tables_assigned ?? 0) > 0) : null
        const totalEntityAssigned = sources.reduce((acc, s) => acc + (entityState[s.id]?.summary?.entities_assigned ?? 0), 0)
        const entNavSrc           = openSource ? sources.find(s => (entityState[s.id]?.summary?.entities_assigned ?? 0) > 0) : null

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
                {
                  label: 'Data Sources', value: n, sub: 'registered', col: text, top: border, loading: false, iconCol: muted, onClick: null,
                  icon: <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>,
                },
                {
                  label: 'Connected', value: connectedSrcs, sub: 'active connections', col: success, top: `${success}70`, loading: false, iconCol: success, onClick: null,
                  icon: <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>,
                },
                {
                  label: 'Assets Discovered',
                  value: schemaLoadingAny ? null : (assetsAvailable ? totalAssets.toLocaleString() : '—'),
                  sub: assetsAvailable ? 'tables across all sources' : 'run schema discovery',
                  col: assetsAvailable ? text : muted, top: assetsAvailable ? `${accent}70` : border,
                  loading: schemaLoadingAny, iconCol: assetsAvailable ? accent : muted, onClick: null,
                  icon: <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18"/><path d="M9 21V9"/></svg>,
                },
                {
                  label: 'Dictionary Review',
                  value: dictApprovalPct !== null ? `${dictApprovalPct}%` : '—',
                  sub: dictApprovalPct !== null ? (pendingReviews > 0 ? `${pendingReviews} pending` : 'fully reviewed') : 'generate dictionary',
                  col: dictApprovalPct !== null ? (dictApprovalPct === 100 ? success : warn) : muted,
                  top: dictApprovalPct !== null ? (dictApprovalPct === 100 ? `${success}70` : `${warn}70`) : border,
                  loading: dictLoading, iconCol: dictApprovalPct !== null ? (dictApprovalPct === 100 ? success : warn) : muted,
                  onClick: dictNavSrc ? () => openSource(dictNavSrc.id, 'dictionary') : null,
                  icon: <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>,
                },
                {
                  label: 'Domain Coverage',
                  value: domainLoading ? null : (totalDomainAssigned > 0 ? totalDomainAssigned : '—'),
                  sub: totalDomainAssigned > 0 ? 'tables classified' : 'generate domains',
                  col: totalDomainAssigned > 0 ? text : muted, top: totalDomainAssigned > 0 ? `${accent}70` : border,
                  loading: domainLoading, iconCol: totalDomainAssigned > 0 ? accent : muted,
                  onClick: domNavSrc ? () => openSource(domNavSrc.id, 'domains') : null,
                  icon: <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/></svg>,
                },
                {
                  label: 'Entity Coverage',
                  value: entityLoading ? null : (totalEntityAssigned > 0 ? totalEntityAssigned : '—'),
                  sub: totalEntityAssigned > 0 ? 'entities mapped' : 'generate entities',
                  col: totalEntityAssigned > 0 ? text : muted, top: totalEntityAssigned > 0 ? `${accent}70` : border,
                  loading: entityLoading, iconCol: totalEntityAssigned > 0 ? accent : muted,
                  onClick: entNavSrc ? () => openSource(entNavSrc.id, 'entities') : null,
                  icon: <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>,
                },
              ].map(m => (
                <div key={m.label}
                  onClick={m.onClick ?? undefined}
                  onMouseEnter={m.onClick ? e => { e.currentTarget.style.boxShadow = `0 0 0 1px ${m.iconCol}40`; e.currentTarget.style.borderTopColor = m.iconCol } : undefined}
                  onMouseLeave={m.onClick ? e => { e.currentTarget.style.boxShadow = 'none'; e.currentTarget.style.borderTopColor = m.top } : undefined}
                  style={{ background: surface, border: `1px solid ${border}`, borderTop: `2px solid ${m.top}`, borderRadius: '10px', padding: '14px 16px', display: 'flex', flexDirection: 'column', cursor: m.onClick ? 'pointer' : 'default', transition: 'box-shadow 0.15s' }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '10px' }}>
                    <span style={{ fontSize: '0.6rem', fontWeight: '700', color: muted, letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT }}>{m.label}</span>
                    <div style={{ width: '26px', height: '26px', borderRadius: '7px', background: `${m.iconCol}18`, color: m.iconCol, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
                      {m.icon}
                    </div>
                  </div>
                  <div style={{ fontSize: '1.9rem', fontWeight: '800', color: m.col, fontFamily: FONT, lineHeight: 1, letterSpacing: '-1px' }}>
                    {m.loading
                      ? <span style={{ fontSize: '1rem', color: muted, fontWeight: '500', opacity: 0.5 }}>…</span>
                      : m.value}
                  </div>
                  <div style={{ fontSize: '0.64rem', color: muted, fontFamily: FONT, marginTop: '5px' }}>{m.sub}</div>
                </div>
              ))}
            </div>


            {/* ── Data Sources enterprise table ── */}
            <div style={{ background: surface, border: `1px solid ${border}`, borderRadius: '12px' }}>

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
                          {/* Pipeline stage chips — clickable shortcuts into each tab */}
                          {openSource && (() => {
                            const srcDictTbls    = dictState[src.id]?.tables ?? []
                            const srcDictCount   = srcDictTbls.length
                            const srcDictPending = srcDictTbls.filter(t => t.is_approved !== 1 && t.is_approved !== true).length
                            const srcDictApprPct = srcDictCount > 0 ? Math.round(((srcDictCount - srcDictPending) / srcDictCount) * 100) : null
                            const srcDomAssigned = domainState[src.id]?.summary?.tables_assigned ?? 0
                            const srcEntAssigned = entityState[src.id]?.summary?.entities_assigned ?? 0
                            if (srcDictApprPct === null && srcDomAssigned === 0 && srcEntAssigned === 0) return null
                            const chip = (label, color, tab) => (
                              <button key={tab} onClick={e => { e.stopPropagation(); openSource(src.id, tab) }}
                                style={{ display: 'inline-flex', alignItems: 'center', padding: '1px 6px', borderRadius: '5px', fontSize: '0.58rem', fontWeight: '600', cursor: 'pointer', fontFamily: FONT, border: `1px solid ${color}35`, background: `${color}10`, color, lineHeight: 1.5 }}>
                                {label}
                              </button>
                            )
                            return (
                              <div style={{ display: 'flex', gap: '3px', marginTop: '5px', flexWrap: 'wrap' }}>
                                {srcDictApprPct !== null && chip(`Dict ${srcDictApprPct}%`, srcDictApprPct === 100 ? success : warn, 'dictionary')}
                                {srcDomAssigned > 0 && chip(`Domains ${srcDomAssigned}`, accent, 'domains')}
                                {srcEntAssigned > 0 && chip(`Entities ${srcEntAssigned}`, accent, 'entities')}
                                {srcDictCount > 0 && srcDictPending > 0 && (
                                  <button onClick={e => { e.stopPropagation(); openSource(src.id, 'dictionary') }}
                                    style={{ display: 'inline-flex', alignItems: 'center', padding: '1px 2px', fontSize: '0.58rem', cursor: 'pointer', fontFamily: FONT, background: 'none', border: 'none', color: warn, textDecoration: 'underline', textUnderlineOffset: '2px', lineHeight: 1.5 }}>
                                    Review {srcDictPending} definition{srcDictPending !== 1 ? 's' : ''} →
                                  </button>
                                )}
                              </div>
                            )
                          })()}
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
                          <div style={{ fontSize: '0.72rem', color: muted, fontFamily: FONT }}>
                            {(() => {
                              const scanTs = profileState[src.id]?.data?.snapshot?.created_at ?? src.last_snapshot_at ?? null
                              const ts_val = scanTs ?? src.last_tested_at ?? null
                              if (!ts_val) return <span style={{ fontSize: '0.72rem', color: muted, fontFamily: FONT }}>{'—'}</span>
                              return (
                                <div style={{ display: "flex", flexDirection: "column", gap: "1px" }}>
                                  <span style={{ fontSize: "0.72rem", color: muted, fontFamily: FONT }}>{fmtRelative(ts_val)}</span>
                                  <span style={{ fontSize: "0.67rem", color: muted, fontFamily: FONT }}>{fmtDateTime(ts_val)}</span>
                                </div>
                              )
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
                                {/* ⋮ button with floating dropdown */}
                                <div style={{ position: 'relative' }}>
                                  <button
                                    onClick={() => setSrcMenu(s => { const isOpen = s[src.id]; return isOpen ? {} : { [src.id]: true } })}
                                    style={{ ...btnGhost({ padding: '5px 8px', fontSize: '1rem' }), color: menuOpen ? accent : muted, borderColor: menuOpen ? `${accent}50` : border, lineHeight: 1 }}
                                    title="More actions"
                                  >
                                    ⋮
                                  </button>
                                  {menuOpen && (
                                    <>
                                      {/* Backdrop — click anywhere outside to close */}
                                      <div onClick={() => setSrcMenu({})} style={{ position: 'fixed', inset: 0, zIndex: 99 }} />
                                      {/* Floating dropdown */}
                                      <div style={{ position: 'absolute', top: 'calc(100% + 4px)', right: 0, zIndex: 100, background: surface, border: `1px solid ${border}`, borderRadius: '10px', boxShadow: '0 8px 28px rgba(0,0,0,0.45)', minWidth: '170px', overflow: 'hidden' }}>
                                        <button
                                          onClick={() => { if (!ts.loading) handleTest(src.id) }}
                                          disabled={ts.loading}
                                          style={{ display: 'flex', alignItems: 'center', gap: '9px', width: '100%', padding: '9px 14px', background: 'none', border: 'none', color: ts.loading ? `${muted}55` : textSec, fontSize: '0.8rem', fontFamily: FONT, cursor: ts.loading ? 'not-allowed' : 'pointer', textAlign: 'left' }}
                                        >
                                          {ts.loading
                                            ? <Spinner size={10} />
                                            : <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>}
                                          {ts.loading ? 'Testing…' : 'Test Connection'}
                                        </button>
                                        <button
                                          onClick={() => { if (!scanBusy && src.source_status === 'ACTIVE') { handleDiscoverAndProfile(src); setSrcMenu({}) } }}
                                          disabled={scanBusy || src.source_status !== 'ACTIVE'}
                                          style={{ display: 'flex', alignItems: 'center', gap: '9px', width: '100%', padding: '9px 14px', background: 'none', border: 'none', color: (scanBusy || src.source_status !== 'ACTIVE') ? `${muted}55` : textSec, fontSize: '0.8rem', fontFamily: FONT, cursor: (scanBusy || src.source_status !== 'ACTIVE') ? 'not-allowed' : 'pointer', textAlign: 'left' }}
                                        >
                                          {scanBusy
                                            ? <Spinner size={10} />
                                            : <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>}
                                          {scanBusy ? 'Scanning…' : 'Scan / Profile'}
                                        </button>
                                        {ts.status && !ts.loading && (
                                          <div style={{ padding: '5px 14px 7px', borderTop: `1px solid ${border}30`, fontSize: '0.71rem', fontWeight: '600', color: ts.status === 'success' ? success : danger, fontFamily: FONT }}>
                                            {ts.status === 'success'
                                              ? `✓ Connected${ts.latency_ms != null ? ` · ${ts.latency_ms}ms` : ''}`
                                              : `✗ ${ts.message ?? 'Failed'}`}
                                          </div>
                                        )}
                                        <div style={{ height: '1px', background: `${border}60`, margin: '2px 0' }} />
                                        <button
                                          onClick={() => { handleDeleteClick(src.id); setSrcMenu({}) }}
                                          style={{ display: 'flex', alignItems: 'center', gap: '9px', width: '100%', padding: '9px 14px', background: 'none', border: 'none', color: danger, fontSize: '0.8rem', fontFamily: FONT, cursor: 'pointer', textAlign: 'left' }}
                                        >
                                          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>
                                          Remove
                                        </button>
                                      </div>
                                    </>
                                  )}
                                </div>
                              </>
                            ) : (
                              <>
                                <button onClick={() => handleDeleteConfirm(src.id)} style={{ background: danger, color: '#fff', border: 'none', borderRadius: '7px', padding: '5px 11px', fontSize: '0.75rem', fontWeight: '600', cursor: 'pointer', fontFamily: FONT }}>Confirm?</button>
                                <button onClick={() => handleDeleteCancel(src.id)} style={btnGhost({ padding: '5px 8px', fontSize: '0.75rem' })}>Cancel</button>
                              </>
                            )}
                          </div>
                        </div>
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
