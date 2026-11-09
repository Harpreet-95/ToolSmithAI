import { useState, useEffect } from 'react'
import ChartSection from './ChartSection'
import KpiTile from './executiveReport/KpiTile'
import EntitySummarySection from './executiveReport/EntitySummarySection'
import ReportTable from './executiveReport/ReportTable'
import TrendHighlights from './executiveReport/TrendHighlights'
import InsightsPanel from './executiveReport/InsightsPanel'
import RecommendationsPanel from './executiveReport/RecommendationsPanel'
import AttentionPanel from './executiveReport/AttentionPanel'
import DataQualityPanel from './executiveReport/DataQualityPanel'
import TechnicalDetails from './executiveReport/TechnicalDetails'
import ExecutiveSummaryPanel from './executiveReport/ExecutiveSummaryPanel'
import ReportGenerationLoading from './executiveReport/ReportGenerationLoading'
import { FONT } from './executiveReport/shared'
import {
  entitySections, resolveInsights, resolveRecommendations, snapshotKpis, trendKpis,
} from '../utils/executiveReport'

const MAX_FEATURED_KPIS = 5

function fmtGeneratedAt(iso) {
  if (!iso) return null
  const d = new Date(iso)
  if (isNaN(d.getTime())) return null
  return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

// Renders the body of a generated executive report as a management
// dashboard — the report shape core.analysis.models.ExecutiveAnalysisReport.
// to_executive_report_data_dict() produces (a superset of the older
// core.reporting.report_models.ExecutiveReportData shape it was originally
// built from, so a legacy KPI-only report still renders correctly with the
// structured sections simply absent). Extracted so this exact rendering is
// shared between the standalone Executive Report page below, the AI
// Workspace composer result card (ComposerReportCard in AIWorkspace.jsx),
// and the Reports tab detail view (App.jsx) — ONE renderer, no second
// reporting system, per Milestone 7's architecture rule.
export function ExecutiveReportContent({ report, C, hideHeader = false }) {
  if (!report) return null

  const featuredSections = entitySections(report, 2)
  const snapshot = snapshotKpis(report).slice(0, MAX_FEATURED_KPIS)
  const snapshotIds = new Set(snapshot.map(k => k.id))

  const shownChartIds = new Set(featuredSections.flatMap(s => (s.charts || []).map(c => c.id)))
  const shownTableIds = new Set(featuredSections.flatMap(s => (s.tables || []).map(t => t.id)))
  const extraCharts = (report.charts || []).filter(c => !shownChartIds.has(c.id))
  const extraTables = (report.tables || []).filter(t => !shownTableIds.has(t.id))

  const trends = trendKpis(report)
  const insights = resolveInsights(report)
  const recommendations = resolveRecommendations(report)
  const generatedAt = fmtGeneratedAt(report.generated_at)

  // report.warnings and report.data_quality_notes carry identical content
  // for a real V3 report (both mirror ExecutiveAnalysisReport.
  // data_quality_notes — see to_executive_report_data_dict()); only show a
  // separate Limitations note for entries data_quality_notes doesn't
  // already cover, so the dashboard never shows the same line twice.
  const dqNoteSet = new Set(report.data_quality_notes || [])
  const extraWarnings = (report.warnings || []).filter(w => !dqNoteSet.has(w))

  // "No KPIs..." is specifically about the absence of quantifiable
  // card/chart/table/section content — independent of narrative/DQ/
  // warnings, which can legitimately still be present (e.g. a source with
  // real data-quality notes but nothing yet safely plannable as a KPI).
  const hasNothing = (
    snapshot.length === 0 && featuredSections.length === 0 &&
    extraCharts.length === 0 && extraTables.length === 0 &&
    trends.length === 0 && insights.length === 0 && recommendations.length === 0
  )

  return (
    <div>
      {!hideHeader && (
        <div style={{ marginBottom: '10px' }}>
          <div style={{ fontSize: '0.62rem', fontWeight: '800', color: C.accent, textTransform: 'uppercase', letterSpacing: '0.14em', marginBottom: '4px' }}>
            Executive Report — Management Overview
          </div>
          <h3 style={{ margin: '0 0 4px', fontSize: '1.25rem', fontWeight: '800', color: C.text, letterSpacing: '-0.4px', fontFamily: FONT }}>
            {report.title}
          </h3>
          {report.subtitle && <div style={{ fontSize: '0.78rem', color: C.textMuted, marginBottom: '4px' }}>{report.subtitle}</div>}
          <div style={{ display: 'flex', gap: '14px', flexWrap: 'wrap', fontSize: '0.7rem', color: C.textMuted }}>
            {report.report_period_label && <span>{report.report_period_label}</span>}
            {generatedAt && <span>Generated {generatedAt}</span>}
          </div>
        </div>
      )}

      <ExecutiveSummaryPanel summary={report.executive_summary} businessOverview={!report.executive_summary ? report.business_overview : null} C={C} />

      {/* Legacy fallback: a pre-V3 report (or a report whose executive_summary
          couldn't be built) still carries its content as flattened
          narrative text — render it so nothing goes blank. */}
      {!report.executive_summary && report.narrative && (
        <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '14px 16px', marginBottom: '18px', fontSize: '0.8rem', color: C.textSec, whiteSpace: 'pre-line' }}>
          {report.narrative}
        </div>
      )}

      {snapshot.length > 0 && (
        <section style={{ marginBottom: '20px' }}>
          <h3 style={{ margin: '0 0 12px', fontSize: '0.95rem', fontWeight: '800', color: C.text, fontFamily: FONT }}>Business Snapshot</h3>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '14px' }}>
            {snapshot.map(k => <KpiTile key={k.id} kpi={k} C={C} size="featured" />)}
          </div>
        </section>
      )}

      {featuredSections.map(section => (
        <EntitySummarySection key={section.section_id} section={section} C={C} hideKpiIds={snapshotIds} />
      ))}

      <TrendHighlights trends={trends} keyChanges={report.key_changes} C={C} />
      <InsightsPanel insights={insights} C={C} />
      <AttentionPanel items={report.areas_requiring_attention} C={C} />
      <RecommendationsPanel recommendations={recommendations} C={C} />

      {extraCharts.length > 0 && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(340px, 1fr))', gap: '14px', marginBottom: '20px' }}>
          {extraCharts.map(chart => (
            <div key={chart.id} style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: '10px' }}>
              <div style={{ padding: '12px 16px 0', fontSize: '0.78rem', fontWeight: '700', color: C.text }}>{chart.title}</div>
              <ChartSection chart={chart} C={C} />
            </div>
          ))}
        </div>
      )}

      {extraTables.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '14px', marginBottom: '20px' }}>
          {extraTables.map(table => <ReportTable key={table.id} table={table} C={C} />)}
        </div>
      )}

      {extraWarnings.length > 0 && (
        <div style={{ background: 'rgba(245,158,11,0.08)', border: '1px solid rgba(245,158,11,0.25)', borderRadius: '8px', padding: '12px 14px', fontSize: '0.72rem', color: '#b45309', marginBottom: '20px' }}>
          <div style={{ fontWeight: '700', marginBottom: '4px' }}>Limitations</div>
          {extraWarnings.map((w, i) => <div key={i}>• {w}</div>)}
        </div>
      )}

      <DataQualityPanel overview={report.data_quality_overview} notes={report.data_quality_notes} C={C} />

      {hasNothing && (
        <div style={{ padding: '32px', textAlign: 'center', color: C.textMuted, fontSize: '0.8rem' }}>
          No KPIs could be safely planned for this database yet.
        </div>
      )}

      <TechnicalDetails diagnostics={report.technical_diagnostics} followUpQuestions={report.suggested_follow_up_questions} C={C} />
    </div>
  )
}

