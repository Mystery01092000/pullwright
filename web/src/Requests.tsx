// The team flow in the browser: log in with GitHub, send a task for a repository, read the plan, approve it,
// and follow the issue, the runner and the PR. The same requests made from Slack or Teams show up here too.
import { useEffect, useMemo, useRef, useState } from 'react'
import { ChevronRight, CircleCheck, FileCode, FlaskConical, GitPullRequest, Inbox, ListTodo, LogOut, Search, ShieldCheck, TriangleAlert } from 'lucide-react'
import { team, type Me, type PlanCardBody, type RepoInfo, type RequestRow, type ThreadMessage } from './api'
import { Btn, Md } from './Chat'
import { usd } from './model'
import { RequestHeader, RunnerPanel, steps, useActivity } from './Timeline'

export const STATE_LABEL: Record<string, string> = {
  planning: 'planning', awaiting_approval: 'needs approval', queued: 'queued', working: 'runner working',
  pr_open: 'PR open', pr_draft: 'draft PR', cancelled: 'cancelled', failed: 'failed', answered: 'answered',
}
export const stateColor = (s: string) =>
  s === 'pr_open' || s === 'answered' ? 'var(--ok)' : s === 'failed' || s === 'cancelled' ? 'var(--err)' : s === 'awaiting_approval' ? 'var(--accent-deep)' : 'var(--warn)'
const LIVE = new Set(['planning', 'awaiting_approval', 'queued', 'working'])

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
        <div key={t.id} className="glass r-md pointer-events-auto px-4 py-2.5 text-sm"
          style={{ boxShadow: `inset 3px 0 0 ${t.tone === 'ok' ? 'var(--ok)' : t.tone === 'err' ? 'var(--err)' : 'var(--accent-deep)'}, var(--glass-shadow)` }}>{t.text}</div>
      ))}
    </div>
  )
}

// ---------- login ----------

export function useMe() {
  const [me, setMe] = useState<Me | null | undefined>(undefined)
  useEffect(() => { team.me().then(setMe).catch(() => setMe(null)) }, [])
  return me
}

export function LoginScreen() {
  const linked = new URLSearchParams(location.search).get('linked')
  return (
    <div className="mx-auto flex h-full max-w-md flex-col items-center justify-center gap-4 px-4 text-center">
      <span className="h-10 w-10 rounded-[12px]" style={{ background: 'linear-gradient(160deg,#e8f6ff,#5fb2f2)', boxShadow: '0 0 30px rgba(95,178,242,.5)' }} aria-hidden />
      <h1 className="text-3xl font-semibold tracking-tight">Pullwright</h1>
      <p style={{ color: 'var(--muted)' }}>Ask it about a repository, or give it a change, in Slack, Teams or here. Questions get answers; changes get a plan, your approval, and a PR that tags you.</p>
      {linked && <p className="text-sm" style={{ color: linked === 'expired' ? 'var(--err)' : 'var(--ok)' }}>
        {linked === 'expired' ? 'That link expired. Ask the bot for a new one.' : `Your ${linked} account is linked.`}</p>}
      <a className="solid r-sm inline-flex h-10 items-center px-5 text-sm font-medium" href={team.loginUrl(location.pathname)}>Log in with GitHub</a>
    </div>
  )
}

// ---------- the plan ----------

function Section({ icon, title, children }: { icon: React.ReactNode; title: string; children: React.ReactNode }) {
  return (
    <section className="mt-4">
      <h4 className="mb-1.5 flex items-center gap-1.5 text-xs font-medium tracking-wide uppercase" style={{ color: 'var(--muted)' }}>{icon}{title}</h4>
      {children}
    </section>
  )
}

/** Collapsed by default: detail a reviewer opens when they want it. */
function Fold({ icon, title, badge, tone, children }: { icon: React.ReactNode; title: string; badge?: string; tone?: string; children: React.ReactNode }) {
  return (
    <details className="fold r-md">
      <summary className="hov r-md flex cursor-pointer list-none items-center gap-1.5 px-2 py-1.5 text-xs font-medium tracking-wide uppercase" style={{ color: 'var(--muted)' }}>
        <ChevronRight size={13} className="fold-chev shrink-0 transition-transform" aria-hidden />{icon}{title}
        {badge && <span className="ml-auto text-xs font-normal tracking-normal normal-case" style={{ color: tone ?? 'var(--dim)' }}>{badge}</span>}
      </summary>
      <div className="px-2 pt-1 pb-2">{children}</div>
    </details>
  )
}

