// Executive Report persistence/viewing bug fix — proves the Reports detail
// view picks the right renderer purely from the persisted content shape
// (never from the report title), and degrades gracefully when neither
// shape is present.
import { describe, it, expect } from 'vitest'
import { resolveReportContentView } from './reportContent'

describe('resolveReportContentView', () => {
  it('resolves an Executive Report payload to the "executive" renderer', () => {
    const report = { title: 'Executive Report — the reference database', kpi_cards: [{ id: 'x', title: 'Total Clients', value: 65000 }] }
    const view = resolveReportContentView({ executive_report: report })
    expect(view.kind).toBe('executive')
    expect(view.report).toBe(report)
  })

  it('resolves a legacy dataset report payload (sections) to the "legacy" renderer', () => {
    const sections = [{ type: 'text', heading: 'Overview', items: ['hello'] }]
    const view = resolveReportContentView({ sections, report_plan: { steps: [] } })
    expect(view.kind).toBe('legacy')
    expect(view.sections).toBe(sections)
    expect(view.reportPlan).toEqual({ steps: [] })
  })

  it('does not require report_plan for legacy reports', () => {
    const sections = [{ type: 'text', heading: 'Overview', items: ['hello'] }]
    const view = resolveReportContentView({ sections })
    expect(view.kind).toBe('legacy')
    expect(view.reportPlan).toBeNull()
  })

  it('falls back to "empty" for missing content', () => {
    expect(resolveReportContentView(undefined).kind).toBe('empty')
    expect(resolveReportContentView(null).kind).toBe('empty')
  })

  it('falls back to "empty" for content with neither shape (legacy report with no sections yet)', () => {
    expect(resolveReportContentView({}).kind).toBe('empty')
    expect(resolveReportContentView({ sections: [] }).kind).toBe('empty')
  })

  it('prefers the executive shape even if a title happens to say "dataset report"', () => {
    // Type must come from the persisted payload shape, never from title text.
    const report = { title: 'My dataset report', kpi_cards: [] }
    const view = resolveReportContentView({ executive_report: report })
    expect(view.kind).toBe('executive')
  })
})
