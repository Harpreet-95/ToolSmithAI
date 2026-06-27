import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { searchMetadata, getSearchFilters, getSearchSuggestions } from '../api/client'

// ── Constants ─────────────────────────────────────────────────────────────────

const ASSET_TYPE_OPTIONS = [
  { value: '',           label: 'All Types' },
  { value: 'table',      label: 'Tables' },
  { value: 'column',     label: 'Columns' },
  { value: 'dictionary', label: 'Dictionary' },
  { value: 'domain',     label: 'Domains' },
  { value: 'entity',     label: 'Entities' },
]

const FIELD_LABELS = {
  table_name:      'Table Name',
  business_name:   'Business Name',
  description:     'Description',
  schema_name:     'Schema',
  source_name:     'Source',
  table_class:     'Classification',
  dict_domain:     'Dictionary Domain',
  assigned_domain: 'Domain',
  assigned_entity: 'Entity',
  column_name:     'Column Name',
  business_label:  'Business Label',
  meaning:         'Meaning',
  semantic_type:   'Semantic Type',
}

const SUGGESTION_LABELS = {
  table:         'Table',
  column:        'Column',
  business_name: 'Business',
  domain:        'Domain',
  entity:        'Entity',
}

const SUGGESTION_COLORS = {
  table:         '#818cf8',
  column:        '#38bdf8',
  business_name: '#10b981',
  domain:        '#f59e0b',
  entity:        '#f472b6',
}

const PAGE_SIZE    = 20
const MAX_RECENT   = 10
const SHOW_REASONS = 3   // reasons shown before "show more"
const RECENT_KEY   = 'ts_search_recent'
const SAVED_KEY    = 'ts_search_saved'

const INITIAL_FILTERS = {
  assetType:          '',
  sourceFilter:       '',
  schemaFilter:       '',
  domainFilter:       '',
  entityFilter:       '',
  semanticTypeFilter: '',
  piiFilter:          false,
  dictStatusFilter:   '',
  classFilter:        '',
  profileStatusFilter:'',
}

// ── localStorage helpers ──────────────────────────────────────────────────────

function loadLocal(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key) ?? 'null') ?? fallback }
  catch { return fallback }
}
function saveLocal(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)) } catch {}
}

// ── Badge / chip helpers ──────────────────────────────────────────────────────

function assetTypeBadge(C, type) {
  const styles = {
    table:  { bg: '#6366f122', color: '#818cf8', label: 'TABLE' },
    column: { bg: '#0ea5e922', color: '#38bdf8', label: 'COLUMN' },
  }
  const s = styles[type] || { bg: '#94a3b822', color: '#94a3b8', label: (type || '').toUpperCase() }
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center',
      background: s.bg, color: s.color,
      border: `1px solid ${s.color}40`,
      borderRadius: '5px', padding: '2px 8px',
      fontSize: '0.67rem', fontWeight: '700', letterSpacing: '0.08em',
    }}>
      {s.label}
    </span>
  )
}

function piiChip() {
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: '4px',
      background: '#f8717122', color: '#f87171',
      border: '1px solid #f8717140',
      borderRadius: '5px', padding: '2px 8px',
      fontSize: '0.67rem', fontWeight: '700', letterSpacing: '0.06em',
    }}>
      PII
    </span>
  )
}

function dictChip(status) {
  if (status === 'none') return null
  const color   = status === 'approved' ? '#10b981' : '#f59e0b'
  const bgColor = status === 'approved' ? '#10b98122' : '#f59e0b22'
  const label   = status === 'approved' ? 'Dict ✓' : 'Dict'
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center',
      background: bgColor, color,
      border: `1px solid ${color}40`,
      borderRadius: '5px', padding: '2px 8px',
      fontSize: '0.67rem', fontWeight: '700', letterSpacing: '0.06em',
    }}>
      {label}
    </span>
  )
}

function scoreBar(score, C) {
  const pct   = Math.min(100, Math.round((score / 400) * 100))
  const color = pct >= 70 ? '#10b981' : pct >= 40 ? '#f59e0b' : '#60a5fa'
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }} title={`Relevance score: ${score}`}>
      <div style={{ width: '52px', height: '4px', borderRadius: '2px', background: C.border, overflow: 'hidden' }}>
        <div style={{ width: `${pct}%`, height: '100%', background: color, borderRadius: '2px' }} />
      </div>
      <span style={{ fontSize: '0.67rem', color: C.textMuted, fontVariantNumeric: 'tabular-nums' }}>
        {score}
      </span>
    </div>
  )
}

function confidenceBadge(C, confidence) {
  if (!confidence || confidence <= 0) return null
  const pct = Math.round(confidence * 100)
  const color = pct >= 80 ? '#10b981' : pct >= 60 ? '#f59e0b' : '#94a3b8'
  return (
    <span style={{
      fontSize: '0.67rem', color,
      background: `${color}18`, border: `1px solid ${color}40`,
      borderRadius: '4px', padding: '2px 6px', fontWeight: '600',
    }} title="Assignment confidence">
      {pct}% conf
    </span>
  )
}

// ── Highlighted text (for autocomplete) ──────────────────────────────────────

function HighlightText({ text, query, C }) {
  if (!query || !text) return <span>{text}</span>
  const idx = text.toLowerCase().indexOf(query.toLowerCase())
  if (idx === -1) return <span>{text}</span>
  return (
    <span>
      {text.slice(0, idx)}
      <strong style={{ color: C.accent, fontWeight: '700' }}>
        {text.slice(idx, idx + query.length)}
      </strong>
      {text.slice(idx + query.length)}
    </span>
  )
}

// ── Copy button ───────────────────────────────────────────────────────────────

