// One request's conversation: your messages, Pullwright's replies and plans, the agents' work as activity rails,
// GitHub comments, the approval card, and the lifecycle beside it. Polls /api/threads and /api/requests/{id}/activity.
import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowUp, BookOpen, Check, GitCommitHorizontal, ChevronDown, GitPullRequest, ListChecks, ShieldCheck, X } from 'lucide-react'
import { team, type Activity, type PlanCardBody, type RepoInfo, type RequestRow, type ThreadMessage } from './api'
import { usd } from './model'
import { steps, useActivity } from './Timeline'
import { ApprovalCard } from '@/components/ui/approval-card'
import { Markdown, StreamingText, ThinkingState, type TraceNode } from '@/components/ui/ai-thinking-and-tool-calls'
import { GrowingInput, Mark, ORB_FOR, Orb, clock, shortLinks, toast, useMedia } from './shared'

const LIVE = new Set(['planning', 'awaiting_approval', 'queued', 'working'])

// ---------- agent activity → rail nodes ----------

/** One tool call as a rail row. Names come from Pullwright's own tools, Claude Code and goose. */
function toolNode(e: Activity): TraceNode {
  const name = String(e.payload.name ?? 'tool')
  const hint = String(e.payload.hint ?? '')
  const n = name.toLowerCase()
  const id = String(e.id)
  if (n === 'git_log') return { id, type: 'tool', primary: 'Git log', secondary: hint || 'recent commits', icon: GitCommitHorizontal }
  if (n === 'git_show') return { id, type: 'tool', primary: 'Git show', secondary: hint || 'HEAD', icon: GitCommitHorizontal }
  if (/bash|shell|exec|command/.test(n)) return { id, type: 'terminal', command: hint || name }
  if (/^(read|read_file|view)$/.test(n) || n.endsWith('__read')) return { id, type: 'tool', toolName: 'read_file', args: { path: hint } }
  if (/^(write|write_file|create)$/.test(n)) return { id, type: 'tool', toolName: 'write_file', args: { path: hint } }
  if (/edit|str_replace|text_editor|multiedit|patch/.test(n)) return { id, type: 'tool', toolName: 'edit_file', args: { path: hint } }
  if (/grep|search/.test(n)) return { id, type: 'tool', toolName: 'search', args: { pattern: hint } }
  if (/glob|^ls$|list/.test(n)) return { id, type: 'tool', toolName: 'glob', args: { pattern: hint } }
  if (/spawn|task|agent/.test(n)) return { id, type: 'tool', toolName: 'agent', args: { description: hint } }
  if (/fetch/.test(n)) return { id, type: 'tool', toolName: 'fetch', args: { url: hint } }
  if (/todo/.test(n)) return { id, type: 'tool', toolName: 'todo', primary: 'Todo', secondary: hint }
  if (/skill/.test(n)) return { id, type: 'tool', primary: 'Skill', secondary: hint || name, icon: BookOpen }
  return { id, type: 'tool', primary: name, secondary: hint }
}

type Group = { key: string; kind: 'plan' | 'answer' | 'runner' | 'feedback'; ts: number; end: number; nodes: TraceNode[]; state: string; cost: number; checks?: Activity }

/** Activity events → one group per planning pass, chat answer or runner run. */
function groupActivity(events: Activity[]): Group[] {
  const groups: Group[] = []
  const runner = new Map<string, Group>()
  let plan: Group | null = null
  for (const e of events) {
    const isPlan = e.run_id.startsWith('req-')
    let g: Group | null
    if (isPlan) {
      if (e.type === 'state' && (e.payload.state === 'describing' || e.payload.state === 'answering') && (!plan || plan.state === 'closed' || e.payload.state === 'answering')) {
        plan = { key: `p${e.id}`, kind: e.payload.state === 'answering' ? 'answer' : 'plan', ts: e.ts, end: e.ts, nodes: [], state: e.payload.state, cost: 0 }
        groups.push(plan)
      }
      g = plan
    } else {
      g = runner.get(e.run_id) ?? null
      if (!g) {
        g = { key: e.run_id, kind: /-pr\d+-\d+$/.test(e.run_id) ? 'feedback' : 'runner', ts: e.ts, end: e.ts, nodes: [], state: 'setup', cost: 0 }
        runner.set(e.run_id, g)
        groups.push(g)
      }
    }
    if (!g) continue
    g.end = e.ts
    if (e.type === 'tool_call') g.nodes.push(toolNode(e))
    else if (e.type === 'usage') g.cost += Number(e.payload.cost_usd ?? 0)
    else if (e.type === 'state') {
      const s = String(e.payload.state)
      if (s === 'verifying') g.nodes.push({ id: String(e.id), type: 'tool', primary: 'Verify', secondary: 'the plan against the code', icon: ShieldCheck })
      if (s === 'reviewing') g.nodes.push({ id: String(e.id), type: 'tool', primary: 'Review', secondary: 'independent review against the plan', icon: ShieldCheck })
      if (['awaiting_approval', 'approved', 'cancelled'].includes(s) && isPlan) g.state = 'closed'
      else g.state = s
    } else if (e.type === 'checks') {
      const results = (e.payload.results ?? []) as Array<{ name: string; ok: boolean; seconds?: number }>
      const failed = results.filter((r) => !r.ok)
      g.checks = e
      g.nodes.push({
        id: String(e.id), type: 'tool', primary: 'Checks', icon: ListChecks, status: failed.length ? 'failed' : 'completed',
        secondary: failed.length ? `${failed.length} failing: ${failed.map((r) => r.name).join(', ')}` : results.map((r) => r.name).join(' · ') + ' pass',
        details: results.map((r) => ({ text: `${r.ok ? '✓' : '✗'} ${r.name}${r.seconds !== undefined ? `  ${Number(r.seconds).toFixed(1)}s` : ''}`, tone: r.ok ? 'add' as const : 'error' as const })),
      })
    } else if (e.type === 'error') {
      g.nodes.push({ id: String(e.id), type: 'tool', primary: 'Error', secondary: String(e.payload.reason ?? e.payload.message ?? 'error'), status: 'failed' })
    }
  }
  return groups
}

