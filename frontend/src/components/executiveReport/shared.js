// Shared formatting/visual constants for the Executive Report management
// dashboard components (frontend/src/components/executiveReport/*.jsx).

export const FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"

export const DIRECTION_META = {
  up:      { symbol: '▲', color: '#10b981' },
  down:    { symbol: '▼', color: '#ef4444' },
  flat:    { symbol: '▬', color: '#94a3b8' },
  unknown: { symbol: '',  color: '#94a3b8' },
}

const CURRENCY_HINT = /amount|revenue|payment|fee|price|cost|total\s*\$|\$/i

export function looksLikeCurrency(title, unit) {
  if (unit && /(\$|usd|dollar)/i.test(unit)) return true
  return !!title && CURRENCY_HINT.test(title)
}

// Renders a raw KPI value the way a management dashboard states it — never
// re-derives it. Large currency-flavored values collapse to $2.00M-style
// text (readable at the "10-15 second glance" the milestone asks for);
// everything else uses locale grouping. Never guesses a unit that isn't
// already on the KPI.
export function fmtKpiValue(value, { unit, isCurrency } = {}) {
  if (value == null) return '—'
  if (typeof value !== 'number' || !isFinite(value)) return String(value)
  if (isCurrency) {
    const abs = Math.abs(value)
    if (abs >= 1_000_000) return `$${(value / 1_000_000).toFixed(2)}M`
    if (abs >= 1_000) return `$${(value / 1_000).toFixed(1)}k`
    return `$${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
  }
  const formatted = Number.isInteger(value)
    ? value.toLocaleString()
    : value.toLocaleString(undefined, { maximumFractionDigits: 2 })
  return unit ? `${formatted} ${unit}` : formatted
}

export function fmtPlainValue(value) {
  if (value == null) return '—'
  if (typeof value !== 'number' || !isFinite(value)) return String(value)
  return Number.isInteger(value) ? value.toLocaleString() : value.toLocaleString(undefined, { maximumFractionDigits: 2 })
}

export function fmtPercent(pct) {
  if (pct == null || !isFinite(pct)) return null
  const rounded = Math.abs(pct) >= 10 ? Math.round(pct) : Math.round(pct * 10) / 10
  return `${rounded > 0 ? '+' : ''}${rounded}%`
}

// Business-area label helper — the display title a section/entity should
// use is whatever business label the backend already resolved (Part 4: use
// semantic labels over physical table names when evidence provides them).
// This never guesses one; it only trims/cleans whitespace on what exists.
export function cleanLabel(label) {
  return (label || '').trim()
}

// Live validation (Tuesday demo sanity check) found core.analysis.
// section_builder.kpi_item_from_evidence/report_builder._build_kpi_cards
// fall back to the raw physical entity/measure name (e.g. "EDU_Student",
// "EDU_Track") as a KPI's `title` whenever no cleaner business_label/measure
// text was resolved upstream — exactly the "EDU_Student"/"EDU_Track" leak
// Part 4 named as unacceptable. UI_RENDERING fix, no backend change: when a
// title looks like a raw identifier, derive a human label from the SAME
// KPI's own already-evidence-backed formatted_value sentence (e.g. "There
// are 12,000 students in the database." -> "Students") instead of showing
// the identifier. This reuses existing backend evidence rather than
// guessing a business concept — it can only ever produce the exact noun the
// backend's own natural-language answer already used (so "EDU_Track" honestly
// becomes "Paths", never a fabricated "Programs" the evidence text doesn't
// itself say). If no such sentence is available, the original title is left
// untouched rather than guessed.
const TECHNICAL_TITLE_RE = /_/
const VALUE_NOUN_RE = /[\d,]+(?:\.\d+)?\s+([a-z][a-z\s]*?)(?=\s+(?:in|this|for|recorded|were|are|was|is)\b|[.,]|$)/i

export function humanizeKpiTitle(title, formattedValue) {
  if (!title || !TECHNICAL_TITLE_RE.test(title)) return title
  const match = typeof formattedValue === 'string' ? formattedValue.match(VALUE_NOUN_RE) : null
  const noun = match?.[1]?.trim()
  if (!noun) return title
  return noun.replace(/\b\w/g, c => c.toUpperCase())
}
