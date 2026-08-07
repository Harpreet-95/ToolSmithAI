// Milestone M-32 — Frontend Conversation State.
//
// Proves the extracted state logic (frontend/src/hooks/useConversationState.js)
// independently of AIWorkspace.jsx's much larger surface, per this
// milestone's own instruction to extract the smallest pure helper/hook
// rather than testing state transitions through the monolithic component.
import { describe, it, expect } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useConversationState, buildComposerPayload } from './useConversationState'

function response(conversationState) {
  return { status: 'success', conversation_state: conversationState }
}

const STATE_A = { conversation_id: 'conv-a', source_id: 1, user_id: 'user-1', turn_number: 1 }
const STATE_B = { conversation_id: 'conv-a', source_id: 1, user_id: 'user-1', turn_number: 2 }

describe('useConversationState', () => {
  it('1. stores conversation_state from a response', () => {
    const { result } = renderHook(() => useConversationState())
    act(() => result.current.capture(response(STATE_A), 1))
    expect(result.current.conversationState).toEqual(STATE_A)
  })

  it('2. forSource returns the stored state for the next request to the same source', () => {
    const { result } = renderHook(() => useConversationState())
    act(() => result.current.capture(response(STATE_A), 1))
    expect(result.current.forSource(1)).toEqual(STATE_A)
  })

  it('3. a newer response replaces the previous state', () => {
    const { result } = renderHook(() => useConversationState())
    act(() => result.current.capture(response(STATE_A), 1))
    act(() => result.current.capture(response(STATE_B), 1))
    expect(result.current.conversationState).toEqual(STATE_B)
    expect(result.current.conversationState).not.toEqual(STATE_A)
  })

  it('replaces state with null when the response has no conversation_state (invalid/expired/non-continuable turn)', () => {
    const { result } = renderHook(() => useConversationState())
    act(() => result.current.capture(response(STATE_A), 1))
    act(() => result.current.capture(response(null), 1))
    expect(result.current.conversationState).toBeNull()
    expect(result.current.forSource(1)).toBeNull()
  })

  it('5. reset() clears state and rotates conversationId (starting a new conversation)', () => {
    const { result } = renderHook(() => useConversationState())
    const originalId = result.current.conversationId
    act(() => result.current.capture(response(STATE_A), 1))
    act(() => result.current.reset())
    expect(result.current.conversationState).toBeNull()
    expect(result.current.conversationId).not.toBe(originalId)
  })

  it('6. context is not sent for a different source, even without an explicit reset', () => {
    const { result } = renderHook(() => useConversationState())
    act(() => result.current.capture(response(STATE_A), 1))
    // Source changed to 2 without conversation.reset() having run yet.
    expect(result.current.forSource(2)).toBeNull()
    // The original source is unaffected.
    expect(result.current.forSource(1)).toEqual(STATE_A)
  })

  it('never merges — a captured response fully replaces prior state, not a shallow merge', () => {
    const { result } = renderHook(() => useConversationState())
    act(() => result.current.capture({ status: 'success', conversation_state: { a: 1, b: 2 } }, 1))
    act(() => result.current.capture({ status: 'success', conversation_state: { a: 9 } }, 1))
    expect(result.current.conversationState).toEqual({ a: 9 })
  })
})

describe('buildComposerPayload', () => {
  it('builds a fresh-question payload with no conversation_state when none is available', () => {
    const payload = buildComposerPayload({
      conversationId: 'conv-1', message: 'query revenue by status', selectedDataSourceId: 1,
    })
    expect(payload).toEqual({
      session_id: 'conv-1', message: 'query revenue by status', selected_data_source: 1,
    })
  })

  it('2. sends conversation_state on a follow-up request', () => {
    const payload = buildComposerPayload({
      conversationId: 'conv-1', message: 'What about last quarter?', selectedDataSourceId: 1,
      conversationState: STATE_A,
    })
    expect(payload.conversation_state).toEqual(STATE_A)
    expect(payload.session_id).toBe('conv-1')
  })

  it('7. a clarification resume includes both clarification_selection and conversation_state', () => {
    const selections = [{ term: 'clients', table_fqn: 'dbo.active_clients', column_name: null }]
    const payload = buildComposerPayload({
      conversationId: 'conv-1', message: 'how many clients', selectedDataSourceId: 1,
      clarificationSelection: selections, conversationState: STATE_A,
    })
    expect(payload.clarification_selection).toEqual(selections)
    expect(payload.conversation_state).toEqual(STATE_A)
    expect(payload.cancel_clarification).toBeUndefined()
  })

  it('8. a clarification cancel includes both cancel_clarification and conversation_state', () => {
    const payload = buildComposerPayload({
      conversationId: 'conv-1', message: 'how many clients', selectedDataSourceId: 1,
      cancelClarification: true, conversationState: STATE_A,
    })
    expect(payload.cancel_clarification).toBe(true)
    expect(payload.conversation_state).toEqual(STATE_A)
    expect(payload.clarification_selection).toBeUndefined()
  })

  it('never sends conversation_state when null is passed (different source, or none captured yet)', () => {
    const payload = buildComposerPayload({
      conversationId: 'conv-1', message: 'a brand new question', selectedDataSourceId: 2,
      conversationState: null,
    })
    expect(payload).not.toHaveProperty('conversation_state')
  })
})
