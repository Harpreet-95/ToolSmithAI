import { Component } from 'react'

// Class component — required by React for error boundary behaviour.
// Catches render errors in any child tree and shows a contained fallback
// so the surrounding app (nav, other tabs, report list) stays mounted.
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { hasError: false }
  }

  static getDerivedStateFromError() {
    return { hasError: true }
  }

  componentDidCatch(error, info) {
    console.error('[ErrorBoundary] render error:', error, info.componentStack)
  }

  render() {
    if (!this.state.hasError) return this.props.children

    // Use theme tokens when the parent passes C; fall back to neutral values
    // so the boundary is usable without a C prop.
    const C       = this.props.C || {}
    const bg      = C.surface || '#101320'
    const border  = C.border  || '#1b1f35'
    const text    = C.text    || '#eef0f8'
    const textSec = C.textSec || '#8890a8'
    const danger  = C.danger  || '#f87171'
    const accent  = C.accent  || '#6366f1'
    const FONT    = "system-ui, -apple-system, 'Segoe UI', sans-serif"

    return (
      <div style={{ background: bg, border: `1px solid ${border}`, borderRadius: '10px', padding: '20px 22px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '9px', marginBottom: '8px' }}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke={danger} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="10"/>
            <line x1="12" y1="8" x2="12" y2="12"/>
            <line x1="12" y1="16" x2="12.01" y2="16"/>
          </svg>
          <span style={{ fontSize: '0.85rem', fontWeight: '600', color: text, fontFamily: FONT }}>
            Something went wrong in this section.
          </span>
        </div>
        <p style={{ margin: '0 0 14px', fontSize: '0.76rem', color: textSec, lineHeight: 1.6, fontFamily: FONT }}>
          A rendering error occurred. The rest of the app is unaffected. You can reset this section or refresh the page.
        </p>
        <button
          onClick={() => this.setState({ hasError: false })}
          style={{ background: 'none', border: `1px solid ${accent}`, borderRadius: '7px', padding: '5px 14px', fontSize: '0.75rem', color: accent, cursor: 'pointer', fontWeight: '600', fontFamily: FONT }}
        >
          Try again
        </button>
      </div>
    )
  }
}
