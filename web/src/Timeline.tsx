// A request's progress: a stepper from plan to PR, and a live panel for the runner (state, round, checks, agents,
// recent actions, spend). Built from /api/requests/{id}/activity, polled.
import { useEffect, useMemo, useRef, useState } from 'react'
import { Bot, Check, CircleX, GitPullRequest, ListChecks, LoaderCircle, Terminal } from 'lucide-react'
import { team, type Activity, type RequestRow } from './api'
import { usd } from './model'

type StepState = 'done' | 'active' | 'todo' | 'failed' | 'warn'
type Step = { key: string; label: string; detail: string; state: StepState }

const PLAN_LABEL: Record<string, string> = {
  describing: 'Describer is reading the repository', verifying: 'Verifier is checking the plan', awaiting_approval: 'Waiting for approval',
}
const RUN_LABEL: Record<string, string> = {
  setup: 'Installing dependencies', working: 'Supervisor and workers are implementing', checking: 'Running the checks',
  reviewing: 'Independent review', green: 'All checks pass', failed: 'Stopped: checks failing', budget: 'Stopped: budget reached',
  setup_failed: 'Setup failed', cancelled: 'Cancelled',
}

export function useActivity(rid: string | undefined, live: boolean) {
  const [events, setEvents] = useState<Activity[]>([])
  const last = useRef(0)
  useEffect(() => {
    setEvents([]); last.current = 0
    if (!rid) return
    let stop = false
    const poll = async () => {
      try {
        const r = await team.activity(rid, last.current)
        if (stop || !r.events.length) return
        last.current = r.events[r.events.length - 1].id
        setEvents((e) => [...e, ...r.events])
      } catch { /* keep polling */ }
    }
    poll()
    const id = setInterval(poll, live ? 2000 : 15000)
    return () => { stop = true; clearInterval(id) }
  }, [rid, live])
  return events
}

const isPlan = (e: Activity) => e.run_id.startsWith('req-')

export function steps(req: RequestRow, events: Activity[]): Step[] {
  const s = req.state
  const planStates = events.filter((e) => isPlan(e) && e.type === 'state').map((e) => e.payload.state as string)
  const runStates = events.filter((e) => !isPlan(e) && e.type === 'state').map((e) => e.payload.state as string)
  const lastPlan = planStates.at(-1) ?? 'describing'
  const lastRun = runStates.at(-1)
  const revisions = events.filter((e) => isPlan(e) && e.type === 'state' && e.payload.state === 'awaiting_approval').length
  const planned = !['planning', 'awaiting_approval'].includes(s) && !!req.issue_number
  const failedBeforeIssue = (s === 'failed' || s === 'cancelled') && !req.issue_number
  const rounds = runStates.filter((x) => x === 'checking').length

  const plan: Step = { key: 'plan', label: 'Plan', state: planned ? 'done' : failedBeforeIssue && s === 'failed' ? 'failed' : s === 'planning' ? 'active' : planned || s === 'awaiting_approval' ? 'done' : 'todo',
    detail: planned || s === 'awaiting_approval' ? `${Math.max(revisions, 1)} revision${revisions > 1 ? 's' : ''}` : s === 'planning' ? PLAN_LABEL[lastPlan] ?? 'Planning' : '' }
  const approve: Step = { key: 'approve', label: 'Approve', state: planned ? 'done' : s === 'awaiting_approval' ? 'active' : failedBeforeIssue && s === 'cancelled' ? 'failed' : 'todo',
    detail: planned ? `issue #${req.issue_number}` : s === 'awaiting_approval' ? 'needs a write-access approver' : s === 'cancelled' && !req.issue_number ? 'cancelled' : '' }
  const queue: Step = { key: 'queue', label: 'Queue', state: s === 'queued' ? 'active' : ['working', 'pr_open', 'pr_draft'].includes(s) || (s === 'failed' && !!req.issue_number) ? 'done' : 'todo',
    detail: s === 'queued' ? 'waiting for a runner' : '' }
  const runFailed = s === 'failed' && !!req.issue_number
  const runner: Step = { key: 'runner', label: 'Runner', state: s === 'working' ? 'active' : ['pr_open', 'pr_draft'].includes(s) ? (s === 'pr_draft' ? 'warn' : 'done') : runFailed ? 'failed' : 'todo',
    detail: s === 'working' ? `${RUN_LABEL[lastRun ?? 'setup'] ?? lastRun}${rounds ? ` · round ${rounds}` : ''}` : lastRun ? `${RUN_LABEL[lastRun] ?? lastRun}${rounds ? `, ${rounds} round${rounds > 1 ? 's' : ''}` : ''}` : '' }
  const pr: Step = { key: 'pr', label: 'Pull request', state: s === 'pr_open' ? 'done' : s === 'pr_draft' ? 'warn' : 'todo',
    detail: s === 'pr_open' ? 'ready for review' : s === 'pr_draft' ? 'draft: needs attention' : '' }
  return [plan, approve, queue, runner, pr]
}

