import { useState, useEffect, useRef, useCallback } from 'react'
import { searchMetadata, getSearchFilters } from '../api/client'

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

const PAGE_SIZE = 20

const INITIAL_FILTERS = {
  assetType:         '',
  sourceFilter:      '',
  schemaFilter:      '',
  domainFilter:      '',
  entityFilter:      '',
  semanticTypeFilter:'',
  piiFilter:         false,
  dictStatusFilter:  '',
  classFilter:       '',
  profileStatusFilter: '',
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

function piiChip(C) {
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

function dictChip(status, C) {
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
  const max   = 400   // raised ceiling now that phrase+multi bonuses can stack
  const pct   = Math.min(100, Math.round((score / max) * 100))
  const color = pct >= 70 ? '#10b981' : pct >= 40 ? '#f59e0b' : '#60a5fa'
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
      <div style={{
        width: '52px', height: '4px', borderRadius: '2px',
        background: C.border, overflow: 'hidden',
      }}>
        <div style={{ width: `${pct}%`, height: '100%', background: color, borderRadius: '2px' }} />
      </div>
      <span style={{ fontSize: '0.67rem', color: C.textMuted, fontVariantNumeric: 'tabular-nums' }}>
        {score}
      </span>
    </div>
  )
}

function confidencePill(C, confidence) {
  if (!confidence || confidence <= 0) return null
  const pct = Math.round(confidence * 100)
  return (
    <span style={{
      fontSize: '0.67rem', color: C.textMuted,
      background: C.bg, border: `1px solid ${C.border}`,
      borderRadius: '4px', padding: '1px 6px',
    }}>
      {pct}% conf
    </span>
  )
}

// ── Sub-components ────────────────────────────────────────────────────────────

function PathCrumb({ C, label, value }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
      <span style={{ fontSize: '0.69rem', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.06em' }}>
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
      <span style={{ fontSize: '0.69rem', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
        {label}
      </span>
      <span style={{ fontSize: '0.78rem', color: C.textSec }}>{value}</span>
    </div>
  )
}

// ── Result card ───────────────────────────────────────────────────────────────

function ResultCard({ result, C, onOpenAsset }) {
  const matchedLabel = FIELD_LABELS[result.matched_field] || result.matched_field

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
          {result.pii_indicator && piiChip(C)}
          {dictChip(result.dictionary_status, C)}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexShrink: 0 }}>
          {confidencePill(C, result.confidence)}
          {scoreBar(result.relevance_score, C)}
        </div>
      </div>

      {/* ── Asset path breadcrumb ── */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '4px', flexWrap: 'wrap' }}>
        <PathCrumb C={C} label="Source"  value={result.source_name} />
        {result.schema_name && <PathCrumb C={C} label="Schema" value={result.schema_name} />}
        {result.table_name && result.asset_type === 'column' &&
          <PathCrumb C={C} label="Table" value={result.table_name} />}
        {result.column_name &&
          <PathCrumb C={C} label="Column" value={result.column_name} />}
      </div>

      {/* ── Match reasons ── */}
      {result.match_reasons?.length > 0 && (
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap' }}>
          <span style={{ fontSize: '0.69rem', color: C.textMuted, flexShrink: 0 }}>
            Matched:
          </span>
          {result.match_reasons.map((reason, i) => (
            <span key={i} style={{
              fontSize: '0.69rem', color: C.accent,
              background: C.accentSoft, borderRadius: '4px',
              padding: '1px 6px',
            }}>
              {reason}
            </span>
          ))}
        </div>
      )}

      {/* Fall back to single matched_field when match_reasons not present */}
      {(!result.match_reasons?.length) && result.matched_field !== 'unknown' && (
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
          <span style={{ fontSize: '0.72rem', color: C.textMuted }}>Matched because</span>
          <span style={{
            fontSize: '0.72rem', color: C.accent,
            background: C.accentSoft, borderRadius: '4px',
            padding: '1px 6px',
          }}>
            {matchedLabel}
          </span>
        </div>
      )}

      {/* ── Description ── */}
      {result.short_description && (
        <p style={{
          margin: 0, fontSize: '0.82rem', color: C.textSec,
          lineHeight: 1.55, maxWidth: '720px',
        }}>
          {result.short_description}
        </p>
      )}

      {/* ── Bottom metadata row ── */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '14px', flexWrap: 'wrap', marginTop: '2px' }}>
        {result.domain        && <MetaChip C={C} label="Domain"     value={result.domain} />}
        {result.entity        && <MetaChip C={C} label="Entity"     value={result.entity} />}
        {result.semantic_type && <MetaChip C={C} label="Class"      value={result.semantic_type} />}
        {result.profiled_at   && (
          <span style={{ fontSize: '0.69rem', color: C.textMuted }}>
            Profiled {result.profiled_at.slice(0, 10)}
          </span>
        )}
        <div style={{ flex: 1 }} />
        <button
          onClick={() => onOpenAsset(result)}
          style={{
            background: 'transparent',
            border: `1px solid ${C.border}`,
            borderRadius: '7px',
            color: C.accent,
            cursor: 'pointer',
            fontSize: '0.78rem',
            fontWeight: '600',
            padding: '5px 14px',
            transition: 'background 0.12s',
          }}
          onMouseEnter={e => e.currentTarget.style.background = C.accentSoft}
          onMouseLeave={e => e.currentTarget.style.background = 'transparent'}
          title={`Open in ${result.nav_target?.tab || 'data-sources'}`}
        >
          Open Asset →
        </button>
      </div>
    </div>
  )
}