const RUN_FINAL = new Set(['green', 'failed', 'budget', 'setup_failed', 'cancelled'])
const groupLive = (g: Group, req: RequestRow | null) =>
  g.kind === 'runner' || g.kind === 'feedback' ? !RUN_FINAL.has(g.state) && (req?.state === 'working' || req?.state === 'queued')
    : g.state !== 'closed' && req?.state === 'planning'

// ---------- pieces of a conversation ----------

function Who({ name, ts, orb, extra }: { name: string; ts?: number; orb?: Parameters<typeof Orb>[0]['state']; extra?: string }) {
  return (
    <div className="flex items-center gap-2 text-[11.5px] leading-[18px]" style={{ color: 'var(--dim)' }}>
      {orb ? <Orb state={orb} size={20} /> : <Mark size={18} />}
      <span className="font-medium" style={{ color: 'var(--muted)' }}>{name}</span>
      {extra && <span>{extra}</span>}
      {ts !== undefined && <time>{clock(ts)}</time>}
    </div>
  )
}

function UserBubble({ m, repo }: { m: ThreadMessage; repo?: string }) {
  const raw = String(m.body.text ?? '')
  const text = repo && raw.startsWith(`${repo}: `) ? raw.slice(repo.length + 2) : raw // the web form's repo prefix
  return (
    <div className="msg-in flex flex-col items-end gap-1.5">
      <div className="max-w-[80%] rounded-[18px] px-4 py-2.5 text-[14px] leading-[22px] whitespace-pre-wrap" style={{ background: 'var(--bubble)' }}>{text}</div>
      <time className="pr-1.5 text-[11.5px]" style={{ color: 'var(--faint)' }}>{clock(m.ts)}</time>
    </div>
  )
}

const SUMMARY_RE = /^\s*\*\*(?:Summary|TL;DR)[:：]?\*\*:?\s*([\s\S]+?)(?:\n\s*\n([\s\S]*))?$/i

/** An answer leads with its summary, set apart, and the details follow. Plain replies render as they are. */
function Reply({ m, stream }: { m: ThreadMessage; stream: boolean }) {
  const text = shortLinks(String(m.body.text ?? ''))
  const sm = text.match(SUMMARY_RE)
  if (sm) {
    const [, lead, rest = ''] = sm
    return (
      <section className="msg-in flex flex-col gap-2" aria-label="Pullwright answer">
        <Who name="Pullwright" ts={m.ts} extra="answer" />
        <div className="rounded-[14px] px-5 py-4" style={{ background: 'var(--card)', boxShadow: 'var(--glass-shadow)' }}>
          <div className="mb-1 text-[12px] font-medium" style={{ color: 'var(--dim)' }}>Summary</div>
          <Markdown text={lead.trim()} className="text-[15px] leading-[23px] font-medium" />
        </div>
        {rest.trim() && (stream ? <StreamingText text={rest} speed={12} chunkSize={3} /> : <Markdown text={rest} />)}
      </section>
    )
  }
  return (
    <section className="msg-in flex flex-col gap-1.5" aria-label="Pullwright">
      <Who name="Pullwright" ts={m.ts} />
      {stream ? <StreamingText text={text} speed={12} chunkSize={3} /> : <Markdown text={text} />}
    </section>
  )
}

