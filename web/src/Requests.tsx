// The team flow in the browser: log in with GitHub, send a task for a repository, read the plan, approve it,
// and follow the issue and PR. The same requests made from Slack or Teams show up here too.
import { useEffect, useRef, useState } from 'react'
import { team, type Me, type PlanCardBody, type RequestRow, type ThreadMessage } from './api'
import { Btn, Md } from './Chat'
import { usd } from './model'

const STATE_LABEL: Record<string, string> = {
  planning: 'planning', awaiting_approval: 'needs your approval', queued: 'queued for a runner', working: 'runner working',
  pr_open: 'PR open', pr_draft: 'draft PR', cancelled: 'cancelled', failed: 'failed',
}
const stateColor = (s: string) =>
  s === 'pr_open' ? 'var(--ok)' : s === 'failed' || s === 'cancelled' ? 'var(--err)' : s === 'awaiting_approval' ? 'var(--accent, #5fb2f2)' : 'var(--warn)'

export function useMe() {
  const [me, setMe] = useState<Me | null | undefined>(undefined)
  useEffect(() => { team.me().then(setMe).catch(() => setMe(null)) }, [])
  return me
}

export function LoginScreen() {
  const linked = new URLSearchParams(location.search).get('linked')
  return (
    <div className="mx-auto flex h-full max-w-md flex-col items-center justify-center gap-4 px-4 text-center">
      <h1 className="text-3xl font-semibold tracking-tight">Pullwright</h1>
      <p style={{ color: 'var(--muted)' }}>Tag it with a task in Slack, Teams or here. It plans, you approve, a runner opens a PR that tags you.</p>
      {linked && <p className="text-sm" style={{ color: linked === 'expired' ? 'var(--err)' : 'var(--ok)' }}>
        {linked === 'expired' ? 'That link expired. Ask the bot for a new one.' : `Your ${linked} account is linked.`}</p>}
      <a className="solid r-sm inline-flex h-10 items-center px-5 text-sm font-medium" href={team.loginUrl(location.pathname)}>Log in with GitHub</a>
    </div>
  )
}

function PlanCard({ card, live, onDecide }: { card: PlanCardBody; live: boolean; onDecide: (d: 'approve' | 'revise' | 'cancel', feedback: string) => void }) {
  const [feedback, setFeedback] = useState('')
  const [revising, setRevising] = useState(false)
  return (
    <div className="glass r-lg my-3 p-4" id={`plan-${card.revision}`}>
      <div className="mono mb-2 flex gap-3 text-xs" style={{ color: 'var(--dim)' }}>
        <span>Plan for {card.repo}{card.revision ? ` · revision ${card.revision}` : ''}</span>
        <span className="ml-auto">planning {usd(card.cost_usd)}</span>
      </div>
      <Md text={card.plan_markdown} />
      {live && (
        <div className="mt-4 space-y-2">
          {revising && (
            <textarea className="r-sm w-full p-2 text-sm outline-none" rows={3} autoFocus placeholder="What should change?"
              style={{ background: 'rgba(0,0,0,.35)', boxShadow: 'inset 0 0 0 1px var(--line)', color: 'var(--text)' }}
              value={feedback} onChange={(e) => setFeedback(e.target.value)} />
          )}
          <div className="flex gap-2">
            <Btn onClick={() => onDecide('approve', '')}>Approve and open issue</Btn>
            {revising
              ? <Btn secondary onClick={() => feedback.trim() && onDecide('revise', feedback)}>Send feedback</Btn>
              : <Btn secondary onClick={() => setRevising(true)}>Revise</Btn>}
            <Btn secondary onClick={() => onDecide('cancel', '')}>Cancel</Btn>
          </div>
        </div>
      )}
    </div>
  )
}

