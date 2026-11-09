// Phase 7, Step 6G — grounded result insights (plural) + single-scalar KPI
// card for the AI-first fast path.
//
// Proves EnterpriseAnswerBlock renders enterprise_answer.kpi (reusing the
// existing KpiCard component) and enterprise_answer.insights (plural, the
// grounded AnswerInsight list the backend already sends but which, before
// this change, had no renderer at all) — reached via ComposerResultPanel,
// the same render-the-exported-component convention as
// AIWorkspace.chart.test.jsx / AIWorkspace.insight.test.jsx.
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
      answer: 'There are 214 students.', summary: '',
      answer_type: 'live_query', confidence: 88, clarification: null,
      ...overrides,
    },
  }
}

function renderPanel(answerOverrides = {}) {
  return render(
    <ComposerResultPanel
      result={baseResult(answerOverrides)} wsInput="how many students are there" C={C}
      onBack={vi.fn()} onOpenReport={vi.fn()}
      onResolveClarification={vi.fn()} onCancelClarification={vi.fn()}
      clarificationResubmitted={false}
    />
  )
}

describe('EnterpriseAnswerBlock — single-scalar KPI card', () => {
  it('renders a KpiCard when kpi is present', () => {
    renderPanel({ kpi: { label: 'Student Count', value: 214 } })
    // KpiCard uppercases its label via CSS text-transform, not DOM text content.
    expect(screen.getByText('Student Count')).toBeInTheDocument()
    expect(screen.getByText('214')).toBeInTheDocument()
  })

  it('renders nothing extra when kpi is absent', () => {
    renderPanel({})
    expect(screen.queryByText('Student Count')).not.toBeInTheDocument()
  })

  it('renders nothing extra when kpi is null', () => {
    renderPanel({ kpi: null })
    expect(screen.queryByText('Student Count')).not.toBeInTheDocument()
  })
})

describe('EnterpriseAnswerBlock — grounded result insights (plural)', () => {
  it('renders every grounded insight title and finding', () => {
    renderPanel({
      insights: [
        { title: 'Highest and lowest', finding: 'Data Analytics has the highest student count (120).' },
        { title: 'Gap between top two', finding: 'Data Analytics leads UX Design by 40 students.' },
      ],
    })
    expect(screen.getByText('Highest and lowest')).toBeInTheDocument()
    expect(screen.getByText('Data Analytics has the highest student count (120).')).toBeInTheDocument()
    expect(screen.getByText('Gap between top two')).toBeInTheDocument()
  })

  it('states the insights are based on the returned rows', () => {
    renderPanel({
      insights: [{ title: 'Highest and lowest', finding: 'A leads.' }],
    })
    expect(screen.getByText('Based on the returned result rows.')).toBeInTheDocument()
  })

  it('renders nothing extra when insights is an empty list', () => {
    renderPanel({ insights: [] })
    expect(screen.queryByText('Based on the returned result rows.')).not.toBeInTheDocument()
  })

  it('renders nothing extra when insights is absent', () => {
    renderPanel({})
    expect(screen.queryByText('Based on the returned result rows.')).not.toBeInTheDocument()
  })
})
