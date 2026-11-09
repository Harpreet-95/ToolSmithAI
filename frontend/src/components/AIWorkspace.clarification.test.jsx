import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ClarificationCard, ComposerResultPanel, RememberConflictPanel } from './AIWorkspace'

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

// Accuracy Program A4, Fix #3 — additive option fields (object_type/
// business_name/domain/entity/why_relevant) exercised by
// tests/test_answering.py's Fix #3 suite. CLIENTS_OPTIONS/REVENUE_OPTIONS
// above intentionally keep the pre-Fix-#3 shape to prove backward
// compatibility with older responses.
const PLACEMENTS_OPTIONS_WITH_METADATA = [
  {
    id: 'opt_5', term: 'placements', table_fqn: 'dbo.EDU_StaffingRecords', column_name: null,
    label: 'Placements', description: "A business table matching 'placements'.", score: 0.61,
    object_type: 'VIEW', business_name: 'Placements', domain: 'Operations', entity: 'Placement',
    why_relevant: [
      'Business name matches the requested term.',
      'Assigned to the Operations domain.',
      'Connected to Candidates and Clients tables.',
      'A fourth reason the backend should never send, but the UI must still cap at three.',
    ],
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
    // Phase 2, Step 7b: onResolve's second argument is the "remember this
    // choice" checkbox state — unchecked (false) by default here.
    expect(onResolve).toHaveBeenCalledWith(
      [{ term: 'clients', table_fqn: 'dbo.active_clients', column_name: null }],
      false,
    )
    const [sentSelections] = onResolve.mock.calls[0]
    expect(sentSelections[0]).not.toHaveProperty('id')
  })

  it('passes remember=true only when the "remember this choice" checkbox is checked', async () => {
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
    await user.click(screen.getByLabelText('Active Clients'))
    await user.click(screen.getByText(/remember this choice/i))
    await user.click(screen.getByRole('button', { name: /use this option/i }))

    expect(onResolve).toHaveBeenCalledWith(
      [{ term: 'clients', table_fqn: 'dbo.active_clients', column_name: null }],
      true,
    )
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

  it('offers a free-text answer box (not just "try a different question") when there are no options', async () => {
    const user = userEvent.setup()
    const onBack = vi.fn()
    const onFreeTextResolve = vi.fn()
    render(
      <ClarificationCard
        clarification={clarification([])}
        wsInput="how many clients"
        C={C}
        onBack={onBack} onResolve={vi.fn()} onCancel={vi.fn()} onFreeTextResolve={onFreeTextResolve}
      />
    )
    expect(screen.queryByRole('radiogroup')).not.toBeInTheDocument()

    const textbox = screen.getByPlaceholderText(/type your answer/i)
    const submit = screen.getByRole('button', { name: /send answer/i })
    expect(submit).toBeDisabled()

    await user.type(textbox, 'IPLAN installment payments')
    expect(submit).toBeEnabled()

    await user.click(submit)
    expect(onFreeTextResolve).toHaveBeenCalledWith('IPLAN installment payments')

    // "Ask a different question" is still available, but only as one of
    // several paths, never the sole way to respond.
    expect(screen.getByRole('button', { name: /ask a different question/i })).toBeInTheDocument()
  })

  it('lets the user cancel the clarification from the free-text empty state', async () => {
    const user = userEvent.setup()
    const onCancel = vi.fn()
    render(
      <ClarificationCard
        clarification={clarification([])}
        wsInput="how many clients"
        C={C}
        onBack={vi.fn()} onResolve={vi.fn()} onCancel={onCancel} onFreeTextResolve={vi.fn()}
      />
    )
    await user.click(screen.getByRole('button', { name: /cancel clarification/i }))
    expect(onCancel).toHaveBeenCalled()
  })

  // -----------------------------------------------------------------
  // Accuracy Program A4, Fix #3 — additive object_type/domain/entity/
  // why_relevant rendering.
  // -----------------------------------------------------------------

  it('renders object_type/domain/entity and why_relevant bullets when present', () => {
    render(
      <ClarificationCard
        clarification={clarification(PLACEMENTS_OPTIONS_WITH_METADATA)}
        wsInput="show placements"
        C={C}
        onBack={vi.fn()} onResolve={vi.fn()} onCancel={vi.fn()}
      />
    )
    expect(screen.getByText('VIEW')).toBeInTheDocument()
    expect(screen.getByText('Operations')).toBeInTheDocument()
    expect(screen.getByText('Placement')).toBeInTheDocument()
    expect(screen.getByText(/Business name matches the requested term\./)).toBeInTheDocument()
    expect(screen.getByText(/Assigned to the Operations domain\./)).toBeInTheDocument()
    expect(screen.getByText(/Connected to Candidates and Clients tables\./)).toBeInTheDocument()
  })

  it('remains backward-compatible when object_type/domain/entity/why_relevant are absent', () => {
    render(
      <ClarificationCard
        clarification={clarification(CLIENTS_OPTIONS)}
        wsInput="how many clients"
        C={C}
        onBack={vi.fn()} onResolve={vi.fn()} onCancel={vi.fn()}
      />
    )
    expect(screen.getByText('Active Clients')).toBeInTheDocument()
    // No metadata badges/bullets render for options that don't carry the new fields.
    expect(screen.queryByText('VIEW')).not.toBeInTheDocument()
    expect(screen.queryByText('TABLE')).not.toBeInTheDocument()
  })

  it('renders at most 3 why_relevant bullets even if the backend sends more', () => {
    render(
      <ClarificationCard
        clarification={clarification(PLACEMENTS_OPTIONS_WITH_METADATA)}
        wsInput="show placements"
        C={C}
        onBack={vi.fn()} onResolve={vi.fn()} onCancel={vi.fn()}
      />
    )
    expect(screen.queryByText(/A fourth reason/)).not.toBeInTheDocument()
  })

  it('never shows score/confidence in the new metadata section, only behind technical details', async () => {
    const user = userEvent.setup()
    render(
      <ClarificationCard
        // Override the default reason text, which itself happens to contain
        // the word "confidence" — irrelevant to this assertion.
        clarification={clarification(PLACEMENTS_OPTIONS_WITH_METADATA, { reason: 'Multiple placement tables match your question.' })}
        wsInput="show placements"
        C={C}
        onBack={vi.fn()} onResolve={vi.fn()} onCancel={vi.fn()}
      />
    )
    expect(screen.queryByText(/confidence/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/0\.61/)).not.toBeInTheDocument()
    await user.click(screen.getByText('Show technical details'))
    expect(screen.getByText(/confidence/i)).toBeInTheDocument()
  })

  it('keeps table_fqn behind the technical-details toggle even with metadata present', () => {
    render(
      <ClarificationCard
        clarification={clarification(PLACEMENTS_OPTIONS_WITH_METADATA)}
        wsInput="show placements"
        C={C}
        onBack={vi.fn()} onResolve={vi.fn()} onCancel={vi.fn()}
      />
    )
    expect(screen.queryByText(/dbo\.EDU_StaffingRecords/)).not.toBeInTheDocument()
  })

  it('still resolves {term, table_fqn, column_name} only — new metadata fields are never sent back', async () => {
    const user = userEvent.setup()
    const onResolve = vi.fn()
    render(
      <ClarificationCard
        clarification={clarification(PLACEMENTS_OPTIONS_WITH_METADATA)}
        wsInput="show placements"
        C={C}
        onBack={vi.fn()} onResolve={onResolve} onCancel={vi.fn()}
      />
    )
    await user.click(screen.getByLabelText('Placements'))
    await user.click(screen.getByRole('button', { name: /use this option/i }))
    expect(onResolve).toHaveBeenCalledWith(
      [{ term: 'placements', table_fqn: 'dbo.EDU_StaffingRecords', column_name: null }],
      false,
    )
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

  it('enriches the resolved selection with the chosen option\'s label before calling onResolveClarification', async () => {
    // Phase 3, Step 3 — ClarificationCard itself keeps sending plain
    // {term, table_fqn, column_name} (pinned by the describe block above);
    // ComposerResultPanel attaches `label` (the canonical term taught to
    // remember_synonym) right before forwarding to the real handler.
    const user = userEvent.setup()
    const onResolveClarification = vi.fn()
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
        onResolveClarification={onResolveClarification} onCancelClarification={vi.fn()}
        clarificationResubmitted={false}
      />
    )
    await user.click(screen.getByLabelText('Active Clients'))
    await user.click(screen.getByRole('button', { name: /use this option/i }))

    expect(onResolveClarification).toHaveBeenCalledWith(
      [{ term: 'clients', table_fqn: 'dbo.active_clients', column_name: null, label: 'Active Clients' }],
      false,
    )
  })
})

describe('RememberConflictPanel', () => {
  function conflict(overrides = {}) {
    return {
      sourceId: 1, term: 'clients', tableFqn: 'dbo.Organizations', columnName: null,
      existingCanonicalTerm: 'customer', requestedCanonicalTerm: 'organization',
      ...overrides,
    }
  }

  it('renders nothing when there is no conflict', () => {
    const { container } = render(
      <RememberConflictPanel conflict={null} busy={false} onKeep={vi.fn()} onReplace={vi.fn()} C={C} />
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('shows conversational wording with the existing and requested terms, not governance jargon', () => {
    render(
      <RememberConflictPanel conflict={conflict()} busy={false} onKeep={vi.fn()} onReplace={vi.fn()} C={C} />
    )
    expect(screen.getByText(/previously taught me/i)).toBeInTheDocument()
    expect(screen.getByText(/"clients"/)).toBeInTheDocument()
    expect(screen.getByText(/"customer"/)).toBeInTheDocument()
    expect(screen.getByText(/"organization"/)).toBeInTheDocument()
    expect(screen.queryByText(/HUMAN_APPROVED/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/governance/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/confidence/i)).not.toBeInTheDocument()
  })

  it('"Keep existing meaning" calls onKeep and never onReplace', async () => {
    const user = userEvent.setup()
    const onKeep = vi.fn()
    const onReplace = vi.fn()
    render(
      <RememberConflictPanel conflict={conflict()} busy={false} onKeep={onKeep} onReplace={onReplace} C={C} />
    )
    await user.click(screen.getByRole('button', { name: /keep existing meaning/i }))
    expect(onKeep).toHaveBeenCalledTimes(1)
    expect(onReplace).not.toHaveBeenCalled()
  })

  it('"Replace remembered meaning" calls onReplace and never onKeep', async () => {
    const user = userEvent.setup()
    const onKeep = vi.fn()
    const onReplace = vi.fn()
    render(
      <RememberConflictPanel conflict={conflict()} busy={false} onKeep={onKeep} onReplace={onReplace} C={C} />
    )
    await user.click(screen.getByRole('button', { name: /replace remembered meaning/i }))
    expect(onReplace).toHaveBeenCalledTimes(1)
    expect(onKeep).not.toHaveBeenCalled()
  })

  it('disables both actions and shows a busy label while replacing', () => {
    render(
      <RememberConflictPanel conflict={conflict()} busy={true} onKeep={vi.fn()} onReplace={vi.fn()} C={C} />
    )
    expect(screen.getByRole('button', { name: /keep existing meaning/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: /replacing/i })).toBeDisabled()
  })
})
