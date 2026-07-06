import { useState, useEffect, useMemo, useCallback } from 'react'
import { acceptAiSuggestion, approveDictionaryColumn, approveDictionaryTable, getDictionaryTable, listAiSuggestions, listDataSources, listDictionaryTables, rejectAiSuggestion } from '../api/client'

const FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"
const MONO = "'Cascadia Code', 'Fira Code', 'JetBrains Mono', monospace"

const DOMAINS = [
  'All', 'Sales', 'Customer', 'Product', 'People', 'Finance',
  'Analytics', 'Operations', 'Reference', 'Education', 'Training', 'General',
]

const SEM = {
  metric:    { bg: '#3b82f615', color: '#3b82f6', border: '#3b82f630' },
  dimension: { bg: '#22c55e15', color: '#22c55e', border: '#22c55e30' },
  date:      { bg: '#8b5cf615', color: '#8b5cf6', border: '#8b5cf630' },
  id:        { bg: '#94a3b815', color: '#94a3b8', border: '#94a3b830' },
  flag:      { bg: '#f59e0b15', color: '#f59e0b', border: '#f59e0b30' },
}
const PII_S = { bg: '#ef444415', color: '#ef4444', border: '#ef444430' }

function Badge({ label, bg, color, border }) {
  return (
    <span style={{
      display: 'inline-block', padding: '1px 7px', borderRadius: '9px',
      fontSize: '0.6rem', fontWeight: '700', letterSpacing: '0.05em',
      textTransform: 'uppercase', background: bg, color,
      border: `1px solid ${border}`, whiteSpace: 'nowrap',
    }}>
      {label}
    </span>
  )
}

const COLS_HDR = ['185px', '170px', '105px', '85px', '1fr', '88px']

