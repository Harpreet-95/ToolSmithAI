// Executive Report persistence/viewing bug fix — proves ExecutiveReportContent
// renders correctly from a payload shaped exactly like what data.report_service
// persists and returns on retrieval (content.executive_report), which is the
// same shape AI Workspace already renders inline. This is the ONE renderer
// reused by both AI Workspace and the Reports detail view — no second
// Executive Report renderer is introduced.
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ExecutiveReportContent } from './ExecutiveReportView'

const C = {
  bg: '#f8f8fb', surface: '#ffffff', card: '#ffffff', border: '#e2e2ec', borderAlt: '#eeeef4',
  text: '#111118', textSec: '#5c5c72', textMuted: '#9898b0', accent: '#6366f1',
}

function savedExecutiveReport(overrides = {}) {
  return {
    title: 'Executive Report — the reference database',
    subtitle: 'Live connected database',
    narrative: 'Client growth remained steady across the reporting period.',
    kpi_cards: [
      { id: 'total_clients', title: 'Total Clients', value: 65000 },
      { id: 'total_payments', title: 'Total Payments', value: 2000000.00 },
      { id: 'avg_payment', title: 'Average Payment Amount', value: 164.42 },
      { id: 'total_events', title: 'Total Events', value: 7473 },
    ],
    charts: [
      { id: 'monthly_new_clients', title: 'Monthly New Clients', chart_type: 'bar', labels: ['Jan', 'Feb'], series: [{ name: 'New Clients', data: [10, 20] }] },
    ],
    tables: [],
    warnings: ['Report is based on data at time of generation — re-run for updated results.'],
    data_quality_notes: ['3 columns had partial null coverage.'],
    technical_diagnostics: [{ table: 'clients', issue: 'null_rate', value: 0.02 }],
    ...overrides,
  }
}

describe('ExecutiveReportContent — renders a saved/retrieved report unchanged', () => {
  it('renders the title, narrative, and every KPI card with its saved value', () => {
    render(<ExecutiveReportContent report={savedExecutiveReport()} C={C} />)

    expect(screen.getByText('Executive Report — the reference database')).toBeInTheDocument()
    expect(screen.getByText(/Client growth remained steady/)).toBeInTheDocument()

    expect(screen.getByText('Total Clients')).toBeInTheDocument()
    expect(screen.getByText('65,000')).toBeInTheDocument()

    // Milestone 7 Part 4 — currency-flavored KPI titles (e.g. containing
    // "Payment") render with the $-and-magnitude treatment ($2.00M) rather
    // than a raw number, so a management reader reads the value at a glance.
    expect(screen.getByText('Total Payments')).toBeInTheDocument()
    expect(screen.getByText('$2.00M')).toBeInTheDocument()

    expect(screen.getByText('Average Payment Amount')).toBeInTheDocument()
    expect(screen.getByText('$164.42')).toBeInTheDocument()

    expect(screen.getByText('Total Events')).toBeInTheDocument()
    expect(screen.getByText('7,473')).toBeInTheDocument()
  })

  it('renders chart titles from the saved chart list', () => {
    render(<ExecutiveReportContent report={savedExecutiveReport()} C={C} />)
    expect(screen.getByText('Monthly New Clients')).toBeInTheDocument()
  })

  it('renders saved warnings as report limitations', () => {
    render(<ExecutiveReportContent report={savedExecutiveReport()} C={C} />)
    expect(screen.getByText('Limitations')).toBeInTheDocument()
    expect(screen.getByText(/Report is based on data at time of generation/)).toBeInTheDocument()
  })

  it('shows the empty-KPI message when a saved report has no KPIs, charts, or tables', () => {
    render(<ExecutiveReportContent report={savedExecutiveReport({ kpi_cards: [], charts: [], tables: [] })} C={C} />)
    expect(screen.getByText('No KPIs could be safely planned for this database yet.')).toBeInTheDocument()
  })

  it('returns nothing for a null report (legacy/missing content) instead of throwing', () => {
    const { container } = render(<ExecutiveReportContent report={null} C={C} />)
    expect(container).toBeEmptyDOMElement()
  })
})
