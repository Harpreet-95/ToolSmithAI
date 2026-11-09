import KpiTile from './KpiTile'
import ReportTable from './ReportTable'
import ChartSection from '../ChartSection'
import { normalizeKpi } from '../../utils/executiveReport'
import { FONT } from './shared'

const STATUS_COPY = {
  AMBIGUOUS: 'This business area supports multiple possible definitions, so no single result was assumed.',
  UNAVAILABLE: 'Reliable analysis is not currently available for this business area.',
  SOURCE_DATA_LIMITED: 'The underlying source data for this business area is not sufficiently populated for reliable analysis.',
  GOVERNANCE_BLOCKED: 'This business area is restricted by data-governance policy.',
}

// Milestone 7 Part 5 — a generic entity-summary panel. Renders WHATEVER
// ExecutiveSection-shaped object it is given (core.analysis.models.
// ExecutiveSection.to_dict()): for the reference database that is student_program_overview
// ("Students & Programs"); for a different connected database it renders
// the same way for whatever business_areas entry is featured (e.g.
// "Customers & Products"). No branching on section title/domain — every
// visual decision here is driven by which fields the section actually
// carries.
export default function EntitySummarySection({ section, C, hideKpiIds }) {
  if (!section) return null
  const allKpis = (section.kpis || []).map(normalizeKpi).filter(Boolean)
  const tables = section.tables || []
  const charts = section.charts || []
  // Milestone 7 Part 6 — a KPI already featured in the top Business
  // Snapshot strip isn't repeated here UNLESS this section would otherwise
  // be empty (Part 9: never render a blank section when real evidence-
  // backed content exists for it).
  const filteredKpis = hideKpiIds ? allKpis.filter(k => !hideKpiIds.has(k.id)) : allKpis
  const kpis = (filteredKpis.length > 0 || tables.length > 0 || charts.length > 0) ? filteredKpis : allKpis
  const limitations = section.limitations || []
  const statusMessage = section.status && section.status !== 'SUPPORTED' ? STATUS_COPY[section.status] : null

  const hasContent = kpis.length > 0 || tables.length > 0 || charts.length > 0

  return (
    <section style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '16px', padding: '22px 24px', marginBottom: '20px' }}>
      <div style={{ marginBottom: hasContent ? '16px' : 0 }}>
        <h3 style={{ margin: '0 0 3px', fontSize: '1.05rem', fontWeight: '800', color: C.text, letterSpacing: '-0.3px', fontFamily: FONT }}>
          {section.displayTitle || section.title}
        </h3>
        {section.subtitle && (
          <div style={{ fontSize: '0.74rem', color: C.textMuted }}>{section.subtitle}</div>
        )}
      </div>

      {!hasContent && (
        <div style={{ padding: '18px 4px', fontSize: '0.78rem', color: C.textMuted }}>
          {statusMessage || 'No evidence-backed detail is available for this business area yet.'}
        </div>
      )}

      {kpis.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '12px', marginBottom: (tables.length || charts.length) ? '16px' : 0 }}>
          {kpis.map(k => <KpiTile key={k.id} kpi={k} C={C} />)}
        </div>
      )}

      {(tables.length > 0 || charts.length > 0) && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: '14px' }}>
          {charts.map(chart => (
            <div key={chart.id} style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '12px 16px' }}>
              <div style={{ fontSize: '0.78rem', fontWeight: '700', color: C.text, marginBottom: '6px' }}>{chart.title}</div>
              <ChartSection chart={chart} C={C} />
            </div>
          ))}
          {tables.map(table => <ReportTable key={table.id} table={table} C={C} />)}
        </div>
      )}

      {statusMessage && hasContent && (
        <div style={{ marginTop: '12px', fontSize: '0.7rem', color: C.textMuted }}>{statusMessage}</div>
      )}

      {limitations.length > 0 && (
        <div style={{ marginTop: '12px', fontSize: '0.68rem', color: C.textMuted, display: 'flex', flexDirection: 'column', gap: '3px' }}>
          {limitations.map((l, i) => <div key={i}>· {l}</div>)}
        </div>
      )}
    </section>
  )
}
