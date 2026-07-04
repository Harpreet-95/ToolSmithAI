import { useEffect, useRef, useState } from 'react'
import { useProfilingJob } from '../context/ProfilingJobContext'

const FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"
const STORAGE_KEY = 'toolsmith.profilingJobCenter.position'

// ─── Style injection ────────────────────────────────────────────────────────────
const STYLE_ID = 'profiling-job-center-keyframes'
function ensureStyles() {
  if (document.getElementById(STYLE_ID)) return
  const el = document.createElement('style')
  el.id = STYLE_ID
  el.textContent = `
    @keyframes pjcRunningGlow {
      0%, 100% {
        box-shadow:
          0 0 0 1.5px rgba(99,102,241,0.60),
          0 0 22px rgba(99,102,241,0.58),
          0 0 52px rgba(99,102,241,0.30),
          0 18px 52px rgba(0,0,0,0.72);
      }
      50% {
        box-shadow:
          0 0 0 1.5px rgba(99,102,241,0.92),
          0 0 38px rgba(99,102,241,0.78),
          0 0 80px rgba(99,102,241,0.44),
          0 18px 52px rgba(0,0,0,0.72);
      }
    }
    @keyframes pjcSuccessGlow {
      0%, 100% {
        box-shadow:
          0 0 0 1.5px rgba(16,185,129,0.55),
          0 0 20px rgba(16,185,129,0.48),
          0 0 48px rgba(16,185,129,0.24),
          0 14px 44px rgba(0,0,0,0.68);
      }
      50% {
        box-shadow:
          0 0 0 1.5px rgba(16,185,129,0.82),
          0 0 34px rgba(16,185,129,0.65),
          0 0 68px rgba(16,185,129,0.36),
          0 14px 44px rgba(0,0,0,0.68);
      }
    }
    @keyframes pjcDangerGlow {
      0%, 100% {
        box-shadow:
          0 0 0 1.5px rgba(248,113,113,0.55),
          0 0 20px rgba(248,113,113,0.48),
          0 0 48px rgba(248,113,113,0.24),
          0 14px 44px rgba(0,0,0,0.68);
      }
      50% {
        box-shadow:
          0 0 0 1.5px rgba(248,113,113,0.82),
          0 0 34px rgba(248,113,113,0.65),
          0 0 68px rgba(248,113,113,0.36),
          0 14px 44px rgba(0,0,0,0.68);
      }
    }
    @keyframes pjcBarShimmer {
      0%   { background-position: -200% center; }
      100% { background-position:  200% center; }
    }
    @keyframes pjcFadeIn {
      from { opacity: 0; transform: scale(0.96); }
      to   { opacity: 1; transform: scale(1); }
    }
    @keyframes pjcPulse {
      0%, 100% { opacity: 1;    transform: scale(1);    }
      50%       { opacity: 0.45; transform: scale(0.75); }
    }
  `
  document.head.appendChild(el)
}

// ─── Position helpers ────────────────────────────────────────────────────────────
function getDefaultPosition() {
  return {
    x: Math.max(8, window.innerWidth  - 424),
    y: Math.max(8, window.innerHeight - 220),
  }
}
function loadPosition() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw) {
      const p = JSON.parse(raw)
      if (typeof p.x === 'number' && typeof p.y === 'number') return p
    }
  } catch {}
  return null
}
function savePosition(pos) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(pos)) } catch {}
}

