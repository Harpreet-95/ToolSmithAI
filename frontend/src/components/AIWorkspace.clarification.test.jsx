import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ClarificationCard, ComposerResultPanel } from './AIWorkspace'

// Minimal theme object matching the shape consumed by these components
// (see C_LIGHT in App.jsx) — only the keys these components actually read.
const C = {
  bg: '#f8f8fb', surface: '#ffffff', border: '#e2e2ec',
  text: '#111118', textSec: '#5c5c72', textMuted: '#9898b0',
}

// Option shapes lifted verbatim from the real backend contract exercised by
// tests/test_clarification_intelligence.py's tied-client SQLite fixture
// (core.answering.explanation_builder._clarification_option).
const CLIENTS_OPTIONS = [
  {
    id: 'opt_1', term: 'clients', table_fqn: 'dbo.active_clients', column_name: null,
    label: 'Active Clients', description: 'dbo.active_clients — confidence 0.82', score: 0.82,
  },
  {
    id: 'opt_2', term: 'clients', table_fqn: 'dbo.legacy_clients', column_name: null,
    label: 'Legacy Clients', description: 'dbo.legacy_clients — confidence 0.80', score: 0.80,
  },
]

const REVENUE_OPTIONS = [
  {
    id: 'opt_3', term: 'revenue', table_fqn: 'dbo.revenue_actuals', column_name: 'amount',
    label: 'Revenue Actuals', description: 'dbo.revenue_actuals — confidence 0.75', score: 0.75,
  },
  {
    id: 'opt_4', term: 'revenue', table_fqn: 'dbo.revenue_forecast', column_name: 'amount',
    label: 'Revenue Forecast', description: 'dbo.revenue_forecast — confidence 0.70', score: 0.70,
  },
]

function clarification(options, overrides = {}) {
  return {
    reason: "I found multiple business concepts that match 'clients' with similar confidence.",
    options,
    expected_impact: 'Selecting a different option resolves the question against a different underlying table or column and may change the result.',
    ...overrides,
  }
}