// ── Filter controls ───────────────────────────────────────────────────────────

function FilterSelect({ C, label, value, onChange, options }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
      <label style={{
        fontSize: '0.63rem', color: C.textMuted,
        fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.07em',
      }}>
        {label}
      </label>
      <select
        value={value}
        onChange={e => onChange(e.target.value)}
        style={{
          background: C.bg, border: `1px solid ${C.border}`,
          borderRadius: '7px', color: C.text,
          fontSize: '0.8rem', padding: '6px 9px',
          cursor: 'pointer', minWidth: '110px',
          outline: 'none',
        }}
      >
        {options.map(o => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>
    </div>
  )
}

function FilterToggle({ C, label, active, onToggle }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
      <label style={{
        fontSize: '0.63rem', color: C.textMuted,
        fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.07em',
      }}>
        {label}
      </label>
      <button
        type="button"
        onClick={onToggle}
        style={{
          background: active ? '#f8717122' : C.bg,
          border: `1px solid ${active ? '#f87171' : C.border}`,
          borderRadius: '7px', color: active ? '#f87171' : C.textSec,
          cursor: 'pointer', fontSize: '0.8rem', fontWeight: '600',
          padding: '6px 12px',
          transition: 'all 0.15s',
        }}
      >
        {active ? '✓ PII' : 'PII'}
      </button>
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────

export default function EnterpriseSearch({ C, token, setActiveNav, openSource }) {
  const [query,     setQuery]     = useState('')
  const [submitted, setSubmitted] = useState('')
  const [page,      setPage]      = useState(0)

  // All filters in one object — all drive server-side SQL
  const [filters, setFilters] = useState(INITIAL_FILTERS)

  // Results
  const [loading,  setLoading]  = useState(false)
  const [error,    setError]    = useState(null)
  const [results,  setResults]  = useState([])
  const [total,    setTotal]    = useState(0)
  const [tokens,   setTokens]   = useState([])

  // Filter options from /v1/search/filters
  const [filterOptions, setFilterOptions] = useState(null)

  const inputRef = useRef(null)
  const abortRef = useRef(null)

  useEffect(() => {
    inputRef.current?.focus()
    if (!token) return
    getSearchFilters(token)
      .then(res => setFilterOptions(res?.data || null))
      .catch(() => {}) // non-fatal — filters still work without options
  }, [token])

  const runSearch = useCallback(async (q, searchFilters, pageNum) => {
    if (!q.trim()) return
    if (abortRef.current) abortRef.current.abort()
    const ctrl = new AbortController()
    abortRef.current = ctrl

    setLoading(true)
    setError(null)
    try {
      const params = {
        limit:  PAGE_SIZE,
        offset: pageNum * PAGE_SIZE,
      }
      if (searchFilters.assetType)          params.asset_type         = searchFilters.assetType
      if (searchFilters.sourceFilter)       params.source_id          = Number(searchFilters.sourceFilter)
      if (searchFilters.schemaFilter)       params.schema             = searchFilters.schemaFilter
      if (searchFilters.domainFilter)       params.domain             = searchFilters.domainFilter
      if (searchFilters.entityFilter)       params.entity             = searchFilters.entityFilter
      if (searchFilters.semanticTypeFilter) params.semantic_type      = searchFilters.semanticTypeFilter
      if (searchFilters.piiFilter)          params.pii                = true
      if (searchFilters.dictStatusFilter)   params.dictionary_status  = searchFilters.dictStatusFilter
      if (searchFilters.classFilter)        params.classification     = searchFilters.classFilter
      if (searchFilters.profileStatusFilter)params.profile_status     = searchFilters.profileStatusFilter

      const res = await searchMetadata(q, token, params)
      const d   = res?.data || {}
      setResults(d.results || [])
      setTotal(d.total   || 0)
      setTokens(d.tokens || [])
    } catch (err) {
      if (err.name === 'AbortError') return
      setError(err.message || 'Search failed')
      setResults([])
      setTotal(0)
    } finally {
      setLoading(false)
    }
  }, [token])

  const handleSubmit = (e) => {
    e?.preventDefault()
    if (!query.trim()) return
    setSubmitted(query.trim())
    setPage(0)
    runSearch(query.trim(), filters, 0)
  }

  const handleFilterChange = (key, val) => {
    const next = { ...filters, [key]: val }
    setFilters(next)
    if (submitted) {
      setPage(0)
      runSearch(submitted, next, 0)
    }
  }

  const handleClearFilters = () => {
    setFilters(INITIAL_FILTERS)
    if (submitted) {
      setPage(0)
      runSearch(submitted, INITIAL_FILTERS, 0)
    }
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
    if (openSource && nav.source_id != null) {
      openSource(nav.source_id, tab)
    } else {
      setActiveNav?.('data-sources')
    }
  }

  const hasResults  = results.length > 0
  const hasSearched = submitted.length > 0
  const totalPages  = Math.ceil(total / PAGE_SIZE)

  // Check whether any filter is active (for "clear" button)
  const hasActiveFilter = Object.entries(filters).some(([, v]) => v !== '' && v !== false)

  // Build option arrays from API response (only when values exist)
  const fo = filterOptions
  const sourceOpts = fo?.sources?.length
    ? [{ value: '', label: 'All Sources' }, ...fo.sources.map(s => ({ value: String(s.id), label: s.name }))]
    : null
  const schemaOpts = fo?.schemas?.length
    ? [{ value: '', label: 'All Schemas' }, ...fo.schemas.map(s => ({ value: s, label: s }))]
    : null
  const domainOpts = fo?.domains?.length
    ? [{ value: '', label: 'All Domains' }, ...fo.domains.map(d => ({ value: d, label: d }))]
    : null
  const entityOpts = fo?.entities?.length
    ? [{ value: '', label: 'All Entities' }, ...fo.entities.map(e => ({ value: e, label: e }))]
    : null
  const semTypeOpts = fo?.semantic_types?.length
    ? [{ value: '', label: 'Any Type' }, ...fo.semantic_types.map(t => ({ value: t, label: t }))]
    : null
  const classOpts = fo?.classifications?.length
    ? [{ value: '', label: 'Any Class' }, ...fo.classifications.map(c => ({ value: c, label: c }))]
    : null
  const dictOpts = fo?.dictionary_statuses?.length
    ? [{ value: '', label: 'Any Status' }, ...fo.dictionary_statuses.map(s => ({ value: s, label: s.charAt(0).toUpperCase() + s.slice(1) }))]
    : null
  const profileOpts = fo?.profile_statuses?.length
    ? [{ value: '', label: 'Any Status' }, ...fo.profile_statuses.map(s => ({ value: s, label: s }))]
    : null

  return (
    <div style={{ maxWidth: '960px', margin: '0 auto', padding: '32px 24px' }}>

      {/* ── Page header ── */}
      <div style={{ marginBottom: '24px' }}>
        <h1 style={{ margin: 0, color: C.text, fontSize: '1.45rem', fontWeight: '700' }}>
          Enterprise Metadata Search
        </h1>
        <p style={{ margin: '6px 0 0', color: C.textMuted, fontSize: '0.85rem' }}>
          Search across tables, columns, business definitions, domains, entities, and more.
        </p>
      </div>

      {/* ── Search bar ── */}
      <form onSubmit={handleSubmit} style={{ marginBottom: '16px' }}>
        <div style={{ display: 'flex', gap: '10px' }}>
          <div style={{ flex: 1, position: 'relative' }}>
            <SearchIcon style={{
              position: 'absolute', left: '14px', top: '50%', transform: 'translateY(-50%)',
              color: C.textMuted, pointerEvents: 'none',
            }} />
            <input
              ref={inputRef}
              type="text"
              value={query}
              onChange={e => setQuery(e.target.value)}
              placeholder="Search metadata… e.g. Employee Email, Finance, Invoices"
              style={{
                width: '100%', boxSizing: 'border-box',
                background: C.surface, border: `1.5px solid ${C.border}`,
                borderRadius: '10px', color: C.text,
                fontSize: '1rem', padding: '14px 14px 14px 44px',
                outline: 'none', transition: 'border-color 0.15s',
              }}
              onFocus={e => e.target.style.borderColor = C.accent}
              onBlur={e  => e.target.style.borderColor = C.border}
            />
          </div>
          <button
            type="submit"
            disabled={!query.trim() || loading}
            style={{
              background: C.accent, color: '#fff', border: 'none',
              borderRadius: '10px', padding: '0 28px',
              fontSize: '0.9rem', fontWeight: '600', cursor: 'pointer',
              opacity: !query.trim() || loading ? 0.5 : 1,
              transition: 'opacity 0.15s',
              whiteSpace: 'nowrap',
            }}
          >
            {loading ? 'Searching…' : 'Search'}
          </button>
        </div>

        {/* Token hint */}
        {tokens.length > 0 && (
          <div style={{ marginTop: '8px', display: 'flex', gap: '6px', alignItems: 'center' }}>
            <span style={{ fontSize: '0.72rem', color: C.textMuted }}>Searching for:</span>
            {tokens.map(t => (
              <span key={t} style={{
                fontSize: '0.72rem', background: C.accentSoft, color: C.accent,
                borderRadius: '4px', padding: '2px 7px',
              }}>{t}</span>
            ))}
          </div>
        )}
      </form>

      {/* ── Sticky filter panel ── */}
      <div style={{
        position: 'sticky', top: 0, zIndex: 10,
        background: C.bg, paddingBottom: '16px',
      }}>
        <div style={{
          background: C.surface, border: `1px solid ${C.border}`,
          borderRadius: '10px', padding: '12px 16px',
          display: 'flex', gap: '10px', flexWrap: 'wrap', alignItems: 'flex-end',
        }}>
          {/* Asset Type — always shown */}
          <FilterSelect
            C={C} label="Asset Type"
            value={filters.assetType}
            onChange={v => handleFilterChange('assetType', v)}
            options={ASSET_TYPE_OPTIONS}
          />

          {/* Source — shown when API returns sources */}
          {sourceOpts && (
            <FilterSelect C={C} label="Source"
              value={filters.sourceFilter}
              onChange={v => handleFilterChange('sourceFilter', v)}
              options={sourceOpts}
            />
          )}

          {/* Schema */}
          {schemaOpts && (
            <FilterSelect C={C} label="Schema"
              value={filters.schemaFilter}
              onChange={v => handleFilterChange('schemaFilter', v)}
              options={schemaOpts}
            />
          )}

          {/* Domain */}
          {domainOpts && (
            <FilterSelect C={C} label="Domain"
              value={filters.domainFilter}
              onChange={v => handleFilterChange('domainFilter', v)}
              options={domainOpts}
            />
          )}

          {/* Entity */}
          {entityOpts && (
            <FilterSelect C={C} label="Entity"
              value={filters.entityFilter}
              onChange={v => handleFilterChange('entityFilter', v)}
              options={entityOpts}
            />
          )}

          {/* Semantic Type */}
          {semTypeOpts && (
            <FilterSelect C={C} label="Semantic Type"
              value={filters.semanticTypeFilter}
              onChange={v => handleFilterChange('semanticTypeFilter', v)}
              options={semTypeOpts}
            />
          )}

          {/* Classification */}
          {classOpts && (
            <FilterSelect C={C} label="Classification"
              value={filters.classFilter}
              onChange={v => handleFilterChange('classFilter', v)}
              options={classOpts}
            />
          )}

          {/* Dictionary Status */}
          {dictOpts && (
            <FilterSelect C={C} label="Dictionary"
              value={filters.dictStatusFilter}
              onChange={v => handleFilterChange('dictStatusFilter', v)}
              options={dictOpts}
            />
          )}

          {/* Profile Status */}
          {profileOpts && (
            <FilterSelect C={C} label="Profile"
              value={filters.profileStatusFilter}
              onChange={v => handleFilterChange('profileStatusFilter', v)}
              options={profileOpts}
            />
          )}

          {/* PII toggle — shown only when PII data exists */}
          {fo?.pii_available && (
            <FilterToggle C={C} label="PII Only"
              active={filters.piiFilter}
              onToggle={() => handleFilterChange('piiFilter', !filters.piiFilter)}
            />
          )}

          {/* Clear filters — shown when any filter is active */}
          {hasActiveFilter && (
            <button
              type="button"
              onClick={handleClearFilters}
              style={{
                alignSelf: 'flex-end',
                background: 'transparent',
                border: `1px solid ${C.border}`,
                borderRadius: '7px',
                color: C.textMuted,
                cursor: 'pointer',
                fontSize: '0.75rem',
                padding: '6px 10px',
                transition: 'color 0.12s',
              }}
              onMouseEnter={e => e.currentTarget.style.color = C.text}
              onMouseLeave={e => e.currentTarget.style.color = C.textMuted}
            >
              Clear filters
            </button>
          )}
        </div>
      </div>

      {/* ── Error ── */}
      {error && (
        <div style={{
          background: C.dangerSoft, border: `1px solid ${C.danger}40`,
          borderRadius: '8px', padding: '12px 16px',
          color: C.danger, fontSize: '0.85rem', marginBottom: '20px',
        }}>
          {error}
        </div>
      )}

      {/* ── Results summary ── */}
      {hasSearched && !loading && (
        <div style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          marginBottom: '14px',
        }}>
          <span style={{ fontSize: '0.82rem', color: C.textMuted }}>
            {total === 0
              ? 'No results'
              : `${total.toLocaleString()} result${total !== 1 ? 's' : ''} — page ${page + 1} of ${totalPages || 1}`}
          </span>
          {totalPages > 1 && (
            <span style={{ fontSize: '0.78rem', color: C.textMuted }}>
              Showing {results.length} of {total.toLocaleString()}
            </span>
          )}
        </div>
      )}

      {/* ── Loading state ── */}
      {loading && (
        <div style={{ textAlign: 'center', padding: '48px 0', color: C.textMuted, fontSize: '0.9rem' }}>
          Searching metadata…
        </div>
      )}

      {/* ── Empty state after search ── */}
      {!loading && hasSearched && !hasResults && (
        <div style={{ textAlign: 'center', padding: '64px 0', color: C.textMuted, fontSize: '0.92rem' }}>
          <EmptyIcon style={{ marginBottom: '16px', color: C.border }} />
          <div style={{ fontWeight: '600', color: C.textSec, marginBottom: '6px' }}>
            No metadata assets match your search.
          </div>
          <div style={{ fontSize: '0.82rem' }}>
            Try a different term or adjust the filters above.
          </div>
        </div>
      )}

      {/* ── Pre-search prompt ── */}
      {!loading && !hasSearched && (
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
            style={paginationBtn(C, page >= totalPages - 1)}
          >
            Next →
          </button>
        </div>
      )}
    </div>
  )
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

// ── SVG icons (inline, no dependency) ────────────────────────────────────────

function SearchIcon({ style }) {
  return (
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinecap="round"
      strokeLinejoin="round" style={style}>
      <circle cx="11" cy="11" r="8"/>
      <line x1="21" y1="21" x2="16.65" y2="16.65"/>
    </svg>
  )
}

function SearchIconLarge({ style }) {
  return (
    <svg width="40" height="40" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"
      strokeLinejoin="round" style={{ display: 'block', margin: '0 auto', ...style }}>
      <circle cx="11" cy="11" r="8"/>
      <line x1="21" y1="21" x2="16.65" y2="16.65"/>
    </svg>
  )
}

function EmptyIcon({ style }) {
  return (
    <svg width="40" height="40" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"
      strokeLinejoin="round" style={{ display: 'block', margin: '0 auto', ...style }}>
      <circle cx="12" cy="12" r="10"/>
      <line x1="8" y1="15" x2="16" y2="15"/>
      <line x1="9" y1="9" x2="9.01" y2="9"/>
      <line x1="15" y1="9" x2="15.01" y2="9"/>
    </svg>
  )
}