// ─── Sub-components ──────────────────────────────────────────────────────────────
function fmtElapsed(startedAt) {
  if (!startedAt) return null
  const s = Math.round((Date.now() - startedAt) / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  return `${m}m ${s % 60}s`
}

function ProgressBar({ pct, accent }) {
  return (
    <div style={{ height: '5px', borderRadius: '3px', background: 'rgba(255,255,255,0.07)', overflow: 'hidden', margin: '4px 0 2px' }}>
      <div style={{
        height: '100%',
        width: `${pct}%`,
        borderRadius: '3px',
        background: `linear-gradient(90deg, ${accent}99, ${accent}ff, ${accent}99)`,
        backgroundSize: '200% 100%',
        animation: pct > 0 && pct < 100 ? 'pjcBarShimmer 1.5s linear infinite' : 'none',
        transition: 'width 0.4s ease',
        boxShadow: `0 0 10px ${accent}cc, 0 0 3px ${accent}`,
      }} />
    </div>
  )
}

function PulsingDot({ color }) {
  return (
    <span style={{
      display:    'inline-block',
      width:      '8px',
      height:     '8px',
      borderRadius: '50%',
      background: color,
      boxShadow:  `0 0 7px ${color}`,
      animation:  'pjcPulse 1.3s ease-in-out infinite',
      flexShrink: 0,
    }} />
  )
}

// ─── Main component ──────────────────────────────────────────────────────────────
export default function ProfilingJobCenter({ C, onNavigate, onSetTab }) {
  useEffect(ensureStyles, [])

  const {
    jobs, lastCompleted, jobCenterOpen, setJobCenterOpen,
    activeJobId, cancelJob, dismissJob,
  } = useProfilingJob()

  // Elapsed timer — re-renders every second while running
  const [, setTick] = useState(0)
  useEffect(() => {
    const job = activeJobId ? jobs[activeJobId] : null
    if (!job?.loading) return
    const t = setInterval(() => setTick(n => n + 1), 1000)
    return () => clearInterval(t)
  }, [activeJobId, jobs])

  // ── Drag state ────────────────────────────────────────────────────────────────
  const [position, setPosition] = useState(() => loadPosition() ?? getDefaultPosition())
  const positionRef = useRef(position)
  const isDragging  = useRef(false)
  const dragOffset  = useRef({ x: 0, y: 0 })
  const cardRef     = useRef(null)
  const [dragging, setDragging] = useState(false)

  // Global mouse handlers — attached once, use refs to avoid stale closures
  useEffect(() => {
    function onMouseMove(e) {
      if (!isDragging.current) return
      const W     = window.innerWidth
      const H     = window.innerHeight
      const cardW = cardRef.current?.offsetWidth  ?? 400
      const cardH = cardRef.current?.offsetHeight ?? 220
      const newPos = {
        x: Math.max(8, Math.min(W - cardW - 8, e.clientX - dragOffset.current.x)),
        y: Math.max(8, Math.min(H - cardH - 8, e.clientY - dragOffset.current.y)),
      }
      positionRef.current = newPos
      setPosition(newPos)
    }
    function onMouseUp() {
      if (!isDragging.current) return
      isDragging.current = false
      setDragging(false)
      savePosition(positionRef.current)
    }
    document.addEventListener('mousemove', onMouseMove)
    document.addEventListener('mouseup',   onMouseUp)
    return () => {
      document.removeEventListener('mousemove', onMouseMove)
      document.removeEventListener('mouseup',   onMouseUp)
    }
  }, [])

  function handleDragStart(e) {
    if (e.button !== 0) return
    isDragging.current = true
    setDragging(true)
    dragOffset.current = {
      x: e.clientX - positionRef.current.x,
      y: e.clientY - positionRef.current.y,
    }
    e.preventDefault()
  }

  function resetPosition() {
    const def = getDefaultPosition()
    positionRef.current = def
    setPosition(def)
    savePosition(def)
  }

  // Prevents buttons inside the drag handle from starting a drag
  const stopDrag = e => e.stopPropagation()

  if (!activeJobId) return null

  const job = jobs[activeJobId] ?? {}
  const lc  = lastCompleted[activeJobId]

  const isRunning  = !!job.loading
  const isComplete = !isRunning && lc?.ok === true
  const isFailed   = !isRunning && (lc?.ok === false || (!lc && !!job.error))

  if (!isRunning && !isComplete && !isFailed) return null

  // ── Theme ─────────────────────────────────────────────────────────────────────
  const accent  = C?.accent    ?? '#6366f1'
  const success = C?.success   ?? '#10b981'
  const danger  = C?.danger    ?? '#f87171'
  const surface = C?.surface   ?? '#0d1128'
  const border  = C?.border    ?? '#1e2b52'
  const text    = C?.text      ?? '#eef0ff'
  const textSec = C?.textSec   ?? '#dde1ff'
  const muted   = C?.textMuted ?? '#7880a8'

  const statusColor = isRunning ? accent  : isComplete ? success : danger
  const borderColor = isRunning ? `${accent}72`  : isComplete ? `${success}60` : `${danger}60`
  const glowAnim    = isRunning
    ? 'pjcRunningGlow 2.0s ease-in-out infinite'
    : isComplete
      ? 'pjcSuccessGlow 2.8s ease-in-out infinite'
      : 'pjcDangerGlow 2.8s ease-in-out infinite'

  const modeLabel  = (isRunning ? job.profileMode : lc?.mode) === 'STRUCTURAL_ONLY' ? 'Quick' : 'Deep'
  const sourceName = isRunning
    ? (job.sourceName ?? `Source #${activeJobId}`)
    : (lc?.sourceName ?? `Source #${activeJobId}`)

  const pct     = isRunning && job.total > 0 ? Math.round((job.progress / job.total) * 100) : 0
  const elapsed = isRunning ? fmtElapsed(job.startedAt) : null

  // ── Container ─────────────────────────────────────────────────────────────────
  const containerStyle = {
    position:   'fixed',
    left:       `${position.x}px`,
    top:        `${position.y}px`,
    zIndex:     1500,
    width:      '400px',
    maxWidth:   'calc(100vw - 16px)',
    borderRadius: '14px',
    background: surface,
    border:     `1.5px solid ${borderColor}`,
    animation:  `pjcFadeIn 0.22s ease-out, ${glowAnim}`,
    fontFamily: FONT,
    overflow:   'hidden',
    userSelect: dragging ? 'none' : 'auto',
  }

  // Shared drag-handle style applied to the header / collapsed row
  const dragHandleCursor = { cursor: dragging ? 'grabbing' : 'grab' }

  // ── Collapsed view ─────────────────────────────────────────────────────────────
  if (!jobCenterOpen) {
    return (
      <div ref={cardRef} style={containerStyle}>
        <div
          onMouseDown={handleDragStart}
          style={{
            display:    'flex',
            alignItems: 'center',
            gap:        '8px',
            padding:    '0 12px',
            height:     '48px',
            ...dragHandleCursor,
          }}
        >
          {isRunning   && <PulsingDot color={statusColor} />}
          {isComplete  && <span style={{ color: success, fontSize: '0.85rem', lineHeight: 1 }}>✓</span>}
          {isFailed    && <span style={{ color: danger,  fontSize: '0.85rem', lineHeight: 1 }}>✗</span>}

          <span style={{
            padding:       '2px 8px',
            borderRadius:  '8px',
            fontSize:      '0.6rem',
            fontWeight:    '700',
            letterSpacing: '0.07em',
            textTransform: 'uppercase',
            background:    `${statusColor}22`,
            color:         statusColor,
            border:        `1px solid ${statusColor}50`,
            flexShrink:    0,
          }}>
            {isRunning ? 'Running' : isComplete ? 'Completed' : 'Failed'}
          </span>

          <span style={{ fontSize: '0.78rem', color: textSec, fontWeight: '500', flexShrink: 0, maxWidth: '110px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {modeLabel} Profile
          </span>

          <span style={{ fontSize: '0.72rem', color: muted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1, minWidth: 0 }}>
            {sourceName}
          </span>

          {isRunning && job.total > 0 && (
            <span style={{ fontSize: '0.72rem', color: accent, fontWeight: '600', flexShrink: 0 }}>{pct}%</span>
          )}

          {isRunning && job.total > 0 && (
            <div style={{ width: '44px', height: '3px', borderRadius: '2px', background: `${border}80`, flexShrink: 0, overflow: 'hidden' }}>
              <div style={{ height: '100%', width: `${pct}%`, background: accent, borderRadius: '2px', transition: 'width 0.4s ease', boxShadow: `0 0 6px ${accent}` }} />
            </div>
          )}

          {isRunning && (
            <button
              onMouseDown={stopDrag}
              onClick={() => cancelJob(activeJobId)}
              title="Cancel profiling"
              style={{ background: 'none', border: `1px solid ${danger}45`, borderRadius: '5px', color: danger, fontSize: '0.62rem', fontWeight: '700', padding: '2px 7px', cursor: 'pointer', fontFamily: FONT, flexShrink: 0 }}
            >
              Cancel
            </button>
          )}

          {!isRunning && (
            <button
              onMouseDown={stopDrag}
              onClick={() => dismissJob(activeJobId)}
              title="Dismiss"
              style={{ background: 'none', border: 'none', color: muted, fontSize: '0.75rem', padding: '2px 4px', cursor: 'pointer', flexShrink: 0 }}
            >
              ✕
            </button>
          )}

          <button
            onMouseDown={stopDrag}
            onClick={() => setJobCenterOpen(true)}
            title="Expand"
            style={{ background: 'none', border: 'none', color: muted, fontSize: '0.72rem', padding: '2px 4px', cursor: 'pointer', flexShrink: 0 }}
          >
            ▸
          </button>
        </div>

        {isRunning && job.total > 0 && (
          <div style={{ height: '2px', background: `${border}60` }}>
            <div style={{ height: '100%', width: `${pct}%`, background: `linear-gradient(90deg, ${accent}88, ${accent})`, transition: 'width 0.4s ease', boxShadow: `0 0 7px ${accent}` }} />
          </div>
        )}
      </div>
    )
  }

  // ── Expanded view ──────────────────────────────────────────────────────────────
  return (
    <div ref={cardRef} style={containerStyle}>

      {/* Header — drag handle */}
      <div
        onMouseDown={handleDragStart}
        style={{
          display:      'flex',
          alignItems:   'center',
          gap:          '8px',
          padding:      '10px 14px 8px',
          background:   `${statusColor}0b`,
          borderBottom: `1px solid ${borderColor}`,
          ...dragHandleCursor,
        }}
      >
        {isRunning  && <PulsingDot color={statusColor} />}
        {isComplete && <span style={{ color: success, fontSize: '0.9rem', lineHeight: 1, flexShrink: 0 }}>✓</span>}
        {isFailed   && <span style={{ color: danger,  fontSize: '0.9rem', lineHeight: 1, flexShrink: 0 }}>✗</span>}

        <span style={{
          padding:       '2px 8px',
          borderRadius:  '8px',
          fontSize:      '0.59rem',
          fontWeight:    '700',
          letterSpacing: '0.08em',
          textTransform: 'uppercase',
          background:    `${statusColor}22`,
          color:         statusColor,
          border:        `1px solid ${statusColor}50`,
          flexShrink:    0,
        }}>
          {isRunning ? 'Running' : isComplete ? 'Completed' : 'Failed'}
        </span>

        <span style={{ fontSize: '0.82rem', color: text, fontWeight: '600', flexShrink: 0 }}>
          {modeLabel} Profile
        </span>

        <div style={{ flex: 1, minWidth: 0 }} />

        {/* Reset position to bottom-right */}
        <button
          onMouseDown={stopDrag}
          onClick={resetPosition}
          title="Reset position to bottom-right"
          style={{ background: 'none', border: 'none', color: muted, cursor: 'pointer', padding: '2px 5px', fontSize: '0.8rem', lineHeight: 1, opacity: 0.55 }}
        >
          ↘
        </button>

        {isRunning && (
          <button
            onMouseDown={stopDrag}
            onClick={() => cancelJob(activeJobId)}
            style={{
              padding:      '3px 12px',
              borderRadius: '6px',
              background:   'transparent',
              border:       `1px solid ${danger}55`,
              color:        danger,
              fontSize:     '0.71rem',
              fontWeight:   '600',
              cursor:       'pointer',
              fontFamily:   FONT,
              flexShrink:   0,
            }}
          >
            Cancel
          </button>
        )}

        <button
          onMouseDown={stopDrag}
          onClick={() => setJobCenterOpen(false)}
          title="Collapse"
          style={{ background: 'none', border: 'none', color: muted, cursor: 'pointer', padding: '2px 5px', fontSize: '0.72rem', lineHeight: 1 }}
        >
          ▾
        </button>

        {!isRunning && (
          <button
            onMouseDown={stopDrag}
            onClick={() => dismissJob(activeJobId)}
            title="Dismiss"
            style={{ background: 'none', border: 'none', color: muted, cursor: 'pointer', padding: '2px 5px', fontSize: '0.72rem', lineHeight: 1 }}
          >
            ✕
          </button>
        )}
      </div>

      {/* Body */}
      <div style={{ padding: '12px 14px', display: 'flex', flexDirection: 'column', gap: '8px' }}>

        {/* Running state */}
        {isRunning && (
          <>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
              <span style={{ fontSize: '0.8rem', color: textSec, fontWeight: '500' }}>{sourceName}</span>
              {elapsed && <span style={{ fontSize: '0.69rem', color: muted }}>· {elapsed} elapsed</span>}
            </div>

            {job.total > 0 && (
              <>
                <ProgressBar pct={pct} accent={accent} />

                <div style={{ display: 'flex', alignItems: 'center', gap: '14px', flexWrap: 'wrap' }}>
                  <span style={{ fontSize: '0.76rem', color: accent, fontWeight: '700' }}>{pct}%</span>
                  <span style={{ fontSize: '0.72rem', color: textSec }}>{job.progress} / {job.total} tables</span>
                </div>

                {(job.statistical > 0 || job.structural > 0) && (
                  <div style={{ display: 'flex', gap: '12px', flexWrap: 'wrap' }}>
                    {job.statistical > 0 && (
                      <span style={{ fontSize: '0.68rem', color: muted }}>
                        Statistical: <span style={{ color: textSec, fontWeight: '600' }}>{job.statistical}</span>
                      </span>
                    )}
                    {job.structural > 0 && (
                      <span style={{ fontSize: '0.68rem', color: muted }}>
                        Structural: <span style={{ color: textSec, fontWeight: '600' }}>{job.structural}</span>
                      </span>
                    )}
                  </div>
                )}
              </>
            )}

            {job.recovered && (
              <span style={{ fontSize: '0.68rem', color: accent, fontStyle: 'italic' }}>
                Resumed from previous session.
              </span>
            )}

            <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginTop: '2px', paddingTop: '6px', borderTop: `1px solid ${border}30` }}>
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke={muted} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
                <circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>
              </svg>
              <span style={{ fontSize: '0.67rem', color: muted }}>
                You can continue using ToolSmithAI — profiling runs in the background.
              </span>
            </div>
          </>
        )}

        {/* Completed state */}
        {isComplete && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
              <span style={{ fontSize: '0.8rem', color: textSec, fontWeight: '500' }}>{sourceName}</span>
              {lc.completedAt && (
                <span style={{ fontSize: '0.68rem', color: muted }}>
                  · {new Date(lc.completedAt).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}
                </span>
              )}
            </div>

            <div style={{ display: 'flex', gap: '16px', flexWrap: 'wrap' }}>
              {lc.duration != null && (
                <div>
                  <div style={{ fontSize: '0.57rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', marginBottom: '2px' }}>Duration</div>
                  <div style={{ fontSize: '0.86rem', fontWeight: '700', color: text }}>
                    {lc.duration >= 60 ? `${Math.floor(lc.duration / 60)}m ${lc.duration % 60}s` : `${lc.duration}s`}
                  </div>
                </div>
              )}
              {lc.tables > 0 && (
                <div>
                  <div style={{ fontSize: '0.57rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', marginBottom: '2px' }}>Tables</div>
                  <div style={{ fontSize: '0.86rem', fontWeight: '700', color: text }}>{lc.tables}</div>
                </div>
              )}
              {lc.statistical > 0 && (
                <div>
                  <div style={{ fontSize: '0.57rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', marginBottom: '2px' }}>Statistical</div>
                  <div style={{ fontSize: '0.86rem', fontWeight: '700', color: text }}>{lc.statistical}</div>
                </div>
              )}
              {lc.structural > 0 && (
                <div>
                  <div style={{ fontSize: '0.57rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', marginBottom: '2px' }}>Structural</div>
                  <div style={{ fontSize: '0.86rem', fontWeight: '700', color: text }}>{lc.structural}</div>
                </div>
              )}
              {lc.reviewCount != null && (
                <div>
                  <div style={{ fontSize: '0.57rem', color: muted, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', marginBottom: '2px' }}>Review Tasks</div>
                  <div style={{ fontSize: '0.86rem', fontWeight: '700', color: lc.reviewCount > 0 ? accent : text }}>{lc.reviewCount}</div>
                </div>
              )}
            </div>

            <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', paddingTop: '4px', borderTop: `1px solid ${border}30` }}>
              <button
                onMouseDown={stopDrag}
                onClick={() => {
                  if (onNavigate) onNavigate('data-sources')
                  if (onSetTab)  onSetTab('profile')
                }}
                style={{ padding: '5px 14px', borderRadius: '7px', background: `${accent}18`, border: `1px solid ${accent}55`, color: accent, fontSize: '0.74rem', fontWeight: '600', cursor: 'pointer', fontFamily: FONT }}
              >
                View Results
              </button>
              {lc.reviewCount > 0 && (
                <button
                  onMouseDown={stopDrag}
                  onClick={() => {
                    if (onNavigate) onNavigate('data-sources')
                    if (onSetTab)  onSetTab('governance')
                  }}
                  style={{ padding: '5px 14px', borderRadius: '7px', background: 'transparent', border: `1px solid ${border}`, color: textSec, fontSize: '0.74rem', fontWeight: '500', cursor: 'pointer', fontFamily: FONT }}
                >
                  Review Tasks
                </button>
              )}
              <button
                onMouseDown={stopDrag}
                onClick={() => dismissJob(activeJobId)}
                style={{ marginLeft: 'auto', padding: '5px 12px', borderRadius: '7px', background: 'transparent', border: `1px solid ${border}`, color: muted, fontSize: '0.72rem', cursor: 'pointer', fontFamily: FONT }}
              >
                Dismiss
              </button>
            </div>
          </div>
        )}

        {/* Failed state */}
        {isFailed && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            <p style={{ margin: 0, fontSize: '0.76rem', color: danger, lineHeight: 1.45 }}>
              {lc?.error ?? job.error ?? 'An unknown error occurred.'}
            </p>
            <button
              onMouseDown={stopDrag}
              onClick={() => dismissJob(activeJobId)}
              style={{ alignSelf: 'flex-start', padding: '4px 12px', borderRadius: '6px', background: 'transparent', border: `1px solid ${border}`, color: muted, fontSize: '0.72rem', cursor: 'pointer', fontFamily: FONT }}
            >
              Dismiss
            </button>
          </div>
        )}

      </div>
    </div>
  )
}
