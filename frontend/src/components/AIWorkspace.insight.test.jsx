// Day 4, Capability 2 — Business Insights.
//
// Proves EnterpriseAnswerBlock's period-comparison insight chip (reached via
// ComposerResultPanel, the same render-the-exported-component convention as
// AIWorkspace.agentTrace.test.jsx / AIWorkspace.clarification.test.jsx).
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
      answer: 'There are 70,772 clients.', summary: '70,772 clients.',
      answer_type: 'live_query', confidence: 88, clarification: null,
      ...overrides,
    },
  }
}

function renderPanel(answerOverrides = {}) {
  return render(
    <ComposerResultPanel
      result={baseResult(answerOverrides)} wsInput="how many clients last quarter" C={C}
      onBack={vi.fn()} onOpenReport={vi.fn()}
      onResolveClarification={vi.fn()} onCancelClarification={vi.fn()}
      clarificationResubmitted={false}
    />
  )
}

describe('EnterpriseAnswerBlock — period-comparison insight', () => {
  it('renders an up-trending insight with its percent change and label', () => {
    renderPanel({
      insight: { type: 'period_comparison', label: 'vs. the previous period', current_value: 70772, previous_value: 50000, percent_change: 41.5, direction: 'up' },
    })
    expect(screen.getByText('+41.5%')).toBeInTheDocument()
    expect(screen.getByText('vs. the previous period')).toBeInTheDocument()
  })

  it('renders a down-trending insight with a negative sign, no extra plus', () => {
    renderPanel({
      insight: { type: 'period_comparison', label: 'vs. the previous period', current_value: 60, previous_value: 100, percent_change: -40.0, direction: 'down' },
    })
    expect(screen.getByText('-40%')).toBeInTheDocument()
  })

  it('renders nothing for the insight chip when insight is absent', () => {
    renderPanel({})
    expect(screen.queryByText(/vs\. the previous period/)).not.toBeInTheDocument()
  })

  it('renders nothing for the insight chip when insight is null', () => {
    renderPanel({ insight: null })
    expect(screen.queryByText(/vs\. the previous period/)).not.toBeInTheDocument()
  })
})