function CopyButton({ C, text, label, activeKey, copiedKey, onCopy }) {
  const done = copiedKey === activeKey
  return (
    <button
      onClick={e => { e.stopPropagation(); onCopy(text, activeKey) }}
      title={label}
      aria-label={done ? 'Copied!' : label}
      style={{
        display: 'inline-flex', alignItems: 'center', gap: '3px',
        background: done ? '#10b98118' : 'transparent',
        border: `1px solid ${done ? '#10b98150' : C.border}`,
        borderRadius: '5px', color: done ? '#10b981' : C.textMuted,
        cursor: 'pointer', fontSize: '0.67rem', fontWeight: '600',
        padding: '2px 7px', transition: 'all 0.15s', whiteSpace: 'nowrap',
      }}
      onMouseEnter={e => { if (!done) { e.currentTarget.style.color = C.text; e.currentTarget.style.borderColor = C.accent } }}
      onMouseLeave={e => { if (!done) { e.currentTarget.style.color = C.textMuted; e.currentTarget.style.borderColor = C.border } }}
    >
      {done ? <CheckIcon size={9} /> : <CopyIcon size={9} />}
      {done ? 'Copied' : label}
    </button>
  )
}

// ── Sub-components ────────────────────────────────────────────────────────────

function PathCrumb({ C, label, value }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
      <span style={{ fontSize: '0.65rem', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.06em' }}>
        {label}
      </span>
      <span style={{
        fontSize: '0.78rem', color: C.textSec,
        background: C.bg, border: `1px solid ${C.border}`,
        borderRadius: '5px', padding: '1px 7px',
      }}>
        {value}
      </span>
      <span style={{ color: C.border, fontSize: '0.75rem', userSelect: 'none' }}>›</span>
    </div>
  )
}

function MetaChip({ C, label, value }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
      <span style={{ fontSize: '0.65rem', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
        {label}
      </span>
      <span style={{ fontSize: '0.78rem', color: C.textSec }}>{value}</span>
    </div>
  )
}

// ── Result card ───────────────────────────────────────────────────────────────

function ResultCard({ result, C, onOpenAsset, expandedReasons, onToggleReasons, onCopy, copiedKey }) {
  const matchedLabel   = FIELD_LABELS[result.matched_field] || result.matched_field
  const allReasons     = result.match_reasons || []
  const isExpanded     = expandedReasons?.has(result.qualified_name)
  const visibleReasons = isExpanded ? allReasons : allReasons.slice(0, SHOW_REASONS)
  const hiddenCount    = allReasons.length - SHOW_REASONS

  return (
    <div
      style={{
        background: C.surface, border: `1px solid ${C.border}`,
        borderRadius: '10px', padding: '16px 20px',
        display: 'flex', flexDirection: 'column', gap: '8px',
        transition: 'border-color 0.15s',
      }}
      onMouseEnter={e => e.currentTarget.style.borderColor = C.accent}
      onMouseLeave={e => e.currentTarget.style.borderColor = C.border}
    >
      {/* ── Top row: type + name + badges + score ── */}
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: '10px', flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap', flex: 1 }}>
          {assetTypeBadge(C, result.asset_type)}
          <span style={{ color: C.text, fontWeight: '600', fontSize: '0.95rem', lineHeight: 1.3 }}>
            {result.display_name}
          </span>
          {result.pii_indicator && piiChip()}
          {dictChip(result.dictionary_status)}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexShrink: 0 }}>
          {confidenceBadge(C, result.confidence)}
          {scoreBar(result.relevance_score, C)}
        </div>
      </div>

      {/* ── Asset path ── */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '4px', flexWrap: 'wrap' }}>
        <PathCrumb C={C} label="Source"  value={result.source_name} />
        {result.schema_name && <PathCrumb C={C} label="Schema" value={result.schema_name} />}
        {result.table_name && result.asset_type === 'column' &&
          <PathCrumb C={C} label="Table" value={result.table_name} />}
        {result.column_name &&
          <PathCrumb C={C} label="Column" value={result.column_name} />}
      </div>

      {/* ── Copy buttons ── */}
      <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
        <CopyButton C={C} text={result.qualified_name} label="Copy Path"
          activeKey={`path-${result.qualified_name}`} copiedKey={copiedKey} onCopy={onCopy} />
        {result.table_name && (
          <CopyButton C={C} text={result.table_name} label="Copy Table"
            activeKey={`table-${result.qualified_name}`} copiedKey={copiedKey} onCopy={onCopy} />
        )}
        {result.column_name && (
          <CopyButton C={C} text={result.column_name} label="Copy Column"
            activeKey={`col-${result.qualified_name}`} copiedKey={copiedKey} onCopy={onCopy} />
        )}
      </div>

      {/* ── Match reasons (expandable) ── */}
      {allReasons.length > 0 && (
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap' }}>
          <span style={{ fontSize: '0.65rem', color: C.textMuted, flexShrink: 0 }}>Matched:</span>
          {visibleReasons.map((r, i) => (
            <span key={i} style={{
              fontSize: '0.67rem', color: C.accent,
              background: C.accentSoft, borderRadius: '4px', padding: '1px 6px',
            }}>
              {r}
            </span>
          ))}
          {hiddenCount > 0 && (
            <button
              onClick={() => onToggleReasons(result.qualified_name)}
              aria-expanded={isExpanded}
              style={{
                background: 'transparent', border: 'none',
                color: C.textMuted, cursor: 'pointer',
                fontSize: '0.67rem', padding: '1px 4px',
                textDecoration: 'underline', textUnderlineOffset: '2px',
              }}
            >
              {isExpanded ? 'Show less' : `+${hiddenCount} more`}
            </button>
          )}
        </div>
      )}
      {/* Fallback to single matched_field when no match_reasons */}
      {allReasons.length === 0 && result.matched_field !== 'unknown' && (
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
          <span style={{ fontSize: '0.72rem', color: C.textMuted }}>Matched:</span>
          <span style={{ fontSize: '0.72rem', color: C.accent, background: C.accentSoft, borderRadius: '4px', padding: '1px 6px' }}>
            {matchedLabel}
          </span>
        </div>
      )}

      {/* ── Description ── */}
      {result.short_description && (
        <p style={{ margin: 0, fontSize: '0.82rem', color: C.textSec, lineHeight: 1.55, maxWidth: '720px' }}>
          {result.short_description}
        </p>
      )}

      {/* ── Bottom row ── */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap', marginTop: '2px' }}>
        {result.domain        && <MetaChip C={C} label="Domain"    value={result.domain} />}
        {result.entity        && <MetaChip C={C} label="Entity"    value={result.entity} />}
        {result.semantic_type && <MetaChip C={C} label="Class"     value={result.semantic_type} />}
        {result.profiled_at   && (
          <span style={{ fontSize: '0.65rem', color: C.textMuted }}>
            Profiled {result.profiled_at.slice(0, 10)}
          </span>
        )}
        <div style={{ flex: 1 }} />
        <button
          onClick={() => onOpenAsset(result)}
          title={`Open in ${result.nav_target?.tab || 'data-sources'}`}
          style={{
            background: 'transparent', border: `1px solid ${C.border}`,
            borderRadius: '7px', color: C.accent, cursor: 'pointer',
            fontSize: '0.78rem', fontWeight: '600', padding: '5px 14px',
            transition: 'background 0.12s',
          }}
          onMouseEnter={e => e.currentTarget.style.background = C.accentSoft}
          onMouseLeave={e => e.currentTarget.style.background = 'transparent'}
        >
          Open Asset →
        </button>
      </div>
    </div>
  )
}

