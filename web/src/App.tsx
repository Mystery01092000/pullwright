import { useEffect, useMemo, useState } from 'react'
import { Coins, Inbox, MessagesSquare, Monitor, Moon, PanelLeft, Plus, Sun, Terminal } from 'lucide-react'
import { api, type RunRow } from './api'
import { Chat, NewTaskComposer } from './Chat'
import { CostsView } from './Costs'
import { usd } from './model'
import { LoginScreen, QueueView, RequestsMain, RequestsSidebar, Toasts, useMe } from './Requests'

type Mode = 'team' | 'queue' | 'costs' | 'local'
type Theme = 'system' | 'light' | 'dark'

type Thread = { id: string; task: string; state: string; cost: number; created_at: number; runs: RunRow[] }

const stateColor = (s: string) => (s === 'done' ? 'var(--ok)' : s === 'failed' || s === 'cancelled' ? 'var(--err)' : 'var(--warn)')
const read = (k: string) => { try { return localStorage.getItem(k) } catch { return null } }
const write = (k: string, v: string) => { try { localStorage.setItem(k, v) } catch { /* private mode */ } }
const narrow = () => window.matchMedia('(max-width: 767px)').matches

const TABS: Array<{ mode: Mode; label: string; icon: React.ReactNode }> = [
  { mode: 'team', label: 'Requests', icon: <MessagesSquare size={13} /> },
  { mode: 'queue', label: 'Queue', icon: <Inbox size={13} /> },
  { mode: 'costs', label: 'Costs', icon: <Coins size={13} /> },
  { mode: 'local', label: 'Local', icon: <Terminal size={13} /> },
]

function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>((read('pw.theme') as Theme) ?? 'system')
  useEffect(() => {
    const root = document.documentElement
    if (theme === 'system') root.removeAttribute('data-theme')
    else root.setAttribute('data-theme', theme)
    write('pw.theme', theme)
  }, [theme])
  const next = () => setTheme((t) => (t === 'system' ? 'light' : t === 'light' ? 'dark' : 'system'))
  return [theme, next]
}