function Thread({ thread }: { thread: string }) {
  const [msgs, setMsgs] = useState<ThreadMessage[]>([])
  const [req, setReq] = useState<RequestRow | null>(null)
  const [toast, setToast] = useState('')
  const last = useRef(0)
  useEffect(() => {
    let stop = false // the component is keyed by thread, so state starts empty for each thread
    const poll = async () => {
      try {
        const t = await team.thread(thread, last.current)
        if (stop) return
        setReq(t.request)
        if (t.messages.length) {
          last.current = t.messages[t.messages.length - 1].id
          setMsgs((m) => [...m, ...t.messages])
        }
      } catch { /* keep polling */ }
    }
    poll()
    const id = setInterval(poll, 2000)
    return () => { stop = true; clearInterval(id) }
  }, [thread])

  const lastPlan = msgs.filter((m) => m.kind === 'plan').at(-1)
  const decide = async (d: 'approve' | 'revise' | 'cancel', feedback: string) => {
    if (!req) return
    const r = await team.decide(req.id, d, feedback)
    setToast(r.message)
    setTimeout(() => setToast(''), 5000)
  }
  return (
    <div className="mx-auto flex h-full max-w-3xl flex-col px-4 pt-10">
      {req && (
        <div className="mono mb-3 flex flex-wrap gap-3 text-xs" style={{ color: 'var(--dim)' }}>
          <span style={{ color: stateColor(req.state) }}>{STATE_LABEL[req.state] ?? req.state}</span>
          <span>{req.repo}</span>
          {req.issue_url && <a className="underline" href={req.issue_url} target="_blank" rel="noreferrer">issue #{req.issue_number}</a>}
          {req.pr_url && <a className="underline" href={req.pr_url} target="_blank" rel="noreferrer">pull request</a>}
          <span className="ml-auto">{usd(req.cost_usd)}</span>
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-auto pb-6">
        {msgs.map((m) =>
          m.kind === 'user' ? (
            <div key={m.id} className="my-3 flex justify-end"><div className="glass r-lg max-w-[80%] px-4 py-2 text-sm">{m.body.text}</div></div>
          ) : m.kind === 'plan' ? (
            <PlanCard key={m.id} card={m.body} live={m === lastPlan && req?.state === 'awaiting_approval'} onDecide={decide} />
          ) : (
            <div key={m.id} className="my-2 text-sm"><Md text={m.body.text} /></div>
          ),
        )}
        {toast && <p className="mono mt-2 text-xs" style={{ color: 'var(--muted)' }}>{toast}</p>}
      </div>
    </div>
  )
}

function Composer({ onSent }: { onSent: (thread: string) => void }) {
  const [repo, setRepo] = useState(localStorage.getItem('pw.repo') ?? '')
  const [task, setTask] = useState('')
  const [busy, setBusy] = useState(false)
  const send = async () => {
    if (!repo.includes('/') || !task.trim()) return
    setBusy(true)
    localStorage.setItem('pw.repo', repo)
    try { onSent((await team.create(repo.trim(), task.trim())).thread); setTask('') } finally { setBusy(false) }
  }
  const field = { background: 'rgba(0,0,0,.35)', boxShadow: 'inset 0 0 0 1px var(--line)', color: 'var(--text)' }
  return (
    <div className="mx-auto flex h-full max-w-2xl flex-col justify-center gap-3 px-4">
      <h1 className="mb-1 text-center text-4xl font-semibold tracking-tight">What should we ship?</h1>
      <p className="mb-4 text-center text-sm" style={{ color: 'var(--muted)' }}>
        A describer and a verifier read the repository and plan the change. You approve it, it becomes an issue, and a runner opens a PR that tags you.
      </p>
      <input className="mono r-sm px-3 py-2 text-sm outline-none" style={field} placeholder="owner/repository" value={repo} onChange={(e) => setRepo(e.target.value)} />
      <textarea className="r-sm px-3 py-2 text-sm outline-none" style={field} rows={5} placeholder="Describe the task"
        value={task} onChange={(e) => setTask(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) send() }} />
      <div className="flex justify-end"><Btn onClick={send}>{busy ? 'Sending...' : 'Plan it'}</Btn></div>
    </div>
  )
}

export function QueueView() {
  const [rows, setRows] = useState<RequestRow[]>([])
  useEffect(() => { const f = () => team.queue().then(setRows).catch(() => {}); f(); const t = setInterval(f, 5000); return () => clearInterval(t) }, [])
  return (
    <div className="mx-auto max-w-4xl px-4 pt-10">
      <h2 className="mb-1 text-xl font-semibold">Queue</h2>
      <p className="mb-4 text-sm" style={{ color: 'var(--muted)' }}>Approved issues are worked one at a time, oldest first.</p>
      <table className="w-full text-sm">
        <thead><tr className="text-left text-xs" style={{ color: 'var(--dim)' }}><th className="py-2">Issue</th><th>Task</th><th>Requested by</th><th>State</th><th>PR</th><th className="text-right">Cost</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} style={{ boxShadow: 'inset 0 1px 0 var(--line)' }}>
              <td className="mono py-2">{r.issue_url ? <a className="underline" href={r.issue_url} target="_blank" rel="noreferrer">{r.repo}#{r.issue_number}</a> : r.repo}</td>
              <td className="max-w-xs truncate">{r.task}</td>
              <td>{r.requester_name} <span className="mono text-xs" style={{ color: 'var(--dim)' }}>@{r.requester_login} · {r.channel}</span></td>
              <td style={{ color: stateColor(r.state) }}>{STATE_LABEL[r.state] ?? r.state}</td>
              <td>{r.pr_url && <a className="underline" href={r.pr_url} target="_blank" rel="noreferrer">open</a>}</td>
              <td className="mono text-right">{usd(r.cost_usd)}</td>
            </tr>
          ))}
          {!rows.length && <tr><td colSpan={6} className="py-6 text-center" style={{ color: 'var(--dim)' }}>Nothing queued.</td></tr>}
        </tbody>
      </table>
    </div>
  )
}

