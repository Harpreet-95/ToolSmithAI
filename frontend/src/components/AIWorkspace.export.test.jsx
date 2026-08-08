// Day 4, Capability 5 — Export.
//
// Proves ExportDropdown's visibility gating (never renders for a refused/
// clarification-required/unsuccessful answer) and its click-through to
// exportComposerAnswer — reached via ComposerResultPanel, the same
// render-the-exported-component convention as the other AIWorkspace.*
// capability test files. api/client's exportComposerAnswer is mocked so
// this stays a component-level test, not an end-to-end network/blob test —
// the client function's own request-shape/blob-download behavior belongs
// to a client.js-level test instead (mirroring
// frontend/src/api/client.conversation-state.test.js's own scope split).
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { ComposerResultPanel } from './AIWorkspace'

vi.mock('../api/client', () => ({
  exportComposerAnswer: vi.fn(),
}))
import { exportComposerAnswer } from '../api/client'

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
    agent_status: 'answered',
    enterprise_answer: {
      answer: 'There are 10,918 students in the database.', summary: '10,918 students.',
      answer_type: 'live_query', confidence: 88, clarification: null,
    },
    ...overrides,
  }
}

function renderPanel(resultOverrides = {}, props = {}) {
  return render(
    <ComposerResultPanel
      result={baseResult(resultOverrides)} wsInput="how many students" C={C}
      onBack={vi.fn()} onOpenReport={vi.fn()}
      onResolveClarification={vi.fn()} onCancelClarification={vi.fn()}
      clarificationResubmitted={false}
      token="tok-123" onSessionExpired={vi.fn()}
      {...props}
    />
  )
}

describe('ExportDropdown — visibility gating', () => {
  it('renders the Export button for a successfully answered agent-routed answer', () => {
    renderPanel({ agent_status: 'answered' })
    expect(screen.getByLabelText('Export')).toBeInTheDocument()
  })

  it('renders the Export button for a legacy (non-agent) successful answer with no agent_status', () => {
    renderPanel({ agent_status: undefined })
    expect(screen.getByLabelText('Export')).toBeInTheDocument()
  })

  it('never renders Export for a safely-refused answer', () => {
    renderPanel({ agent_status: 'safely_refused' })
    expect(screen.queryByLabelText('Export')).not.toBeInTheDocument()
  })

  it('never renders Export for a governance-blocked answer', () => {
    renderPanel({ agent_status: 'governance_blocked' })
    expect(screen.queryByLabelText('Export')).not.toBeInTheDocument()
  })

  it('never renders Export for an execution-failed answer', () => {
    renderPanel({ agent_status: 'execution_failed' })
    expect(screen.queryByLabelText('Export')).not.toBeInTheDocument()
  })

  it('never renders Export when there is no enterprise_answer at all', () => {
    renderPanel({ enterprise_answer: null })
    expect(screen.queryByLabelText('Export')).not.toBeInTheDocument()
  })
})

describe('ExportDropdown — interaction', () => {
  it('opens a menu with Excel, CSV, and PDF options when clicked', () => {
    renderPanel()
    fireEvent.click(screen.getByLabelText('Export'))
    expect(screen.getByText('Excel (.xlsx)')).toBeInTheDocument()
    expect(screen.getByText('CSV')).toBeInTheDocument()
    expect(screen.getByText('PDF')).toBeInTheDocument()
  })

  it('calls exportComposerAnswer with the question, enterprise_answer, agent_status, and chosen format', async () => {
    exportComposerAnswer.mockResolvedValueOnce(undefined)
    const result = baseResult()
    renderPanel()
    fireEvent.click(screen.getByLabelText('Export'))
    fireEvent.click(screen.getByText('Excel (.xlsx)'))

    await waitFor(() => expect(exportComposerAnswer).toHaveBeenCalledTimes(1))
    expect(exportComposerAnswer).toHaveBeenCalledWith('tok-123', {
      question: 'how many students',
      enterpriseAnswer: result.enterprise_answer,
      agentStatus: 'answered',
      format: 'xlsx',
    })
  })

  it('calls onSessionExpired on a 401 error instead of showing an inline error', async () => {
    const onSessionExpired = vi.fn()
    exportComposerAnswer.mockRejectedValueOnce(new Error('401: token expired'))
    renderPanel({}, { onSessionExpired })
    fireEvent.click(screen.getByLabelText('Export'))
    fireEvent.click(screen.getByText('CSV'))

    await waitFor(() => expect(onSessionExpired).toHaveBeenCalledTimes(1))
    expect(screen.queryByText(/token expired/)).not.toBeInTheDocument()
  })

  it('shows an inline error message on a non-401 failure', async () => {
    exportComposerAnswer.mockRejectedValueOnce(new Error('500: export failed'))
    renderPanel()
    fireEvent.click(screen.getByLabelText('Export'))
    fireEvent.click(screen.getByText('PDF'))

    await waitFor(() => expect(screen.getByText('export failed')).toBeInTheDocument())
  })
})
