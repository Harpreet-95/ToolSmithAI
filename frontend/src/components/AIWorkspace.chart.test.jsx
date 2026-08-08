// Day 4, Capability 3 — Automatic Charts.
//
// Proves EnterpriseAnswerBlock renders the existing ChartSection component
// when enterprise_answer.chart is present, and renders nothing extra when
// it is absent — reached via ComposerResultPanel, the same
// render-the-exported-component convention as AIWorkspace.insight.test.jsx.
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ComposerResultPanel } from './AIWorkspace'

const C = {
  bg: '#f8f8fb', surface: '#ffffff', border: '#e2e2ec',
  text: '#111118', textSec: '#5c5c72', textMuted: '#9898b0',
}

function baseResult(overrides = {}) {
  return {
    resolved_intent: { intent_type: 'sql_request', confidence: 0.9, keywords_matched: [] },
    services_selected: ['live_query'],
    evidence_summary: {},
    evidence_package: { evidence: [] },
    status: 'success',
    warnings: [],
    errors: [],
    enterprise_answer: {
      answer: 'Students started each year, shown below.', summary: '3 years.',
      answer_type: 'live_query', confidence: 88, clarification: null,
      ...overrides,
    },
  }
}

function renderPanel(answerOverrides = {}) {
  return render(
    <ComposerResultPanel
      result={baseResult(answerOverrides)} wsInput="how many students started each year" C={C}
      onBack={vi.fn()} onOpenReport={vi.fn()}
      onResolveClarification={vi.fn()} onCancelClarification={vi.fn()}
      clarificationResubmitted={false}
    />
  )
}

describe('EnterpriseAnswerBlock — automatic chart', () => {
  it('renders a line chart when chart.chart_type is "line"', () => {
    renderPanel({
      chart: { chart_type: 'line', labels: ['2021', '2022', '2023'], series: [{ name: 'Students', data: [100, 140, 180] }] },
    })
    // LineChart renders an svg with an aria-label matching the series name.
    expect(screen.getByLabelText('Students')).toBeInTheDocument()
  })

  it('renders a donut chart when chart.chart_type is "donut"', () => {
    renderPanel({
      chart: {
        chart_type: 'donut',
        labels: ['Active', 'Stalled', 'Graduated', 'Not Started'],
        series: [{ name: 'Participants', data: [40, 5, 60, 12] }],
      },
    })
    expect(screen.getByText('Active')).toBeInTheDocument()
    expect(screen.getByText('Graduated')).toBeInTheDocument()
  })

  it('renders a horizontal bar chart when chart.chart_type is "bar_horizontal"', () => {
    renderPanel({
      chart: {
        chart_type: 'bar_horizontal',
        labels: ['CS101', 'Data Science 101'],
        series: [{ name: 'Students', data: [90, 75] }],
      },
    })
    expect(screen.getByText('CS101')).toBeInTheDocument()
    expect(screen.getByText('Data Science 101')).toBeInTheDocument()
  })

  it('renders nothing extra when chart is absent (e.g. a scalar answer)', () => {
    renderPanel({})
    expect(screen.queryByText('No chart data available.')).not.toBeInTheDocument()
  })

  it('renders nothing extra when chart is null', () => {
    renderPanel({ chart: null })
    expect(screen.queryByText('No chart data available.')).not.toBeInTheDocument()
  })

  it('still renders the result table alongside the chart, never replacing it', () => {
    renderPanel({
      chart: { chart_type: 'line', labels: ['2021', '2022'], series: [{ name: 'Students', data: [100, 140] }] },
      result_preview: [{ Year: 2021, Students: 100 }, { Year: 2022, Students: 140 }],
    })
    // The chart's own trend-summary text ("Start"/"End") proves the chart rendered.
    expect(screen.getByText('Start')).toBeInTheDocument()
    // The table's column headers prove the table is still rendered, not replaced.
    expect(screen.getByRole('columnheader', { name: 'Year' })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'Students' })).toBeInTheDocument()
  })
})
