// Day 4, Capability 3 — Automatic Charts.
//
// Proves EnterpriseAnswerBlock renders the existing ChartSection component
// when enterprise_answer.chart is present, and renders nothing extra when
// it is absent — reached via ComposerResultPanel, the same
// render-the-exported-component convention as AIWorkspace.insight.test.jsx.
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ComposerResultPanel } from './AIWorkspace'
import ChartSection from './ChartSection'

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
    // Each full label appears twice: once as the bar row's own label, once
    // in the accessible "View data" table fallback (requirement: complete
    // names + exact values available outside the bar's visual rendering).
    expect(screen.getAllByText('CS101').length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText('Data Science 101').length).toBeGreaterThanOrEqual(1)
    // Each row is keyboard-focusable and exposes the full label + exact value.
    expect(screen.getByRole('listitem', { name: 'CS101: 90' })).toBeInTheDocument()
    expect(screen.getByRole('listitem', { name: 'Data Science 101: 75' })).toBeInTheDocument()
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

describe('ChartSection — dense time-series label thinning', () => {
  // Phase 7, Step 6G1 -- a long monthly trend (e.g. 172 months of real
  // the reference database enrollment data) previously rendered one rotated <text> label per
  // point and collapsed into an unreadable smear. Every data point/circle
  // must still render (no data loss); only the TEXT label density thins.
  function monthlyChart(n) {
    const labels = Array.from({ length: n }, (_, i) => `2012-${String((i % 12) + 1).padStart(2, '0')}`)
    const data = Array.from({ length: n }, (_, i) => i + 1)
    return { chart_type: 'line', labels, series: [{ name: 'Enrollment Total', data }] }
  }

  it('renders far fewer text labels than data points for a 172-point line chart', () => {
    const { container } = render(<ChartSection chart={monthlyChart(172)} C={C} />)
    const texts = container.querySelectorAll('svg text')
    const circles = container.querySelectorAll('svg circle')
    expect(circles.length).toBe(172)
    expect(texts.length).toBeGreaterThan(0)
    expect(texts.length).toBeLessThan(30)
  })

  it('always keeps the last label visible so the chart end date is never cut off', () => {
    const chart = monthlyChart(172)
    const { container } = render(<ChartSection chart={chart} C={C} />)
    const texts = Array.from(container.querySelectorAll('svg text')).map(t => t.textContent)
    expect(texts.some(t => chart.labels[chart.labels.length - 1].startsWith(t.replace('…', '')))).toBe(true)
  })

  it('renders every label for a short series (no thinning below the threshold)', () => {
    const { container } = render(<ChartSection chart={monthlyChart(10)} C={C} />)
    expect(container.querySelectorAll('svg text').length).toBeGreaterThanOrEqual(10)
  })
})

describe('ChartSection — grouped_line (date + multiple measures)', () => {
  it('renders one line series per measure without crashing or losing points', () => {
    const chart = {
      chart_type: 'grouped_line',
      labels: ['2026-01', '2026-02', '2026-03'],
      series: [
        { name: 'Payment Count', data: [279, 268, 253] },
        { name: 'Total Amount Paid', data: [4969, 7573, 13542] },
      ],
    }
    const { container } = render(<ChartSection chart={chart} C={C} />)
    expect(screen.getByText('Payment Count (left axis)')).toBeInTheDocument()
    expect(screen.getByText('Total Amount Paid (right axis)')).toBeInTheDocument()
    // One circle per data point per series (3 points x 2 series).
    expect(container.querySelectorAll('svg circle').length).toBe(6)
  })

  it('gives two series of very different magnitude their own labelled axes (no flat line on zero)', () => {
    const chart = {
      chart_type: 'grouped_line',
      labels: ['2026-01', '2026-02', '2026-03'],
      series: [
        { name: 'Payment Count', data: [250, 260, 255] },
        { name: 'Total Amount Paid', data: [4000, 46000, 13000] },
      ],
    }
    const { container } = render(<ChartSection chart={chart} C={C} />)
    expect(screen.getByText('Payment Count (left axis)')).toBeInTheDocument()
    expect(screen.getByText('Total Amount Paid (right axis)')).toBeInTheDocument()
    // The small series must span a real vertical range, not sit on the baseline.
    const countCircles = Array.from(container.querySelectorAll('svg circle')).slice(0, 3)
    const ys = countCircles.map(c => Number(c.getAttribute('cy')))
    const baseY = 20 + 160
    expect(Math.min(...ys)).toBeLessThan(baseY - 20)
    // Distinct colours per series.
    const fills = new Set(Array.from(container.querySelectorAll('svg circle')).map(c => c.getAttribute('fill')))
    expect(fills.size).toBe(2)
  })

  it('keeps one shared axis when the two series are of comparable magnitude', () => {
    const chart = {
      chart_type: 'grouped_line',
      labels: ['a', 'b', 'c'],
      series: [
        { name: 'Revenue', data: [100, 120, 90] },
        { name: 'Cost', data: [60, 70, 65] },
      ],
    }
    render(<ChartSection chart={chart} C={C} />)
    expect(screen.getByText('Revenue')).toBeInTheDocument()
    expect(screen.queryByText(/axis\)/)).not.toBeInTheDocument()
  })
})
