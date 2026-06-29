import { useState, useEffect } from 'react'
import { getTableProfileDetail, getTableBusinessContext, getColumnBusinessContext } from '../api/client'

const FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"
const MONO = "'Cascadia Code', 'Fira Code', 'JetBrains Mono', monospace"

// ── Semantic type palette ──────────────────────────────────────────────────────
const SEM_COLOR = {
  EMAIL: '#38bdf8', PHONE: '#a78bfa', SSN: '#f87171',
  ID: '#818cf8', AMOUNT: '#34d399', COUNT: '#2dd4bf',
  DATE: '#fb923c', STATUS: '#fbbf24', CODE: '#94a3b8',
  FLAG: '#f472b6', NAME: '#c084fc', TEXT: '#64748b',
  BINARY: '#475569', UNKNOWN: '#475569',
}

// ── Table class palette ────────────────────────────────────────────────────────
const CLASS_COLOR = {
  Master: '#818cf8', Reference: '#2dd4bf', Transactional: '#fb923c',
  Audit: '#fbbf24', Staging: '#94a3b8', Reporting: '#a78bfa',
  Unknown: '#64748b',
}

// ── Row count tier palette ─────────────────────────────────────────────────────
const TIER_COLOR = {
  EMPTY: '#f87171', TINY: '#fbbf24', SMALL: '#34d399',
  MEDIUM: '#38bdf8', LARGE: '#fb923c', VERY_LARGE: '#a78bfa',
}

const TIER_ORDER = { EMPTY: 0, TINY: 1, SMALL: 2, MEDIUM: 3, LARGE: 4, VERY_LARGE: 5 }

// ── Column grid definition ─────────────────────────────────────────────────────
const GRID_COLS = [
  { label: 'Column Name',  field: 'column_name',         w: 162, mono: true  },
  { label: 'Type',         field: 'data_type',           w: 76               },
  { label: 'Raw Type',     field: 'raw_type',            w: 90,  mono: true  },
  { label: 'Semantic',     field: 'semantic_type',       w: 108              },
  { label: 'Conf.',        field: 'semantic_confidence', w: 58               },
  { label: 'Null.',        field: 'is_nullable',         w: 44               },
  { label: 'PK',           field: 'is_primary_key',      w: 36               },
  { label: 'ID',           field: 'is_identity',         w: 36               },
  { label: 'PII',          field: 'pii_name_heuristic',  w: 42               },
  { label: 'Null %',       field: 'null_percentage',     w: 64               },
  { label: 'Distinct %',   field: 'distinct_percentage', w: 78               },
  { label: 'Uniqueness',   field: 'uniqueness_score',    w: 80               },
  { label: 'Cardinality',  field: 'cardinality_tier',    w: 90               },
  { label: 'Avg Len',      field: 'avg_length',          w: 68               },
  { label: 'Pattern',      field: 'dominant_pattern',    w: 128, mono: true  },
  { label: 'Status',       field: 'profiling_status',    w: 76               },
]

// ── Pure helpers ───────────────────────────────────────────────────────────────

function fmtPct(v) {
  if (v == null) return null
  return `${Number(v).toFixed(1)}%`
}