export default function DictionaryReview({ C = {}, token, sourceId: sourceIdProp = null, embedded = false, hideSourceSelector = false }) {
  const bg      = C.bg        ?? '#07091a'
  const surface = C.surface   ?? '#0d1128'
  const border  = C.border    ?? '#1e2b52'
  const text    = C.text      ?? '#eef0ff'
  const textSec = C.textSec   ?? '#dde1ff'
  const muted   = C.textMuted ?? '#7880a8'
  const accent  = C.accent    ?? '#6366f1'
  const success = C.success   ?? '#10b981'

  const [sources,      setSources]      = useState([])
  const [sourceId,     setSourceId]     = useState(sourceIdProp ?? null)
  const [tables,       setTables]       = useState([])
  const [loadingTbls,  setLoadingTbls]  = useState(false)
  const [selectedFqn,  setSelectedFqn]  = useState(null)
  const [details,      setDetails]      = useState(null)
  const [loadingDet,   setLoadingDet]   = useState(false)
  const [search,       setSearch]       = useState('')
  const [domainFilter, setDomainFilter] = useState('All')
  const [colSearch,    setColSearch]    = useState('')
  const [piiFilter,    setPiiFilter]    = useState('all')
  const [error,         setError]         = useState(null)
  const [approvingTable, setApprovingTable] = useState(false)
  const [approvingCols,  setApprovingCols]  = useState(new Set())
  const [coverage,         setCoverage]         = useState(null)
  const [aiSuggestions,    setAiSuggestions]    = useState([])
  const [loadingAiSugs,    setLoadingAiSugs]    = useState(false)
  const [expandedSugId,    setExpandedSugId]    = useState(null)
  const [actingOnSug,      setActingOnSug]      = useState(new Set())
  const [aiSugMsg,         setAiSugMsg]         = useState(null)

  // Load sources once (skip when a sourceId is pre-bound and selector is hidden)
  useEffect(() => {
    if (hideSourceSelector && sourceIdProp != null) return
    listDataSources(token)
      .then(d => {
        const s = d?.data ?? []
        setSources(s)
        if (s.length === 1) setSourceId(s[0].id)
      })
      .catch(() => setError('Failed to load data sources.'))
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Load dictionary tables when source changes
  useEffect(() => {
    if (!sourceId) return
    setTables([]); setSelectedFqn(null); setDetails(null)
    setLoadingTbls(true); setError(null)
    listDictionaryTables(sourceId, token)
      .then(d => setTables(d?.data ?? []))
      .catch(() => setError('Failed to load dictionary. Generate it first from Data Sources.'))
      .finally(() => setLoadingTbls(false))
  }, [sourceId]) // eslint-disable-line react-hooks/exhaustive-deps

  // Load table detail when row selected
  useEffect(() => {
    if (!selectedFqn || !sourceId) return
    setDetails(null); setColSearch(''); setPiiFilter('all')
    setLoadingDet(true)
    getDictionaryTable(sourceId, selectedFqn, token)
      .then(d => setDetails(d?.data ?? null))
      .catch(() => setError('Failed to load table details.'))
      .finally(() => setLoadingDet(false))
  }, [selectedFqn]) // eslint-disable-line react-hooks/exhaustive-deps

  // Load pending AI suggestions when source changes
  const refreshAiSuggestions = useCallback(() => {
    if (!sourceId) { setAiSuggestions([]); return }
    setLoadingAiSugs(true)
    listAiSuggestions(sourceId, token, 'PENDING')
      .then(d => setAiSuggestions(d?.data ?? []))
      .catch(() => setAiSuggestions([]))
      .finally(() => setLoadingAiSugs(false))
  }, [sourceId, token])

  useEffect(() => { refreshAiSuggestions() }, [refreshAiSuggestions])

  async function handleAcceptSuggestion(sug) {
    setActingOnSug(s => new Set(s).add(sug.id))
    setAiSugMsg(null)
    try {
      const resp = await acceptAiSuggestion(sourceId, sug.id, token)
      if (resp?.data?.blocked) {
        setAiSugMsg({ type: 'error', text: resp?.data?.reason ?? 'Blocked: row is human-approved.' })
      } else {
        setAiSugMsg({ type: 'success', text: `Applied suggestion for ${sug.column_name}.` })
        refreshAiSuggestions()
        // Refresh table details if current table is affected
        if (selectedFqn === sug.table_fqn) {
          getDictionaryTable(sourceId, selectedFqn, token)
            .then(d => setDetails(d?.data ?? null))
            .catch(() => {})
        }
      }
    } catch (e) {
      const msg = e?.message ?? ''
      if (msg.includes('409') || msg.toLowerCase().includes('human-approved')) {
        setAiSugMsg({ type: 'error', text: 'Blocked: this column has been human-approved.' })
      } else {
        setAiSugMsg({ type: 'error', text: e?.message ?? 'Accept failed.' })
      }
    } finally {
      setActingOnSug(s => { const n = new Set(s); n.delete(sug.id); return n })
    }
  }

  async function handleRejectSuggestion(sug) {
    setActingOnSug(s => new Set(s).add(sug.id))
    setAiSugMsg(null)
    try {
      await rejectAiSuggestion(sourceId, sug.id, token)
      setAiSugMsg({ type: 'success', text: `Rejected suggestion for ${sug.column_name}.` })
      refreshAiSuggestions()
    } catch (e) {
      setAiSugMsg({ type: 'error', text: e?.message ?? 'Reject failed.' })
    } finally {
      setActingOnSug(s => { const n = new Set(s); n.delete(sug.id); return n })
    }
  }

  async function handleApproveTable() {
    if (!sourceId || !selectedFqn) return
    setApprovingTable(true)
    try {
      const resp = await approveDictionaryTable(sourceId, selectedFqn, token)
      if (resp?.data?.coverage) setCoverage(resp.data.coverage)
      const d = await getDictionaryTable(sourceId, selectedFqn, token)
      setDetails(d?.data ?? null)
    } catch (e) { setError(e?.message ?? 'Approval failed. Please try again.') }
    finally { setApprovingTable(false) }
  }

  async function handleApproveColumn(columnName) {
    setApprovingCols(s => new Set(s).add(columnName))
    try {
      const resp = await approveDictionaryColumn(sourceId, selectedFqn, columnName, token)
      if (resp?.data?.coverage) setCoverage(resp.data.coverage)
      const d = await getDictionaryTable(sourceId, selectedFqn, token)
      setDetails(d?.data ?? null)
    } catch (e) { setError(e?.message ?? 'Column approval failed. Please try again.') }
    finally { setApprovingCols(s => { const n = new Set(s); n.delete(columnName); return n }) }
  }

  const filteredTables = useMemo(() => {
    const q = search.toLowerCase()
    return tables.filter(t => {
      if (domainFilter !== 'All' && t.domain !== domainFilter) return false
      if (!q) return true
      return (
        t.business_name?.toLowerCase().includes(q) ||
        t.table_fqn?.toLowerCase().includes(q) ||
        t.domain?.toLowerCase().includes(q)
      )
    })
  }, [tables, search, domainFilter])

  const visibleTables = filteredTables.slice(0, 100)

  const filteredCols = useMemo(() => {
    const cols = details?.columns ?? []
    const q = colSearch.toLowerCase()
    return cols.filter(c => {
      if (piiFilter === 'pii' && !c.pii_risk) return false
      if (piiFilter === 'no_pii' && c.pii_risk) return false
      if (!q) return true
      return (
        c.business_label?.toLowerCase().includes(q) ||
        c.column_name?.toLowerCase().includes(q)
      )
    })
  }, [details, colSearch, piiFilter])

  // ── Style helpers ─────────────────────────────────────────────────────────
  const card  = (x = {}) => ({ background: surface, border: `1px solid ${border}`, borderRadius: '10px', ...x })
  const inp   = (x = {}) => ({
    background: bg, border: `1px solid ${border}`, borderRadius: '7px',
    color: text, fontSize: '0.82rem', padding: '7px 10px', outline: 'none',
    fontFamily: FONT, width: '100%', boxSizing: 'border-box', ...x,
  })
  const lbl   = { fontSize: '0.65rem', color: muted, fontWeight: '600', letterSpacing: '0.06em', textTransform: 'uppercase', marginBottom: '5px' }

  return (
    <div style={{ display: 'flex', height: embedded ? 'calc(100vh - 250px)' : 'calc(100vh - 112px)', fontFamily: FONT, color: text, gap: '16px', overflow: 'hidden' }}>

      {/* ── Left panel ───────────────────────────────────────────────────── */}
      <div style={{ width: '290px', flexShrink: 0, display: 'flex', flexDirection: 'column', gap: '10px', overflow: 'hidden' }}>

        {/* Source selector — hidden when pre-bound from workspace */}
        {!hideSourceSelector && (
          <div>
            <div style={lbl}>Data Source</div>
            <select style={{ ...inp(), cursor: 'pointer' }} value={sourceId ?? ''} onChange={e => setSourceId(e.target.value ? Number(e.target.value) : null)}>
              {sources.length === 0 && <option value="">No sources</option>}
              {sources.length > 1  && <option value="">Select source…</option>}
              {sources.map(s => <option key={s.id} value={s.id}>{s.display_name}</option>)}
            </select>
          </div>
        )}

        {/* Search */}
        <input style={inp()} placeholder="Search tables…" value={search} onChange={e => setSearch(e.target.value)} />

        {/* Domain filter */}
        <select style={{ ...inp(), cursor: 'pointer' }} value={domainFilter} onChange={e => setDomainFilter(e.target.value)}>
          {DOMAINS.map(d => <option key={d} value={d}>{d === 'All' ? 'All Domains' : d}</option>)}
        </select>

        {/* Count */}
        <div style={{ fontSize: '0.68rem', color: muted }}>
          {loadingTbls
            ? 'Loading…'
            : tables.length === 0 && sourceId
              ? 'No dictionary found'
              : `Showing ${visibleTables.length} of ${filteredTables.length}`
          }
          {!loadingTbls && filteredTables.length < tables.length && (
            <span style={{ color: accent }}> (of {tables.length})</span>
          )}
        </div>

        {/* Table list */}
        <div style={{ flex: 1, overflowY: 'auto' }}>
          {error && (
            <div style={{ padding: '10px 12px', fontSize: '0.76rem', color: '#f87171', background: '#f8717112', borderRadius: '7px', marginBottom: '8px' }}>
              {error}
            </div>
          )}
          {!sourceId && (
            <div style={{ textAlign: 'center', padding: '32px 0', color: muted, fontSize: '0.78rem' }}>Select a data source above.</div>
          )}
          {sourceId && !loadingTbls && tables.length === 0 && (
            <div style={{ textAlign: 'center', padding: '32px 10px', color: muted, fontSize: '0.76rem', lineHeight: 1.5 }}>
              No dictionary found.<br />Generate one from the Data Sources tab.
            </div>
          )}

          {visibleTables.map(t => {
            const isSelected = t.table_fqn === selectedFqn
            return (
              <div
                key={t.table_fqn}
                onClick={() => setSelectedFqn(t.table_fqn)}
                style={{
                  padding: '8px 10px', cursor: 'pointer', borderRadius: '7px', marginBottom: '2px',
                  background: isSelected ? `${accent}15` : 'transparent',
                  border: `1px solid ${isSelected ? accent + '40' : 'transparent'}`,
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: '5px', marginBottom: '2px' }}>
                  <span style={{ fontSize: '0.82rem', fontWeight: '500', color: text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>
                    {t.business_name || t.table_name}
                  </span>
                  {t.is_approved === 1 && <span style={{ color: success, fontSize: '0.68rem', flexShrink: 0 }}>✓</span>}
                </div>
                <div style={{ fontSize: '0.66rem', color: muted, fontFamily: MONO, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', marginBottom: '3px' }}>
                  {t.table_fqn}
                </div>
                {t.domain && t.domain !== 'General' && (
                  <Badge label={t.domain} bg={`${accent}12`} color={`${accent}cc`} border={`${accent}25`} />
                )}
              </div>
            )
          })}

          {filteredTables.length > 100 && (
            <div style={{ textAlign: 'center', padding: '10px', fontSize: '0.7rem', color: muted }}>
              +{filteredTables.length - 100} more — refine search to narrow
            </div>
          )}
        </div>
      </div>

      {/* ── Right panel ──────────────────────────────────────────────────── */}
      <div style={{ flex: 1, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '12px', minWidth: 0 }}>

        {/* ── AI Suggestions panel ─────────────────────────────────────────── */}
        {sourceId && (aiSuggestions.length > 0 || loadingAiSugs) && (() => {
          const aiAccent = '#f59e0b'
          return (
            <div style={{ ...card({ padding: '0' }), border: `1px solid ${aiAccent}30` }}>
              {/* Header */}
              <div style={{
                display: 'flex', alignItems: 'center', gap: '8px',
                padding: '10px 16px', borderBottom: `1px solid ${aiAccent}20`,
                background: `${aiAccent}08`,
              }}>
                <span style={{ fontSize: '0.75rem', fontWeight: '700', color: aiAccent, letterSpacing: '0.04em' }}>
                  AI SUGGESTIONS
                </span>
                {!loadingAiSugs && (
                  <span style={{
                    fontSize: '0.65rem', fontWeight: '700', padding: '1px 7px', borderRadius: '9px',
                    background: `${aiAccent}20`, color: aiAccent, border: `1px solid ${aiAccent}35`,
                  }}>
                    {aiSuggestions.length} PENDING
                  </span>
                )}
                <span style={{ fontSize: '0.68rem', color: muted, marginLeft: 'auto' }}>
                  Review and accept or reject each suggestion.
                </span>
              </div>

              {/* Feedback message */}
              {aiSugMsg && (
                <div style={{
                  padding: '6px 16px', fontSize: '0.74rem',
                  color: aiSugMsg.type === 'success' ? success : '#f87171',
                  background: aiSugMsg.type === 'success' ? `${success}10` : '#f8717110',
                  borderBottom: `1px solid ${border}`,
                }}>
                  {aiSugMsg.text}
                </div>
              )}

              {/* Loading */}
              {loadingAiSugs && (
                <div style={{ padding: '16px', textAlign: 'center', color: muted, fontSize: '0.78rem' }}>
                  Loading…
                </div>
              )}

              {/* Suggestion rows */}
              {!loadingAiSugs && aiSuggestions.map(sug => {
                const isExpanded = expandedSugId === sug.id
                const isActing   = actingOnSug.has(sug.id)
                const conf       = sug.ai_confidence ?? 0
                const confPct    = Math.round(conf * 100)
                const confColor  = conf >= 0.8 ? success : conf >= 0.6 ? aiAccent : '#94a3b8'
                const reasoning  = Array.isArray(sug.ai_reasoning) ? sug.ai_reasoning : []
                return (
                  <div key={sug.id} style={{ borderBottom: `1px solid ${border}20` }}>
                    {/* Row summary */}
                    <div
                      style={{
                        display: 'grid',
                        gridTemplateColumns: '1fr 1fr 60px 120px',
                        alignItems: 'center',
                        padding: '8px 16px', gap: '8px',
                        cursor: 'pointer',
                        background: isExpanded ? `${aiAccent}06` : 'transparent',
                      }}
                      onClick={() => setExpandedSugId(isExpanded ? null : sug.id)}
                    >
                      {/* Table / column */}
                      <div style={{ minWidth: 0 }}>
                        <div style={{ fontSize: '0.72rem', fontFamily: MONO, color: muted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {sug.table_fqn}
                        </div>
                        <div style={{ fontSize: '0.82rem', fontWeight: '600', color: text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {sug.column_name}
                        </div>
                      </div>

                      {/* Suggested business name */}
                      <div style={{ minWidth: 0 }}>
                        <div style={{ fontSize: '0.63rem', color: muted, fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: '1px' }}>
                          Suggested name
                        </div>
                        <div style={{ fontSize: '0.8rem', color: text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {sug.suggested_business_name ?? '—'}
                        </div>
                      </div>

                      {/* Confidence */}
                      <div style={{ textAlign: 'center' }}>
                        <div style={{ fontSize: '0.63rem', color: muted, fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: '2px' }}>Conf.</div>
                        <div style={{ fontSize: '0.78rem', fontWeight: '700', color: confColor }}>{confPct}%</div>
                      </div>

                      {/* Actions */}
                      <div style={{ display: 'flex', gap: '5px', justifyContent: 'flex-end' }} onClick={e => e.stopPropagation()}>
                        <button
                          onClick={() => handleAcceptSuggestion(sug)}
                          disabled={isActing}
                          style={{
                            background: `${success}15`, color: success, border: `1px solid ${success}35`,
                            borderRadius: '5px', padding: '3px 10px', fontSize: '0.68rem', fontWeight: '700',
                            cursor: isActing ? 'default' : 'pointer', fontFamily: FONT,
                          }}
                        >
                          {isActing ? '…' : 'Accept'}
                        </button>
                        <button
                          onClick={() => handleRejectSuggestion(sug)}
                          disabled={isActing}
                          style={{
                            background: '#ef444415', color: '#f87171', border: '1px solid #ef444430',
                            borderRadius: '5px', padding: '3px 10px', fontSize: '0.68rem', fontWeight: '700',
                            cursor: isActing ? 'default' : 'pointer', fontFamily: FONT,
                          }}
                        >
                          {isActing ? '…' : 'Reject'}
                        </button>
                      </div>
                    </div>

                    {/* Expanded detail */}
                    {isExpanded && (
                      <div style={{ padding: '0 16px 12px', background: `${aiAccent}04` }}>
                        {sug.suggested_description && (
                          <div style={{ marginBottom: '8px' }}>
                            <div style={{ fontSize: '0.63rem', color: muted, fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: '3px' }}>
                              Suggested Description
                            </div>
                            <div style={{ fontSize: '0.78rem', color: textSec, lineHeight: 1.5 }}>
                              {sug.suggested_description}
                            </div>
                          </div>
                        )}
                        {(sug.suggested_domain || sug.suggested_entity) && (
                          <div style={{ display: 'flex', gap: '16px', marginBottom: '8px' }}>
                            {sug.suggested_domain && (
                              <div>
                                <div style={{ fontSize: '0.63rem', color: muted, fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: '2px' }}>Domain</div>
                                <div style={{ fontSize: '0.76rem', color: textSec }}>{sug.suggested_domain}</div>
                              </div>
                            )}
                            {sug.suggested_entity && (
                              <div>
                                <div style={{ fontSize: '0.63rem', color: muted, fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: '2px' }}>Entity</div>
                                <div style={{ fontSize: '0.76rem', color: textSec }}>{sug.suggested_entity}</div>
                              </div>
                            )}
                          </div>
                        )}
                        {reasoning.length > 0 && (
                          <div>
                            <div style={{ fontSize: '0.63rem', color: muted, fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: '4px' }}>
                              AI Reasoning
                            </div>
                            <ul style={{ margin: 0, padding: '0 0 0 16px' }}>
                              {reasoning.map((r, i) => (
                                <li key={i} style={{ fontSize: '0.74rem', color: textSec, lineHeight: 1.6 }}>{r}</li>
                              ))}
                            </ul>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )
        })()}

        {!selectedFqn && (
          <div style={{ ...card({ padding: '48px 24px' }), textAlign: 'center' }}>
            <div style={{ fontSize: '0.9rem', fontWeight: '500', color: textSec, marginBottom: '6px' }}>No table selected</div>
            <div style={{ fontSize: '0.78rem', color: muted }}>Choose a table from the left panel to review its dictionary entry.</div>
          </div>
        )}

        {selectedFqn && loadingDet && (
          <div style={{ ...card({ padding: '36px 24px' }), textAlign: 'center', color: muted, fontSize: '0.82rem' }}>
            Loading table details…
          </div>
        )}

        {selectedFqn && !loadingDet && details && (() => {
          const tbl = details.table ?? {}
          return (
            <>
              {/* Table metadata ─────────────────────────────────────────── */}
              <div style={card({ padding: '16px 20px' })}>
                <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: '12px', gap: '12px', flexWrap: 'wrap' }}>
                  <div style={{ minWidth: 0 }}>
                    <h3 style={{ margin: '0 0 4px', fontSize: '1.1rem', fontWeight: '700', color: text }}>{tbl.business_name}</h3>
                    <span style={{ fontSize: '0.72rem', color: muted, fontFamily: MONO }}>{tbl.table_fqn}</span>
                  </div>
                  <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', flexShrink: 0 }}>
                    {tbl.domain && <Badge label={tbl.domain} bg={`${accent}15`} color={accent} border={`${accent}30`} />}
                    {tbl.table_type === 'VIEW' && <Badge label="view" bg={`${muted}15`} color={muted} border={`${muted}30`} />}
                    {tbl.generation_method && (
                      <Badge label={tbl.generation_method.replace('_', ' ')} bg={`${muted}12`} color={muted} border={`${muted}25`} />
                    )}
                    {tbl.is_approved === 1 && <Badge label="approved" bg={`${success}15`} color={success} border={`${success}30`} />}
                    {tbl.is_approved !== 1 && (
                      <button
                        onClick={handleApproveTable}
                        disabled={approvingTable}
                        style={{
                          background: `${accent}15`, color: accent,
                          border: `1px solid ${accent}30`, borderRadius: '7px',
                          padding: '4px 12px', fontSize: '0.72rem', fontWeight: '600',
                          cursor: approvingTable ? 'default' : 'pointer', fontFamily: FONT,
                        }}
                      >
                        {approvingTable ? 'Approving…' : 'Approve Table'}
                      </button>
                    )}
                  </div>
                </div>

                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
                  {[
                    { key: 'Grain',       val: tbl.grain },
                    { key: 'Description', val: tbl.description },
                  ].map(({ key, val }) => val && (
                    <div key={key}>
                      <div style={lbl}>{key}</div>
                      <div style={{ fontSize: '0.82rem', color: textSec }}>{val}</div>
                    </div>
                  ))}
                </div>
              </div>

              {coverage && (
                <div style={{ display: 'flex', gap: '20px', flexWrap: 'wrap', padding: '4px 2px' }}>
                  {[
                    { label: 'tables',  val: coverage.tables_approved,  total: coverage.tables_total },
                    { label: 'columns', val: coverage.columns_approved, total: coverage.columns_total },
                  ].map(({ label, val, total }) => (
                    <span key={label} style={{ fontSize: '0.74rem', color: textSec }}>
                      <span style={{ fontWeight: '600', color: success }}>{val}</span>
                      <span style={{ color: muted }}> / {total} {label} approved</span>
                    </span>
                  ))}
                </div>
              )}

              {/* Column grid ─────────────────────────────────────────────── */}
              <div style={card({ overflow: 'hidden' })}>

                {/* Column filter bar */}
                <div style={{ padding: '10px 14px', borderBottom: `1px solid ${border}`, display: 'flex', gap: '10px', alignItems: 'center', flexWrap: 'wrap' }}>
                  <input
                    style={{ ...inp({ width: '200px', padding: '6px 10px', fontSize: '0.78rem' }) }}
                    placeholder="Search columns…"
                    value={colSearch}
                    onChange={e => setColSearch(e.target.value)}
                  />
                  <select
                    style={{ ...inp({ width: '150px', padding: '6px 10px', fontSize: '0.78rem', cursor: 'pointer' }) }}
                    value={piiFilter}
                    onChange={e => setPiiFilter(e.target.value)}
                  >
                    <option value="all">All columns</option>
                    <option value="pii">PII only</option>
                    <option value="no_pii">Non-PII only</option>
                  </select>
                  <span style={{ fontSize: '0.7rem', color: muted, marginLeft: 'auto' }}>
                    {filteredCols.length} / {details.columns?.length ?? 0} columns
                  </span>
                </div>

                {/* Horizontally scrollable grid area — header + rows scroll together */}
                <div style={{ overflowX: 'auto' }}>

                {/* Grid header */}
                <div style={{
                  display: 'grid', gridTemplateColumns: COLS_HDR.join(' '),
                  minWidth: '760px',
                  padding: '7px 14px', borderBottom: `1px solid ${border}`,
                  fontSize: '0.6rem', fontWeight: '700', color: muted,
                  letterSpacing: '0.06em', textTransform: 'uppercase', background: `${bg}80`,
                }}>
                  <span>Business Label</span>
                  <span>Column Name</span>
                  <span>Type</span>
                  <span>Flags</span>
                  <span>Meaning</span>
                  <span>Action</span>
                </div>

                {/* Grid rows */}
                <div style={{ maxHeight: '480px', overflowY: 'auto' }}>
                  {filteredCols.length === 0 && (
                    <div style={{ padding: '24px', textAlign: 'center', color: muted, fontSize: '0.78rem' }}>
                      No columns match the current filter.
                    </div>
                  )}
                  {filteredCols.map((col, i) => {
                    const isPii = Boolean(col.pii_risk)
                    const ss    = SEM[col.semantic_type] ?? {}
                    return (
                      <div
                        key={col.column_name}
                        style={{
                          display: 'grid', gridTemplateColumns: COLS_HDR.join(' '),
                          minWidth: '760px',
                          padding: '6px 14px', alignItems: 'center',
                          background: isPii
                            ? '#ef44440a'
                            : i % 2 === 0 ? 'transparent' : `${bg}60`,
                          borderBottom: `1px solid ${border}20`,
                          fontSize: '0.78rem',
                        }}
                      >
                        {/* Business label */}
                        <span style={{ color: text, fontWeight: '500', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', paddingRight: '8px' }}>
                          {col.business_label}
                        </span>

                        {/* Column name */}
                        <span style={{ color: muted, fontFamily: MONO, fontSize: '0.7rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', paddingRight: '8px' }}>
                          {col.column_name}
                        </span>

                        {/* Semantic type / PII badge */}
                        <span>
                          {isPii
                            ? <Badge label="PII"            bg={PII_S.bg}  color={PII_S.color}  border={PII_S.border} />
                            : ss.bg
                              ? <Badge label={col.semantic_type} bg={ss.bg} color={ss.color} border={ss.border} />
                              : <span style={{ fontSize: '0.68rem', color: muted }}>{col.semantic_type || '—'}</span>
                          }
                        </span>

                        {/* Flag badges */}
                        <span style={{ display: 'flex', gap: '3px', flexWrap: 'wrap' }}>
                          {Boolean(col.is_metric)    && <Badge label="M"  bg={SEM.metric.bg}    color={SEM.metric.color}    border={SEM.metric.border} />}
                          {Boolean(col.is_dimension) && <Badge label="D"  bg={SEM.dimension.bg} color={SEM.dimension.color} border={SEM.dimension.border} />}
                          {Boolean(col.is_date)      && <Badge label="Dt" bg={SEM.date.bg}      color={SEM.date.color}      border={SEM.date.border} />}
                          {Boolean(col.is_id)        && <Badge label="ID" bg={SEM.id.bg}        color={SEM.id.color}        border={SEM.id.border} />}
                        </span>

                        {/* Meaning */}
                        <span style={{ color: textSec, fontSize: '0.74rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                              title={col.meaning}>
                          {col.meaning}
                        </span>

                        {/* Approve */}
                        <span>
                          <button
                            onClick={() => !col.is_approved && !approvingCols.has(col.column_name) && handleApproveColumn(col.column_name)}
                            disabled={Boolean(col.is_approved) || approvingCols.has(col.column_name)}
                            style={{
                              background: col.is_approved ? `${success}15` : `${accent}10`,
                              color:      col.is_approved ? success : accent,
                              border: `1px solid ${col.is_approved ? success + '30' : accent + '25'}`,
                              borderRadius: '6px', padding: '2px 8px',
                              fontSize: '0.65rem', fontWeight: '600', fontFamily: FONT,
                              cursor: col.is_approved || approvingCols.has(col.column_name) ? 'default' : 'pointer',
                              whiteSpace: 'nowrap',
                            }}
                          >
                            {approvingCols.has(col.column_name) ? '…' : col.is_approved ? '✓' : 'Approve'}
                          </button>
                        </span>
                      </div>
                    )
                  })}
                </div>
                </div>{/* end overflowX scroll wrapper */}
              </div>
            </>
          )
        })()}
      </div>
    </div>
  )
}
