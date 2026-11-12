// The team flow in the browser: log in with GitHub, send a task for a repository, follow the conversation, the
// plan, the approval, the runner and the PR (Thread.tsx). The same requests made from Slack or Teams show up here too.
import { useEffect, useState } from 'react'
import { ArrowUp, GitPullRequest, Inbox, LogOut, Search } from 'lucide-react'
import { team, type Me, type RepoInfo, type RequestRow } from './api'
import { usd } from './model'
import { Thread } from './Thread'
import { GrowingInput, Mark, Orb, Toasts, toast } from './shared'

export { Toasts, toast }

export const STATE_LABEL: Record<string, string> = {
  planning: 'Planning', awaiting_approval: 'Needs approval', queued: 'Queued', working: 'Runner working',
  pr_open: 'PR open', pr_draft: 'Draft PR', cancelled: 'Cancelled', failed: 'Failed', answered: 'Answered',
}
export const stateColor = (s: string) =>
  s === 'pr_open' ? 'var(--ok)' : s === 'failed' ? 'var(--err)' : s === 'cancelled' || s === 'answered' ? 'var(--faint)'
    : s === 'awaiting_approval' ? 'var(--text)' : 'var(--warn)'
const LIVE = new Set(['planning', 'awaiting_approval', 'queued', 'working'])

// ---------- login ----------

export function useMe() {
  const [me, setMe] = useState<Me | null | undefined>(undefined)
  useEffect(() => { team.me().then(setMe).catch(() => setMe(null)) }, [])
  return me
}

export function LoginScreen() {
  const linked = new URLSearchParams(location.search).get('linked')
  return (
    <div className="msg-in mx-auto flex h-full max-w-md flex-col items-center justify-center gap-5 px-4 text-center">
      <Orb state="breathing" size={64} />
      <h1 className="text-[28px] font-semibold tracking-[-0.02em]">Pullwright</h1>
      <p className="text-[14px] leading-[22px]" style={{ color: 'var(--muted)' }}>
        Ask it about a repository, or give it a change, in Slack, Teams or here. Questions get answers; changes get a plan, your approval, and a pull request that tags you.
      </p>
      {linked && <p className="text-sm" style={{ color: linked === 'expired' ? 'var(--err)' : 'var(--ok)' }}>
        {linked === 'expired' ? 'That link expired. Ask the bot for a new one.' : `Your ${linked} account is linked.`}</p>}
      <a className="inline-flex h-10 items-center rounded-full px-5 text-sm font-medium" style={{ background: 'var(--primary)', color: 'var(--on-primary)' }}
        href={team.loginUrl(location.pathname)}>Log in with GitHub</a>
    </div>
  )
}

// ---------- a new request ----------

const EXAMPLES = [
  { kind: 'Question', text: 'Give me a summary of this project' },
  { kind: 'Change', text: 'Add a /health endpoint that returns the app version' },
  { kind: 'Fix', text: 'Fix the flaky date test in tests/test_reports.py' },
]