const STEP_COLOR: Record<StepState, string> = { done: 'var(--ok)', active: 'var(--accent-deep)', todo: 'var(--dim)', failed: 'var(--err)', warn: 'var(--warn)' }

function StepIcon({ state }: { state: StepState }) {
  const c = STEP_COLOR[state]
  const ring = { boxShadow: `inset 0 0 0 1.5px ${c}`, color: c }
  if (state === 'done') return <span className="grid h-6 w-6 place-items-center rounded-full" style={ring}><Check size={14} strokeWidth={2.5} /></span>
  if (state === 'failed') return <span className="grid h-6 w-6 place-items-center rounded-full" style={ring}><CircleX size={14} /></span>
  if (state === 'active') return <span className="grid h-6 w-6 place-items-center rounded-full" style={ring}><LoaderCircle size={14} className="animate-spin" /></span>
  if (state === 'warn') return <span className="grid h-6 w-6 place-items-center rounded-full text-xs font-bold" style={ring}>!</span>
  return <span className="grid h-6 w-6 place-items-center rounded-full" style={ring}><span className="h-1.5 w-1.5 rounded-full" style={{ background: c }} /></span>
}

export function Stepper({ items }: { items: Step[] }) {
  return (
    <ol className="grid grid-cols-5 gap-1" aria-label="Progress">
      {items.map((st, i) => (
        <li key={st.key} className="relative flex min-w-0 flex-col items-center text-center" aria-current={st.state === 'active' ? 'step' : undefined}>
          {i > 0 && <span className="absolute top-3 h-px" style={{ left: '-50%', width: '100%', background: items[i - 1].state === 'done' ? 'var(--ok)' : 'var(--line)' }} aria-hidden />}
          <span className="relative z-10 rounded-full" style={{ background: 'var(--bg)' }}><StepIcon state={st.state} /></span>
          <span className="mt-1.5 text-xs font-medium" style={{ color: st.state === 'todo' ? 'var(--dim)' : 'var(--text)' }}>{st.label}</span>
          <span className="mt-0.5 line-clamp-2 hidden text-[11px] leading-tight sm:block" style={{ color: 'var(--dim)' }}>{st.detail}</span>
        </li>
      ))}
    </ol>
  )
}

type RunView = { id: string; feedback: boolean; state: string; rounds: number; cost: number; checksRound: number; checks: Array<{ name: string; ok: boolean; seconds: number }> | null
  agents: Array<{ id: string; name: string; model: string; ended: boolean }>; tools: Array<{ id: number; agent: string; name: string; hint: string }> }

function runViews(events: Activity[]): RunView[] {
  const runs = new Map<string, RunView>()
  for (const e of events) {
    if (isPlan(e)) continue
    let r = runs.get(e.run_id)
    if (!r) { r = { id: e.run_id, feedback: /-pr\d+-\d+$/.test(e.run_id), state: '', rounds: 0, cost: 0, checksRound: 0, checks: null, agents: [], tools: [] }; runs.set(e.run_id, r) }
    if (e.type === 'state') { r.state = e.payload.state; if (e.payload.state === 'checking') r.rounds += 1 }
    else if (e.type === 'checks') { r.checks = e.payload.results; r.checksRound = r.rounds }
    else if (e.type === 'usage') r.cost += Number(e.payload.cost_usd ?? 0)
    else if (e.type === 'agent_start') r.agents.push({ id: e.agent_id, name: e.payload.name, model: e.payload.model, ended: false })
    else if (e.type === 'agent_end') { const a = r.agents.find((x) => x.id === e.agent_id); if (a) a.ended = true }
    else if (e.type === 'tool_call') r.tools.push({ id: e.id, agent: r.agents.find((x) => x.id === e.agent_id)?.name ?? e.agent_id, name: e.payload.name, hint: e.payload.hint })
  }
  return [...runs.values()]
}

const FINAL = new Set(['green', 'failed', 'budget', 'setup_failed', 'cancelled'])