export default function App() {
  const [runs, setRuns] = useState<RunRow[]>([])
  const [sel, setSel] = useState<string | null>(null)
  const [cwd, setCwd] = useState(read('cwd') ?? '')
  const [maxCost, setMaxCost] = useState(Number(read('maxCost') ?? 20))
  const [sidebar, setSidebar] = useState(!narrow())
  const [mode, setModeState] = useState<Mode>((read('pw.mode') as Mode) ?? 'team')
  const [thread, setThreadState] = useState<string | null>(new URLSearchParams(location.search).get('thread'))
  const [theme, nextTheme] = useTheme()
  const me = useMe()
  const setMode = (m: Mode) => { write('pw.mode', m); setModeState(m); if (narrow()) setSidebar(false) }
  const setThread = (t: string | null) => { setThreadState(t); if (narrow()) setSidebar(false) }

  const refresh = () => api.runs().then(setRuns).catch(() => {})
  useEffect(() => {
    if (mode !== 'local') return
    refresh(); const t = setInterval(refresh, 3000); return () => clearInterval(t)
  }, [mode])

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
    write('cwd', cwd); write('maxCost', String(maxCost))
    const text = kind && kind !== 'Feature' ? `[${kind}] ${task}` : task
    const { id } = await api.start({ task: text, cwd: continueFrom ? undefined : cwd, main_model: model, max_cost_usd: maxCost, continue_from: continueFrom })
    if (!continueFrom) setSel(id)
    refresh()
  }
  const current = threads.find((t) => t.id === sel)
  const field = 'field mono r-sm mt-1 w-full px-2.5 py-1.5 text-xs outline-none'
  const ThemeIcon = theme === 'light' ? Sun : theme === 'dark' ? Moon : Monitor

  return (
    <div className="flex h-full gap-3 p-0 md:p-3">
      {sidebar && <button className="fixed inset-0 z-20 bg-black/40 md:hidden" aria-label="Close sidebar" onClick={() => setSidebar(false)} />}
      <aside className="glass r-lg fixed inset-y-2 left-2 z-30 flex min-h-0 shrink-0 flex-col transition-all md:static md:inset-auto md:z-auto"
        style={{ width: sidebar ? 272 : 0, overflow: 'hidden', opacity: sidebar ? 1 : 0, pointerEvents: sidebar ? undefined : 'none' }} aria-hidden={!sidebar}>
        <div className="flex items-center gap-2 px-4 pt-4 pb-3">
          <span className="h-3 w-3 rounded-[4px]" style={{ background: 'linear-gradient(160deg,#e8f6ff,#5fb2f2)', boxShadow: '0 0 12px rgba(95,178,242,.6)' }} aria-hidden />
          <span className="font-semibold tracking-tight">Pullwright</span>
          <button className="hov r-sm ml-auto p-1.5" style={{ color: 'var(--muted)' }} onClick={nextTheme} aria-label={`Theme: ${theme}. Switch theme`} title={`Theme: ${theme}`}><ThemeIcon size={14} /></button>
          <button className="pill r-sm inline-flex items-center gap-1 px-2.5 py-1 text-xs" onClick={() => (mode === 'local' ? setSel(null) : (setMode('team'), setThread(null)))}><Plus size={12} /> New</button>
        </div>
        <nav className="mx-3 mb-3 grid grid-cols-4 gap-1 text-[11px]" role="tablist" aria-label="Views">
          {TABS.map((t) => (
            <button key={t.mode} role="tab" aria-selected={mode === t.mode} className="hov r-sm flex flex-col items-center gap-0.5 px-1 py-1.5 transition"
              style={{ background: mode === t.mode ? 'var(--sel)' : undefined, color: mode === t.mode ? 'var(--text)' : 'var(--muted)' }}
              onClick={() => setMode(t.mode)}>{t.icon}{t.label}</button>
          ))}
        </nav>
        {mode !== 'local' ? (me ? <RequestsSidebar me={me} sel={thread} onSelect={setThread} /> : <div className="flex-1" />) : (<>
        <div className="min-h-0 flex-1 overflow-auto px-2 py-1">
          {!threads.length && <p className="px-3 py-6 text-center text-xs" style={{ color: 'var(--dim)' }}>No local runs yet.</p>}
          {threads.map((t) => (
            <button key={t.id} className="hov r-sm mb-1 block w-full px-3 py-2 text-left text-sm transition" style={{ background: sel === t.id ? 'var(--sel)' : undefined }} onClick={() => { setSel(t.id); if (narrow()) setSidebar(false) }}>
              <div className="truncate">{t.task.replace(/^\[[^\]]+\]\s*/, '')}</div>
              <div className="mono mt-0.5 flex gap-2 text-[11px]" style={{ color: 'var(--dim)' }}>
                <span style={{ color: stateColor(t.state) }}>{t.state.replace(/_/g, ' ')}</span>
                {t.runs.length > 1 && <span>· {t.runs.length} turns</span>}
                <span className="tnum ml-auto">{usd(t.cost)}</span>
              </div>
            </button>
          ))}
        </div>
        <div className="space-y-2 px-4 pt-2 pb-4 text-xs" style={{ color: 'var(--muted)', boxShadow: 'inset 0 1px 0 var(--line)' }}>
          <label className="block">Repository
            <input className={field} placeholder="path to repo" value={cwd} onChange={(e) => setCwd(e.target.value)} />
          </label>
          <label className="block">Cost cap (USD)
            <input className={field} type="number" value={maxCost} onChange={(e) => setMaxCost(Number(e.target.value))} />
          </label>
        </div>
        </>)}
      </aside>
      <main className="relative min-h-0 min-w-0 flex-1">
        <button aria-label="Toggle sidebar" aria-expanded={sidebar} className="hov r-sm absolute top-2 left-2 z-10 p-1.5" style={{ color: 'var(--muted)' }} onClick={() => setSidebar(!sidebar)}><PanelLeft size={16} /></button>
        {mode !== 'local' ? (
          me === undefined ? null : me === null ? <LoginScreen /> : mode === 'queue' ? <div className="h-full overflow-auto"><QueueView /></div> : mode === 'costs' ? <div className="h-full overflow-auto"><CostsView /></div> : <RequestsMain sel={thread} onSelect={setThread} />
        ) : current ? (
          <Chat key={current.id} runs={current.runs} onSubmit={start} />
        ) : (
          <div className="mx-auto flex h-full max-w-3xl flex-col justify-center px-4">
            <h1 className="mb-2 text-center text-3xl font-semibold tracking-tight sm:text-4xl">What should we build?</h1>
            <p className="mb-8 text-center" style={{ color: 'var(--muted)' }}>Local mode runs on this machine: intake proposes a plan, a supervisor delegates to workers, diagnosis verifies, and the result lands on a branch.</p>
            <NewTaskComposer onSubmit={(t, k, m) => start(t, k, m)} />
            {!cwd && <p className="mt-3 text-center text-xs" style={{ color: 'var(--warn)' }}>Set the repository path in the sidebar first.</p>}
          </div>
        )}
      </main>
      <Toasts />
    </div>
  )
}
