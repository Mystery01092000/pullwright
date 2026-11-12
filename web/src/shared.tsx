// Small pieces shared by the request views: toasts, the growing input, the status orb, link shortening.
import { useEffect, useRef, useState } from 'react'
import { ThinkingOrb, type OrbState } from 'thinking-orbs'

// ---------- toasts ----------

type Toast = { id: number; text: string; tone: 'ok' | 'err' | 'info' }
let pushToast: (t: Omit<Toast, 'id'>) => void = () => {}
export const toast = (text: string, tone: Toast['tone'] = 'info') => pushToast({ text, tone })

export function Toasts() {
  const [items, setItems] = useState<Toast[]>([])
  useEffect(() => {
    let n = 0
    pushToast = (t) => {
      const id = ++n
      setItems((xs) => [...xs, { ...t, id }])
      setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== id)), 5000)
    }
  }, [])
  return (
    <div className="pointer-events-none fixed right-4 bottom-4 z-50 flex max-w-sm flex-col gap-2" role="status" aria-live="polite">
      {items.map((t) => (
        <div key={t.id} className="msg-in pointer-events-auto flex items-center gap-2.5 rounded-xl bg-card px-4 py-2.5 text-sm shadow-card">
          <span className="size-1.5 shrink-0 rounded-full" style={{ background: t.tone === 'ok' ? 'var(--ok)' : t.tone === 'err' ? 'var(--err)' : 'var(--muted)' }} aria-hidden />
          {t.text}
        </div>
      ))}
    </div>
  )
}

// ---------- layout helpers ----------

export function useMedia(query: string) {
  const [match, setMatch] = useState(() => window.matchMedia(query).matches)
  useEffect(() => {
    const m = window.matchMedia(query)
    const on = () => setMatch(m.matches)
    m.addEventListener('change', on)
    return () => m.removeEventListener('change', on)
  }, [query])
  return match
}

/** A textarea that grows with its text (up to 40% of the screen); Enter sends, Shift+Enter adds a line. */
export function GrowingInput({ value, onChange, onSend, placeholder, minRows = 2, label, className }: {
  value: string; onChange: (v: string) => void; onSend: () => void; placeholder: string; minRows?: number; label: string; className?: string
}) {
  const ref = useRef<HTMLTextAreaElement>(null)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, window.innerHeight * 0.4)}px`
  }, [value])
  return (
    <textarea ref={ref} className={className ?? 'w-full resize-none bg-transparent px-4 pt-3.5 pb-1 text-[14px] leading-[22px] outline-none placeholder:text-[var(--faint)]'}
      rows={minRows} placeholder={placeholder} aria-label={label} value={value} onChange={(e) => onChange(e.target.value)}
      onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); onSend() } }} />
  )
}

// ---------- the status orb (thinking-orbs) ----------

/** What Pullwright is doing, as an orb state. */
export const ORB_FOR: Record<string, OrbState> = {
  describing: 'searching', // reading the repository and drafting the plan
  answering: 'composing', // writing a reply
  verifying: 'solving', // checking the plan against the code
  awaiting_approval: 'breathing', // waiting for you
  queued: 'connecting', // waiting for a runner
  setup: 'connecting',
  working: 'working', // the coder is changing code
  checking: 'weaving', // the project's checks run
  reviewing: 'solving', // independent review
}

/** The orb, or a still Pullwright mark when nothing is happening. Reduced motion pauses it. */
export function Orb({ state, size = 20 }: { state?: OrbState | null; size?: 20 | 32 | 64 }) {
  const reduced = useMedia('(prefers-reduced-motion: reduce)')
  if (!state) return <Mark size={size} />
  return (
    <span className="inline-grid shrink-0 place-items-center" style={{ width: size, height: size }} aria-hidden>
      <ThinkingOrb state={state} size={size} paused={reduced} aria-label="" />
    </span>
  )
}

export function Mark({ size = 20 }: { size?: number }) {
  return (
    <span aria-hidden className="inline-grid shrink-0 place-items-center rounded-[6px]" style={{ width: size, height: size, background: 'var(--primary)' }}>
      <span className="block rounded-[2px]" style={{ width: Math.round(size * 0.34), height: Math.round(size * 0.34), background: 'var(--on-primary)' }} />
    </span>
  )
}

// ---------- text helpers ----------

/** GitHub URLs in bot text read as "PR #2", "issue #1", "comment on PR #2" links. */
export function shortLinks(text: string): string {
  return text.replace(/(?<!\]\()https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/(pull|issues)\/(\d+)(#issuecomment-\d+)?(?=[\s.,;:)]|$)/g,
    (url, kind: string, n: string, comment?: string) => `[${comment ? `comment on PR #${n}` : kind === 'pull' ? `PR #${n}` : `issue #${n}`}](${url})`)
}

export const clock = (ts: number) => new Date(ts * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
