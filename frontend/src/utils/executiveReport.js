// Executive Report V3 Management Dashboard — pure data-shaping helpers.
//
// These functions never invent numbers or call any API; they only
// normalize/select fields already present on the report dict produced by
// core.analysis.models.ExecutiveAnalysisReport.to_executive_report_data_dict()
// (frontend/src/components/ExecutiveReportView.jsx renders the result). A
// field this module can't find on the report is treated as unavailable, not
// guessed.
import { humanizeKpiTitle } from '../components/executiveReport/shared'

// Normalizes the two KPI shapes this report can carry into one common shape:
//   - top-level report.kpi_cards: core.reporting.report_models.KPICard
//     (flat previous_value/percent_change/direction/comparison_label, and
//     NO formatted_value field at all — that dataclass never carries one)
//   - report.business_areas[].kpis / student_program_overview.kpis / etc.:
//     core.analysis.models.KPIItem (nested `comparison` object, DOES carry
//     formatted_value — the evidence's own raw_answer_text sentence)
//
// `fallbackFormattedValue` lets a caller supply the matching section KPI's
// formatted_value for a top-level kpi_cards entry sharing the same id (see
// buildFormattedValueIndex) so title-humanization (below) still has real
// evidence text to work from even though KPICard itself has none.
export function normalizeKpi(raw, fallbackFormattedValue) {
  if (!raw) return null
  const cmp = raw.comparison || null
  const formattedValue = raw.formatted_value ?? fallbackFormattedValue ?? null
  return {
    id: raw.id,
    title: humanizeKpiTitle(raw.title, formattedValue),
    value: raw.value,
    formattedValue,
    unit: raw.unit ?? null,
    previousValue: cmp ? cmp.previous_value : raw.previous_value ?? null,
    absoluteChange: cmp ? cmp.absolute_change : raw.absolute_change ?? null,
    percentChange: cmp ? cmp.percent_change : raw.percent_change ?? null,
    direction: cmp ? cmp.direction : raw.direction ?? null,
    comparisonLabel: cmp ? cmp.comparison_label : raw.comparison_label ?? null,
    warning: raw.warning ?? null,
    limitation: raw.limitation ?? null,
    confidence: raw.confidence ?? null,
  }
}

// core.analysis.section_builder.build_sections gives student_program_
// overview/financial_performance/operations_engagement the SAME
// ExecutiveSection.title as their underlying discovered domain (e.g.
// "Enrollment"), not a category label — the category itself is asserted by
// WHICH typed field the backend put the section under (core.analysis.
// section_classifier's own classification), not by any text on the section.
// A frontend label keyed only on that field name is therefore reusing
// existing backend evidence (Part 4's own "entity = Program, value = 40"
// example), not inventing one — and it's fully generic: any source whose
// evidence classifies as financial/operations gets the SAME fixed label,
// never a database-specific string.
const CATEGORY_LABELS = {
  student_program_overview: 'Students & Programs',
  financial_performance: 'Business Performance',
  operations_engagement: 'Operations & Engagement',
}

// Every ExecutiveSection-shaped object the report can carry, in display
// priority order. Named sections (student_program_overview/
// financial_performance/operations_engagement) come first because
// core.analysis.section_classifier only populates them from real matched
// evidence; business_areas holds one section per discovered domain and acts
// as the generic fallback for any business this isn't the reference database (Part 5/17).
export function allSections(report) {
  if (!report) return []
  const namedKeys = ['student_program_overview', 'financial_performance', 'operations_engagement']
  const named = namedKeys
    .filter(key => report[key])
    .map(key => ({ ...report[key], displayTitle: CATEGORY_LABELS[key] }))
  const namedIds = new Set(named.map(s => s.section_id))
  const areas = (report.business_areas || []).filter(s => s && !namedIds.has(s.section_id))
  return [...named, ...areas]
}

// Sections worth featuring as an "entity summary" panel (Part 5) — must
// carry at least one KPI or table, i.e. actual evidence-backed content, not
// just a title with nothing under it.
export function entitySections(report, max = 2) {
  return allSections(report)
    .filter(s => (s.kpis?.length || 0) > 0 || (s.tables?.length || 0) > 0)
    .slice(0, max)
}

// report.kpi_cards (KPICard) and a section's own kpis (KPIItem) for the SAME
// underlying evidence share the same id (both set id=task_id — see
// core.analysis.report_builder._build_kpi_cards and .section_builder.
// kpi_item_from_evidence), so a section's formatted_value can be reliably
// looked up for the matching top-level kpi_cards entry.
function buildFormattedValueIndex(report) {
  const index = new Map()
  allSections(report).forEach(s => (s.kpis || []).forEach(k => {
    if (k?.id && k.formatted_value) index.set(k.id, k.formatted_value)
  }))
  return index
}

// Top-level KPIs for the Business Snapshot strip: report.kpi_cards already
// IS this curated top set (core.analysis.report_builder._build_kpi_cards),
// deduplicated by id against anything already shown in a featured entity
// section so the same fact isn't presented twice.
export function snapshotKpis(report, excludeIds = new Set()) {
  const fmtIndex = buildFormattedValueIndex(report)
  return (report?.kpi_cards || [])
    .map(raw => normalizeKpi(raw, fmtIndex.get(raw?.id)))
    .filter(k => k && !excludeIds.has(k.id))
}

// KPIs (from anywhere in the report) that carry a real period comparison —
// these back the "Key Trends" section. Never computed here; only selected
// from comparisons the backend already produced.
export function trendKpis(report) {
  const fmtIndex = buildFormattedValueIndex(report)
  const seen = new Set()
  const out = []
  const consider = (raw) => {
    const k = normalizeKpi(raw, fmtIndex.get(raw?.id))
    if (!k || k.previousValue == null || seen.has(k.id)) return
    seen.add(k.id)
    out.push(k)
  }
  ;(report?.kpi_cards || []).forEach(consider)
  allSections(report).forEach(s => (s.kpis || []).forEach(consider))
  return out
}

export function resolveInsights(report) {
  return (report?.insights || []).filter(f => f && !f.is_plain_kpi)
}

export function resolveRecommendations(report) {
  return report?.recommendations || []
}

// True once a report carries the V3 structured sections this dashboard is
// built around. A legacy (pre-Executive-Report-V3) or KPI-only report still
// renders — just without the entity-summary/trend/insight panels that have
// no backing data.
export function isStructuredReport(report) {
  return !!report && (
    report.version >= 3 ||
    !!report.student_program_overview ||
    (report.business_areas || []).length > 0
  )
}

export function hasAnyContent(report) {
  if (!report) return false
  return (
    (report.kpi_cards?.length || 0) > 0 ||
    (report.charts?.length || 0) > 0 ||
    (report.tables?.length || 0) > 0 ||
    allSections(report).length > 0
  )
}