function GitHubComment({ m }: { m: ThreadMessage }) {
  const item = m.body as { author: string; body: string; url: string; kind: string; state?: string; path?: string; line?: number; pr_url?: string }
  const pr = item.pr_url?.match(/\/pull\/(\d+)/)?.[1]
  const verb = item.state === 'APPROVED' ? 'approved' : item.state === 'CHANGES_REQUESTED' ? 'requested changes on' : item.kind === 'review_comment' ? 'commented on the diff of' : 'commented on'
  return (
    <section className="msg-in flex flex-col gap-1.5" aria-label="Comment on GitHub">
      <div className="flex flex-wrap items-center gap-2 text-[11.5px] leading-[18px]" style={{ color: 'var(--dim)' }}>
        <span className="grid size-[18px] place-items-center rounded-full" style={{ background: 'var(--muted-bg)' }} aria-hidden><GitPullRequest size={11} style={{ color: 'var(--text)' }} /></span>
        <span className="font-medium" style={{ color: 'var(--muted)' }}>{item.author}</span>
        <span style={item.state === 'APPROVED' ? { color: 'var(--ok)' } : item.state === 'CHANGES_REQUESTED' ? { color: 'var(--warn)' } : undefined}>{verb} PR{pr ? ` #${pr}` : ''}</span>
        {item.path && <code className="text-[11px]">{item.path}{item.line ? `:${item.line}` : ''}</code>}
        <time>{clock(m.ts)}</time>
        {item.url && <a className="ml-auto underline decoration-current/30 underline-offset-2" href={item.url} target="_blank" rel="noreferrer">View on GitHub</a>}
      </div>
      {item.body?.trim() && <blockquote className="border-l-2 pl-3 text-[14px] leading-[22px]" style={{ borderColor: 'var(--muted-bg)', color: 'var(--muted)' }}><Markdown text={item.body} className="text-[var(--muted)]" /></blockquote>}
    </section>
  )
}

function ActivityGroup({ g, live }: { g: Group; live: boolean }) {
  const label = g.kind === 'runner' ? 'runner' : g.kind === 'feedback' ? 'review follow-up' : g.kind === 'answer' ? 'reading the repository' : 'planning'
  const orb = live ? ORB_FOR[g.state] ?? 'working' : null
  if (!g.nodes.length && !live) return null
  return (
    <section className="msg-in flex flex-col gap-1" aria-label={`Pullwright ${label}`}>
      <Who name="Pullwright" ts={g.ts} orb={orb} extra={label} />
      {g.nodes.length > 0
        ? <ThinkingState nodes={g.nodes} autoPlay={false} live={live} defaultExpanded={live ? true : undefined} />
        : <p className="shimmer-text pl-7 text-[12px]">Starting…</p>}
    </section>
  )
}

/** A long summary is cut to a few lines with "Show more". */
function Summary({ text }: { text: string }) {
  const [more, setMore] = useState(false)
  const long = text.length > 700
  return (
    <div>
      <div className={long && !more ? 'fade-clamp' : ''}><Markdown text={text} className="text-[14px] leading-[22px]" /></div>
      {long && <button className="mt-1 text-xs underline decoration-current/30 underline-offset-2" style={{ color: 'var(--muted)' }} onClick={() => setMore(!more)}>{more ? 'Show less' : 'Show more'}</button>}
    </div>
  )
}

function Pill({ children, open, onClick, tone }: { children: React.ReactNode; open?: boolean; onClick?: () => void; tone?: 'ok' | 'warn' }) {
  const bg = tone === 'ok' ? 'color-mix(in srgb, var(--ok) 12%, transparent)' : tone === 'warn' ? 'color-mix(in srgb, var(--warn) 12%, transparent)' : 'var(--secondary)'
  const fg = tone === 'ok' ? 'var(--ok)' : tone === 'warn' ? 'var(--warn)' : 'var(--muted)'
  return (
    <button type="button" aria-expanded={open} onClick={onClick} className="inline-flex h-7 items-center gap-1.5 rounded-full px-3 text-[12px] font-medium transition-colors hover:brightness-125"
      style={{ background: bg, color: fg }}>
      {children}
      {onClick && <ChevronDown size={11} className="transition-transform" style={{ transform: open ? 'rotate(180deg)' : undefined }} aria-hidden />}
    </button>
  )
}

/** A section of the plan: a small label, then its content, one column. */
function PlanSection({ label, count, children }: { label: string; count?: number; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-2.5">
      <h4 className="text-[12px] font-medium tracking-[0.01em]" style={{ color: 'var(--dim)' }}>{label}{count !== undefined && <span style={{ color: 'var(--faint)' }}> · {count}</span>}</h4>
      {children}
    </section>
  )
}

