// Milestone 7 — Executive Report V3 UI / Management Dashboard, Part 22 test
// coverage. Exercises ExecutiveReportContent against the real payload shape
// (see frontend/src/fixtures/executiveReportFixtures.js) and
// ExecutiveReportView's generation flow with a mocked API.
import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ExecutiveReportContent } from './ExecutiveReportView'
import ExecutiveReportView from './ExecutiveReportView'
import { referenceExecutiveReportFixture, genericExecutiveReportFixture } from '../fixtures/executiveReportFixtures'

const C = {
  bg: '#f8f8fb', surface: '#ffffff', card: '#ffffff', border: '#e2e2ec', borderAlt: '#eeeef4',
  text: '#111118', textSec: '#5c5c72', textMuted: '#9898b0', accent: '#6366f1',
}

describe('ExecutiveReportContent — the reference database fixture (Milestone 7 Part 18)', () => {
  const report = referenceExecutiveReportFixture()

  it('renders KPI cards from the report data (test 1) with a real production database values', () => {
    render(<ExecutiveReportContent report={report} C={C} />)
    expect(screen.getByText('Total Students')).toBeInTheDocument()
    expect(screen.getByText('12,000')).toBeInTheDocument()
    expect(screen.getByText('Total Programs')).toBeInTheDocument()
    expect(screen.getByText('40')).toBeInTheDocument()
    expect(screen.getByText('Total Amount')).toBeInTheDocument()
    expect(screen.getByText('$2.00M')).toBeInTheDocument()
  })

  it('values are not hardcoded — changing the fixture value changes the render (test 2)', () => {
    const modified = referenceExecutiveReportFixture()
    modified.kpi_cards[0].value = 55555
    render(<ExecutiveReportContent report={modified} C={C} />)
    expect(screen.getByText('55,555')).toBeInTheDocument()
    expect(screen.queryByText('12,000')).not.toBeInTheDocument()
  })

  it('renders Student content when supported (test 3)', () => {
    render(<ExecutiveReportContent report={report} C={C} />)
    expect(screen.getByText('Students & Programs')).toBeInTheDocument()
    expect(screen.getByText('Students by Certificate Name')).toBeInTheDocument()
  })

  it('renders Program content when supported (test 4)', () => {
    render(<ExecutiveReportContent report={report} C={C} />)
    expect(screen.getAllByText(/Program/).length).toBeGreaterThan(0)
    expect(screen.getByText('Enrollment Trend')).toBeInTheDocument()
  })

  it('renders the Executive Summary panel verbatim (test 7)', () => {
    render(<ExecutiveReportContent report={report} C={C} />)
    expect(screen.getByText(/the reference database served 12,000 students across 40 programs/)).toBeInTheDocument()
  })

  it('renders a Key Trend with the real backend comparison values (test 8)', () => {
    render(<ExecutiveReportContent report={report} C={C} />)
    expect(screen.getByText('Key Trends')).toBeInTheDocument()
    expect(screen.getByText('170')).toBeInTheDocument()
    expect(screen.getAllByText('10').length).toBeGreaterThan(0)
    expect(screen.getAllByText(/-94%/).length).toBeGreaterThan(0)
  })

  it('renders a management insight (test 9)', () => {
    render(<ExecutiveReportContent report={report} C={C} />)
    expect(screen.getByText('Active User Engagement Declined Sharply')).toBeInTheDocument()
  })

  it('renders a recommendation (test 10)', () => {
    render(<ExecutiveReportContent report={report} C={C} />)
    expect(screen.getByText(/Investigate the drop in active user engagement/)).toBeInTheDocument()
  })

  it('renders the data quality overview, kept secondary (test 11)', () => {
    render(<ExecutiveReportContent report={report} C={C} />)
    expect(screen.getByText('Data Quality & Coverage')).toBeInTheDocument()
    expect(screen.getByText(/usable analytical evidence/)).toBeInTheDocument()
  })

  it('keeps technical diagnostics collapsed by default (test 14)', async () => {
    const withDiag = referenceExecutiveReportFixture()
    withDiag.technical_diagnostics = [{ task_id: 't1', question: 'q', domain: 'Payments', agent_status: 'safely_refused', stop_code: 'sql_generation_refused', raw_message: 'internal trace', limitation_class: 'SYSTEM_CAPABILITY_LIMITATION' }]
    render(<ExecutiveReportContent report={withDiag} C={C} />)
    expect(screen.getByText('Technical Details')).toBeInTheDocument()
    expect(screen.queryByText(/internal trace/)).not.toBeInTheDocument()
    await userEvent.click(screen.getByText('Technical Details'))
    expect(screen.getByText(/internal trace/)).toBeInTheDocument()
  })

  it('never leaks technical/internal language into the management view (test 15)', () => {
    const { container } = render(<ExecutiveReportContent report={report} C={C} />)
    const text = container.textContent.toLowerCase()
    for (const banned of ['stop_code', 'agentstatus', 'safely_refused', 'dbo.', 'sql generation', 'query execution', 'semantic adjudication', 'metadata lifecycle']) {
      expect(text).not.toContain(banned)
    }
  })

  it('does not hardcode the reference database production values in the rendered dashboard beyond what the fixture supplied', () => {
    // Confirms the KPI tiles come from the report prop, not a component-
    // level constant: removing the values from the fixture removes the KPI
    // tiles that displayed them.
    const empty = referenceExecutiveReportFixture()
    empty.kpi_cards = []
    empty.student_program_overview = null
    empty.financial_performance = null
    empty.business_areas = []
    render(<ExecutiveReportContent report={empty} C={C} />)
    expect(screen.queryByText('Total Students')).not.toBeInTheDocument()
    expect(screen.queryByText('Total Programs')).not.toBeInTheDocument()
    expect(screen.queryByText('Business Snapshot')).not.toBeInTheDocument()
  })
})