/** A long summary is cut to a few lines with "Show more", so the plan's structure stays visible. */
function Summary({ text }: { text: string }) {
  const [more, setMore] = useState(false)
  const long = text.length > 360
  return (
    <div className="mt-1 text-sm" style={{ color: 'var(--muted)' }}>
      <div className={long && !more ? 'fade-clamp' : ''}><Md text={text} /></div>
      {long && <button className="mt-1 text-xs underline" style={{ color: 'var(--accent)' }} onClick={() => setMore(!more)}>{more ? 'Show less' : 'Show more'}</button>}
    </div>
  )
}

function PlanCard({ card, live, onDecide }: { card: PlanCardBody; live: boolean; onDecide: (d: 'approve' | 'revise' | 'cancel', feedback: string) => Promise<void> }) {
  const [feedback, setFeedback] = useState('')
  const [revising, setRevising] = useState(false)
  const [busy, setBusy] = useState(false)
  const spec = card.spec
  const v = card.verification
  const act = async (d: 'approve' | 'revise' | 'cancel', fb = '') => { setBusy(true); try { await onDecide(d, fb) } finally { setBusy(false) } }
  return (
    <article className="glass r-lg my-3 p-5" id={`plan-${card.revision}`} aria-label={`Plan revision ${card.revision}`}>
      <div className="mono mb-2 flex flex-wrap gap-x-3 text-xs" style={{ color: 'var(--dim)' }}>
        <span>Plan for {card.repo}{card.revision ? ` · revision ${card.revision}` : ''}</span>
        <span className="tnum ml-auto">planning {usd(card.cost_usd)}</span>
      </div>
      {spec ? (
        <>
          <h3 className="text-lg font-semibold tracking-tight">{spec.title}</h3>
          <Summary text={spec.summary} />
          <div className="grid gap-x-6 md:grid-cols-2">
            <Section icon={<ListTodo size={13} />} title="Steps">
              <ol className="list-decimal space-y-1 pl-5 text-sm">{spec.plan.map((s) => <li key={s}>{s}</li>)}</ol>
            </Section>
            <Section icon={<FileCode size={13} />} title={`Files (${spec.relevant_files.length})`}>
              <ul className="space-y-1 text-sm">
                {spec.relevant_files.map((f) => (
                  <li key={f.path} className="min-w-0 break-words"><code className="mono">{f.path}</code><span className="block text-xs" style={{ color: 'var(--dim)' }}>{f.why}</span></li>
                ))}
              </ul>
            </Section>
          </div>
          {spec.test_plan.length > 0 && (
            <Section icon={<FlaskConical size={13} />} title="Tests">
              <ul className="list-disc space-y-1 pl-5 text-sm">{spec.test_plan.map((s) => <li key={s}>{s}</li>)}</ul>
            </Section>
          )}
          <div className="mt-4 space-y-1" style={{ boxShadow: 'inset 0 1px 0 var(--line)', paddingTop: 8 }}>
            {v && (
              <Fold icon={v.approved ? <ShieldCheck size={13} /> : <TriangleAlert size={13} />} title="Verifier"
                badge={v.approved ? 'checked against the code' : `${v.corrections.length} correction${v.corrections.length === 1 ? '' : 's'}`}
                tone={v.approved ? 'var(--ok)' : 'var(--warn)'}>
                {v.approved ? <p className="text-sm" style={{ color: 'var(--muted)' }}>The verifier opened the files this plan names and found it ready to hand over.</p>
                  : <ul className="list-disc space-y-1 pl-5 text-sm">{v.corrections.map((c) => <li key={c}>{c}</li>)}</ul>}
                {v.evidence && <p className="mt-2 text-xs" style={{ color: 'var(--dim)' }}>{v.evidence}</p>}
              </Fold>
            )}
            {spec.acceptance_criteria.length > 0 && (
              <Fold icon={<CircleCheck size={13} />} title="Done when" badge={`${spec.acceptance_criteria.length}`}>
                <ul className="space-y-1 text-sm">{spec.acceptance_criteria.map((s) => <li key={s} className="flex gap-2"><span style={{ color: 'var(--ok)' }} aria-hidden>✓</span>{s}</li>)}</ul>
              </Fold>
            )}
            {spec.risks.length > 0 && (
              <Fold icon={<TriangleAlert size={13} />} title="Risks" badge={`${spec.risks.length}`}>
                <ul className="list-disc space-y-1 pl-5 text-sm" style={{ color: 'var(--muted)' }}>{spec.risks.map((s) => <li key={s}>{s}</li>)}</ul>
              </Fold>
            )}
          </div>
          {spec.questions.length > 0 && (
            <div className="r-md mt-4 px-3 py-2 text-sm" style={{ background: 'var(--hover)', boxShadow: 'inset 3px 0 0 var(--warn)' }}>
              <div className="mb-1 text-xs font-medium" style={{ color: 'var(--warn)' }}>Open questions: answer them in the chat below</div>
              <ul className="list-disc pl-5">{spec.questions.map((q) => <li key={q}>{q}</li>)}</ul>
            </div>
          )}
        </>
      ) : <Md text={card.plan_markdown} />}
      {live && (
        <div className="mt-5 space-y-2">
          {revising && (
            <textarea className="field r-sm w-full p-2.5 text-sm outline-none" rows={3} autoFocus placeholder="What should change? Answer any open questions here."
              value={feedback} onChange={(e) => setFeedback(e.target.value)} aria-label="Revision feedback" />
          )}
          <div className="flex flex-wrap gap-2">
            <Btn onClick={() => act('approve')} disabled={busy}>Approve and open issue</Btn>
            {revising
              ? <Btn secondary onClick={() => feedback.trim() && act('revise', feedback)} disabled={busy || !feedback.trim()}>Send feedback</Btn>
              : <Btn secondary onClick={() => setRevising(true)} disabled={busy}>Revise</Btn>}
            <Btn secondary onClick={() => act('cancel')} disabled={busy}>Cancel</Btn>
          </div>
        </div>
      )}
    </article>
  )
}