function Composer({ onSent }: { onSent: (thread: string) => void }) {
  const [repo, setRepo] = useState(localStorage.getItem('pw.repo') ?? '')
  const [task, setTask] = useState('')
  const [busy, setBusy] = useState(false)
  const [repos, setRepos] = useState<RepoInfo[]>([])
  useEffect(() => { team.repos().then(setRepos).catch(() => {}) }, [])
  const badRepo = repo.trim() !== '' && !repo.includes('/')
  const send = async () => {
    if (!task.trim() || badRepo || busy) return
    setBusy(true)
    localStorage.setItem('pw.repo', repo)
    try { onSent((await team.create(repo.trim(), task.trim())).thread); setTask('') }
    catch (e) { toast(`Could not send the task: ${String(e).slice(0, 200)}`, 'err') }
    finally { setBusy(false) }
  }
  return (
    <div className="mx-auto flex h-full w-full max-w-[820px] flex-col justify-center gap-5 px-6">
      <div className="msg-in flex flex-col items-center gap-4 text-center">
        <Orb state={busy ? 'working' : 'breathing'} size={64} />
        <h1 className="text-[30px] leading-tight font-semibold tracking-[-0.025em] sm:text-[34px]">What should we ship?</h1>
        <p className="max-w-[560px] text-[14px] leading-[22px]" style={{ color: 'var(--muted)' }}>
          Ask about a repository and get an answer here. Ask for a change and you get a plan; once you approve it, a runner opens a pull request that tags you.
        </p>
      </div>
      <form className="msg-in rounded-[20px] transition-shadow focus-within:shadow-[0_0_0_1px_var(--ring)]" style={{ background: 'var(--field)', boxShadow: '0 0 0 1px var(--border)' }}
        onSubmit={(e) => { e.preventDefault(); send() }}>
        <div className="flex items-center gap-2 px-4 pt-3.5">
          <span className="text-[12px]" style={{ color: 'var(--dim)' }}>Repository</span>
          <input className="mono min-w-0 flex-1 bg-transparent text-[12.5px] outline-none placeholder:text-[var(--faint)]" list="pw-repos" value={repo} onChange={(e) => setRepo(e.target.value)}
            placeholder="owner/repository, or leave empty and I'll pick one" aria-label="Repository" aria-invalid={badRepo} />
          <datalist id="pw-repos">{repos.map((r) => <option key={r.repo} value={r.repo}>{r.description}</option>)}</datalist>
        </div>
        <GrowingInput value={task} onChange={setTask} onSend={send} minRows={3} label="Task" placeholder="Ask a question or describe a change" />
        <div className="flex items-center gap-2 py-2.5 pr-2.5 pl-4">
          <span className="hidden text-[11.5px] sm:inline" style={{ color: 'var(--faint)' }}>Enter to send · Shift+Enter for a new line</span>
          <button type="submit" aria-label="Send" disabled={busy || !task.trim() || badRepo}
            className="ml-auto grid size-9 place-items-center rounded-full transition-[opacity,transform] active:scale-95 disabled:opacity-35" style={{ background: 'var(--primary)', color: 'var(--on-primary)' }}>
            <ArrowUp size={17} strokeWidth={2.2} />
          </button>
        </div>
      </form>
      {badRepo && <p className="px-2 text-xs" style={{ color: 'var(--err)' }}>Use the owner/repository form.</p>}
      <div className="msg-in grid gap-2 sm:grid-cols-3">
        {EXAMPLES.map((x) => (
          <button key={x.text} type="button" onClick={() => setTask(x.text)} className="rounded-[14px] px-4 py-3 text-left text-[13px] leading-[19px] transition-colors hover:bg-[var(--sel)]"
            style={{ background: 'var(--card)' }}>
            <span className="mb-1 block text-[11px] font-medium" style={{ color: 'var(--dim)' }}>{x.kind}</span>{x.text}
          </button>
        ))}
      </div>
    </div>
  )
}

// ---------- the queue ----------

export function StateBadge({ state }: { state: string }) {
  const c = stateColor(state)
  return (
    <span className="inline-flex items-center gap-1.5 text-[11.5px]" style={{ color: 'var(--dim)' }}>
      <span className={`size-1.5 rounded-full ${LIVE.has(state) ? 'pulse-dot' : ''}`} style={{ background: c }} aria-hidden />
      {STATE_LABEL[state] ?? state}
    </span>
  )
}