describe('ExecutiveReportContent — generic non-the reference database fixture (Milestone 7 Part 17)', () => {
  const report = genericExecutiveReportFixture()

  it('renders generic entities (Customers) with the same component, no the reference database wording (test 5/23)', () => {
    const { container } = render(<ExecutiveReportContent report={report} C={C} />)
    expect(screen.getByText('Customers')).toBeInTheDocument()
    expect(screen.getByText('Total Customers')).toBeInTheDocument()
    expect(screen.getByText('Total Products')).toBeInTheDocument()
    const text = container.textContent
    expect(text).not.toMatch(/\bStudent\b/)
    expect(text).not.toMatch(/\bProgram\b/)
    expect(text).not.toMatch(/the reference database/)
  })

  it('prefers the business-area title over any physical table name (test 6)', () => {
    render(<ExecutiveReportContent report={report} C={C} />)
    expect(screen.getByText('Customers by Region')).toBeInTheDocument()
  })

  it('does not fabricate charts when a section has none, and does not fabricate recommendations text (test 12/13)', () => {
    const noExtras = genericExecutiveReportFixture()
    noExtras.recommendations = []
    noExtras.business_areas[0].charts = []
    render(<ExecutiveReportContent report={noExtras} C={C} />)
    expect(screen.queryByText('Recommended Actions')).not.toBeInTheDocument()
  })
})