export default function ExecutiveReportView({ C, token, listDataSources, generateExecutiveReport, onBack }) {
  const [sources, setSources] = useState([])
  const [sourcesLoading, setSourcesLoading] = useState(true)
  const [selectedSourceId, setSelectedSourceId] = useState('')
  const [report, setReport] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    queueMicrotask(() => {
      if (!cancelled) setSourcesLoading(true)
    })
    listDataSources(token)
      .then(res => {
        if (cancelled) return
        const list = res?.data || []
        setSources(list)
        if (list.length > 0) setSelectedSourceId(String(list[0].id))
      })
      .catch(() => { if (!cancelled) setSources([]) })
      .finally(() => { if (!cancelled) setSourcesLoading(false) })
    return () => { cancelled = true }
  }, [listDataSources, token])

  const handleGenerate = async () => {
    if (!selectedSourceId || loading) return
    setLoading(true)
    setError(null)
    setReport(null)
    try {
      const res = await generateExecutiveReport(selectedSourceId, token)
      setReport(res?.data || null)
    } catch (e) {
      setError(e?.message || 'Executive report generation failed.')
    } finally {
      setLoading(false)
    }
  }

  const selectedSourceName = sources.find(s => String(s.id) === String(selectedSourceId))?.display_name

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '16px' }}>
        <div>
          <h2 style={{ margin: '0 0 4px', fontSize: '1.4rem', fontWeight: '700', color: C.text, letterSpacing: '-0.4px' }}>Executive Report</h2>
          <p style={{ margin: 0, color: C.textMuted, fontSize: '0.75rem' }}>Generated on demand from a connected database — every number traces to a validated, governed query.</p>
        </div>
        <button onClick={onBack} style={{ background: 'none', border: `1px solid ${C.border}`, borderRadius: '7px', padding: '6px 14px', color: C.textSec, fontSize: '0.72rem', cursor: 'pointer', fontFamily: FONT }}>
          ← Back to Reports
        </button>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '18px' }}>
        <select
          value={selectedSourceId}
          onChange={e => setSelectedSourceId(e.target.value)}
          disabled={sourcesLoading || sources.length === 0 || loading}
          style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: '7px', padding: '7px 10px', color: C.text, fontSize: '0.75rem', fontFamily: FONT, minWidth: '220px' }}
        >
          {sourcesLoading && <option>Loading sources…</option>}
          {!sourcesLoading && sources.length === 0 && <option>No connected data sources</option>}
          {sources.map(s => (
            <option key={s.id} value={s.id}>{s.display_name || `Source ${s.id}`}</option>
          ))}
        </select>
        <button
          onClick={handleGenerate}
          disabled={loading || !selectedSourceId}
          style={{ background: C.accent, border: 'none', borderRadius: '7px', padding: '8px 16px', color: '#fff', fontSize: '0.75rem', fontWeight: '600', cursor: loading ? 'default' : 'pointer', fontFamily: FONT, opacity: loading ? 0.7 : 1 }}
        >
          {loading ? 'Generating…' : 'Generate Report'}
        </button>
      </div>

      {error && (
        <div style={{ padding: '14px 16px', borderRadius: '8px', background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.25)', color: '#ef4444', fontSize: '0.78rem', marginBottom: '16px' }}>
          {error}
        </div>
      )}

      {loading && <ReportGenerationLoading C={C} sourceName={selectedSourceName} />}

      {!loading && report && <ExecutiveReportContent report={report} C={C} />}
    </div>
  )
}
