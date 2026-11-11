import { useEffect, useMemo, useState } from 'react'
import { api, type RunRow } from './api'
import { Chat, NewTaskComposer } from './Chat'
import { usd } from './model'
import { LoginScreen, QueueView, RequestsMain, RequestsSidebar, useMe } from './Requests'

type Mode = 'team' | 'queue' | 'local'

type Thread = { id: string; task: string; state: string; cost: number; created_at: number; runs: RunRow[] }

const stateColor = (s: string) => (s === 'done' ? 'var(--ok)' : s === 'failed' || s === 'cancelled' ? 'var(--err)' : 'var(--warn)')

export default function App() {
  const [runs, setRuns] = useState<RunRow[]>([])
  const [sel, setSel] = useState<string | null>(null)
  const [cwd, setCwd] = useState(localStorage.getItem('cwd') ?? '')
  const [maxCost, setMaxCost] = useState(Number(localStorage.getItem('maxCost') ?? 20))
  const [sidebar, setSidebar] = useState(true)
  const [mode, setModeState] = useState<Mode>((localStorage.getItem('pw.mode') as Mode) ?? 'team')
  const [thread, setThread] = useState<string | null>(new URLSearchParams(location.search).get('thread'))
  const me = useMe()
  const setMode = (m: Mode) => { localStorage.setItem('pw.mode', m); setModeState(m) }

  const refresh = () => api.runs().then(setRuns).catch(() => {})
  useEffect(() => { refresh(); const t = setInterval(refresh, 3000); return () => clearInterval(t) }, [])

  // group runs into threads: a thread is one chat, its runs are the turns
  const threads = useMemo<Thread[]>(() => {
    const by = new Map<string, RunRow[]>()
    for (const r of runs) by.set(r.thread_id, [...(by.get(r.thread_id) ?? []), r])
    return [...by.entries()]
      .map(([id, rs]) => {
        const sorted = rs.sort((a, b) => a.created_at - b.created_at)
        return { id, task: sorted[0].task, state: sorted[sorted.length - 1].state, cost: rs.reduce((n, r) => n + r.cost_usd, 0), created_at: sorted[sorted.length - 1].created_at, runs: sorted }
      })
      .sort((a, b) => b.created_at - a.created_at)
  }, [runs])

  const start = async (task: string, kind: string, model: string, continueFrom?: string) => {
    if (!continueFrom && !cwd) { setSidebar(true); return }
    localStorage.setItem('cwd', cwd); localStorage.setItem('maxCost', String(maxCost))
    const text = kind && kind !== 'Feature' ? `[${kind}] ${task}` : task
    const { id } = await api.start({ task: text, cwd: continueFrom ? undefined : cwd, main_model: model, max_cost_usd: maxCost, continue_from: continueFrom })
    if (!continueFrom) setSel(id)
    refresh()
  }
  const current = threads.find((t) => t.id === sel)
  const field = 'mono r-sm mt-1 w-full px-2.5 py-1.5 text-xs outline-none'
  const fieldStyle = { background: 'rgba(0,0,0,.35)', boxShadow: 'inset 0 0 0 1px var(--line)', color: 'var(--text)' }

  return (
    <div className="flex h-full p-3 gap-3">
      <aside className="glass r-lg flex min-h-0 shrink-0 flex-col transition-all" style={{ width: sidebar ? 264 : 0, overflow: 'hidden', opacity: sidebar ? 1 : 0 }}>
        <div className="flex items-center gap-2 px-4 pt-4 pb-2">
          <span className="h-3 w-3 rounded-[4px]" style={{ background: 'linear-gradient(160deg,#e8f6ff,#5fb2f2)', boxShadow: '0 0 12px rgba(95,178,242,.6)' }} />
          <span className="font-semibold tracking-tight">Pullwright</span>
          <button className="pill r-sm ml-auto px-2.5 py-1 text-xs" onClick={() => (mode === 'local' ? setSel(null) : (setMode('team'), setThread(null)))}>+ New</button>
        </div>
        <div className="mx-3 mb-2 flex gap-1 text-xs" role="tablist">
          {(['team', 'queue', 'local'] as Mode[]).map((m) => (
            <button key={m} role="tab" aria-selected={mode === m} className="r-sm flex-1 px-2 py-1 transition hover:bg-white/5"
              style={{ background: mode === m ? 'rgba(255,255,255,.1)' : undefined, color: mode === m ? 'var(--text)' : 'var(--muted)' }}
              onClick={() => setMode(m)}>{m === 'team' ? 'Requests' : m === 'queue' ? 'Queue' : 'Local runs'}</button>
          ))}
        </div>
        {mode !== 'local' ? (me ? <RequestsSidebar me={me} sel={thread} onSelect={setThread} /> : <div className="flex-1" />) : (<>
        <div className="min-h-0 flex-1 overflow-auto px-2 py-1">
          {threads.map((t) => (
            <button key={t.id} className="r-sm mb-1 block w-full px-3 py-2 text-left text-sm transition hover:bg-white/5" style={{ background: sel === t.id ? 'rgba(255,255,255,.08)' : undefined }} onClick={() => setSel(t.id)}>
              <div className="truncate">{t.task.replace(/^\[[^\]]+\]\s*/, '')}</div>
              <div className="mono mt-0.5 flex gap-2 text-[11px]" style={{ color: 'var(--dim)' }}>
                <span style={{ color: stateColor(t.state) }}>{t.state.replace(/_/g, ' ')}</span>
                {t.runs.length > 1 && <span>· {t.runs.length} turns</span>}
                <span className="ml-auto">{usd(t.cost)}</span>
              </div>
            </button>
          ))}
        </div>
        <div className="space-y-2 px-4 pb-4 pt-2 text-xs" style={{ color: 'var(--muted)', boxShadow: 'inset 0 1px 0 var(--line)' }}>
          <label className="block">Repository
            <input className={field} style={fieldStyle} placeholder="path to repo" value={cwd} onChange={(e) => setCwd(e.target.value)} />
          </label>
          <label className="block">Cost cap (USD)
            <input className={field} style={fieldStyle} type="number" value={maxCost} onChange={(e) => setMaxCost(Number(e.target.value))} />
          </label>
        </div>
        </>)}
      </aside>
      <main className="relative min-h-0 min-w-0 flex-1">
        <button aria-label="Toggle sidebar" className="r-sm absolute left-1 top-1 z-10 px-2 py-1 text-sm hover:bg-white/5" style={{ color: 'var(--muted)' }} onClick={() => setSidebar(!sidebar)}>☰</button>
        {mode !== 'local' ? (
          me === undefined ? null : me === null ? <LoginScreen /> : mode === 'queue' ? <QueueView /> : <RequestsMain sel={thread} onSelect={setThread} />
        ) : current ? (
          <Chat key={current.id} runs={current.runs} onSubmit={start} />
        ) : (
          <div className="mx-auto flex h-full max-w-3xl flex-col justify-center px-4">
            <h1 className="mb-2 text-center text-4xl font-semibold tracking-tight">What should we build?</h1>
            <p className="mb-8 text-center" style={{ color: 'var(--muted)' }}>Intake proposes a plan, a supervisor delegates to workers, diagnosis verifies, and the result lands on a branch.</p>
            <NewTaskComposer onSubmit={(t, k, m) => start(t, k, m)} />
            {!cwd && <p className="mt-3 text-center text-xs" style={{ color: 'var(--warn)' }}>Set the repository path in the sidebar first.</p>}
          </div>
        )}
      </main>
    </div>
  )
}