// ── Filter controls ───────────────────────────────────────────────────────────

function FilterSelect({ C, label, value, onChange, options, id }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
      <label htmlFor={id} style={{
        fontSize: '0.63rem', color: C.textMuted,
        fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.07em',
      }}>
        {label}
      </label>
      <select
        id={id}
        value={value}
        onChange={e => onChange(e.target.value)}
        aria-label={label}
        style={{
          background: C.bg, border: `1px solid ${C.border}`,
          borderRadius: '7px', color: C.text,
          fontSize: '0.8rem', padding: '6px 9px',
          cursor: 'pointer', minWidth: '110px', outline: 'none',
        }}
      >
        {options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </div>
  )
}

function FilterToggle({ C, label, active, onToggle }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
      <label style={{ fontSize: '0.63rem', color: C.textMuted, fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.07em' }}>
        {label}
      </label>
      <button
        type="button"
        onClick={onToggle}
        aria-pressed={active}
        style={{
          background: active ? '#f8717122' : C.bg,
          border: `1px solid ${active ? '#f87171' : C.border}`,
          borderRadius: '7px', color: active ? '#f87171' : C.textSec,
          cursor: 'pointer', fontSize: '0.8rem', fontWeight: '600',
          padding: '6px 12px', transition: 'all 0.15s',
        }}
      >
        {active ? '✓ PII' : 'PII'}
      </button>
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────

export default function EnterpriseSearch({ C, token, setActiveNav, openSource }) {

  // ── Search state ─────────────────────────────────────────────────────────
  const [query,     setQuery]     = useState('')
  const [submitted, setSubmitted] = useState('')
  const [page,      setPage]      = useState(0)
  const [filters,   setFilters]   = useState(INITIAL_FILTERS)

  // ── Results ───────────────────────────────────────────────────────────────
  const [loading,        setLoading]        = useState(false)
  const [error,          setError]          = useState(null)
  const [results,        setResults]        = useState([])
  const [total,          setTotal]          = useState(0)
  const [tokens,         setTokens]         = useState([])
  const [searchDuration, setSearchDuration] = useState(null)

  // ── Autocomplete ──────────────────────────────────────────────────────────
  const [suggestions,    setSuggestions]    = useState([])
  const [suggLoading,    setSuggLoading]    = useState(false)
  const [inputFocused,   setInputFocused]   = useState(false)
  const [activeIndex,    setActiveIndex]    = useState(-1)

  // ── History / saved ───────────────────────────────────────────────────────
  const [recentSearches, setRecentSearches] = useState(() => loadLocal(RECENT_KEY, []))
  const [savedSearches,  setSavedSearches]  = useState(() => loadLocal(SAVED_KEY, []))
  const [showSaved,      setShowSaved]      = useState(false)
  const [editingId,      setEditingId]      = useState(null)
  const [editingName,    setEditingName]    = useState('')

  // ── Card UX ───────────────────────────────────────────────────────────────
  const [expandedReasons, setExpandedReasons] = useState(new Set())
  const [copiedKey,       setCopiedKey]       = useState(null)

  // ── Filter options ────────────────────────────────────────────────────────
  const [filterOptions, setFilterOptions] = useState(null)

  // ── Refs ──────────────────────────────────────────────────────────────────
  const inputRef        = useRef(null)
  const abortRef        = useRef(null)
  const suggestAbortRef = useRef(null)
  const suggestTimer    = useRef(null)
  const searchCacheRef  = useRef(null)   // {key, results, total, tokens}
  const dropdownRef     = useRef(null)   // wraps input + dropdown for click-outside
  const filterPanelRef  = useRef(null)

  // ── Unified dropdown items ────────────────────────────────────────────────
  // When input empty and focused → recent searches; when typing → suggestions
  const dropdownItems = useMemo(() => {
    if (query.length === 0 && recentSearches.length > 0) {
      return recentSearches.slice(0, 8).map(r => ({ text: r, type: 'recent' }))
    }
    if (query.length > 0 && suggestions.length > 0) {
      return suggestions
    }
    return []
  }, [query, recentSearches, suggestions])

  const dropdownOpen = inputFocused && dropdownItems.length > 0

  // ── On mount ─────────────────────────────────────────────────────────────
  useEffect(() => {
    inputRef.current?.focus()
    if (!token) return
    getSearchFilters(token)
      .then(res => setFilterOptions(res?.data || null))
      .catch(() => {})
  }, [token])

  // ── Click-outside closes dropdown ────────────────────────────────────────
  useEffect(() => {
    const handler = e => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target)) {
        setInputFocused(false)
        setActiveIndex(-1)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  // ── Global keyboard shortcuts ─────────────────────────────────────────────
  useEffect(() => {
    const handler = e => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
        e.preventDefault()
        inputRef.current?.focus()
        inputRef.current?.select()
      }
      if ((e.ctrlKey || e.metaKey) && e.key === '/') {
        e.preventDefault()
        filterPanelRef.current?.querySelector('select, button')?.focus()
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])

  // ── Debounced suggestion fetch ────────────────────────────────────────────
  useEffect(() => {
    clearTimeout(suggestTimer.current)
    if (!query.trim() || !token) {
      setSuggestions([])
      return
    }
    suggestTimer.current = setTimeout(async () => {
      if (suggestAbortRef.current) suggestAbortRef.current.abort()
      const ctrl = new AbortController()
      suggestAbortRef.current = ctrl
      setSuggLoading(true)
      try {
        const res = await getSearchSuggestions(query.trim(), token)
        setSuggestions(res?.data || [])
      } catch {
        setSuggestions([])
      } finally {
        setSuggLoading(false)
      }
    }, 300)
    return () => clearTimeout(suggestTimer.current)
  }, [query, token])

  // ── Cache key builder ─────────────────────────────────────────────────────
  const buildCacheKey = (q, f) =>
    JSON.stringify([q, f.assetType, f.sourceFilter, f.schemaFilter,
                    f.domainFilter, f.entityFilter, f.semanticTypeFilter,
                    f.piiFilter, f.dictStatusFilter, f.classFilter, f.profileStatusFilter])

  // ── Core search ───────────────────────────────────────────────────────────
  const runSearch = useCallback(async (q, searchFilters, pageNum) => {
    if (!q.trim()) return

    const cacheKey = buildCacheKey(q, searchFilters)

    // Return cached response without network round-trip
    if (pageNum === 0 && searchCacheRef.current?.key === cacheKey) {
      const c = searchCacheRef.current
      setResults(c.results)
      setTotal(c.total)
      setTokens(c.tokens)
      setSearchDuration(0)
      setLoading(false)
      return
    }

    if (abortRef.current) abortRef.current.abort()
    const ctrl = new AbortController()
    abortRef.current = ctrl

    setLoading(true)
    setError(null)
    setSearchDuration(null)
    const t0 = performance.now()

    try {
      const params = { limit: PAGE_SIZE, offset: pageNum * PAGE_SIZE }
      if (searchFilters.assetType)           params.asset_type         = searchFilters.assetType
      if (searchFilters.sourceFilter)        params.source_id          = Number(searchFilters.sourceFilter)
      if (searchFilters.schemaFilter)        params.schema             = searchFilters.schemaFilter
      if (searchFilters.domainFilter)        params.domain             = searchFilters.domainFilter
      if (searchFilters.entityFilter)        params.entity             = searchFilters.entityFilter
      if (searchFilters.semanticTypeFilter)  params.semantic_type      = searchFilters.semanticTypeFilter
      if (searchFilters.piiFilter)           params.pii                = true
      if (searchFilters.dictStatusFilter)    params.dictionary_status  = searchFilters.dictStatusFilter
      if (searchFilters.classFilter)         params.classification     = searchFilters.classFilter
      if (searchFilters.profileStatusFilter) params.profile_status     = searchFilters.profileStatusFilter

      const res = await searchMetadata(q, token, params)
      const d   = res?.data || {}
      const newResults = d.results || []
      const newTotal   = d.total   || 0
      const newTokens  = d.tokens  || []

      setResults(newResults)
      setTotal(newTotal)
      setTokens(newTokens)
      setSearchDuration(Math.round(performance.now() - t0))

      // Cache first-page results
      if (pageNum === 0) {
        searchCacheRef.current = { key: cacheKey, results: newResults, total: newTotal, tokens: newTokens }
      }
    } catch (err) {
      if (err.name === 'AbortError') return
      setError(err.message || 'Search failed. Please try again.')
      setResults([])
      setTotal(0)
      setSearchDuration(null)
    } finally {
      setLoading(false)
    }
  }, [token])

  // ── Recent searches helpers ───────────────────────────────────────────────
  const addRecentSearch = (q) => {
    if (!q.trim()) return
    setRecentSearches(prev => {
      const deduped = [q, ...prev.filter(s => s !== q)].slice(0, MAX_RECENT)
      saveLocal(RECENT_KEY, deduped)
      return deduped
    })
  }

  const clearRecentSearches = () => {
    setRecentSearches([])
    saveLocal(RECENT_KEY, [])
  }

  // ── Saved searches helpers ────────────────────────────────────────────────
  const saveCurrentSearch = () => {
    if (!submitted) return
    const name = submitted
    const entry = { id: Date.now(), name, query: submitted, filters: { ...filters } }
    setSavedSearches(prev => {
      const updated = [entry, ...prev]
      saveLocal(SAVED_KEY, updated)
      return updated
    })
  }

  const deleteSavedSearch = (id) => {
    setSavedSearches(prev => {
      const updated = prev.filter(s => s.id !== id)
      saveLocal(SAVED_KEY, updated)
      return updated
    })
  }

  const executeSavedSearch = (saved) => {
    const q = saved.query
    const f = saved.filters || INITIAL_FILTERS
    setQuery(q)
    setFilters(f)
    setSubmitted(q)
    setPage(0)
    setShowSaved(false)
    addRecentSearch(q)
    runSearch(q, f, 0)
  }

  const startRename = (saved) => {
    setEditingId(saved.id)
    setEditingName(saved.name)
  }

  const finishRename = () => {
    setSavedSearches(prev => {
      const updated = prev.map(s => s.id === editingId ? { ...s, name: editingName.trim() || s.name } : s)
      saveLocal(SAVED_KEY, updated)
      return updated
    })
    setEditingId(null)
  }

  // ── Suggestion selection ──────────────────────────────────────────────────
  const selectItem = (item) => {
    const text = item.text
    setQuery(text)
    setActiveIndex(-1)
    setInputFocused(false)
    setSuggestions([])
    setSubmitted(text)
    setPage(0)
    addRecentSearch(text)
    runSearch(text, filters, 0)
  }

  // ── Handlers ─────────────────────────────────────────────────────────────
  const handleSubmit = (e) => {
    e?.preventDefault()
    // If an item is highlighted in dropdown, select it
    if (dropdownOpen && activeIndex >= 0 && dropdownItems[activeIndex]) {
      selectItem(dropdownItems[activeIndex])
      return
    }
    if (!query.trim()) return
    setActiveIndex(-1)
    setInputFocused(false)
    setSuggestions([])
    setSubmitted(query.trim())
    setPage(0)
    addRecentSearch(query.trim())
    runSearch(query.trim(), filters, 0)
  }

  const handleKeyDown = (e) => {
    if (!dropdownOpen) return
    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault()
        setActiveIndex(i => Math.min(i + 1, dropdownItems.length - 1))
        break
      case 'ArrowUp':
        e.preventDefault()
        setActiveIndex(i => Math.max(i - 1, -1))
        break
      case 'Enter':
        if (activeIndex >= 0) {
          e.preventDefault()
          selectItem(dropdownItems[activeIndex])
        }
        break
      case 'Escape':
        e.preventDefault()
        setInputFocused(false)
        setActiveIndex(-1)
        break
      default:
        break
    }
  }

  const handleFilterChange = (key, val) => {
    const next = { ...filters, [key]: val }
    setFilters(next)
    if (submitted) { setPage(0); runSearch(submitted, next, 0) }
  }

  const handleClearFilters = () => {
    setFilters(INITIAL_FILTERS)
    if (submitted) { setPage(0); runSearch(submitted, INITIAL_FILTERS, 0) }
  }

  const handlePage = (dir) => {
    const next = page + dir
    if (next < 0) return
    setPage(next)
    runSearch(submitted, filters, next)
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  const handleOpenAsset = (result) => {
    const nav = result.nav_target
    if (!nav) { setActiveNav?.('data-sources'); return }
    const tab = nav.tab || (nav.type === 'column' ? 'profile' : 'schema')
    if (openSource && nav.source_id != null) openSource(nav.source_id, tab)
    else setActiveNav?.('data-sources')
  }

  const handleCopy = async (text, key) => {
    try {
      await navigator.clipboard.writeText(text)
      setCopiedKey(key)
      setTimeout(() => setCopiedKey(null), 1500)
    } catch {}
  }

  const toggleReasons = (qualifiedName) => {
    setExpandedReasons(prev => {
      const next = new Set(prev)
      if (next.has(qualifiedName)) next.delete(qualifiedName)
      else next.add(qualifiedName)
      return next
    })
  }

  // ── Derived values ────────────────────────────────────────────────────────
  const hasResults      = results.length > 0
  const hasSearched     = submitted.length > 0
  const totalPages      = Math.ceil(total / PAGE_SIZE)
  const hasActiveFilter = Object.entries(filters).some(([, v]) => v !== '' && v !== false)
  const noMetadata      = filterOptions !== null && !filterOptions.schemas?.length && !filterOptions.sources?.length

  const fo = filterOptions
  const mkOpts = (list, placeholder) =>
    list?.length ? [{ value: '', label: placeholder }, ...list.map(v => ({ value: v, label: v }))] : null
  const sourceOpts = fo?.sources?.length
    ? [{ value: '', label: 'All Sources' }, ...fo.sources.map(s => ({ value: String(s.id), label: s.name }))]
    : null
  const schemaOpts = mkOpts(fo?.schemas, 'All Schemas')
  const domainOpts = mkOpts(fo?.domains, 'All Domains')
  const entityOpts = mkOpts(fo?.entities, 'All Entities')
  const semTypeOpts = mkOpts(fo?.semantic_types, 'Any Type')
  const classOpts  = mkOpts(fo?.classifications, 'Any Class')
  const dictOpts   = fo?.dictionary_statuses?.length
    ? [{ value: '', label: 'Any Status' }, ...fo.dictionary_statuses.map(s => ({ value: s, label: s.charAt(0).toUpperCase() + s.slice(1) }))]
    : null
  const profileOpts = mkOpts(fo?.profile_statuses, 'Any Status')

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div style={{ maxWidth: '960px', margin: '0 auto', padding: '32px 24px' }}>

      {/* Screen-reader live region for result count */}
      <div aria-live="polite" aria-atomic="true" style={{ position: 'absolute', left: '-9999px', width: '1px', height: '1px', overflow: 'hidden' }}>
        {hasSearched && !loading && `${total} results found`}
      </div>

      {/* ── Page header ── */}
      <div style={{ marginBottom: '20px', display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '12px', flexWrap: 'wrap' }}>
        <div>
          <h1 style={{ margin: 0, color: C.text, fontSize: '1.45rem', fontWeight: '700' }}>
            Enterprise Metadata Search
          </h1>
          <p style={{ margin: '4px 0 0', color: C.textMuted, fontSize: '0.83rem' }}>
            Tables · Columns · Business definitions · Domains · Entities
            <span style={{ marginLeft: '12px', color: C.border }}>·</span>
            <span style={{ marginLeft: '12px', fontSize: '0.75rem', color: C.textMuted }}>
              Ctrl+K to focus
            </span>
          </p>
        </div>
        {/* Saved searches toggle */}
        <button
          onClick={() => setShowSaved(v => !v)}
          aria-expanded={showSaved}
          aria-controls="saved-searches-panel"
          style={{
            display: 'flex', alignItems: 'center', gap: '6px',
            background: showSaved ? C.accentSoft : 'transparent',
            border: `1px solid ${showSaved ? C.accent : C.border}`,
            borderRadius: '8px', color: showSaved ? C.accent : C.textSec,
            cursor: 'pointer', fontSize: '0.8rem', fontWeight: '600',
            padding: '7px 14px', transition: 'all 0.15s',
            whiteSpace: 'nowrap',
          }}
        >
          <BookmarkIcon size={13} />
          Saved{savedSearches.length > 0 ? ` (${savedSearches.length})` : ''}
        </button>
      </div>

      {/* ── Saved searches panel ── */}
      {showSaved && (
        <div id="saved-searches-panel" style={{
          background: C.surface, border: `1px solid ${C.border}`,
          borderRadius: '10px', padding: '14px 16px', marginBottom: '16px',
        }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '10px' }}>
            <span style={{ fontSize: '0.8rem', fontWeight: '600', color: C.text }}>Saved Searches</span>
            {submitted && (
              <button
                onClick={saveCurrentSearch}
                style={{
                  display: 'flex', alignItems: 'center', gap: '5px',
                  background: C.accent, color: '#fff', border: 'none',
                  borderRadius: '6px', cursor: 'pointer',
                  fontSize: '0.75rem', fontWeight: '600', padding: '4px 10px',
                }}
              >
                + Save current search
              </button>
            )}
          </div>
          {savedSearches.length === 0 ? (
            <p style={{ margin: 0, fontSize: '0.82rem', color: C.textMuted }}>
              No saved searches yet. Run a search and click "+ Save current search".
            </p>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
              {savedSearches.map(s => (
                <div key={s.id} style={{
                  display: 'flex', alignItems: 'center', gap: '8px',
                  background: C.bg, border: `1px solid ${C.border}`,
                  borderRadius: '7px', padding: '7px 10px',
                }}>
                  {editingId === s.id ? (
                    <input
                      autoFocus
                      value={editingName}
                      onChange={e => setEditingName(e.target.value)}
                      onBlur={finishRename}
                      onKeyDown={e => { if (e.key === 'Enter') finishRename(); if (e.key === 'Escape') setEditingId(null) }}
                      aria-label="Rename saved search"
                      style={{
                        flex: 1, background: 'transparent', border: 'none',
                        borderBottom: `1px solid ${C.accent}`, color: C.text,
                        fontSize: '0.82rem', outline: 'none', padding: '0 2px',
                      }}
                    />
                  ) : (
                    <span style={{ flex: 1, fontSize: '0.82rem', color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {s.name}
                    </span>
                  )}
                  <button onClick={() => executeSavedSearch(s)} title="Run this search"
                    style={smallActionBtn(C, C.accent)}
                    aria-label={`Run saved search: ${s.name}`}>
                    ▶ Run
                  </button>
                  <button onClick={() => startRename(s)} title="Rename"
                    style={smallActionBtn(C, C.textMuted)}
                    aria-label={`Rename saved search: ${s.name}`}>
                    ✎
                  </button>
                  <button onClick={() => deleteSavedSearch(s.id)} title="Delete"
                    style={smallActionBtn(C, '#f87171')}
                    aria-label={`Delete saved search: ${s.name}`}>
                    ✕
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* ── Search bar + dropdown ── */}
      <div ref={dropdownRef} style={{ position: 'relative', marginBottom: '16px' }}>
        <form onSubmit={handleSubmit}>
          <div style={{ display: 'flex', gap: '10px' }}>
            <div style={{ flex: 1, position: 'relative' }}>
              <SearchIcon style={{
                position: 'absolute', left: '14px', top: '50%', transform: 'translateY(-50%)',
                color: C.textMuted, pointerEvents: 'none',
              }} />
              <input
                ref={inputRef}
                type="text"
                role="combobox"
                aria-label="Search metadata"
                aria-autocomplete="list"
                aria-expanded={dropdownOpen}
                aria-controls="search-dropdown"
                aria-activedescendant={activeIndex >= 0 ? `suggestion-${activeIndex}` : undefined}
                value={query}
                onChange={e => { setQuery(e.target.value); setActiveIndex(-1) }}
                onFocus={() => setInputFocused(true)}
                onKeyDown={handleKeyDown}
                placeholder="Search metadata… e.g. Employee Email, Finance, Invoices"
                autoComplete="off"
                style={{
                  width: '100%', boxSizing: 'border-box',
                  background: C.surface, border: `1.5px solid ${C.border}`,
                  borderRadius: '10px', color: C.text,
                  fontSize: '1rem', padding: '14px 14px 14px 44px',
                  outline: 'none', transition: 'border-color 0.15s',
                }}
                onFocus={e => { setInputFocused(true); e.target.style.borderColor = C.accent }}
                onBlur={e  => e.target.style.borderColor = C.border}
              />
            </div>
            <button
              type="submit"
              disabled={!query.trim() || loading}
              aria-label="Search"
              style={{
                background: C.accent, color: '#fff', border: 'none',
                borderRadius: '10px', padding: '0 28px',
                fontSize: '0.9rem', fontWeight: '600', cursor: 'pointer',
                opacity: !query.trim() || loading ? 0.5 : 1,
                transition: 'opacity 0.15s', whiteSpace: 'nowrap',
              }}
            >
              {loading ? 'Searching…' : 'Search'}
            </button>
          </div>
        </form>

        {/* ── Autocomplete / Recent searches dropdown ── */}
        {dropdownOpen && (
          <div
            id="search-dropdown"
            role="listbox"
            aria-label={query.length === 0 ? 'Recent searches' : 'Search suggestions'}
            style={{
              position: 'absolute', top: 'calc(100% + 4px)', left: 0, right: '90px',
              background: C.surface, border: `1px solid ${C.border}`,
              borderRadius: '10px', boxShadow: '0 8px 24px #0006',
              zIndex: 100, overflow: 'hidden',
            }}
          >
            {/* Header row */}
            <div style={{
              display: 'flex', justifyContent: 'space-between', alignItems: 'center',
              padding: '8px 12px 4px',
              borderBottom: `1px solid ${C.border}`,
            }}>
              <span style={{ fontSize: '0.65rem', color: C.textMuted, fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                {query.length === 0 ? 'Recent Searches' : (suggLoading ? 'Searching…' : 'Suggestions')}
              </span>
              {query.length === 0 && recentSearches.length > 0 && (
                <button
                  onClick={clearRecentSearches}
                  style={{
                    background: 'none', border: 'none', color: C.textMuted,
                    cursor: 'pointer', fontSize: '0.72rem', padding: '2px 4px',
                  }}
                >
                  Clear
                </button>
              )}
            </div>

            {/* Items */}
            {dropdownItems.map((item, i) => {
              const isActive = i === activeIndex
              const typeColor = item.type === 'recent' ? C.textMuted : (SUGGESTION_COLORS[item.type] || C.textMuted)
              const typeLabel = item.type === 'recent' ? '' : (SUGGESTION_LABELS[item.type] || item.type)
              return (
                <div
                  key={`${item.type}-${item.text}-${i}`}
                  id={`suggestion-${i}`}
                  role="option"
                  aria-selected={isActive}
                  onMouseDown={e => { e.preventDefault(); selectItem(item) }}
                  onMouseEnter={() => setActiveIndex(i)}
                  style={{
                    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                    padding: '8px 12px', cursor: 'pointer', gap: '8px',
                    background: isActive ? C.accentSoft : 'transparent',
                    transition: 'background 0.1s',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px', minWidth: 0 }}>
                    {item.type === 'recent'
                      ? <ClockIcon size={12} style={{ color: C.textMuted, flexShrink: 0 }} />
                      : <span style={{ width: '6px', height: '6px', borderRadius: '50%', background: typeColor, flexShrink: 0 }} />
                    }
                    <span style={{ fontSize: '0.85rem', color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {query.length > 0
                        ? <HighlightText text={item.text} query={query} C={C} />
                        : item.text
                      }
                    </span>
                  </div>
                  {typeLabel && (
                    <span style={{
                      fontSize: '0.65rem', color: typeColor, fontWeight: '600',
                      background: `${typeColor}18`, borderRadius: '4px', padding: '1px 5px',
                      flexShrink: 0, textTransform: 'uppercase', letterSpacing: '0.04em',
                    }}>
                      {typeLabel}
                    </span>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* Token hint */}
      {tokens.length > 0 && !dropdownOpen && (
        <div style={{ marginBottom: '10px', display: 'flex', gap: '6px', alignItems: 'center', flexWrap: 'wrap' }}>
          <span style={{ fontSize: '0.72rem', color: C.textMuted }}>Searching for:</span>
          {tokens.map(t => (
            <span key={t} style={{ fontSize: '0.72rem', background: C.accentSoft, color: C.accent, borderRadius: '4px', padding: '2px 7px' }}>
              {t}
            </span>
          ))}
        </div>
      )}

      {/* ── Sticky filter panel ── */}
      <div
        ref={filterPanelRef}
        style={{ position: 'sticky', top: 0, zIndex: 10, background: C.bg, paddingBottom: '16px' }}
        aria-label="Search filters"
      >
        <div style={{
          background: C.surface, border: `1px solid ${C.border}`,
          borderRadius: '10px', padding: '12px 16px',
          display: 'flex', gap: '10px', flexWrap: 'wrap', alignItems: 'flex-end',
        }}>
          <FilterSelect C={C} label="Asset Type" id="filter-asset-type"
            value={filters.assetType} onChange={v => handleFilterChange('assetType', v)}
            options={ASSET_TYPE_OPTIONS} />
          {sourceOpts && (
            <FilterSelect C={C} label="Source" id="filter-source"
              value={filters.sourceFilter} onChange={v => handleFilterChange('sourceFilter', v)}
              options={sourceOpts} />
          )}
          {schemaOpts && (
            <FilterSelect C={C} label="Schema" id="filter-schema"
              value={filters.schemaFilter} onChange={v => handleFilterChange('schemaFilter', v)}
              options={schemaOpts} />
          )}
          {domainOpts && (
            <FilterSelect C={C} label="Domain" id="filter-domain"
              value={filters.domainFilter} onChange={v => handleFilterChange('domainFilter', v)}
              options={domainOpts} />
          )}
          {entityOpts && (
            <FilterSelect C={C} label="Entity" id="filter-entity"
              value={filters.entityFilter} onChange={v => handleFilterChange('entityFilter', v)}
              options={entityOpts} />
          )}
          {semTypeOpts && (
            <FilterSelect C={C} label="Semantic Type" id="filter-semtype"
              value={filters.semanticTypeFilter} onChange={v => handleFilterChange('semanticTypeFilter', v)}
              options={semTypeOpts} />
          )}
          {classOpts && (
            <FilterSelect C={C} label="Classification" id="filter-class"
              value={filters.classFilter} onChange={v => handleFilterChange('classFilter', v)}
              options={classOpts} />
          )}
          {dictOpts && (
            <FilterSelect C={C} label="Dictionary" id="filter-dict"
              value={filters.dictStatusFilter} onChange={v => handleFilterChange('dictStatusFilter', v)}
              options={dictOpts} />
          )}
          {profileOpts && (
            <FilterSelect C={C} label="Profile" id="filter-profile"
              value={filters.profileStatusFilter} onChange={v => handleFilterChange('profileStatusFilter', v)}
              options={profileOpts} />
          )}
          {fo?.pii_available && (
            <FilterToggle C={C} label="PII Only" active={filters.piiFilter}
              onToggle={() => handleFilterChange('piiFilter', !filters.piiFilter)} />
          )}
          {hasActiveFilter && (
            <button
              type="button" onClick={handleClearFilters}
              aria-label="Clear all filters"
              style={{
                alignSelf: 'flex-end', background: 'transparent',
                border: `1px solid ${C.border}`, borderRadius: '7px',
                color: C.textMuted, cursor: 'pointer', fontSize: '0.75rem',
                padding: '6px 10px', transition: 'color 0.12s',
              }}
              onMouseEnter={e => e.currentTarget.style.color = C.text}
              onMouseLeave={e => e.currentTarget.style.color = C.textMuted}
            >
              Clear filters
            </button>
          )}
        </div>
      </div>

      {/* ── Error state ── */}
      {error && (
        <div role="alert" style={{
          background: C.dangerSoft, border: `1px solid ${C.danger}40`,
          borderRadius: '8px', padding: '12px 16px',
          color: C.danger, fontSize: '0.85rem', marginBottom: '20px',
        }}>
          {error}
        </div>
      )}

      {/* ── Results toolbar ── */}
      {hasSearched && !loading && (
        <div style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          marginBottom: '14px', gap: '12px', flexWrap: 'wrap',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
            <span style={{ fontSize: '0.82rem', color: C.textMuted }}>
              {total === 0
                ? 'No results'
                : `${total.toLocaleString()} result${total !== 1 ? 's' : ''}`}
            </span>
            {searchDuration !== null && (
              <span style={{ fontSize: '0.72rem', color: C.textMuted }}>
                {searchDuration === 0 ? '(cached)' : `in ${searchDuration}ms`}
              </span>
            )}
            {totalPages > 1 && (
              <span style={{ fontSize: '0.72rem', color: C.textMuted }}>
                · page {page + 1} of {totalPages}
              </span>
            )}
          </div>
          {total > 0 && (
            <button
              onClick={saveCurrentSearch}
              style={{
                display: 'flex', alignItems: 'center', gap: '5px',
                background: 'transparent', border: `1px solid ${C.border}`,
                borderRadius: '7px', color: C.textSec, cursor: 'pointer',
                fontSize: '0.75rem', fontWeight: '600', padding: '4px 10px',
                transition: 'border-color 0.12s',
              }}
              onMouseEnter={e => e.currentTarget.style.borderColor = C.accent}
              onMouseLeave={e => e.currentTarget.style.borderColor = C.border}
            >
              <BookmarkIcon size={11} /> Save search
            </button>
          )}
        </div>
      )}

      {/* ── Loading ── */}
      {loading && (
        <div role="status" aria-live="polite" style={{ textAlign: 'center', padding: '48px 0', color: C.textMuted, fontSize: '0.9rem' }}>
          Searching metadata…
        </div>
      )}

      {/* ── No metadata discovered ── */}
      {!loading && !hasSearched && noMetadata && (
        <div style={{ textAlign: 'center', padding: '72px 0' }}>
          <EmptyIcon style={{ marginBottom: '20px', color: C.border }} />
          <div style={{ fontWeight: '600', color: C.textSec, fontSize: '1rem', marginBottom: '8px' }}>
            No metadata has been discovered.
          </div>
          <div style={{ fontSize: '0.84rem', color: C.textMuted, lineHeight: 1.6 }}>
            When a data source is connected and scanned, all tables, columns,<br />
            business definitions, domains, and entities will appear here.
          </div>
        </div>
      )}

      {/* ── Empty state after search ── */}
      {!loading && hasSearched && !hasResults && (
        <div style={{ textAlign: 'center', padding: '64px 0', color: C.textMuted }}>
          <EmptyIcon style={{ marginBottom: '16px', color: C.border }} />
          <div style={{ fontWeight: '600', color: C.textSec, marginBottom: '6px', fontSize: '0.92rem' }}>
            No metadata assets match your search.
          </div>
          <div style={{ fontSize: '0.82rem' }}>
            Try a different term, broaden the query, or adjust filters.
          </div>
        </div>
      )}

      {/* ── Pre-search prompt ── */}
      {!loading && !hasSearched && !noMetadata && (
        <div style={{ textAlign: 'center', padding: '64px 0', color: C.textMuted }}>
          <SearchIconLarge style={{ marginBottom: '16px', color: C.border }} />
          <div style={{ fontSize: '0.92rem', color: C.textSec, fontWeight: '600', marginBottom: '6px' }}>
            Search your metadata catalog
          </div>
          <div style={{ fontSize: '0.82rem' }}>
            Find tables, columns, business terms, domains, and more.
          </div>
        </div>
      )}

      {/* ── Result cards ── */}
      {!loading && hasResults && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
          {results.map((result, i) => (
            <ResultCard
              key={`${result.asset_type}-${result.qualified_name}-${i}`}
              result={result}
              C={C}
              onOpenAsset={handleOpenAsset}
              expandedReasons={expandedReasons}
              onToggleReasons={toggleReasons}
              onCopy={handleCopy}
              copiedKey={copiedKey}
            />
          ))}
        </div>
      )}

      {/* ── Pagination ── */}
      {!loading && totalPages > 1 && (
        <div style={{
          display: 'flex', justifyContent: 'center', gap: '10px',
          marginTop: '28px', alignItems: 'center',
        }}>
          <button
            onClick={() => handlePage(-1)}
            disabled={page === 0}
            aria-label="Previous page"
            style={paginationBtn(C, page === 0)}
          >
            ← Previous
          </button>
          <span style={{ fontSize: '0.82rem', color: C.textMuted }}>
            {page + 1} / {totalPages}
          </span>
          <button
            onClick={() => handlePage(1)}
            disabled={page >= totalPages - 1}
            aria-label="Next page"
            style={paginationBtn(C, page >= totalPages - 1)}
          >
            Next →
          </button>
        </div>
      )}
    </div>
  )
}

// ── Utility ───────────────────────────────────────────────────────────────────

function smallActionBtn(C, color) {
  return {
    background: 'transparent', border: `1px solid ${color}40`,
    borderRadius: '5px', color, cursor: 'pointer',
    fontSize: '0.72rem', fontWeight: '600', padding: '3px 8px',
    transition: 'background 0.1s',
  }
}

function paginationBtn(C, disabled) {
  return {
    background: disabled ? C.bg : C.surface,
    border: `1px solid ${C.border}`,
    borderRadius: '7px', color: disabled ? C.textMuted : C.text,
    cursor: disabled ? 'not-allowed' : 'pointer',
    fontSize: '0.82rem', fontWeight: '600',
    padding: '8px 18px', opacity: disabled ? 0.45 : 1,
  }
}

// ── SVG icons ─────────────────────────────────────────────────────────────────

function SearchIcon({ style }) {
  return (
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={style}>
      <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
    </svg>
  )
}

function SearchIconLarge({ style }) {
  return (
    <svg width="40" height="40" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"
      style={{ display: 'block', margin: '0 auto', ...style }}>
      <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
    </svg>
  )
}

function EmptyIcon({ style }) {
  return (
    <svg width="40" height="40" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"
      style={{ display: 'block', margin: '0 auto', ...style }}>
      <circle cx="12" cy="12" r="10"/>
      <line x1="8" y1="15" x2="16" y2="15"/>
      <line x1="9" y1="9" x2="9.01" y2="9"/>
      <line x1="15" y1="9" x2="15.01" y2="9"/>
    </svg>
  )
}

function ClockIcon({ size = 14, style }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={style}>
      <circle cx="12" cy="12" r="10"/>
      <polyline points="12 6 12 12 16 14"/>
    </svg>
  )
}

function BookmarkIcon({ size = 14 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>
    </svg>
  )
}

function CopyIcon({ size = 12 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
    </svg>
  )
}

function CheckIcon({ size = 12 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="20 6 9 17 4 12"/>
    </svg>
  )
}