function fmtNum(v) {
  if (v == null) return null
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`
  if (v >= 1_000)     return `${(v / 1_000).toFixed(1)}K`
  return Number(v).toLocaleString()
}

function fmtScore(v) {
  if (v == null) return null
  return Number(v).toFixed(3)
}

function parseMaybeJson(v) {
  if (!v) return null
  if (typeof v !== 'string') return v
  try { return JSON.parse(v) } catch { return v }
}

function bool(v) {
  return v === true || v === 1
}

// ── Chip / badge renderers (inline, no extra DOM) ──────────────────────────────

function SemChip({ type }) {
  if (!type) return null
  const c = SEM_COLOR[type] ?? '#64748b'
  return (
    <span style={{
      display: 'inline-block', padding: '1px 7px', borderRadius: '6px',
      fontSize: '0.6rem', fontWeight: '700', letterSpacing: '0.05em',
      textTransform: 'uppercase', background: `${c}18`, color: c,
      border: `1px solid ${c}35`, fontFamily: FONT, whiteSpace: 'nowrap',
    }}>
      {type}
    </span>
  )
}

function ClassChip({ cls }) {
  if (!cls) return null
  const c = CLASS_COLOR[cls] ?? '#64748b'
  return (
    <span style={{
      display: 'inline-block', padding: '1px 8px', borderRadius: '6px',
      fontSize: '0.6rem', fontWeight: '700', letterSpacing: '0.05em',
      textTransform: 'uppercase', background: `${c}18`, color: c,
      border: `1px solid ${c}35`, fontFamily: FONT, whiteSpace: 'nowrap',
    }}>
      {cls}
    </span>
  )
}

function TierChip({ tier }) {
  if (!tier) return null
  const c = TIER_COLOR[tier] ?? '#64748b'
  return (
    <span style={{
      display: 'inline-block', padding: '1px 6px', borderRadius: '5px',
      fontSize: '0.58rem', fontWeight: '700', letterSpacing: '0.04em',
      background: `${c}15`, color: c, border: `1px solid ${c}30`,
      fontFamily: FONT, whiteSpace: 'nowrap',
    }}>
      {tier}
    </span>
  )
}

function ConfChip({ v, success, warn, danger }) {
  if (v == null) return <span style={{ color: '#64748b', fontFamily: MONO, fontSize: '0.72rem' }}>—</span>
  const pct = Math.round(v * 100)
  const c = pct >= 80 ? success : pct >= 60 ? warn : danger
  return (
    <span style={{
      display: 'inline-block', padding: '1px 7px', borderRadius: '6px',
      fontSize: '0.68rem', fontWeight: '700',
      background: `${c}18`, color: c, border: `1px solid ${c}35`,
      fontFamily: MONO, whiteSpace: 'nowrap',
    }}>
      {pct}%
    </span>
  )
}

function PiiBadge({ v, danger }) {
  if (!bool(v)) return null
  return (
    <span style={{
      display: 'inline-block', padding: '1px 6px', borderRadius: '5px',
      fontSize: '0.58rem', fontWeight: '700', letterSpacing: '0.05em',
      background: `${danger}18`, color: danger, border: `1px solid ${danger}35`,
      fontFamily: FONT,
    }}>
      PII
    </span>
  )
}

function BoolIcon({ v, success, muted }) {
  return bool(v)
    ? <span style={{ color: success, fontSize: '0.72rem', fontWeight: '700' }}>✓</span>
    : <span style={{ color: muted, fontSize: '0.72rem' }}>—</span>
}

// ── Sort helpers ───────────────────────────────────────────────────────────────

function sortItems(items, field, dir) {
  if (!field || !items) return items
  return [...items].sort((a, b) => {
    const av = a[field] ?? ''
    const bv = b[field] ?? ''
    const cmp = (field === 'row_count_tier')
      ? ((TIER_ORDER[av] ?? -1) - (TIER_ORDER[bv] ?? -1))
      : typeof av === 'number'
        ? av - bv
        : String(av).localeCompare(String(bv))
    return dir === 'desc' ? -cmp : cmp
  })
}

function nextSort(current, field) {
  if (current.field !== field) return { field, dir: 'asc' }
  if (current.dir === 'asc')   return { field, dir: 'desc' }
  return { field: null, dir: 'asc' }
}

function SortArrow({ field, sort }) {
  if (sort.field !== field) return <span style={{ color: '#ffffff20' }}> ↕</span>
  return <span style={{ color: '#ffffff80' }}>{sort.dir === 'asc' ? ' ↑' : ' ↓'}</span>
}

// ── Column Detail Drawer ───────────────────────────────────────────────────────

function ColumnDetailDrawer({ col, colBkg, onClose, C }) {
  const bg      = C.bg      ?? '#07091a'
  const surface = C.surface ?? '#0d1128'
  const border  = C.border  ?? '#1e2b52'
  const text    = C.text    ?? '#eef0ff'
  const textSec = C.textSec ?? '#dde1ff'
  const muted   = C.textMuted ?? '#7880a8'
  const accent  = C.accent  ?? '#6366f1'
  const success = C.success ?? '#10b981'
  const danger  = C.danger  ?? '#f87171'
  const warn    = C.warn    ?? '#f59e0b'

  const semEvidence = parseMaybeJson(col.semantic_evidence_json)
  const piiSignals  = parseMaybeJson(col.pii_signals_json)

  const row = (label, value, opts = {}) => {
    if (value == null || value === '') return null
    const displayVal = opts.pct ? fmtPct(value) : opts.score ? fmtScore(value) : value
    if (displayVal == null) return null
    return (
      <div key={label} style={{ display: 'flex', alignItems: 'flex-start', gap: '8px', padding: '5px 0', borderBottom: `1px solid ${border}18` }}>
        <span style={{ fontSize: '0.72rem', color: muted, fontFamily: FONT, minWidth: '130px', flexShrink: 0 }}>
          {label}
        </span>
        <span style={{ fontSize: '0.78rem', color: opts.mono ? textSec : text, fontFamily: opts.mono ? MONO : FONT, wordBreak: 'break-all' }}>
          {opts.chip ? displayVal : String(displayVal)}
        </span>
      </div>
    )
  }

  const boolRow = (label, v) => {
    if (v == null) return null
    return (
      <div key={label} style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '5px 0', borderBottom: `1px solid ${border}18` }}>
        <span style={{ fontSize: '0.72rem', color: muted, fontFamily: FONT, minWidth: '130px', flexShrink: 0 }}>{label}</span>
        <BoolIcon v={v} success={success} muted={muted} />
      </div>
    )
  }

  return (
    <>
      {/* Backdrop */}
      <div
        onClick={onClose}
        style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)', zIndex: 999 }}
      />
      {/* Drawer */}
      <div style={{
        position: 'fixed', top: 0, right: 0, bottom: 0, width: '400px',
        background: bg, borderLeft: `1px solid ${border}`,
        zIndex: 1000, overflowY: 'auto', display: 'flex', flexDirection: 'column',
        boxShadow: '-8px 0 40px rgba(0,0,0,0.5)',
      }}>
        {/* Header */}
        <div style={{ padding: '16px 20px', borderBottom: `1px solid ${border}`, position: 'sticky', top: 0, background: bg, zIndex: 1 }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px', marginBottom: '8px' }}>
            <span style={{ fontSize: '0.9rem', fontWeight: '700', color: text, fontFamily: MONO, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {col.column_name}
            </span>
            <button onClick={onClose} style={{ background: 'transparent', border: 'none', color: muted, cursor: 'pointer', fontSize: '1.2rem', lineHeight: 1, padding: '2px 6px', borderRadius: '4px', flexShrink: 0 }}>×</button>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap' }}>
            {col.semantic_type && <SemChip type={col.semantic_type} />}
            {col.semantic_confidence != null && <ConfChip v={col.semantic_confidence} success={success} warn={warn} danger={danger} />}
            {bool(col.pii_name_heuristic) && <PiiBadge v={1} danger={danger} />}
          </div>
        </div>

        {/* Body */}
        <div style={{ padding: '12px 20px', display: 'flex', flexDirection: 'column', gap: 0 }}>

          {/* ── Business Context (from Knowledge Service) ── */}
          {colBkg && (colBkg.dictionary || colBkg.table_context?.domain) && (
            <>
              <div style={{ fontSize: '0.64rem', color: accent, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', marginBottom: '6px', marginTop: '4px' }}>Business Context</div>
              {colBkg.dictionary?.business_label && row('Business Label', colBkg.dictionary.business_label, { mono: false })}
              {colBkg.dictionary?.meaning        && row('Meaning',        colBkg.dictionary.meaning)}
              {colBkg.dictionary?.is_approved    && (
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '5px 0', borderBottom: `1px solid ${border}18` }}>
                  <span style={{ fontSize: '0.72rem', color: muted, fontFamily: FONT, minWidth: '130px', flexShrink: 0 }}>Approved</span>
                  <span style={{ fontSize: '0.72rem', color: success, fontWeight: '700' }}>✓ Human approved</span>
                </div>
              )}
              {colBkg.table_context?.domain && colBkg.table_context.domain !== 'Unknown' &&
                row('Domain', colBkg.table_context.domain)}
              {colBkg.table_context?.entity && colBkg.table_context.entity !== 'Unknown' &&
                row('Entity', colBkg.table_context.entity)}
              {colBkg.confidence != null && (
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '5px 0', borderBottom: `1px solid ${border}18` }}>
                  <span style={{ fontSize: '0.72rem', color: muted, fontFamily: FONT, minWidth: '130px', flexShrink: 0 }}>Confidence</span>
                  <ConfChip v={colBkg.confidence} success={success} warn={warn} danger={danger} />
                </div>
              )}
              {Array.isArray(colBkg.evidence) && colBkg.evidence.length > 0 && (
                <div style={{ padding: '6px 0', borderBottom: `1px solid ${border}18` }}>
                  <div style={{ fontSize: '0.72rem', color: muted, fontFamily: FONT, marginBottom: '4px' }}>Evidence</div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
                    {colBkg.evidence.map((e, i) => (
                      <span key={i} style={{ fontSize: '0.72rem', color: textSec, fontFamily: FONT, paddingLeft: '8px', borderLeft: `2px solid ${accent}30` }}>{e}</span>
                    ))}
                  </div>
                </div>
              )}
              <div style={{ height: '10px' }} />
            </>
          )}

          {/* Schema */}
          <div style={{ fontSize: '0.64rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', marginBottom: '6px', marginTop: '4px' }}>Schema</div>
          {row('Data Type',   col.data_type)}
          {row('Raw Type',    col.raw_type,   { mono: true })}
          {boolRow('Nullable',    col.is_nullable)}
          {boolRow('Primary Key', col.is_primary_key)}
          {boolRow('Identity',    col.is_identity)}

          {/* Semantic classification */}
          {(col.semantic_type || semEvidence) && (
            <>
              <div style={{ fontSize: '0.64rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', marginBottom: '6px', marginTop: '14px' }}>Semantic Classification</div>
              {col.semantic_type       && row('Semantic Type', col.semantic_type)}
              {col.semantic_confidence != null && <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '5px 0', borderBottom: `1px solid ${border}18` }}><span style={{ fontSize: '0.72rem', color: muted, fontFamily: FONT, minWidth: '130px' }}>Confidence</span><ConfChip v={col.semantic_confidence} success={success} warn={warn} danger={danger} /></div>}
              {Array.isArray(semEvidence) && semEvidence.length > 0 && (
                <div style={{ padding: '6px 0', borderBottom: `1px solid ${border}18` }}>
                  <div style={{ fontSize: '0.72rem', color: muted, fontFamily: FONT, marginBottom: '4px' }}>Evidence</div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
                    {semEvidence.map((e, i) => (
                      <span key={i} style={{ fontSize: '0.72rem', color: textSec, fontFamily: FONT, paddingLeft: '8px', borderLeft: `2px solid ${accent}30` }}>{e}</span>
                    ))}
                  </div>
                </div>
              )}
            </>
          )}

          {/* PII */}
          {(bool(col.pii_name_heuristic) || bool(col.pii_confirmed)) && (
            <>
              <div style={{ fontSize: '0.64rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', marginBottom: '6px', marginTop: '14px' }}>PII</div>
              {boolRow('Name Heuristic', col.pii_name_heuristic)}
              {boolRow('Confirmed',      col.pii_confirmed)}
              {Array.isArray(piiSignals) && piiSignals.length > 0 && (
                <div style={{ padding: '6px 0', borderBottom: `1px solid ${border}18` }}>
                  <div style={{ fontSize: '0.72rem', color: muted, fontFamily: FONT, marginBottom: '4px' }}>Signals</div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
                    {piiSignals.map((s, i) => (
                      <span key={i} style={{ fontSize: '0.72rem', color: danger, fontFamily: FONT, paddingLeft: '8px', borderLeft: `2px solid ${danger}30` }}>{s}</span>
                    ))}
                  </div>
                </div>
              )}
              {typeof piiSignals === 'string' && piiSignals && row('Signals', piiSignals)}
            </>
          )}

          {/* Statistics */}
          {(col.null_percentage != null || col.distinct_percentage != null || col.min_value != null) && (
            <>
              <div style={{ fontSize: '0.64rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', marginBottom: '6px', marginTop: '14px' }}>Statistics</div>
              {col.null_percentage     != null && row('Null %',        fmtPct(col.null_percentage))}
              {col.distinct_percentage != null && row('Distinct %',    fmtPct(col.distinct_percentage))}
              {col.uniqueness_score    != null && row('Uniqueness',    fmtScore(col.uniqueness_score), { mono: true })}
              {col.cardinality_tier             && row('Cardinality',  col.cardinality_tier)}
              {col.min_value                    && row('Min Value',    col.min_value, { mono: true })}
              {col.max_value                    && row('Max Value',    col.max_value, { mono: true })}
              {col.avg_length          != null  && row('Avg Length',   `${Number(col.avg_length).toFixed(1)}`)}
              {col.min_length          != null  && row('Min Length',   col.min_length)}
              {col.max_length_observed != null  && row('Max Length',   col.max_length_observed)}
              {col.mean_value          != null  && row('Mean',         Number(col.mean_value).toFixed(4), { mono: true })}
              {col.std_deviation       != null  && row('Std Dev',      Number(col.std_deviation).toFixed(4), { mono: true })}
            </>
          )}

          {/* Patterns */}
          {(col.dominant_pattern || col.email_match_rate != null) && (
            <>
              <div style={{ fontSize: '0.64rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', marginBottom: '6px', marginTop: '14px' }}>Patterns</div>
              {col.dominant_pattern   && row('Dominant Pattern', col.dominant_pattern, { mono: true })}
              {col.pattern_coverage   != null && row('Pattern Coverage', fmtPct(col.pattern_coverage))}
              {col.top_values_coverage!= null && row('Top Values Cov.',  fmtPct(col.top_values_coverage))}
              {col.email_match_rate   != null && row('Email Match %',    fmtPct(col.email_match_rate))}
              {col.phone_match_rate   != null && row('Phone Match %',    fmtPct(col.phone_match_rate))}
              {col.guid_match_rate    != null && row('GUID Match %',     fmtPct(col.guid_match_rate))}
              {col.date_string_rate   != null && row('Date String %',    fmtPct(col.date_string_rate))}
              {col.masked_value_rate  != null && row('Masked Value %',   fmtPct(col.masked_value_rate))}
            </>
          )}

          {/* Execution */}
          <div style={{ fontSize: '0.64rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', marginBottom: '6px', marginTop: '14px' }}>Execution</div>
          {row('Profiling Depth',  col.profiling_depth)}
          {row('Profiling Status', col.profiling_status)}

        </div>
      </div>
    </>
  )
}

// ── BusinessContextPanel ───────────────────────────────────────────────────────

function BusinessContextPanel({ bkg, loading, C }) {
  const surface = C.surface   ?? '#0d1128'
  const border  = C.border    ?? '#1e2b52'
  const text    = C.text      ?? '#eef0ff'
  const textSec = C.textSec   ?? '#dde1ff'
  const muted   = C.textMuted ?? '#7880a8'
  const accent  = C.accent    ?? '#6366f1'
  const success = C.success   ?? '#10b981'
  const danger  = C.danger    ?? '#f87171'
  const warn    = C.warn      ?? '#f59e0b'

  if (loading) {
    return (
      <div style={{ background: surface, border: `1px solid ${accent}25`, borderLeft: `3px solid ${accent}50`, borderRadius: '10px', padding: '12px 18px', marginBottom: '0' }}>
        <span style={{ fontSize: '0.72rem', color: muted, fontFamily: FONT }}>Loading business context…</span>
      </div>
    )
  }

  if (!bkg) return null

  const { dictionary, domain, entity, profiling, relationships, governance, overall_confidence, metadata_completeness } = bkg

  const hasDictionary = !!(dictionary?.business_name || dictionary?.description)
  const hasDomain     = !!(domain?.domain && domain.domain !== 'Unknown')
  const hasEntity     = !!(entity?.entity && entity.entity !== 'Unknown')

  if (!hasDictionary && !hasDomain && !hasEntity) return null

  const lbl = (t) => (
    <div style={{ fontSize: '0.6rem', fontWeight: '700', color: muted, letterSpacing: '0.06em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '2px' }}>{t}</div>
  )
  const val = (v, extra = {}) => (
    <div style={{ fontSize: '0.8rem', color: text, fontFamily: FONT, ...extra }}>{v}</div>
  )

  const outbound = relationships?.outbound ?? []
  const inbound  = relationships?.inbound  ?? []

  const govBadge = (label, ok) => ok ? (
    <span key={label} style={{ display: 'inline-flex', alignItems: 'center', gap: '3px', padding: '1px 7px', borderRadius: '5px', fontSize: '0.6rem', fontWeight: '700', background: `${success}12`, color: success, border: `1px solid ${success}30`, fontFamily: FONT }}>
      ✓ {label}
    </span>
  ) : (
    <span key={label} style={{ display: 'inline-flex', alignItems: 'center', gap: '3px', padding: '1px 7px', borderRadius: '5px', fontSize: '0.6rem', fontWeight: '700', background: `${muted}10`, color: muted, border: `1px solid ${muted}20`, fontFamily: FONT }}>
      — {label}
    </span>
  )

  const piiPending = governance?.pii_columns_pending_review ?? 0

  return (
    <div style={{ background: surface, border: `1px solid ${accent}25`, borderLeft: `3px solid ${accent}60`, borderRadius: '10px', padding: '14px 18px' }}>

      {/* Panel header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px' }}>
        <span style={{ fontSize: '0.62rem', fontWeight: '700', color: accent, letterSpacing: '0.08em', textTransform: 'uppercase', fontFamily: FONT }}>
          Business Context
        </span>
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
          {overall_confidence != null && (
            <ConfChip v={overall_confidence} success={success} warn={warn} danger={danger} />
          )}
          {metadata_completeness?.completeness_score != null && (
            <span style={{ fontSize: '0.62rem', color: muted, fontFamily: FONT }}>
              {Math.round(metadata_completeness.completeness_score * 100)}% complete
            </span>
          )}
        </div>
      </div>

      {/* Business facts grid */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px 24px', marginBottom: hasDictionary && dictionary.description ? '10px' : '0' }}>
        {hasDictionary && dictionary.business_name && (
          <div>
            {lbl('Business Name')}
            <div style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
              {val(dictionary.business_name, { fontWeight: '600' })}
              {dictionary.is_approved && (
                <span style={{ fontSize: '0.6rem', color: success, fontWeight: '700' }}>✓</span>
              )}
            </div>
          </div>
        )}
        {hasDomain && (
          <div>
            {lbl('Domain')}
            <div style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
              {val(domain.domain)}
              {domain.confidence != null && (
                <span style={{ fontSize: '0.62rem', color: muted, fontFamily: MONO }}>{Math.round(domain.confidence * 100)}%</span>
              )}
            </div>
          </div>
        )}
        {hasEntity && (
          <div>
            {lbl('Business Entity')}
            <div style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
              {val(entity.entity)}
              {entity.confidence != null && (
                <span style={{ fontSize: '0.62rem', color: muted, fontFamily: MONO }}>{Math.round(entity.confidence * 100)}%</span>
              )}
            </div>
          </div>
        )}
        {dictionary?.grain && (
          <div>
            {lbl('Grain')}
            {val(dictionary.grain)}
          </div>
        )}
        {profiling?.table_class && (
          <div>
            {lbl('Classification')}
            <div style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
              <ClassChip cls={profiling.table_class} />
              {profiling.classification_confidence != null && (
                <span style={{ fontSize: '0.62rem', color: muted, fontFamily: MONO }}>{Math.round(profiling.classification_confidence * 100)}%</span>
              )}
            </div>
          </div>
        )}
        {(profiling?.pii_column_count > 0) && (
          <div>
            {lbl('PII Summary')}
            <div style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
              <span style={{ fontSize: '0.8rem', fontWeight: '600', color: danger, fontFamily: FONT }}>{profiling.pii_column_count} flagged</span>
              {profiling.confirmed_pii_count > 0 && (
                <span style={{ fontSize: '0.72rem', color: muted, fontFamily: FONT }}>/ {profiling.confirmed_pii_count} confirmed</span>
              )}
              {piiPending > 0 && (
                <span style={{ fontSize: '0.6rem', color: warn, fontFamily: FONT }}>({piiPending} pending review)</span>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Description (full width) */}
      {hasDictionary && dictionary.description && (
        <div style={{ padding: '8px 10px', borderRadius: '6px', background: `${accent}07`, marginBottom: '10px' }}>
          {lbl('Description')}
          <div style={{ fontSize: '0.78rem', color: textSec, fontFamily: FONT, lineHeight: '1.5', marginTop: '2px' }}>{dictionary.description}</div>
        </div>
      )}

      {/* Governance badges */}
      <div style={{ display: 'flex', gap: '5px', flexWrap: 'wrap', marginBottom: (outbound.length + inbound.length > 0) ? '10px' : '0' }}>
        {govBadge('Dict', governance?.dictionary_approved)}
        {govBadge('Domain', governance?.domain_assigned)}
        {govBadge('Entity', governance?.entity_assigned)}
        {piiPending > 0 && (
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: '3px', padding: '1px 7px', borderRadius: '5px', fontSize: '0.6rem', fontWeight: '700', background: `${warn}12`, color: warn, border: `1px solid ${warn}30`, fontFamily: FONT }}>
            ⚠ {piiPending} PII pending
          </span>
        )}
      </div>

      {/* Relationships */}
      {(outbound.length > 0 || inbound.length > 0) && (
        <div>
          <div style={{ fontSize: '0.6rem', fontWeight: '700', color: muted, letterSpacing: '0.06em', textTransform: 'uppercase', fontFamily: FONT, marginBottom: '5px' }}>
            Relationships · {outbound.length} outbound · {inbound.length} inbound
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
            {outbound.slice(0, 3).map((r, i) => (
              <div key={i} style={{ display: 'flex', alignItems: 'center', gap: '5px', fontSize: '0.72rem', fontFamily: MONO, color: textSec }}>
                <span style={{ color: accent, fontSize: '0.62rem' }}>→</span>
                <span style={{ color: muted }}>{r.from_column}</span>
                <span style={{ color: `${muted}50`, fontSize: '0.62rem' }}>→</span>
                <span>{r.to_table_fqn}.{r.to_column}</span>
                {r.relationship_name && <span style={{ fontSize: '0.62rem', color: muted }}>({r.relationship_name})</span>}
              </div>
            ))}
            {inbound.slice(0, 2).map((r, i) => (
              <div key={`in-${i}`} style={{ display: 'flex', alignItems: 'center', gap: '5px', fontSize: '0.72rem', fontFamily: MONO, color: textSec }}>
                <span style={{ color: `${success}90`, fontSize: '0.62rem' }}>←</span>
                <span>{r.from_table_fqn}.{r.from_column}</span>
                <span style={{ color: `${muted}50`, fontSize: '0.62rem' }}>→</span>
                <span style={{ color: muted }}>{r.to_column}</span>
              </div>
            ))}
            {(outbound.length > 3 || inbound.length > 2) && (
              <span style={{ fontSize: '0.68rem', color: muted, fontFamily: FONT }}>
                +{Math.max(0, outbound.length - 3) + Math.max(0, inbound.length - 2)} more
              </span>
            )}
          </div>
        </div>
      )}

    </div>
  )
}

// ── TableProfileHeader ─────────────────────────────────────────────────────────

function TableProfileHeader({ table, C }) {
  const surface = C.surface  ?? '#0d1128'
  const border  = C.border   ?? '#1e2b52'
  const text    = C.text     ?? '#eef0ff'
  const textSec = C.textSec  ?? '#dde1ff'
  const muted   = C.textMuted ?? '#7880a8'
  const success = C.success  ?? '#10b981'
  const danger  = C.danger   ?? '#f87171'
  const warn    = C.warn     ?? '#f59e0b'

  const kpi = (label, value) => value == null ? null : (
    <div key={label} style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
      <span style={{ fontSize: '0.6rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT }}>{label}</span>
      <span style={{ fontSize: '0.88rem', fontWeight: '700', color: text, fontFamily: FONT }}>{value}</span>
    </div>
  )

  const flag = (label, v) => v == null ? null : (
    <div key={label} style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
      <span style={{ fontSize: '0.7rem', color: muted, fontFamily: FONT }}>{label}:</span>
      <BoolIcon v={v} success={success} muted={muted} />
    </div>
  )

  const rowCount = table.exact_row_count ?? table.estimated_row_count
  const rowCountLabel = rowCount != null ? fmtNum(rowCount) + (table.exact_row_count == null ? ' est.' : '') : null
  const confPct = table.classification_confidence != null ? `${Math.round(table.classification_confidence * 100)}%` : null

  return (
    <div style={{ background: surface, border: `1px solid ${border}`, borderRadius: '10px', padding: '14px 18px', marginBottom: '12px' }}>
      {/* Title row */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '10px', flexWrap: 'wrap' }}>
        <span style={{ fontSize: '1rem', fontWeight: '700', color: text, fontFamily: MONO }}>
          {table.table_fqn}
        </span>
        <ClassChip cls={table.table_class} />
        {confPct && (
          <span style={{ fontSize: '0.7rem', color: muted, fontFamily: FONT }}>{confPct} confidence</span>
        )}
        {(table.pii_column_count > 0) && (
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: '4px', padding: '1px 8px', borderRadius: '6px', fontSize: '0.6rem', fontWeight: '700', background: `${danger}18`, color: danger, border: `1px solid ${danger}35`, fontFamily: FONT }}>
            {table.pii_column_count} PII
          </span>
        )}
        <TierChip tier={table.row_count_tier} />
      </div>

      {/* Meta row */}
      <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', marginBottom: '12px', alignItems: 'center' }}>
        <span style={{ fontSize: '0.72rem', color: muted, fontFamily: FONT }}>Schema: <span style={{ color: textSec, fontFamily: MONO }}>{table.schema_name}</span></span>
        <span style={{ color: `${muted}50` }}>·</span>
        <span style={{ fontSize: '0.72rem', color: muted, fontFamily: FONT }}>{table.table_type}</span>
        <span style={{ color: `${muted}50` }}>·</span>
        <span style={{ fontSize: '0.72rem', color: muted, fontFamily: FONT }}>Depth: <span style={{ color: textSec }}>{table.profiling_depth}</span></span>
        {table.profiling_status && (
          <>
            <span style={{ color: `${muted}50` }}>·</span>
            <span style={{ fontSize: '0.72rem', color: table.profiling_status === 'COMPLETE' ? success : warn, fontFamily: FONT }}>{table.profiling_status}</span>
          </>
        )}
      </div>

      {/* KPI row */}
      <div style={{ display: 'flex', gap: '20px', flexWrap: 'wrap', alignItems: 'flex-start' }}>
        {kpi('Rows',         rowCountLabel)}
        {kpi('Columns',      table.column_count)}
        {kpi('Primary Keys', table.pk_column_count)}
        {kpi('Foreign Keys', table.fk_count)}
        {kpi('Referenced By', table.referenced_by_count)}
        {kpi('PII Columns',  table.pii_column_count > 0 ? table.pii_column_count : null)}
        <div style={{ display: 'flex', gap: '16px', flexWrap: 'wrap', alignItems: 'center', marginTop: '2px' }}>
          {flag('Root',     table.is_root_table)}
          {flag('Leaf',     table.is_leaf_table)}
          {flag('Junction', table.is_junction_table)}
          {flag('Identity', table.has_identity_column)}
        </div>
      </div>
    </div>
  )
}

// ── ColumnGrid ─────────────────────────────────────────────────────────────────

function ColumnGrid({ columns, sort, onSort, onSelectCol, C }) {
  const surface = C.surface  ?? '#0d1128'
  const border  = C.border   ?? '#1e2b52'
  const text    = C.text     ?? '#eef0ff'
  const textSec = C.textSec  ?? '#dde1ff'
  const muted   = C.textMuted ?? '#7880a8'
  const accent  = C.accent   ?? '#6366f1'
  const success = C.success  ?? '#10b981'
  const danger  = C.danger   ?? '#f87171'
  const warn    = C.warn     ?? '#f59e0b'

  const totalW = GRID_COLS.reduce((s, c) => s + c.w, 0) + GRID_COLS.length * 8

  const hdrCell = (col) => (
    <div
      key={col.field}
      onClick={() => onSort(col.field)}
      style={{
        width: col.w, minWidth: col.w, maxWidth: col.w,
        fontSize: '0.6rem', fontWeight: '700', color: sort.field === col.field ? text : muted,
        letterSpacing: '0.06em', textTransform: 'uppercase', fontFamily: FONT,
        cursor: 'pointer', userSelect: 'none', overflow: 'hidden', textOverflow: 'ellipsis',
        whiteSpace: 'nowrap', padding: '0 4px',
      }}
    >
      {col.label}<SortArrow field={col.field} sort={sort} />
    </div>
  )

  const cell = (value, opts = {}) => (
    <div key={opts.key} style={{
      width: opts.w, minWidth: opts.w, maxWidth: opts.w,
      fontSize: opts.sm ? '0.68rem' : '0.74rem',
      color: opts.color ?? textSec, fontFamily: opts.mono ? MONO : FONT,
      overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
      padding: '0 4px', display: 'flex', alignItems: 'center',
    }}>
      {value}
    </div>
  )

  const renderColCells = (col) => (
    GRID_COLS.map(({ field, w, mono }) => {
      const v = col[field]
      if (field === 'column_name') return cell(
        <span title={v} style={{ fontFamily: MONO, fontSize: '0.74rem', color: text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{v}</span>,
        { w, mono: true, key: field }
      )
      if (field === 'data_type')  return cell(<span style={{ fontSize: '0.7rem', color: muted }}>{v ?? '—'}</span>, { w, key: field })
      if (field === 'raw_type')   return cell(<span title={v} style={{ fontSize: '0.68rem', color: muted, fontFamily: MONO }}>{v ?? '—'}</span>, { w, key: field })
      if (field === 'semantic_type')       return cell(<SemChip type={v} />, { w, key: field })
      if (field === 'semantic_confidence') return cell(<ConfChip v={v} success={success} warn={warn} danger={danger} />, { w, key: field })
      if (field === 'is_nullable')         return cell(<BoolIcon v={v} success={success} muted={muted} />, { w, key: field })
      if (field === 'is_primary_key')      return cell(<BoolIcon v={v} success={success} muted={muted} />, { w, key: field })
      if (field === 'is_identity')         return cell(<BoolIcon v={v} success={success} muted={muted} />, { w, key: field })
      if (field === 'pii_name_heuristic')  return cell(bool(v) ? <PiiBadge v={v} danger={danger} /> : <span style={{ color: muted, fontSize: '0.68rem' }}>—</span>, { w, key: field })
      if (field === 'null_percentage')     return cell(<span style={{ color: v != null && v > 30 ? warn : (v != null && v > 10 ? `${warn}aa` : muted), fontFamily: MONO, fontSize: '0.72rem' }}>{fmtPct(v) ?? '—'}</span>, { w, key: field })
      if (field === 'distinct_percentage') return cell(<span style={{ color: muted, fontFamily: MONO, fontSize: '0.72rem' }}>{fmtPct(v) ?? '—'}</span>, { w, key: field })
      if (field === 'uniqueness_score')    return cell(<span style={{ color: muted, fontFamily: MONO, fontSize: '0.72rem' }}>{v != null ? fmtScore(v) : '—'}</span>, { w, key: field })
      if (field === 'cardinality_tier')    return cell(<span style={{ fontSize: '0.65rem', color: muted }}>{v ?? '—'}</span>, { w, key: field })
      if (field === 'avg_length')          return cell(<span style={{ color: muted, fontFamily: MONO, fontSize: '0.72rem' }}>{v != null ? Number(v).toFixed(1) : '—'}</span>, { w, key: field })
      if (field === 'dominant_pattern')    return cell(<span title={v} style={{ fontFamily: MONO, fontSize: '0.68rem', color: muted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{v ?? '—'}</span>, { w, key: field })
      if (field === 'profiling_status')    return cell(<span style={{ fontSize: '0.66rem', color: v === 'COMPLETE' ? success : warn }}>{v ?? '—'}</span>, { w, key: field })
      return cell(<span style={{ color: muted, fontSize: '0.72rem' }}>{v != null ? String(v) : '—'}</span>, { w, key: field })
    })
  )

  return (
    <div style={{ background: surface, border: `1px solid ${border}`, borderRadius: '10px', overflow: 'hidden' }}>
      {/* Sticky header */}
      <div style={{ overflowX: 'auto' }}>
        <div style={{ minWidth: totalW }}>
          {/* Header row */}
          <div style={{
            display: 'flex', alignItems: 'center', gap: '8px',
            padding: '8px 14px', background: `${surface}f0`,
            borderBottom: `1px solid ${border}`,
            position: 'sticky', top: 0, zIndex: 1,
          }}>
            {GRID_COLS.map(hdrCell)}
          </div>
          {/* Data rows */}
          <div style={{ maxHeight: '420px', overflowY: 'auto' }}>
            {columns.map((col, i) => (
              <div
                key={col.column_name ?? i}
                onClick={() => onSelectCol(col)}
                style={{
                  display: 'flex', alignItems: 'center', gap: '8px',
                  padding: '7px 14px',
                  borderBottom: `1px solid ${border}20`,
                  background: i % 2 === 0 ? 'transparent' : `${accent}05`,
                  cursor: 'pointer',
                  transition: 'background 0.1s',
                }}
                onMouseEnter={e => e.currentTarget.style.background = `${accent}10`}
                onMouseLeave={e => e.currentTarget.style.background = i % 2 === 0 ? 'transparent' : `${accent}05`}
              >
                {renderColCells(col)}
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}

// ── Main component ─────────────────────────────────────────────────────────────

export default function ColumnProfileExplorer({
  C = {},
  token,
  sourceId,
  profileData,
  profLoading,
  profError,
  hasSchema,
  onRunProfile,
  profileRunning,
}) {
  const bg      = C.bg       ?? '#07091a'
  const surface = C.surface  ?? '#0d1128'
  const border  = C.border   ?? '#1e2b52'
  const text    = C.text     ?? '#eef0ff'
  const textSec = C.textSec  ?? '#dde1ff'
  const muted   = C.textMuted ?? '#7880a8'
  const accent  = C.accent   ?? '#6366f1'
  const success = C.success  ?? '#10b981'
  const danger  = C.danger   ?? '#f87171'

  const [selectedFqn,   setSelectedFqn]   = useState(null)
  const [tableDetail,   setTableDetail]   = useState(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError,   setDetailError]   = useState(null)
  const [selectedCol,   setSelectedCol]   = useState(null)
  const [tblSearch,     setTblSearch]     = useState('')
  const [classFilter,   setClassFilter]   = useState('')
  const [tblSort,       setTblSort]       = useState({ field: 'table_name', dir: 'asc' })
  const [colSort,       setColSort]       = useState({ field: null, dir: 'asc' })

  // Business Knowledge Graph state
  const [bkgData,    setBkgData]    = useState(null)
  const [bkgLoading, setBkgLoading] = useState(false)
  const [colBkgData, setColBkgData] = useState(null)

  // Fetch profiling detail + business context when a table is selected
  useEffect(() => {
    if (!selectedFqn) {
      setTableDetail(null); setSelectedCol(null)
      setBkgData(null); setColBkgData(null)
      return
    }
    setDetailLoading(true)
    setDetailError(null)
    setTableDetail(null)
    setSelectedCol(null)
    setBkgData(null)
    setBkgLoading(true)

    getTableProfileDetail(sourceId, selectedFqn, token)
      .then(resp => setTableDetail(resp?.data ?? null))
      .catch(e => setDetailError(e?.message ?? 'Failed to load table profile.'))
      .finally(() => setDetailLoading(false))

    // Business context is enrichment — fails silently so profiling still works
    getTableBusinessContext(sourceId, selectedFqn, token)
      .then(resp => setBkgData(resp?.data ?? null))
      .catch(() => setBkgData(null))
      .finally(() => setBkgLoading(false))
  }, [selectedFqn, sourceId, token])

  // Fetch column business context when a column drawer opens
  useEffect(() => {
    if (!selectedCol || !selectedFqn) { setColBkgData(null); return }
    getColumnBusinessContext(sourceId, selectedFqn, selectedCol.column_name, token)
      .then(resp => setColBkgData(resp?.data ?? null))
      .catch(() => setColBkgData(null))
  }, [selectedCol, selectedFqn, sourceId, token])

  const tables = profileData?.tables ?? []
  const snapshot = profileData?.snapshot

  const uniqueClasses = [...new Set(tables.map(t => t.table_class).filter(Boolean))].sort()

  const filteredTables = sortItems(
    tables.filter(t => {
      if (tblSearch) {
        const q = tblSearch.toLowerCase()
        if (!(t.table_name ?? '').toLowerCase().includes(q) &&
            !(t.schema_name ?? '').toLowerCase().includes(q)) return false
      }
      if (classFilter && t.table_class !== classFilter) return false
      return true
    }),
    tblSort.field, tblSort.dir,
  )

  const sortedColumns = sortItems(tableDetail?.columns ?? [], colSort.field, colSort.dir)

  const card = (extra = {}) => ({
    background: surface, border: `1px solid ${border}`,
    borderRadius: '10px', ...extra,
  })

  const inp = () => ({
    background: bg, border: `1px solid ${border}`, borderRadius: '8px',
    color: text, fontFamily: FONT, fontSize: '0.78rem', padding: '6px 10px',
    outline: 'none', width: '100%', boxSizing: 'border-box',
  })

  // ── Pre-profile gate ────────────────────────────────────────────────────────
  if (!hasSchema) {
    return (
      <div style={{ ...card({ padding: '40px 24px' }), textAlign: 'center' }}>
        <p style={{ margin: '0 0 6px', fontSize: '0.88rem', color: textSec, fontWeight: '500', fontFamily: FONT }}>No profiling data yet</p>
        <p style={{ margin: 0, fontSize: '0.78rem', color: muted, fontFamily: FONT }}>Run Discover & Profile to generate a structural profile.</p>
      </div>
    )
  }

  if (profLoading) {
    return (
      <div style={{ ...card({ padding: '24px' }), textAlign: 'center', color: muted, fontSize: '0.82rem', fontFamily: FONT }}>
        Loading profile…
      </div>
    )
  }

  if (profError) {
    return (
      <div style={{ padding: '10px 14px', borderRadius: '8px', background: `${danger}10`, border: `1px solid ${danger}30` }}>
        <span style={{ fontSize: '0.78rem', color: danger, fontFamily: FONT }}>{profError}</span>
      </div>
    )
  }

  if (!profileData || tables.length === 0) {
    return (
      <div style={{ ...card({ padding: '40px 24px' }), textAlign: 'center' }}>
        <p style={{ margin: '0 0 6px', fontSize: '0.88rem', color: textSec, fontWeight: '500', fontFamily: FONT }}>Profile not available</p>
        <p style={{ margin: '0 0 16px', fontSize: '0.78rem', color: muted, fontFamily: FONT }}>Schema was discovered but structural profiling has not run yet.</p>
        <button onClick={onRunProfile} disabled={profileRunning} style={{ background: `${accent}18`, border: `1px solid ${accent}50`, color: accent, borderRadius: '8px', padding: '7px 16px', fontSize: '0.8rem', fontFamily: FONT, cursor: profileRunning ? 'not-allowed' : 'pointer', opacity: profileRunning ? 0.65 : 1 }}>
          {profileRunning ? 'Running…' : 'Run Profile'}
        </button>
      </div>
    )
  }

  // ── Summary banner ──────────────────────────────────────────────────────────
  const banner = snapshot && (
    <div style={{ ...card({ padding: '10px 16px', marginBottom: '12px' }), display: 'flex', gap: '20px', flexWrap: 'wrap', alignItems: 'center' }}>
      {[
        ['Mode',     snapshot.mode],
        ['Tables',   snapshot.tables_profiled != null ? `${snapshot.tables_profiled} / ${snapshot.tables_total}` : null],
        ['Columns',  snapshot.columns_total?.toLocaleString()],
        ['PII Cols', snapshot.pii_columns_found > 0 ? snapshot.pii_columns_found : null],
        ['Status',   snapshot.status],
      ].filter(([, v]) => v != null).map(([label, value]) => (
        <div key={label} style={{ display: 'flex', flexDirection: 'column', gap: '1px' }}>
          <span style={{ fontSize: '0.6rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT }}>{label}</span>
          <span style={{ fontSize: '0.84rem', fontWeight: '700', color: label === 'Status' && value === 'COMPLETE' ? success : text, fontFamily: FONT }}>{value}</span>
        </div>
      ))}
    </div>
  )

  // ── Split layout ────────────────────────────────────────────────────────────
  return (
    <div style={{ display: 'flex', flexDirection: 'column' }}>
      {banner}

      <div style={{ display: 'flex', gap: '12px', alignItems: 'flex-start' }}>

        {/* ── LEFT PANEL: Table Inventory ────────────────────────────────── */}
        <div style={{ width: '268px', flexShrink: 0, ...card({ overflow: 'hidden', padding: 0 }) }}>

          {/* Search + filter */}
          <div style={{ padding: '10px 12px', borderBottom: `1px solid ${border}`, display: 'flex', flexDirection: 'column', gap: '6px' }}>
            <input
              style={inp()}
              placeholder="Search tables…"
              value={tblSearch}
              onChange={e => setTblSearch(e.target.value)}
            />
            <select
              style={{ ...inp(), cursor: 'pointer' }}
              value={classFilter}
              onChange={e => setClassFilter(e.target.value)}
            >
              <option value="">All Classes</option>
              {uniqueClasses.map(c => <option key={c} value={c}>{c}</option>)}
            </select>
          </div>

          {/* Column headers */}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr auto', padding: '5px 12px', borderBottom: `1px solid ${border}`, gap: '8px' }}>
            {[
              { label: 'Table', field: 'table_name' },
              { label: 'Cols',  field: 'column_count' },
            ].map(({ label, field }) => (
              <div
                key={field}
                onClick={() => setTblSort(s => nextSort(s, field))}
                style={{ fontSize: '0.58rem', fontWeight: '700', color: tblSort.field === field ? text : muted, letterSpacing: '0.06em', textTransform: 'uppercase', fontFamily: FONT, cursor: 'pointer', userSelect: 'none' }}
              >
                {label}<SortArrow field={field} sort={tblSort} />
              </div>
            ))}
          </div>

          {/* Table rows */}
          <div style={{ overflowY: 'auto', maxHeight: '560px' }}>
            {filteredTables.length === 0 && (
              <div style={{ padding: '24px', textAlign: 'center', color: muted, fontSize: '0.78rem', fontFamily: FONT }}>
                No tables match the filter.
              </div>
            )}
            {filteredTables.map((t, i) => {
              const selected = t.table_fqn === selectedFqn
              return (
                <div
                  key={t.table_fqn ?? i}
                  onClick={() => setSelectedFqn(t.table_fqn)}
                  style={{
                    padding: '8px 12px',
                    borderBottom: `1px solid ${border}15`,
                    background: selected ? `${accent}14` : i % 2 === 0 ? 'transparent' : `${bg}50`,
                    borderLeft: selected ? `3px solid ${accent}` : '3px solid transparent',
                    cursor: 'pointer',
                    transition: 'background 0.1s',
                  }}
                  onMouseEnter={e => { if (!selected) e.currentTarget.style.background = `${accent}08` }}
                  onMouseLeave={e => { if (!selected) e.currentTarget.style.background = i % 2 === 0 ? 'transparent' : `${bg}50` }}
                >
                  {/* Row 1: name + cols */}
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '6px', marginBottom: '4px' }}>
                    <span style={{ fontSize: '0.76rem', fontWeight: '600', color: selected ? accent : text, fontFamily: MONO, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }} title={t.table_fqn}>
                      {t.table_name}
                    </span>
                    {t.column_count > 0 && (
                      <span style={{ fontSize: '0.62rem', color: muted, fontFamily: MONO, flexShrink: 0 }}>{t.column_count}</span>
                    )}
                  </div>
                  {/* Row 2: schema + badges */}
                  <div style={{ display: 'flex', alignItems: 'center', gap: '4px', flexWrap: 'wrap' }}>
                    <span style={{ fontSize: '0.62rem', color: muted, fontFamily: FONT }}>{t.schema_name}</span>
                    {t.table_class && <ClassChip cls={t.table_class} />}
                    <TierChip tier={t.row_count_tier} />
                    {t.pii_column_count > 0 && (
                      <span style={{ fontSize: '0.58rem', fontWeight: '700', color: danger, fontFamily: FONT }}>{t.pii_column_count}✗</span>
                    )}
                    {t.classification_confidence != null && (
                      <span style={{ fontSize: '0.58rem', color: muted, fontFamily: MONO }}>{Math.round(t.classification_confidence * 100)}%</span>
                    )}
                  </div>
                </div>
              )
            })}
          </div>

          {/* Footer count */}
          <div style={{ padding: '6px 12px', borderTop: `1px solid ${border}`, fontSize: '0.62rem', color: muted, fontFamily: FONT }}>
            {filteredTables.length} of {tables.length} tables
          </div>
        </div>

        {/* ── RIGHT PANEL: Table Profile ────────────────────────────────── */}
        <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: '10px' }}>

          {!selectedFqn && (
            <div style={{ ...card({ padding: '48px 24px' }), textAlign: 'center' }}>
              <p style={{ margin: '0 0 4px', fontSize: '0.9rem', color: textSec, fontWeight: '500', fontFamily: FONT }}>Select a table</p>
              <p style={{ margin: 0, fontSize: '0.78rem', color: muted, fontFamily: FONT }}>Click any table in the inventory to view its full profile and column intelligence.</p>
            </div>
          )}

          {selectedFqn && detailLoading && (
            <div style={{ ...card({ padding: '24px' }), textAlign: 'center', color: muted, fontSize: '0.82rem', fontFamily: FONT }}>
              Loading table profile…
            </div>
          )}

          {selectedFqn && detailError && (
            <div style={{ padding: '10px 14px', borderRadius: '8px', background: `${danger}10`, border: `1px solid ${danger}30` }}>
              <span style={{ fontSize: '0.78rem', color: danger, fontFamily: FONT }}>{detailError}</span>
            </div>
          )}

          {selectedFqn && tableDetail?.table && (
            <>
              <BusinessContextPanel bkg={bkgData} loading={bkgLoading && !bkgData} C={C} />
              <TableProfileHeader table={tableDetail.table} C={C} />

              {sortedColumns.length > 0 && (
                <>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '8px' }}>
                    <span style={{ fontSize: '0.7rem', fontWeight: '700', color: muted, letterSpacing: '0.07em', textTransform: 'uppercase', fontFamily: FONT }}>
                      Column Intelligence · {sortedColumns.length} columns
                    </span>
                    <span style={{ fontSize: '0.68rem', color: muted, fontFamily: FONT }}>Click a column for full detail</span>
                  </div>
                  <ColumnGrid
                    columns={sortedColumns}
                    sort={colSort}
                    onSort={field => setColSort(s => nextSort(s, field))}
                    onSelectCol={setSelectedCol}
                    C={C}
                  />
                </>
              )}

              {sortedColumns.length === 0 && !detailLoading && (
                <div style={{ ...card({ padding: '24px' }), textAlign: 'center', color: muted, fontSize: '0.78rem', fontFamily: FONT }}>
                  No column profiles stored for this table.
                </div>
              )}
            </>
          )}
        </div>
      </div>

      {/* ── Column Detail Drawer ───────────────────────────────────────────── */}
      {selectedCol && (
        <ColumnDetailDrawer col={selectedCol} colBkg={colBkgData} onClose={() => setSelectedCol(null)} C={C} />
      )}
    </div>
  )
}