// Tuesday demo live-validation finding: a live the reference database run returned KPI cards
// titled literally "EDU_Student"/"EDU_Track" (core.analysis.report_builder
// falls back to the raw physical entity name when no cleaner business label
// resolved upstream) — both in report.kpi_cards (Business Snapshot, no
// formatted_value at all) and in student_program_overview.kpis (which does
// carry formatted_value). Reproduces that exact live shape.
describe('ExecutiveReportContent — humanizes raw physical-identifier KPI titles (live-validation fix)', () => {
  function liveShapedReport() {
    return {
      version: 3, title: 'Business Analysis — the reference database SQL Server', source_id: 1,
      kpi_cards: [
        { id: 'sp1', title: 'EDU_Student', value: 12000, unit: null, previous_value: null, absolute_change: null, percent_change: null, direction: null, comparison_label: null, warning: null },
        { id: 'sp2', title: 'EDU_Track', value: 40, unit: null, previous_value: null, absolute_change: null, percent_change: null, direction: null, comparison_label: null, warning: null },
        { id: 't6', title: 'Amount', value: 2000000.00, unit: null, previous_value: null, absolute_change: null, percent_change: null, direction: null, comparison_label: null, warning: null },
      ],
      charts: [], tables: [], narrative: null, warnings: [], data_quality_notes: [], technical_diagnostics: [],
      business_areas: [
        {
          section_id: 'area-operations', title: 'Operations', subtitle: 'Campaign, Client, Program, Student, Unknown', domain: 'Operations', status: 'SUPPORTED',
          kpis: [
            { id: 'sp1', title: 'EDU_Student', value: 12000, formatted_value: 'There are 12,000 students in the database.', unit: null, comparison: null, trend_direction: null, evidence_task_ids: ['sp1'], confidence: 0.61, limitation: null },
            { id: 'sp2', title: 'EDU_Track', value: 40, formatted_value: 'There are 40 paths in the database.', unit: null, comparison: null, trend_direction: null, evidence_task_ids: ['sp2'], confidence: 0.55, limitation: null },
          ],
          charts: [], tables: [], findings: [], insights: [], recommendations: [], limitations: [], evidence_refs: [],
        },
      ],
      student_program_overview: {
        section_id: 'area-operations', title: 'Operations', subtitle: 'Campaign, Client, Program, Student, Unknown', domain: 'Operations', status: 'SUPPORTED',
        kpis: [
          { id: 'sp1', title: 'EDU_Student', value: 12000, formatted_value: 'There are 12,000 students in the database.', unit: null, comparison: null, trend_direction: null, evidence_task_ids: ['sp1'], confidence: 0.61, limitation: null },
          { id: 'sp2', title: 'EDU_Track', value: 40, formatted_value: 'There are 40 paths in the database.', unit: null, comparison: null, trend_direction: null, evidence_task_ids: ['sp2'], confidence: 0.55, limitation: null },
        ],
        charts: [], tables: [], findings: [], insights: [], recommendations: [], limitations: [], evidence_refs: [],
      },
      financial_performance: null, operations_engagement: null,
      key_changes: [], positive_developments: [], areas_requiring_attention: [], insights: [], recommendations: [],
      data_quality_overview: '', suggested_follow_up_questions: [], executive_summary: '', report_period_label: '',
    }
  }

  it('never shows a raw physical identifier as a KPI title, in the snapshot or the entity section', () => {
    const { container } = render(<ExecutiveReportContent report={liveShapedReport()} C={C} />)
    expect(container.textContent).not.toContain('EDU_Student')
    expect(container.textContent).not.toContain('EDU_Track')
  })

  it('humanizes the title using the KPI\'s own evidence sentence, honestly (not asserting an unverified concept)', () => {
    render(<ExecutiveReportContent report={liveShapedReport()} C={C} />)
    // Real evidence text says "students"/"paths" — never an invented "Programs".
    expect(screen.getAllByText('Students').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Paths').length).toBeGreaterThan(0)
    expect(screen.queryByText('Programs')).not.toBeInTheDocument()
  })

  it('leaves an already-clean title (no underscore) untouched', () => {
    render(<ExecutiveReportContent report={liveShapedReport()} C={C} />)
    expect(screen.getByText('Amount')).toBeInTheDocument()
  })

  it('shows the numeric value, never the full evidence sentence, as the KPI value (live-validation fix)', () => {
    const { container } = render(<ExecutiveReportContent report={liveShapedReport()} C={C} />)
    expect(screen.getAllByText('12,000').length).toBeGreaterThan(0)
    expect(screen.getAllByText('40').length).toBeGreaterThan(0)
    expect(container.textContent).not.toContain('There are 12,000 students in the database.')
    expect(container.textContent).not.toContain('There are 40 paths in the database.')
  })
})

describe('ExecutiveReportContent — empty/legacy report handling', () => {
  it('shows the empty-state message when nothing is available', () => {
    render(<ExecutiveReportContent report={{ title: 'Empty', source_id: 1, kpi_cards: [], charts: [], tables: [] }} C={C} />)
    expect(screen.getByText('No KPIs could be safely planned for this database yet.')).toBeInTheDocument()
  })

  it('returns nothing for a null report instead of throwing', () => {
    const { container } = render(<ExecutiveReportContent report={null} C={C} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('still renders a legacy (pre-V3) narrative-only report', () => {
    render(<ExecutiveReportContent report={{
      title: 'Legacy Report', source_id: 1, narrative: 'Legacy narrative text.',
      kpi_cards: [{ id: 'k1', title: 'Total Clients', value: 65000 }], charts: [], tables: [],
    }} C={C} />)
    expect(screen.getByText('Total Clients')).toBeInTheDocument()
    expect(screen.getByText('65,000')).toBeInTheDocument()
    expect(screen.getByText('Legacy narrative text.')).toBeInTheDocument()
  })
})

describe('ExecutiveReportView — generation UX (Milestone 7 Parts 11/12)', () => {
  function setup(generateExecutiveReport) {
    const listDataSources = vi.fn().mockResolvedValue({ data: [{ id: 1, display_name: 'EXAMPLECO' }] })
    render(
      <ExecutiveReportView
        C={C} token="tok" listDataSources={listDataSources}
        generateExecutiveReport={generateExecutiveReport} onBack={() => {}}
      />
    )
  }

  it('shows a staged, non-fabricated loading state and prevents duplicate submission (test 16/17)', async () => {
    let resolveFn
    const pending = new Promise(r => { resolveFn = r })
    const generateExecutiveReport = vi.fn().mockReturnValue(pending)
    setup(generateExecutiveReport)

    await waitFor(() => expect(screen.getByText('EXAMPLECO')).toBeInTheDocument())
    const btn = screen.getByText('Generate Report')
    await userEvent.click(btn)
    await userEvent.click(btn)
    await userEvent.click(btn)

    expect(generateExecutiveReport).toHaveBeenCalledTimes(1)
    expect(screen.getByText(/Building Executive Report/)).toBeInTheDocument()
    expect(screen.queryByText(/%\s*complete/i)).not.toBeInTheDocument()
    expect(screen.getByText('Executive analysis may take a few minutes. Every number is validated against live, governed data as it is computed.')).toBeInTheDocument()

    resolveFn({ data: referenceExecutiveReportFixture() })
    await waitFor(() => expect(screen.getByText('Total Students')).toBeInTheDocument())
  })

  it('renders the API error cleanly and restores the button on failure (test 18)', async () => {
    const generateExecutiveReport = vi.fn().mockRejectedValue(new Error('Executive report generation failed.'))
    setup(generateExecutiveReport)
    await waitFor(() => expect(screen.getByText('EXAMPLECO')).toBeInTheDocument())
    await userEvent.click(screen.getByText('Generate Report'))
    await waitFor(() => expect(screen.getByText('Executive report generation failed.')).toBeInTheDocument())
    expect(screen.getByText('Generate Report')).not.toBeDisabled()
  })

  it('renders the completed report without a page refresh, using the existing report API only (test 19/20/21)', async () => {
    const generateExecutiveReport = vi.fn().mockResolvedValue({ data: referenceExecutiveReportFixture() })
    setup(generateExecutiveReport)
    await waitFor(() => expect(screen.getByText('EXAMPLECO')).toBeInTheDocument())
    await userEvent.click(screen.getByText('Generate Report'))
    await waitFor(() => expect(screen.getByText('Total Students')).toBeInTheDocument())
    expect(generateExecutiveReport).toHaveBeenCalledWith('1', 'tok')
    expect(generateExecutiveReport).toHaveBeenCalledTimes(1)
  })
})