describe('ClarificationCard', () => {
  it('renders the reason, the original question, and option labels without raw table_fqn', () => {
    render(
      <ClarificationCard
        clarification={clarification(CLIENTS_OPTIONS)}
        wsInput="how many clients"
        C={C}
        onBack={vi.fn()} onResolve={vi.fn()} onCancel={vi.fn()}
      />
    )
    expect(screen.getByText(/multiple business concepts/i)).toBeInTheDocument()
    expect(screen.getByText('how many clients')).toBeInTheDocument()
    expect(screen.getByText('Active Clients')).toBeInTheDocument()
    expect(screen.getByText('Legacy Clients')).toBeInTheDocument()
    expect(screen.queryByText(/dbo\.active_clients/)).not.toBeInTheDocument()
    expect(screen.queryByText(/dbo\.legacy_clients/)).not.toBeInTheDocument()
  })

  it('reveals table_fqn only after expanding technical details', async () => {
    const user = userEvent.setup()
    render(
      <ClarificationCard
        clarification={clarification(CLIENTS_OPTIONS)}
        wsInput="how many clients"
        C={C}
        onBack={vi.fn()} onResolve={vi.fn()} onCancel={vi.fn()}
      />
    )
    await user.click(screen.getAllByText('Show technical details')[0])
    expect(screen.getByText('dbo.active_clients')).toBeInTheDocument()
  })

  it('disables submit until an option is picked, then submits {term, table_fqn, column_name} — not id', async () => {
    const user = userEvent.setup()
    const onResolve = vi.fn()
    render(
      <ClarificationCard
        clarification={clarification(CLIENTS_OPTIONS)}
        wsInput="how many clients"
        C={C}
        onBack={vi.fn()} onResolve={onResolve} onCancel={vi.fn()}
      />
    )
    const submit = screen.getByRole('button', { name: /use this option/i })
    expect(submit).toBeDisabled()

    await user.click(screen.getByLabelText('Active Clients'))
    expect(submit).toBeEnabled()

    await user.click(submit)
    expect(onResolve).toHaveBeenCalledWith([
      { term: 'clients', table_fqn: 'dbo.active_clients', column_name: null },
    ])
    const [sentSelections] = onResolve.mock.calls[0]
    expect(sentSelections[0]).not.toHaveProperty('id')
  })

  it('requires one selection per ambiguous term before enabling submit', async () => {
    const user = userEvent.setup()
    const onResolve = vi.fn()
    render(
      <ClarificationCard
        clarification={clarification([...CLIENTS_OPTIONS, ...REVENUE_OPTIONS])}
        wsInput="revenue for our clients"
        C={C}
        onBack={vi.fn()} onResolve={onResolve} onCancel={vi.fn()}
      />
    )
    const submit = screen.getByRole('button', { name: /use these options/i })
    expect(submit).toBeDisabled()

    await user.click(screen.getByLabelText('Active Clients'))
    expect(submit).toBeDisabled()

    await user.click(screen.getByLabelText('Revenue Actuals'))
    expect(submit).toBeEnabled()

    await user.click(submit)
    const [sentSelections] = onResolve.mock.calls[0]
    expect(sentSelections).toEqual(
      expect.arrayContaining([
        { term: 'clients', table_fqn: 'dbo.active_clients', column_name: null },
        { term: 'revenue', table_fqn: 'dbo.revenue_actuals', column_name: 'amount' },
      ])
    )
  })

  it('shows a disabled/submitting state immediately on submit and cancel click', async () => {
    const user = userEvent.setup()
    render(
      <ClarificationCard
        clarification={clarification(CLIENTS_OPTIONS)}
        wsInput="how many clients"
        C={C}
        onBack={vi.fn()} onResolve={vi.fn()} onCancel={vi.fn()}
      />
    )
    await user.click(screen.getByLabelText('Active Clients'))
    const submit = screen.getByRole('button', { name: /use this option/i })
    await user.click(submit)
    expect(screen.getByRole('button', { name: /submitting/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: /cancel clarification/i })).toBeDisabled()
  })

  it('calls onCancel when Cancel clarification is clicked', async () => {
    const user = userEvent.setup()
    const onCancel = vi.fn()
    render(
      <ClarificationCard
        clarification={clarification(CLIENTS_OPTIONS)}
        wsInput="how many clients"
        C={C}
        onBack={vi.fn()} onResolve={vi.fn()} onCancel={onCancel}
      />
    )
    await user.click(screen.getByRole('button', { name: /cancel clarification/i }))
    expect(onCancel).toHaveBeenCalled()
  })

  it('shows a "choose again" notice only when resubmitted is true', () => {
    const { rerender } = render(
      <ClarificationCard
        clarification={clarification(CLIENTS_OPTIONS)}
        wsInput="how many clients"
        C={C}
        onBack={vi.fn()} onResolve={vi.fn()} onCancel={vi.fn()}
        resubmitted={false}
      />
    )
    expect(screen.queryByText(/couldn't be applied/i)).not.toBeInTheDocument()

    rerender(
      <ClarificationCard
        clarification={clarification(CLIENTS_OPTIONS)}
        wsInput="how many clients"
        C={C}
        onBack={vi.fn()} onResolve={vi.fn()} onCancel={vi.fn()}
        resubmitted={true}
      />
    )
    expect(screen.getByText(/couldn't be applied/i)).toBeInTheDocument()
  })

  it('renders a defensive empty state when there are no options', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    render(
      <ClarificationCard
        clarification={clarification([])}
        wsInput="how many clients"
        C={C}
        onBack={onBack} onResolve={vi.fn()} onCancel={vi.fn()}
      />
    )
    expect(screen.getByText(/couldn't find specific options/i)).toBeInTheDocument()
    expect(screen.queryByRole('radiogroup')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /try a different question/i }))
    expect(onBack).toHaveBeenCalled()
  })
})

describe('ComposerResultPanel clarification routing', () => {
  function baseResult(overrides = {}) {
    return {
      resolved_intent: { intent_type: 'live_query_answer', confidence: 0.9, keywords_matched: [] },
      services_selected: [],
      evidence_summary: {},
      evidence_package: { evidence: [] },
      status: 'success',
      warnings: [],
      errors: [],
      ...overrides,
    }
  }

  it('renders ClarificationCard, not EnterpriseAnswerBlock, for a clarification_needed answer', () => {
    const result = baseResult({
      enterprise_answer: {
        answer: 'Which clients table?', summary: 'Clarification needed.',
        answer_type: 'clarification_needed', confidence: 0,
        clarification: clarification(CLIENTS_OPTIONS),
      },
    })
    render(
      <ComposerResultPanel
        result={result} wsInput="how many clients" C={C}
        onBack={vi.fn()} onOpenReport={vi.fn()}
        onResolveClarification={vi.fn()} onCancelClarification={vi.fn()}
        clarificationResubmitted={false}
      />
    )
    expect(screen.getByText('Clarification Needed')).toBeInTheDocument()
    expect(screen.queryByText('Enterprise Answer')).not.toBeInTheDocument()
    expect(screen.queryByText('Evidence Summary')).not.toBeInTheDocument()
  })

  it('renders EnterpriseAnswerBlock with no clarification UI for a normal answer', () => {
    const result = baseResult({
      enterprise_answer: {
        answer: 'There are 42 active clients.', summary: 'Query executed successfully.',
        answer_type: 'live_query', confidence: 88, clarification: null,
      },
    })
    render(
      <ComposerResultPanel
        result={result} wsInput="how many clients" C={C}
        onBack={vi.fn()} onOpenReport={vi.fn()}
        onResolveClarification={vi.fn()} onCancelClarification={vi.fn()}
        clarificationResubmitted={false}
      />
    )
    expect(screen.getByText('Enterprise Answer')).toBeInTheDocument()
    expect(screen.queryByText('Clarification Needed')).not.toBeInTheDocument()
    expect(screen.queryByRole('radiogroup')).not.toBeInTheDocument()
  })
})