export function RequestsSidebar({ me, sel, onSelect }: { me: Me; sel: string | null; onSelect: (thread: string | null) => void }) {
  const [rows, setRows] = useState<RequestRow[]>([])
  useEffect(() => { const f = () => team.requests().then(setRows).catch(() => {}); f(); const t = setInterval(f, 3000); return () => clearInterval(t) }, [])
  return (
    <>
      <div className="min-h-0 flex-1 overflow-auto px-2 py-1">
        {rows.map((r) => {
          const thread = r.thread_ref?.thread as string | undefined
          return (
            <button key={r.id} className="r-sm mb-1 block w-full px-3 py-2 text-left text-sm transition hover:bg-white/5"
              style={{ background: thread && sel === thread ? 'rgba(255,255,255,.08)' : undefined }}
              onClick={() => onSelect(thread ?? null)} title={thread ? '' : `from ${r.channel}: reply in that thread`}>
              <div className="truncate">{r.task}</div>
              <div className="mono mt-0.5 flex gap-2 text-[11px]" style={{ color: 'var(--dim)' }}>
                <span style={{ color: stateColor(r.state) }}>{STATE_LABEL[r.state] ?? r.state}</span>
                {r.channel !== 'web' && <span>· {r.channel}</span>}
                <span className="ml-auto">{usd(r.cost_usd)}</span>
              </div>
            </button>
          )
        })}
      </div>
      <div className="flex items-center gap-2 px-4 pb-4 pt-2 text-xs" style={{ color: 'var(--muted)', boxShadow: 'inset 0 1px 0 var(--line)' }}>
        <span className="truncate">@{me.login}</span>
        <button className="ml-auto underline" onClick={() => team.logout().then(() => location.reload())}>Log out</button>
      </div>
    </>
  )
}

export function RequestsMain({ sel, onSelect }: { sel: string | null; onSelect: (thread: string) => void }) {
  return sel ? <Thread key={sel} thread={sel} /> : <Composer onSent={onSelect} />
}