function PlanningSkeleton({ stage }: { stage: string }) {
  if (stage === 'answering') return <p className="my-3 animate-pulse text-sm" style={{ color: 'var(--muted)' }}>Reading the repository…</p>
  const label = stage === 'verifying' ? 'The verifier is checking the plan against the code…' : 'The describer is reading the repository and drafting a plan…'
  return (
    <div className="glass r-lg my-3 p-5" aria-busy="true" aria-label="Planning">
      <p className="mb-4 text-sm" style={{ color: 'var(--muted)' }}>{label}</p>
      <div className="skeleton mb-3 h-5 w-2/3" />
      <div className="skeleton mb-2 h-3 w-full" />
      <div className="skeleton mb-5 h-3 w-5/6" />
      <div className="grid gap-4 md:grid-cols-2">
        {[0, 1].map((i) => <div key={i} className="space-y-2"><div className="skeleton h-3 w-24" /><div className="skeleton h-3 w-full" /><div className="skeleton h-3 w-4/5" /><div className="skeleton h-3 w-3/5" /></div>)}
      </div>
    </div>
  )
}

function useMedia(query: string) {
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
function GrowingInput({ value, onChange, onSend, placeholder, minRows = 2, label }: {
  value: string; onChange: (v: string) => void; onSend: () => void; placeholder: string; minRows?: number; label: string
}) {
  const ref = useRef<HTMLTextAreaElement>(null)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, window.innerHeight * 0.4)}px`
  }, [value])
  return (
    <textarea ref={ref} className="w-full resize-none bg-transparent px-3 py-2.5 text-[15px] leading-relaxed outline-none" rows={minRows}
      placeholder={placeholder} aria-label={label} value={value} onChange={(e) => onChange(e.target.value)}
      onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); onSend() } }} />
  )
}

/** A comment or review from the pull request, copied into the chat (intake.sync_github). */
function GitHubComment({ item }: { item: { author: string; body: string; url: string; kind: string; state?: string; path?: string; line?: number } }) {
  const verdict = item.state === 'APPROVED' ? { text: 'approved', color: 'var(--ok)' }
    : item.state === 'CHANGES_REQUESTED' ? { text: 'requested changes', color: 'var(--warn)' } : null
  return (
    <div className="r-lg my-3 px-4 py-3 text-sm" style={{ background: 'var(--hover)', boxShadow: 'inset 3px 0 0 var(--accent-deep)' }}>
      <div className="mb-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs" style={{ color: 'var(--muted)' }}>
        <GitPullRequest size={13} aria-hidden />
        <span className="font-medium" style={{ color: 'var(--text)' }}>@{item.author}</span>
        <span>{verdict ? <span style={{ color: verdict.color }}>{verdict.text}</span> : item.kind === 'review_comment' ? 'commented on the diff' : 'commented'} on GitHub</span>
        {item.path && <code className="mono">{item.path}{item.line ? `:${item.line}` : ''}</code>}
        {item.url && <a className="ml-auto underline" style={{ color: 'var(--accent)' }} href={item.url} target="_blank" rel="noreferrer">View on GitHub</a>}
      </div>
      {item.body?.trim() && <Md text={item.body} />}
    </div>
  )
}

// ---------- one request ----------

function Thread({ thread }: { thread: string }) {
  const [msgs, setMsgs] = useState<ThreadMessage[]>([])
  const [req, setReq] = useState<RequestRow | null>(null)
  const last = useRef(0)
  const bottom = useRef<HTMLDivElement>(null)
  const docked = useMedia('(orientation: landscape) and (min-width: 1024px)')
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
  useEffect(() => { bottom.current?.scrollIntoView({ block: 'end' }) }, [msgs.length])
  const events = useActivity(req?.id, !!req && LIVE.has(req.state))
  const items = useMemo(() => (req ? steps(req, events) : []), [req, events])
  const planStage = events.filter((e) => e.run_id.startsWith('req-') && e.type === 'state').at(-1)?.payload.state ?? 'describing'
  const isTask = !!req && req.state !== 'answered' && !(req.state === 'failed' && !req.plan)

  const lastPlan = msgs.filter((m) => m.kind === 'plan').at(-1)
  const decide = async (d: 'approve' | 'revise' | 'cancel', feedback: string) => {
    if (!req) return
    try {
      const r = await team.decide(req.id, d, feedback)
      toast(r.message, d === 'cancel' ? 'info' : 'ok')
    } catch (e) {
      toast(`Could not send that: ${String(e).slice(0, 200)}`, 'err')
    }
  }
  return (
    <div className="flex h-full min-h-0 gap-3">
      <div className="flex min-h-0 min-w-0 flex-1 flex-col px-4 pt-12">
        <div className="min-h-0 flex-1 overflow-auto pb-6">
          <div className="mx-auto w-full max-w-4xl">
            {req && isTask && !docked && <RequestHeader req={req} items={items} />}
            {msgs.map((m) =>
              m.kind === 'user' ? (
                <div key={m.id} className="my-3 flex justify-end"><div className="glass r-lg max-w-[80%] whitespace-pre-wrap px-4 py-2 text-sm">{m.body.text}</div></div>
              ) : m.kind === 'plan' ? (
                <PlanCard key={m.id} card={m.body} live={m === lastPlan && req?.state === 'awaiting_approval'} onDecide={decide} />
              ) : m.kind === 'github' ? (
                <GitHubComment key={m.id} item={m.body} />
              ) : (
                <div key={m.id} className="reply my-3 text-[15px] leading-relaxed"><Md text={m.body.text} /></div>
              ),
            )}
            {req?.state === 'planning' && <PlanningSkeleton stage={planStage} />}
            {!docked && <div className="mt-4"><RunnerPanel events={events} /></div>}
            <div ref={bottom} />
          </div>
        </div>
        {req?.state !== 'planning' && msgs.length > 0 && <div className="mx-auto w-full max-w-4xl"><FollowUp thread={thread} req={req} /></div>}
      </div>
      {docked && req && isTask && (
        <aside className="w-[340px] shrink-0 space-y-3 overflow-auto pt-12 pb-3 xl:w-[380px]" aria-label="Lifecycle">
          <RequestHeader req={req} items={items} vertical />
          <RunnerPanel events={events} compact />
        </aside>
      )}
    </div>
  )
}

const FOLLOW_UP_HINT: Record<string, string> = {
  awaiting_approval: 'Ask about the plan, ask for changes, or say "approve"…',
  pr_open: 'Ask about the PR, or ask for a change: it goes to the PR as a review comment…',
  pr_draft: 'Ask about the PR, or ask for a change: it goes to the PR as a review comment…',
  queued: 'Ask a question while the task waits for a runner…',
  working: 'Ask a question while the runner works…',
}

/** The thread's message box: questions, decisions on a waiting plan, or the next task. */
function FollowUp({ thread, req }: { thread: string; req: RequestRow | null }) {
  const [repo, setRepo] = useState(req?.repo || (localStorage.getItem('pw.repo') ?? ''))
  const [task, setTask] = useState('')
  const [busy, setBusy] = useState(false)
  const [repos, setRepos] = useState<RepoInfo[]>([])
  useEffect(() => { team.repos().then(setRepos).catch(() => {}) }, [])
  const askRepo = !req // once the thread has a request, its repository is implied
  const badRepo = askRepo && repo.trim() !== '' && !repo.includes('/')
  const send = async () => {
    if (!task.trim() || badRepo || busy) return
    setBusy(true)
    try { await team.create(askRepo ? repo.trim() : '', task.trim(), thread); setTask('') }
    catch (e) { toast(`Could not send that: ${String(e).slice(0, 200)}`, 'err') }
    finally { setBusy(false) }
  }
  return (
    <div className="glass r-lg mb-3 shrink-0 p-2">
      {askRepo && (
        <div className="flex gap-2 px-1 pb-1">
          <input className="field mono r-sm w-56 shrink-0 px-2.5 py-1.5 text-xs outline-none" list="pw-repos-followup" value={repo}
            onChange={(e) => setRepo(e.target.value)} placeholder="owner/repository" aria-label="Repository" aria-invalid={badRepo} />
          <datalist id="pw-repos-followup">{repos.map((r) => <option key={r.repo} value={r.repo}>{r.description}</option>)}</datalist>
        </div>
      )}
      <GrowingInput value={task} onChange={setTask} onSend={send} label="Message"
        placeholder={FOLLOW_UP_HINT[req?.state ?? ''] ?? 'Ask a question, or describe the next change…'} />
      <div className="flex items-center gap-2 px-1">
        <span className="hidden text-[11px] sm:inline" style={{ color: 'var(--dim)' }}>Enter to send · Shift+Enter for a new line{req ? ` · ${req.repo}` : ''}</span>
        <span className="ml-auto" />
        <Btn onClick={send} disabled={busy || !task.trim() || badRepo}>{busy ? 'Sending…' : 'Send'}</Btn>
      </div>
      {badRepo && <p className="px-1 text-xs" style={{ color: 'var(--err)' }}>Use the owner/repository form.</p>}
    </div>
  )
}

const EXAMPLES = ['Give me a summary of this project', 'Add a /health endpoint that returns the app version', 'Fix the flaky date test in tests/test_reports.py']

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
    <div className="mx-auto flex h-full w-full max-w-4xl flex-col justify-center gap-3 px-4">
      <h1 className="mb-1 text-center text-3xl font-semibold tracking-tight sm:text-4xl">What should we ship?</h1>
      <p className="mx-auto mb-4 max-w-2xl text-center text-sm" style={{ color: 'var(--muted)' }}>
        Ask about a repository and get an answer here. Ask for a change and you get a plan; once you approve it, a runner opens a PR that tags you.
      </p>
      <div className="glass r-lg p-2">
        <div className="px-1 pb-1">
          <input className="field mono r-sm w-full px-3 py-2 text-sm outline-none" list="pw-repos" value={repo} onChange={(e) => setRepo(e.target.value)}
            placeholder="owner/repository (leave empty: I'll pick it from the task)" aria-label="Repository" aria-invalid={badRepo} />
          <datalist id="pw-repos">{repos.map((r) => <option key={r.repo} value={r.repo}>{r.description}</option>)}</datalist>
          {badRepo && <p className="px-1 pt-1 text-xs" style={{ color: 'var(--err)' }}>Use the owner/repository form.</p>}
        </div>
        <GrowingInput value={task} onChange={setTask} onSend={send} minRows={4} label="Task" placeholder="Ask a question or describe a change" />
        <div className="flex items-center gap-2 px-1">
          <span className="hidden text-xs sm:inline" style={{ color: 'var(--dim)' }}>Enter to send · Shift+Enter for a new line</span>
          <span className="ml-auto" />
          <Btn onClick={send} disabled={busy || !task.trim() || badRepo}>{busy ? 'Sending…' : 'Send'}</Btn>
        </div>
      </div>
      <div className="flex flex-wrap justify-center gap-2">
        {EXAMPLES.map((x) => <button key={x} className="chip hov r-sm px-3 py-1 text-xs" onClick={() => setTask(x)}>{x}</button>)}
      </div>
    </div>
  )
}

// ---------- the queue ----------

export function StateBadge({ state }: { state: string }) {
  const c = stateColor(state)
  return (
    <span className="inline-flex items-center gap-1.5 text-xs" style={{ color: c }}>
      <span className={`h-1.5 w-1.5 rounded-full ${LIVE.has(state) ? 'pulse-dot' : ''}`} style={{ background: c }} aria-hidden />
      {STATE_LABEL[state] ?? state}
    </span>
  )
}

export function QueueView() {
  const [rows, setRows] = useState<RequestRow[] | null>(null)
  useEffect(() => { const f = () => team.queue().then(setRows).catch(() => setRows((r) => r ?? [])); f(); const t = setInterval(f, 5000); return () => clearInterval(t) }, [])
  return (
    <div className="mx-auto max-w-5xl px-4 pt-12 pb-10">
      <h2 className="mb-1 text-xl font-semibold">Queue</h2>
      <p className="mb-4 text-sm" style={{ color: 'var(--muted)' }}>Approved issues are worked one at a time, oldest first.</p>
      {rows === null ? <div className="space-y-2">{[0, 1, 2].map((i) => <div key={i} className="skeleton h-12" />)}</div> : !rows.length ? (
        <div className="glass r-lg flex flex-col items-center gap-2 px-4 py-12 text-center">
          <Inbox size={28} style={{ color: 'var(--dim)' }} />
          <p className="font-medium">Nothing queued</p>
          <p className="text-sm" style={{ color: 'var(--muted)' }}>Approve a plan and its issue shows up here until the PR is open.</p>
        </div>
      ) : (
        <>
          <table className="hidden w-full text-sm md:table">
            <thead><tr className="text-left text-xs" style={{ color: 'var(--dim)' }}><th className="py-2 font-normal">Issue</th><th className="font-normal">Task</th><th className="font-normal">Requested by</th><th className="font-normal">State</th><th className="font-normal">PR</th><th className="text-right font-normal">Cost</th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} style={{ boxShadow: 'inset 0 1px 0 var(--line)' }}>
                  <td className="mono py-2.5">{r.issue_url ? <a className="underline" href={r.issue_url} target="_blank" rel="noreferrer">{r.repo}#{r.issue_number}</a> : r.repo}</td>
                  <td className="max-w-xs truncate">{r.task}</td>
                  <td>{r.requester_name} <span className="mono text-xs" style={{ color: 'var(--dim)' }}>@{r.requester_login} · {r.channel}</span></td>
                  <td><StateBadge state={r.state} /></td>
                  <td>{r.pr_url && <a className="inline-flex items-center gap-1 underline" href={r.pr_url} target="_blank" rel="noreferrer"><GitPullRequest size={13} />open</a>}</td>
                  <td className="mono tnum text-right">{usd(r.cost_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <ul className="space-y-2 md:hidden">
            {rows.map((r) => (
              <li key={r.id} className="glass r-md p-3 text-sm">
                <div className="flex items-center gap-2"><StateBadge state={r.state} /><span className="mono tnum ml-auto text-xs">{usd(r.cost_usd)}</span></div>
                <div className="mt-1.5">{r.task}</div>
                <div className="mono mt-1 flex flex-wrap gap-x-3 text-xs" style={{ color: 'var(--dim)' }}>
                  {r.issue_url ? <a className="underline" href={r.issue_url} target="_blank" rel="noreferrer">{r.repo}#{r.issue_number}</a> : <span>{r.repo}</span>}
                  <span>@{r.requester_login}</span>
                  {r.pr_url && <a className="underline" href={r.pr_url} target="_blank" rel="noreferrer">PR</a>}
                </div>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  )
}

// ---------- sidebar ----------

const FILTERS: Array<{ key: string; label: string; match: (s: string) => boolean }> = [
  { key: 'all', label: 'All', match: () => true },
  { key: 'active', label: 'Active', match: (s) => LIVE.has(s) },
  { key: 'review', label: 'To do', match: (s) => s === 'awaiting_approval' || s === 'pr_open' || s === 'pr_draft' },
  { key: 'done', label: 'Closed', match: (s) => s === 'failed' || s === 'cancelled' },
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
        <label className="field r-sm flex items-center gap-2 px-2.5 py-1.5">
          <Search size={13} style={{ color: 'var(--dim)' }} />
          <input className="w-full bg-transparent text-sm outline-none" placeholder="Search requests" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search requests" />
        </label>
        <div className="mt-2 flex gap-1" role="group" aria-label="Filter">
          {FILTERS.map((f) => (
            <button key={f.key} className="chip r-sm flex-1 px-1.5 py-0.5 text-[11px]" aria-pressed={filter === f.key}
              onClick={() => { localStorage.setItem('pw.filter', f.key); setFilter(f.key) }}>{f.label}</button>
          ))}
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-auto px-2 py-1">
        {rows === null && [0, 1, 2, 3].map((i) => <div key={i} className="skeleton mx-1 mb-2 h-11" />)}
        {rows !== null && !shown.length && <p className="px-3 py-6 text-center text-xs" style={{ color: 'var(--dim)' }}>{rows.length ? 'No requests match.' : 'No requests yet. Send one with + New.'}</p>}
        {shown.map((r) => {
          const thread = r.thread_ref?.thread as string | undefined
          return (
            <button key={r.id} className="hov r-sm mb-1 block w-full px-3 py-2 text-left text-sm transition"
              style={{ background: thread && sel === thread ? 'var(--sel)' : undefined }} aria-current={thread && sel === thread ? 'true' : undefined}
              onClick={() => onSelect(thread ?? null)} title={thread ? '' : `from ${r.channel}: reply in that thread`}>
              <div className="truncate">{r.task}</div>
              <div className="mt-0.5 flex items-center gap-2 text-[11px]" style={{ color: 'var(--dim)' }}>
                <StateBadge state={r.state} />
                {r.channel !== 'web' && <span>· {r.channel}</span>}
                <span className="mono tnum ml-auto">{usd(r.cost_usd)}</span>
              </div>
            </button>
          )
        })}
      </div>
      <div className="flex items-center gap-2 px-4 pt-2 pb-4 text-xs" style={{ color: 'var(--muted)', boxShadow: 'inset 0 1px 0 var(--line)' }}>
        <span className="truncate">@{me.login}</span>
        <button className="hov r-sm ml-auto inline-flex items-center gap-1 px-1.5 py-1" onClick={() => team.logout().then(() => location.reload())}><LogOut size={12} /> Log out</button>
      </div>
    </>
  )
}

export function RequestsMain({ sel, onSelect }: { sel: string | null; onSelect: (thread: string) => void }) {
  return sel ? <Thread key={sel} thread={sel} /> : <Composer onSent={onSelect} />
}