export function QueueView() {
  const [rows, setRows] = useState<RequestRow[] | null>(null)
  useEffect(() => { const f = () => team.queue().then(setRows).catch(() => setRows((r) => r ?? [])); f(); const t = setInterval(f, 5000); return () => clearInterval(t) }, [])
  return (
    <div className="mx-auto max-w-5xl px-6 pt-14 pb-10">
      <h2 className="mb-1 text-xl font-semibold tracking-[-0.01em]">Queue</h2>
      <p className="mb-5 text-sm" style={{ color: 'var(--muted)' }}>Approved issues are worked in order, oldest first.</p>
      {rows === null ? <div className="space-y-2">{[0, 1, 2].map((i) => <div key={i} className="skeleton h-12" />)}</div> : !rows.length ? (
        <div className="flex flex-col items-center gap-2 rounded-[16px] px-4 py-14 text-center" style={{ background: 'var(--card)' }}>
          <Inbox size={26} style={{ color: 'var(--dim)' }} />
          <p className="font-medium">Nothing queued</p>
          <p className="text-sm" style={{ color: 'var(--muted)' }}>Approve a plan and its issue shows up here until the PR is open.</p>
        </div>
      ) : (
        <div className="flex flex-col gap-1.5">
          {rows.map((r) => (
            <div key={r.id} className="msg-in flex flex-wrap items-center gap-x-4 gap-y-1 rounded-[12px] px-4 py-3 text-[13px]" style={{ background: 'var(--card)' }}>
              <StateBadge state={r.state} />
              <span className="min-w-0 flex-1 truncate">{r.task}</span>
              <span className="mono text-[12px]" style={{ color: 'var(--dim)' }}>{r.issue_url ? <a className="underline decoration-current/30 underline-offset-2" href={r.issue_url} target="_blank" rel="noreferrer">{r.repo}#{r.issue_number}</a> : r.repo}</span>
              <span className="text-[12px]" style={{ color: 'var(--dim)' }}>@{r.requester_login}</span>
              {r.pr_url && <a className="inline-flex items-center gap-1 text-[12px] underline decoration-current/30 underline-offset-2" href={r.pr_url} target="_blank" rel="noreferrer"><GitPullRequest size={12} />PR</a>}
              <span className="mono tnum text-[12px]" style={{ color: 'var(--dim)' }}>{usd(r.cost_usd)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ---------- sidebar ----------

const FILTERS: Array<{ key: string; label: string; match: (s: string) => boolean }> = [
  { key: 'all', label: 'All', match: () => true },
  { key: 'active', label: 'Active', match: (s) => LIVE.has(s) },
  { key: 'review', label: 'To do', match: (s) => s === 'awaiting_approval' || s === 'pr_open' || s === 'pr_draft' },
  { key: 'done', label: 'Closed', match: (s) => s === 'failed' || s === 'cancelled' || s === 'answered' },
]

export function RequestsSidebar({ me, sel, onSelect }: { me: Me; sel: string | null; onSelect: (thread: string | null) => void }) {
  const [rows, setRows] = useState<RequestRow[] | null>(null)
  const [q, setQ] = useState('')
  const [filter, setFilter] = useState(localStorage.getItem('pw.filter') ?? 'all')
  useEffect(() => { const f = () => team.requests().then(setRows).catch(() => setRows((r) => r ?? [])); f(); const t = setInterval(f, 3000); return () => clearInterval(t) }, [])
  const match = FILTERS.find((f) => f.key === filter)?.match ?? (() => true)
  const shown = (rows ?? []).filter((r) => match(r.state) && (!q || `${r.task} ${r.repo}`.toLowerCase().includes(q.toLowerCase())))
  return (
    <>
      <div className="px-3 pb-2">
        <label className="flex h-8 items-center gap-2 rounded-[8px] px-2.5" style={{ background: 'var(--field)' }}>
          <Search size={13} style={{ color: 'var(--dim)' }} />
          <input className="w-full bg-transparent text-[12.5px] outline-none placeholder:text-[var(--faint)]" placeholder="Search" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search requests" />
        </label>
        <div className="mt-2 flex gap-1" role="group" aria-label="Filter">
          {FILTERS.map((f) => (
            <button key={f.key} className="chip flex-1 rounded-full px-1.5 py-0.5 text-[11px] transition-colors" aria-pressed={filter === f.key}
              onClick={() => { localStorage.setItem('pw.filter', f.key); setFilter(f.key) }}>{f.label}</button>
          ))}
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-auto px-2 py-1">
        {rows === null && [0, 1, 2, 3].map((i) => <div key={i} className="skeleton mx-1 mb-2 h-11" />)}
        {rows !== null && !shown.length && <p className="px-3 py-6 text-center text-xs" style={{ color: 'var(--dim)' }}>{rows.length ? 'No requests match.' : 'No requests yet. Start one with New.'}</p>}
        {shown.map((r) => {
          const thread = r.thread_ref?.thread as string | undefined
          const current = !!thread && sel === thread
          return (
            <button key={r.id} className="hov mb-px block w-full rounded-[8px] px-3 py-2 text-left transition-colors"
              style={{ background: current ? 'var(--sel)' : undefined }} aria-current={current ? 'true' : undefined}
              onClick={() => onSelect(thread ?? null)} title={thread ? '' : `from ${r.channel}: reply in that thread`}>
              <div className="truncate text-[13px]" style={{ color: current ? 'var(--text)' : 'var(--muted)', fontWeight: current ? 500 : 400 }}>{r.task}</div>
              <div className="flex items-center gap-2 text-[11.5px] leading-[18px]" style={{ color: 'var(--dim)' }}>
                <StateBadge state={r.state} />
                {r.channel !== 'web' && <span>· {r.channel}</span>}
                <span className="mono tnum ml-auto text-[11px]">{usd(r.cost_usd)}</span>
              </div>
            </button>
          )
        })}
      </div>
      <div className="flex items-center gap-2.5 px-4 pt-2 pb-4 text-[12px]" style={{ color: 'var(--muted)' }}>
        <span className="grid size-[26px] place-items-center rounded-full text-[10px] font-semibold" style={{ background: 'var(--muted-bg)', color: 'var(--text)' }} aria-hidden>
          {me.login.slice(0, 2).toUpperCase()}</span>
        <span className="truncate">{me.login}</span>
        <button className="hov ml-auto inline-flex items-center gap-1 rounded-[6px] px-1.5 py-1" style={{ color: 'var(--dim)' }} onClick={() => team.logout().then(() => location.reload())}>
          <LogOut size={12} /> Log out</button>
      </div>
    </>
  )
}

export function RequestsMain({ sel, onSelect }: { sel: string | null; onSelect: (thread: string) => void }) {
  return sel ? <Thread key={sel} thread={sel} /> : <Composer onSent={onSelect} />
}

export { Mark }