export function RunnerPanel({ events }: { events: Activity[] }) {
  const runs = useMemo(() => runViews(events), [events])
  if (!runs.length) return null
  return (
    <div className="space-y-3">
      {runs.map((r, i) => {
        const live = !FINAL.has(r.state)
        return (
          <section key={r.id} className="glass r-md p-4" aria-label={`Runner run ${i + 1}`}>
            <header className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
              <span className="flex items-center gap-2 font-medium">
                {live && <span className="pulse-dot h-2 w-2 rounded-full" style={{ background: 'var(--accent-deep)' }} aria-hidden />}
                {r.feedback ? `Review follow-up ${i}` : 'Implementation'}
              </span>
              <span style={{ color: r.state === 'green' ? 'var(--ok)' : FINAL.has(r.state) ? 'var(--err)' : 'var(--muted)' }}>{RUN_LABEL[r.state] ?? r.state}</span>
              <span className="mono tnum ml-auto text-xs" style={{ color: 'var(--dim)' }}>{r.rounds} round{r.rounds === 1 ? '' : 's'} · {usd(r.cost)}</span>
            </header>
            <div className="grid gap-4 md:grid-cols-2">
              <div>
                <h4 className="mb-1.5 flex items-center gap-1.5 text-xs font-medium" style={{ color: 'var(--muted)' }}><ListChecks size={13} /> Checks{r.checks && r.checksRound ? ` (round ${r.checksRound}${r.checksRound < r.rounds && live ? '; round ' + r.rounds + ' running' : ''})` : ''}</h4>
                {r.checks ? (
                  <ul className="space-y-1 text-sm">
                    {r.checks.map((c) => (
                      <li key={c.name} className="flex items-center gap-2">
                        {c.ok ? <Check size={14} style={{ color: 'var(--ok)' }} aria-label="pass" /> : <CircleX size={14} style={{ color: 'var(--err)' }} aria-label="fail" />}
                        <span>{c.name}</span>
                        <span className="mono tnum ml-auto text-xs" style={{ color: 'var(--dim)' }}>{c.seconds?.toFixed?.(1) ?? c.seconds}s</span>
                      </li>
                    ))}
                  </ul>
                ) : <p className="text-sm" style={{ color: 'var(--dim)' }}>Not run yet.</p>}
                <h4 className="mt-3 mb-1.5 flex items-center gap-1.5 text-xs font-medium" style={{ color: 'var(--muted)' }}><Bot size={13} /> Agents</h4>
                <ul className="space-y-1 text-sm">
                  {r.agents.map((a) => (
                    <li key={a.id} className="flex items-center gap-2">
                      <span className="h-1.5 w-1.5 rounded-full" style={{ background: a.ended ? 'var(--dim)' : 'var(--accent-deep)' }} aria-hidden />
                      <span>{a.name}</span>
                      <span className="mono ml-auto truncate text-xs" style={{ color: 'var(--dim)' }}>{a.model}</span>
                    </li>
                  ))}
                </ul>
              </div>
              <div className="min-w-0">
                <h4 className="mb-1.5 flex items-center gap-1.5 text-xs font-medium" style={{ color: 'var(--muted)' }}><Terminal size={13} /> Recent actions</h4>
                {r.tools.length ? (
                  <ul className="space-y-1">
                    {r.tools.slice(-8).map((t) => (
                      <li key={t.id} className="mono flex min-w-0 gap-2 text-xs">
                        <span className="shrink-0" style={{ color: 'var(--dim)' }}>{t.agent}</span>
                        <span className="shrink-0" style={{ color: 'var(--accent)' }}>{t.name}</span>
                        <span className="truncate" style={{ color: 'var(--muted)' }} title={t.hint}>{t.hint}</span>
                      </li>
                    ))}
                  </ul>
                ) : <p className="text-sm" style={{ color: 'var(--dim)' }}>Nothing yet.</p>}
              </div>
            </div>
          </section>
        )
      })}
    </div>
  )
}

export function RequestHeader({ req, items }: { req: RequestRow; items: Step[] }) {
  return (
    <div className="glass r-lg mb-4 p-4">
      <div className="mb-4 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        <span className="mono" style={{ color: 'var(--muted)' }}>{req.repo}</span>
        {req.issue_url && <a className="underline" style={{ color: 'var(--accent)' }} href={req.issue_url} target="_blank" rel="noreferrer">issue #{req.issue_number}</a>}
        {req.pr_url && <a className="inline-flex items-center gap-1 underline" style={{ color: 'var(--accent)' }} href={req.pr_url} target="_blank" rel="noreferrer"><GitPullRequest size={12} /> pull request</a>}
        <span className="mono tnum ml-auto" style={{ color: 'var(--dim)' }}>{usd(req.cost_usd)}</span>
      </div>
      <Stepper items={items} />
      {req.error && <p className="mt-3 text-sm" style={{ color: 'var(--err)' }}>{req.error}</p>}
    </div>
  )
}
