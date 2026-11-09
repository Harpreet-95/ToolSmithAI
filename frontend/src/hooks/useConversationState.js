import { useCallback, useState } from 'react'

// Milestone M-32 — the narrowest browser-side state needed to round-trip
// core.orchestrator.agent.ConversationContext (M-31) across composer/ask
// requests for the same data source.
//
// The backend is the sole authority on shape/content: this hook never
// merges, derives, or mutates conversation_state client-side — it only
// stores exactly what POST /v1/composer/ask last returned, and only ever
// hands it back for the SAME data source it was captured against (belt and
// suspenders — the backend independently enforces source/user isolation on
// every request regardless).
//
// conversationId is the one piece of state that did not already exist
// before this milestone: composer/ask's session_id was previously
// regenerated on every single request (see AIWorkspace.jsx's old
// handleComposerAsk), which is fine for the backend's own legacy stateless
// path but breaks conversation continuity — core.orchestrator.agent.
// ConversationContext isolation requires the SAME session_id/conversation_id
// across a question and its follow-up. This hook makes it stable across
// turns of one conversation, and rotates it only when reset() is called.

function generateConversationId() {
  return `conv_${Date.now()}_${Math.random().toString(36).slice(2, 9)}`
}

export function useConversationState() {
  const [conversationId, setConversationId] = useState(generateConversationId)
  const [conversationState, setConversationState] = useState(null)
  const [stateSourceId, setStateSourceId] = useState(null)

  // Call after every composer/ask response. Replaces (never merges) the
  // stored state with whatever the response returned — including replacing
  // it with null, which is exactly how an invalid/expired/non-continuable
  // turn (the backend simply omits conversation_state) clears itself here.
  const capture = useCallback((response, forSourceId) => {
    const next = response?.conversation_state ?? null
    setConversationState(next)
    setStateSourceId(next ? forSourceId : null)
  }, [])

  // Starts a genuinely new conversation: a fresh conversation_id and no
  // carried-over state. Call on source change, an explicit "new
  // conversation"/reset action, or an authentication/user-context change.
  const reset = useCallback(() => {
    setConversationId(generateConversationId())
    setConversationState(null)
    setStateSourceId(null)
  }, [])

  // The value to send on the NEXT request for currentSourceId — never
  // reused across a different data source, even if a caller forgets to
  // reset() on a source change.
  const forSource = useCallback(
    (currentSourceId) => (conversationState && stateSourceId === currentSourceId ? conversationState : null),
    [conversationState, stateSourceId],
  )

  return { conversationId, conversationState, capture, reset, forSource }
}

// Milestone M-32 — the exact POST /v1/composer/ask request body shape,
// extracted as a pure function so a fresh question, a follow-up, a
// clarification resume, and a clarification cancel are all provably built
// the same way (conversation_state attached whenever one is available for
// the current source) rather than needing separate, divergent logic per
// caller. AIWorkspace.jsx's handleComposerAsk is the only caller; this
// exists so that contract is directly testable without mounting the whole
// component.
export function buildComposerPayload({
  conversationId, message, selectedDataSourceId, datasetId = null,
  clarificationSelection = null, cancelClarification = false, conversationState = null,
}) {
  const payload = {
    session_id: conversationId,
    message,
    selected_data_source: selectedDataSourceId ?? null,
  }
  if (datasetId != null) payload.dataset_id = datasetId
  if (clarificationSelection) payload.clarification_selection = clarificationSelection
  if (cancelClarification) payload.cancel_clarification = true
  if (conversationState) payload.conversation_state = conversationState
  return payload
}