/** "Do X. Then details…" → a short lead the eye can scan, and the details under it. */
function splitStep(s: string): [string, string] {
  let code = false
  for (let k = 0; k < Math.min(s.length - 1, 170); k++) {
    const c = s[k]
    if (c === '`') code = !code
    else if (!code && k >= 12 && '.:;'.includes(c) && /\s/.test(s[k + 1]) && /[A-Z`("']/.test(s.slice(k + 1).trimStart()[0] ?? '')) {
      return [s.slice(0, c === '.' ? k + 1 : k), s.slice(k + 1).trim()]
    }
  }
  return [s, '']
}

const isNewFile = (why: string) => /^(to create|new( file)?|create[sd]?)\b/i.test(why.trim())
const isUnchanged = (why: string) => /\bno change\b|\bunchanged\b|\bread[- ]only\b|\bfor reference\b/i.test(why)

function PlanBlock({ card }: { card: PlanCardBody }) {
  const spec = card.spec
  const v = card.verification
  const [open, setOpen] = useState<'' | 'verifier' | 'done' | 'risks'>('')
  const [allFiles, setAllFiles] = useState(false)
  const toggle = (k: typeof open) => setOpen((o) => (o === k ? '' : k))
  if (!spec) return <div className="rounded-[14px] p-5" style={{ background: 'var(--card)', boxShadow: 'var(--glass-shadow)' }}><Markdown text={card.plan_markdown} /></div>
  const files = allFiles ? spec.relevant_files : spec.relevant_files.slice(0, 6)
  return (
    <article className="overflow-hidden rounded-[14px]" style={{ background: 'var(--card)', boxShadow: 'var(--glass-shadow)' }} aria-label={`Plan revision ${card.revision}`}>
      <header className="px-6 pt-5 pb-1">
        <div className="text-[11.5px]" style={{ color: 'var(--dim)' }}>Plan{card.revision ? ` · revision ${card.revision}` : ''} · <span className="mono text-[11.5px]">{usd(card.cost_usd)}</span></div>
        <h3 className="mt-1 text-[17px] leading-6 font-semibold tracking-[-0.015em]">{spec.title}</h3>
      </header>
      <div className="flex flex-col gap-6 px-6 pt-4 pb-5">
        <PlanSection label="What I found">
          <Summary text={spec.summary} />
        </PlanSection>

        <PlanSection label="What I'll do" count={spec.plan.length}>
          <ol className="flex flex-col gap-3">
            {spec.plan.map((s, i) => {
              const [lead, rest] = splitStep(s)
              return (
                <li key={i} className="flex gap-3">
                  <span className="tnum mt-px grid size-[22px] shrink-0 place-items-center rounded-full text-[11.5px] font-medium" style={{ background: 'var(--secondary)', color: 'var(--muted)' }}>{i + 1}</span>
                  <div className="min-w-0 flex-1">
                    <Markdown text={lead} className="text-[14px] leading-[22px] font-medium" />
                    {rest && <Markdown text={rest} className="mt-0.5 text-[13px] leading-[20px] text-[var(--muted)]" />}
                  </div>
                </li>
              )
            })}
          </ol>
        </PlanSection>

        {spec.relevant_files.length > 0 && (
          <PlanSection label="Files" count={spec.relevant_files.length}>
            <ul className="flex flex-col overflow-hidden rounded-[10px]" style={{ background: 'var(--bg)' }}>
              {files.map((f, i) => {
                const created = isNewFile(f.why)
                const kind = created ? 'new' : isUnchanged(f.why) ? 'read' : 'edit'
                return (
                  <li key={f.path} className="flex items-start gap-3 px-3.5 py-2.5" style={i ? { boxShadow: 'inset 0 1px 0 var(--border)' } : undefined}>
                    <span className="mt-[3px] w-9 shrink-0 rounded-full py-px text-center text-[10.5px] font-medium"
                      style={{ background: created ? 'color-mix(in srgb, var(--ok) 13%, transparent)' : kind === 'read' ? 'transparent' : 'var(--secondary)', color: created ? 'var(--ok)' : kind === 'read' ? 'var(--faint)' : 'var(--muted)', boxShadow: kind === 'read' ? 'inset 0 0 0 1px var(--border)' : undefined }}>
                      {kind}</span>
                    <div className="min-w-0 flex-1">
                      <code className="block truncate text-[12.5px]" title={f.path}>{f.path}</code>
                      <span className="block text-[12.5px] leading-[19px]" style={{ color: 'var(--dim)' }}>{f.why.replace(/^(to create|new file|new)[:.,]?\s*/i, '')}</span>
                    </div>
                  </li>
                )
              })}
            </ul>
            {spec.relevant_files.length > 6 && (
              <button type="button" className="self-start text-[12px] underline decoration-current/30 underline-offset-2" style={{ color: 'var(--muted)' }} onClick={() => setAllFiles(!allFiles)}>
                {allFiles ? 'Show fewer' : `Show all ${spec.relevant_files.length} files`}</button>
            )}
          </PlanSection>
        )}

        {spec.test_plan.length > 0 && (
          <PlanSection label="How I'll check it">
            <ul className="flex flex-col gap-1.5">{spec.test_plan.map((s, i) => (
              <li key={i} className="flex gap-2.5 text-[13px] leading-[20px]"><Check size={13} className="mt-[3px] shrink-0" style={{ color: 'var(--dim)' }} aria-hidden /><Markdown text={s} className="text-[13px] leading-[20px]" /></li>
            ))}</ul>
          </PlanSection>
        )}

        {spec.questions.length > 0 && (
          <div className="rounded-[10px] px-4 py-3 text-[13px]" style={{ background: 'color-mix(in srgb, var(--warn) 8%, transparent)' }}>
            <div className="mb-1 text-[12px] font-medium" style={{ color: 'var(--warn)' }}>Open questions: answer them in the chat</div>
            <ul className="flex flex-col gap-0.5">{spec.questions.map((q, i) => <li key={i} className="flex gap-2"><span style={{ color: 'var(--warn)' }}>•</span>{q}</li>)}</ul>
          </div>
        )}

        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap gap-1.5">
            {v && <Pill tone={v.approved ? 'ok' : 'warn'} open={open === 'verifier'} onClick={() => toggle('verifier')}>
              {v.approved ? <><Check size={12} aria-hidden /> Verifier checked it</> : `Verifier: ${v.corrections.length} correction${v.corrections.length === 1 ? '' : 's'}`}</Pill>}
            {spec.acceptance_criteria.length > 0 && <Pill open={open === 'done'} onClick={() => toggle('done')}>Done when · {spec.acceptance_criteria.length}</Pill>}
            {spec.risks.length > 0 && <Pill open={open === 'risks'} onClick={() => toggle('risks')}>Risks · {spec.risks.length}</Pill>}
          </div>
          {open && (
            <div className="msg-in text-[13px] leading-[21px]">
              {open === 'verifier' && v && (v.approved
                ? <p style={{ color: 'var(--muted)' }}>The verifier opened the files this plan names and found it ready to hand over.{v.evidence ? ` ${v.evidence}` : ''}</p>
                : <ul className="flex flex-col gap-1">{v.corrections.map((c, i) => <li key={i} className="flex gap-2"><span style={{ color: 'var(--warn)' }}>•</span>{c}</li>)}</ul>)}
              {open === 'done' && <ul className="flex flex-col gap-1">{spec.acceptance_criteria.map((c, i) => <li key={i} className="flex gap-2"><Check size={13} className="mt-1 shrink-0" style={{ color: 'var(--ok)' }} aria-hidden />{c}</li>)}</ul>}
              {open === 'risks' && <ul className="flex flex-col gap-1" style={{ color: 'var(--muted)' }}>{spec.risks.map((c, i) => <li key={i} className="flex gap-2"><span>•</span>{c}</li>)}</ul>}
            </div>
          )}
        </div>
      </div>
    </article>
  )
}

type Decision = 'approve' | 'revise' | 'cancel'

function Approval({ status, repo, onDecide, decidedAt }: { status: 'pending' | 'approved' | 'revised' | 'cancelled'; repo: string; decidedAt?: number; onDecide: (d: Decision, fb: string) => Promise<void> }) {
  const [revising, setRevising] = useState(false)
  const [feedback, setFeedback] = useState('')
  const [busy, setBusy] = useState(false)
  const act = async (d: Decision, fb = '') => { setBusy(true); try { await onDecide(d, fb) } finally { setBusy(false) } }
  if (status !== 'pending') {
    const done = status === 'approved'
    return (
      <ApprovalCard className="msg-in py-4" status={status} title={done ? 'Plan approved' : status === 'revised' ? 'Plan revised' : 'Plan cancelled'}
        icon={done ? <Check size={16} style={{ color: 'var(--ok)' }} /> : status === 'cancelled' ? <X size={16} /> : undefined}
        description={decidedAt ? `${done ? 'Approved' : 'Decided'} at ${clock(decidedAt)}` : undefined} />
    )
  }
  return (
    <ApprovalCard
      className="msg-in"
      title={revising ? 'What should change?' : 'Approve this plan?'}
      icon={<Orb state="breathing" size={20} />}
      description={revising ? 'Your feedback goes to the planner, and a new revision follows.'
        : <>Approving opens an issue in <code className="text-[12.5px]">{repo}</code> and starts a runner. Only people with write access can approve.</>}
      options={revising
        ? [{ label: busy ? 'Sending…' : 'Send feedback', value: 'send', disabled: busy || !feedback.trim() }, { label: 'Back', value: 'back' }]
        : [{ label: busy ? 'Approving…' : 'Approve', value: 'approve', disabled: busy }, { label: 'Revise', value: 'revise', disabled: busy }, { label: 'Cancel', value: 'cancel', disabled: busy }]}
      onDecision={(v) => {
        if (v === 'revise') setRevising(true)
        else if (v === 'back') setRevising(false)
        else if (v === 'send') act('revise', feedback.trim()).then(() => { setRevising(false); setFeedback('') })
        else act(v as Decision)
      }}
    >
      {revising && (
        <textarea autoFocus rows={3} value={feedback} onChange={(e) => setFeedback(e.target.value)} aria-label="Revision feedback"
          placeholder="What should change? Answer any open questions here."
          className="mt-3 w-full resize-none rounded-[10px] px-3 py-2 text-[13.5px] outline-none" style={{ background: 'var(--secondary)', color: 'var(--text)' }} />
      )}
    </ApprovalCard>
  )
}

/** What Pullwright is doing right now, at the bottom of the thread. */
function LiveStatus({ req, groups }: { req: RequestRow; groups: Group[] }) {
  const last = groups.at(-1)
  // until the first activity arrives we can't tell a question from a change: say the neutral thing
  let state = req.state === 'planning' ? (last?.state === 'verifying' ? 'verifying' : last?.kind === 'plan' && last.state !== 'closed' ? 'describing' : 'answering')
    : req.state === 'queued' ? 'queued' : req.state === 'working' ? (last?.state ?? 'working') : ''
  if (!state || RUN_FINAL.has(state)) return null
  if (req.state === 'working' && state === 'closed') state = 'working'
  const label: Record<string, string> = {
    describing: 'Reading the repository and drafting a plan', answering: 'Reading the repository', verifying: 'Checking the plan against the code',
    queued: 'Waiting for a runner', setup: 'Setting up the runner', working: 'Working on the change', checking: 'Running the checks', reviewing: 'Reviewing the change',
  }
  return (
    <div className="msg-in flex items-center gap-3 py-1" role="status" aria-live="polite">
      <Orb state={ORB_FOR[state] ?? 'working'} size={32} />
      <span className="shimmer-text text-[13px] font-medium">{label[state] ?? 'Working'}…</span>
    </div>
  )
}

// ---------- lifecycle ----------

function Lifecycle({ req, events, inline }: { req: RequestRow; events: Activity[]; inline?: boolean }) {
  const items = steps(req, events)
  const lastChecks = [...events].reverse().find((e) => e.type === 'checks')
  return (
    <aside className={inline ? 'flex w-full flex-col gap-3.5' : 'flex w-[300px] shrink-0 flex-col gap-3.5 overflow-auto px-5 pt-16 pb-6 xl:w-[320px]'} aria-label="Lifecycle">
      <section className="rounded-[14px] p-4" style={{ background: 'var(--card)', boxShadow: 'var(--glass-shadow)' }}>
        <div className="mb-3.5 flex items-baseline"><h2 className="text-[12.5px] font-semibold">Progress</h2><span className="mono ml-auto text-[11.5px]" style={{ color: 'var(--dim)' }}>{usd(req.cost_usd)}</span></div>
        <ol className="flex flex-col gap-3 text-[12.5px] leading-[18px]">
          {items.map((s) => (
            <li key={s.key} className="flex gap-2.5">
              <span aria-hidden className="mt-px grid size-[18px] shrink-0 place-items-center rounded-full" style={{
                background: s.state === 'done' ? 'color-mix(in srgb, var(--ok) 15%, transparent)' : s.state === 'failed' ? 'color-mix(in srgb, var(--err) 15%, transparent)' : 'transparent',
                boxShadow: s.state === 'active' ? 'inset 0 0 0 1.5px var(--muted)' : s.state === 'todo' ? 'inset 0 0 0 1.5px var(--muted-bg)' : s.state === 'warn' ? 'inset 0 0 0 1.5px var(--warn)' : undefined,
              }}>
                {s.state === 'done' && <Check size={11} strokeWidth={3} style={{ color: 'var(--ok)' }} />}
                {s.state === 'failed' && <X size={11} strokeWidth={3} style={{ color: 'var(--err)' }} />}
                {s.state === 'active' && <span className="pulse-dot size-1.5 rounded-full" style={{ background: 'var(--text)' }} />}
              </span>
              <span className="min-w-0" style={{ color: s.state === 'todo' ? 'var(--faint)' : undefined }}>{s.label}{s.detail && <span className="block truncate" style={{ color: 'var(--dim)' }}>{s.detail}</span>}</span>
            </li>
          ))}
        </ol>
        {(req.issue_url || req.pr_url) && (
          <div className="mt-4 flex gap-3 text-[12px]">
            {req.issue_url && <a className="underline decoration-current/30 underline-offset-2" href={req.issue_url} target="_blank" rel="noreferrer">Issue #{req.issue_number}</a>}
            {req.pr_url && <a className="underline decoration-current/30 underline-offset-2" href={req.pr_url} target="_blank" rel="noreferrer">Pull request</a>}
          </div>
        )}
      </section>
      {lastChecks && (
        <section className="rounded-[14px] p-4" style={{ background: 'var(--card)', boxShadow: 'var(--glass-shadow)' }}>
          <div className="mb-3 flex items-baseline gap-2"><h2 className="text-[12.5px] font-semibold">Last checks</h2>
            <span className="text-[12px]" style={{ color: lastChecks.payload.green ? 'var(--ok)' : 'var(--err)' }}>{lastChecks.payload.green ? 'green' : 'failing'}</span></div>
          <div className="mono flex flex-col gap-1.5 text-[12px] leading-[18px]" style={{ color: 'var(--muted)' }}>
            {(lastChecks.payload.results ?? []).map((c: { name: string; ok: boolean; seconds?: number }) => (
              <div key={c.name} className="flex"><span className="w-[18px]" style={{ color: c.ok ? 'var(--ok)' : 'var(--err)' }}>{c.ok ? '✓' : '✗'}</span>{c.name}
                <span className="ml-auto" style={{ color: 'var(--faint)' }}>{c.seconds !== undefined ? `${Number(c.seconds).toFixed(1)}s` : ''}</span></div>
            ))}
          </div>
        </section>
      )}
      {req.error && <p className="px-1 text-[12.5px]" style={{ color: 'var(--err)' }}>{req.error}</p>}
    </aside>
  )
}

// ---------- the thread ----------

type Item = { ts: number; key: string; node: React.ReactNode }

/** The plan's title once there is one; else the request's first sentence, kept short. */
function threadTitle(req: RequestRow | null, plan?: PlanCardBody): string {
  if (plan?.spec?.title) return plan.spec.title
  const first = (req?.task ?? 'Request').split('\n')[0].split(/(?<=[.!?:])\s/)[0]
  return first.length > 90 ? `${first.slice(0, 88)}…` : first
}

export function Thread({ thread }: { thread: string }) {
  const [msgs, setMsgs] = useState<ThreadMessage[]>([])
  const [req, setReq] = useState<RequestRow | null>(null)
  const last = useRef(0)
  const loadedOnce = useRef(false)
  const initialIds = useRef<Set<number>>(new Set())
  const scroller = useRef<HTMLDivElement>(null)
  const docked = useMedia('(orientation: landscape) and (min-width: 1100px)')
  useEffect(() => {
    let stop = false
    const poll = async () => {
      try {
        const t = await team.thread(thread, last.current)
        if (stop) return
        setReq(t.request)
        if (!loadedOnce.current) { t.messages.forEach((m) => initialIds.current.add(m.id)); loadedOnce.current = true }
        if (t.messages.length) {
          last.current = Math.max(last.current, t.messages[t.messages.length - 1].id)
          setMsgs((m) => { const seen = new Set(m.map((x) => x.id)); return [...m, ...t.messages.filter((x) => !seen.has(x.id))] }) // overlapping polls
        }
      } catch { /* keep polling */ }
    }
    poll()
    const id = setInterval(poll, 2000)
    return () => { stop = true; clearInterval(id) }
  }, [thread])
  const events = useActivity(req?.id, !!req && LIVE.has(req.state))
  const groups = useMemo(() => groupActivity(events), [events])
  const plans = msgs.filter((m) => m.kind === 'plan')
  const lastPlan = plans.at(-1)
  const isTask = !!req && req.state !== 'answered' && !(req.state === 'failed' && !req.plan)

  const decide = async (d: Decision, feedback: string) => {
    if (!req) return
    try {
      const r = await team.decide(req.id, d, feedback)
      toast(r.message, d === 'cancel' ? 'info' : 'ok')
    } catch (e) {
      toast(`Could not send that: ${String(e).slice(0, 200)}`, 'err')
    }
  }

  const items: Item[] = []
  msgs.forEach((m, i) => {
    if (m.kind === 'user') {
      const t = String(m.body.text ?? '').trim().toLowerCase()
      if (t === 'approve' || t === 'cancel') return // the approval card already says so
      items.push({ ts: m.ts, key: `m${m.id}`, node: <UserBubble m={m} repo={req?.repo} /> })
    } else if (m.kind === 'plan') {
      const card = m.body as PlanCardBody
      const isLast = m === lastPlan
      const status = isLast && req?.state === 'awaiting_approval' ? 'pending' as const
        : !isLast ? 'revised' as const : req?.state === 'cancelled' || req?.state === 'failed' ? 'cancelled' as const : 'approved' as const
      const decidedAt = msgs.slice(i + 1).find((x) => x.kind === 'user')?.ts
      items.push({
        ts: m.ts, key: `m${m.id}`, node: (
          <section className="msg-in flex flex-col gap-2.5" aria-label="Pullwright plan">
            <Who name="Pullwright" ts={m.ts} extra={card.revision ? `plan, revision ${card.revision}` : 'plan'} />
            <PlanBlock card={card} />
            <Approval status={status} repo={card.repo} decidedAt={status === 'pending' ? undefined : decidedAt} onDecide={decide} />
          </section>
        ),
      })
    } else if (m.kind === 'github') {
      // placed by when it was written on GitHub, not when the sync copied it
      const at = Date.parse(String(m.body.at ?? '')) / 1000
      const ts = Number.isFinite(at) ? at : m.ts
      items.push({ ts, key: `m${m.id}`, node: <GitHubComment m={{ ...m, ts }} /> })
    } else {
      items.push({ ts: m.ts, key: `m${m.id}`, node: <Reply m={m} stream={!initialIds.current.has(m.id) && loadedOnce.current} /> })
    }
  })
  const lastPlanGroup = [...groups].reverse().find((g) => g.kind === 'plan' || g.kind === 'answer')
  groups.forEach((g) => {
    // only the newest planning pass can still be running; a runner run is live until it reports a final state
    const live = groupLive(g, req) && (g.kind === 'runner' || g.kind === 'feedback' || g === lastPlanGroup)
    if (g.nodes.length || live) items.push({ ts: g.ts, key: g.key, node: <ActivityGroup g={g} live={live} /> })
  })
  items.sort((a, b) => a.ts - b.ts)

  useEffect(() => { scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: loadedOnce.current ? 'smooth' : 'auto' }) }, [items.length, req?.state])

  return (
    <div className="flex h-full min-h-0">
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <header className="flex flex-wrap items-center gap-2.5 px-14 pt-4 pb-2 sm:px-8 sm:pl-14">
          <h1 className="min-w-0 truncate text-[14px] font-semibold tracking-[-0.01em]" title={req?.task}>{threadTitle(req, lastPlan?.body as PlanCardBody | undefined)}</h1>
          {req && <span className="mono truncate text-[12px]" style={{ color: 'var(--dim)' }}>{req.repo}</span>}
          {req?.pr_url && <a href={req.pr_url} target="_blank" rel="noreferrer" className="ml-auto inline-flex h-6 items-center gap-1.5 rounded-full px-2.5 text-[12px]" style={{ background: 'var(--secondary)', color: 'var(--muted)' }}>
            <span className="size-1.5 rounded-full" style={{ background: req.state === 'pr_open' ? 'var(--ok)' : 'var(--warn)' }} aria-hidden />PR #{req.pr_url.split('/').pop()}</a>}
        </header>
        <div ref={scroller} className="min-h-0 flex-1 overflow-auto">
          <div className="mx-auto flex w-full max-w-[860px] flex-col gap-7 px-6 pt-6 pb-6">
            {items.map((it) => <div key={it.key}>{it.node}</div>)}
            {req && <LiveStatus req={req} groups={groups} />}
            {req && isTask && !docked && <div className="pt-2"><Lifecycle req={req} events={events} inline /></div>}
          </div>
        </div>
        {req?.state !== 'planning' && msgs.length > 0 && <FollowUp thread={thread} req={req} />}
      </div>
      {docked && req && isTask && <Lifecycle req={req} events={events} />}
    </div>
  )
}

const FOLLOW_UP_HINT: Record<string, string> = {
  awaiting_approval: 'Ask about the plan, ask for changes, or say "approve"',
  pr_open: 'Ask about the PR, or ask for a change',
  pr_draft: 'Ask about the PR, or ask for a change',
  queued: 'Ask a question while the task waits for a runner',
  working: 'Ask a question while the runner works',
}
const FOLLOW_UP_NOTE: Record<string, string> = {
  pr_open: 'Changes go to the PR as your review comment',
  pr_draft: 'Changes go to the PR as your review comment',
  awaiting_approval: 'Questions are answered; changes make a new revision',
}

/** The thread's message box: questions, decisions on a waiting plan, or the next task. */
function FollowUp({ thread, req }: { thread: string; req: RequestRow | null }) {
  const [repo, setRepo] = useState(req?.repo || (localStorage.getItem('pw.repo') ?? ''))
  const [task, setTask] = useState('')
  const [busy, setBusy] = useState(false)
  const [repos, setRepos] = useState<RepoInfo[]>([])
  useEffect(() => { team.repos().then(setRepos).catch(() => {}) }, [])
  const askRepo = !req
  const badRepo = askRepo && repo.trim() !== '' && !repo.includes('/')
  const send = async () => {
    if (!task.trim() || badRepo || busy) return
    setBusy(true)
    try { await team.create(askRepo ? repo.trim() : '', task.trim(), thread); setTask('') }
    catch (e) { toast(`Could not send that: ${String(e).slice(0, 200)}`, 'err') }
    finally { setBusy(false) }
  }
  return (
    <form className="mx-auto w-full max-w-[860px] shrink-0 px-6 pt-2 pb-6" onSubmit={(e) => { e.preventDefault(); send() }}>
      <div className="rounded-[18px] transition-shadow focus-within:shadow-[0_0_0_1px_var(--ring)]" style={{ background: 'var(--field)', boxShadow: '0 0 0 1px var(--border)' }}>
        {askRepo && (
          <div className="px-4 pt-3">
            <input className="mono w-full bg-transparent text-[12px] outline-none" list="pw-repos-followup" value={repo} onChange={(e) => setRepo(e.target.value)}
              placeholder="owner/repository" aria-label="Repository" aria-invalid={badRepo} />
            <datalist id="pw-repos-followup">{repos.map((r) => <option key={r.repo} value={r.repo}>{r.description}</option>)}</datalist>
          </div>
        )}
        <GrowingInput value={task} onChange={setTask} onSend={send} label="Message" minRows={1}
          placeholder={FOLLOW_UP_HINT[req?.state ?? ''] ?? 'Ask a question, or describe the next change'} />
        <div className="flex items-center gap-2 py-2 pr-2.5 pl-4">
          <span className="truncate text-[11.5px]" style={{ color: 'var(--faint)' }}>{FOLLOW_UP_NOTE[req?.state ?? ''] ?? 'Enter to send · Shift+Enter for a new line'}</span>
          <button type="submit" aria-label="Send" disabled={busy || !task.trim() || badRepo}
            className="ml-auto grid size-8 shrink-0 place-items-center rounded-full transition-[opacity,transform] active:scale-95 disabled:opacity-35"
            style={{ background: 'var(--primary)', color: 'var(--on-primary)' }}>
            <ArrowUp size={16} strokeWidth={2.2} className={busy ? 'animate-pulse' : undefined} />
          </button>
        </div>
      </div>
      {badRepo && <p className="px-2 pt-1 text-xs" style={{ color: 'var(--err)' }}>Use the owner/repository form.</p>}
    </form>
  )
}
