import DOMPurify from 'dompurify'
import { marked } from 'marked'
import { useEffect, useMemo, useRef, useState } from 'react'
import PromptComposer from '@/components/ui/prompt-composer'
import { api, type RunRow } from './api'
import { TraceView } from './Trace'
import { empty, k, reduce, usd, type Agent, type ToolCall, type Turn, type View } from './model'

// ---------- shared bits ----------

export function Md({ text }: { text: string }) {
  const html = useMemo(() => DOMPurify.sanitize(marked.parse(text, { async: false }) as string), [text])
  return <div className="md" dangerouslySetInnerHTML={{ __html: html }} />
}

export function Btn({ children, onClick, secondary }: { children: React.ReactNode; onClick: () => void; secondary?: boolean }) {
  return (
    <button className={`${secondary ? 'pill' : 'solid'} r-sm h-9 px-4 text-sm font-medium transition`} onClick={onClick}>
      {children}
    </button>
  )
}

// ---------- the chat for one thread (one or more runs that continue each other) ----------

const FINISHED = new Set(['done', 'failed', 'cancelled'])
const clean = (task: string) => task.replace(/^\[[^\]]+\]\s*/, '')

export function Chat({ runs, onSubmit }: { runs: RunRow[]; onSubmit: (text: string, kind: string, model: string, continueFrom?: string) => void }) {
  const [views, setViews] = useState<Record<string, View>>({})
  const [tab, setTab] = useState<'chat' | 'trace'>('chat')
  const bottom = useRef<HTMLDivElement>(null)
  const last = runs[runs.length - 1]
  const lv = views[last.id] ?? empty()
  const lastIds = runs.map((r) => views[r.id]?.lastId ?? 0).join(',')
  useEffect(() => { if (tab === 'chat') bottom.current?.scrollIntoView({ block: 'end' }) }, [lastIds, tab])

  const finished = FINISHED.has(lv.state)
  const revising = lv.pending?.kind === 'confirm_intake'
  const all = runs.map((r) => views[r.id]).filter(Boolean)

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex justify-center pt-2">
        <div className="glass flex rounded-full p-1 text-xs">
          {(['chat', 'trace'] as const).map((t) => (
            <button key={t} className="rounded-full px-3 py-1 capitalize transition" style={{ background: tab === t ? 'rgba(255,255,255,.12)' : 'transparent', color: tab === t ? 'var(--text)' : 'var(--muted)' }} onClick={() => setTab(t)}>
              {t}
            </button>
          ))}
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        {tab === 'trace' ? (
          <TraceView runs={runs} />
        ) : (
          <div className="mx-auto max-w-3xl px-5 pt-6 pb-4">
            {runs.map((r) => <RunTurn key={r.id} run={r} onView={(v) => setViews((p) => (p[r.id] === v ? p : { ...p, [r.id]: v }))} />)}
            <div ref={bottom} />
          </div>
        )}
      </div>
      <div className="mx-auto w-full max-w-3xl px-5 pb-4">
        {!finished && lv.state !== 'created' && (
          <div className="mb-2 flex justify-center">
            <Btn secondary onClick={() => api.cancel(last.id)}>Stop run</Btn>
          </div>
        )}
        {tab === 'chat' && finished && <NewTaskComposer placeholder="Continue this thread: ask for a follow-up, a fix, or to resume where it stopped…" onSubmit={(t, k, m) => onSubmit(t, k, m, last.id)} />}
        {tab === 'chat' && revising && (
          <PromptComposer
            prompts={[]}
            placeholder="Describe what to change in the plan, then press Enter…"
            platforms={[{ label: 'Revise plan', icon: 'chat' }]}
            models={['Opus 5.5']}
            simulate={false}
            hint={false}
            onSubmit={(s) => api.decide(last.id, false, s.prompt)}
          />
        )}
        <CostStrip views={all} />
      </div>
    </div>
  )
}

function RunTurn({ run, onView }: { run: RunRow; onView: (v: View) => void }) {
  const [v, setV] = useState<View>(empty)
  useEffect(() => { setV(empty()); return api.stream(run.id, (e) => setV((prev) => reduce(prev, e))) }, [run.id])
  useEffect(() => onView(v), [v])
  return (
    <>
      <UserMsg text={clean(run.task)} />
      <div className="mb-10"><AssistantTurn v={v} id={run.id} /></div>
    </>
  )
}

export const KINDS = [
  { label: 'Feature', icon: 'spark' as const },
  { label: 'Bug fix', icon: 'bolt' as const },
  { label: 'Refactor', icon: 'code' as const },
  { label: 'Review', icon: 'eye' as const },
]
export const MODELS = ['Sonnet 5.5', 'Opus 5.5', 'Haiku 5.5', 'Fable 5.2']
export const MODEL_IDS: Record<string, string> = { 'Sonnet 5.5': 'claude-sonnet-5-5', 'Opus 5.5': 'claude-opus-5-5', 'Haiku 5.5': 'claude-haiku-5-5', 'Fable 5.2': 'claude-fable-5-2' }
const EXAMPLES = [
  'Add rate limiting to the public API, with tests and a config flag...',
  'Fix the flaky checkout test and explain what was racing...',
  'Split the 900-line handlers file into one module per resource...',
  'Review the auth middleware for security issues and patch them...',
]

export function NewTaskComposer({ onSubmit, placeholder }: { onSubmit: (text: string, kind: string, model: string) => void; placeholder?: string }) {
  return (
    <PromptComposer
      prompts={EXAMPLES}
      placeholder={placeholder ?? 'Describe the task. Pullwright proposes a plan before touching code.'}
      platforms={KINDS}
      models={MODELS}
      modelNotes={['Balanced, default', 'Fastest, cheapest', 'Most capable, priciest']}
      simulate={false}
      hint
      onSubmit={(s) => onSubmit(s.prompt, s.platform, MODEL_IDS[s.model] ?? 'claude-opus-5-5')}
    />
  )
}

function UserMsg({ text }: { text: string }) {
  return (
    <div className="mb-8 flex justify-end">
      <div className="glass r-lg max-w-[85%] whitespace-pre-wrap px-5 py-3" style={{ borderBottomRightRadius: 8 }}>{text}</div>
    </div>
  )
}

function AssistantTurn({ v, id }: { v: View; id: string }) {
  const agents = v.order.map((a) => v.agents[a])
  const intake = agents.find((a) => a.name === 'intake')
  const summarizer = agents.find((a) => a.name === 'summarizer')
  const work = agents.filter((a) => !a.parent && a.name !== 'intake' && a.name !== 'summarizer')
  const supervisorText = agents.filter((a) => a.name === 'supervisor').map((a) => a.turns.map((t) => t.text).join('')).filter(Boolean).pop()

  return (
    <div className="flex gap-4">
      <div className="mt-1 h-7 w-7 shrink-0 r-sm" style={{ background: 'linear-gradient(160deg,#e8f6ff,#a9dcff 48%,#5fb2f2)', boxShadow: '0 4px 16px rgba(95,178,242,.42)' }} />
      <div className="min-w-0 flex-1 space-y-5">
        {intake && <Steps agents={[intake]} v={v} />}
        {v.intake ? (
          <Md text={`${v.intake.understanding}\n\n**Plan**\n${v.intake.plan.map((s: string, i: number) => `${i + 1}. ${s}`).join('\n')}${v.intake.questions?.length ? `\n\n**Questions**\n${v.intake.questions.map((q: string) => `- ${q}`).join('\n')}` : ''}`} />
        ) : v.state === 'intake' ? <Dots label="Reading the request" /> : null}
        {v.pending?.kind === 'confirm_intake' && (
          <div className="flex items-center gap-3">
            <Btn onClick={() => api.decide(id, true)}>Proceed</Btn>
            <span className="text-sm" style={{ color: 'var(--muted)' }}>or describe changes below</span>
          </div>
        )}
        {v.branch && <p className="mono text-xs" style={{ color: 'var(--dim)' }}>working on branch {v.branch}</p>}
        {work.length > 0 && <Steps agents={work} v={v} />}
        {v.pending?.kind === 'approve_command' && (
          <div className="glass r-md p-4">
            <p className="mb-2 text-sm font-medium">Allow this command?</p>
            <pre className="mb-3 r-sm p-3" style={{ background: 'var(--code)' }}>{v.pending.command}</pre>
            <div className="flex gap-2"><Btn onClick={() => api.decide(id, true)}>Allow</Btn><Btn secondary onClick={() => api.decide(id, false)}>Deny</Btn></div>
          </div>
        )}
        {v.state === 'working' && !v.pending && <Dots label="Working" />}
        {supervisorText && v.state !== 'working' && <Md text={supervisorText} />}
        {v.state === 'diagnosing' && <Dots label="Verifying the result against your request" />}
        {v.diagnosis && (
          <div className="flex items-start gap-2 text-sm" style={{ color: v.diagnosis.complete ? 'var(--ok)' : 'var(--warn)' }}>
            <span>{v.diagnosis.complete ? '✓' : '!'}</span>
            <div>
              <span className="font-medium">{v.diagnosis.complete ? 'Verified complete' : 'Gaps found'}</span>
              {v.diagnosis.gaps?.length > 0 && <ul className="mt-1 list-disc pl-5" style={{ color: 'var(--text)' }}>{v.diagnosis.gaps.map((g: string, i: number) => <li key={i}>{g}</li>)}</ul>}
            </div>
          </div>
        )}
        {v.state === 'summarizing' && <Dots label="Writing the summary" />}
        {v.summary && <Md text={v.summary.replace(/\nTITLE:.*$/s, '')} />}
        {summarizer && <Steps agents={[summarizer]} v={v} compact />}
        {v.pending?.kind === 'want_pr' && (
          <div className="flex items-center gap-2">
            <Btn onClick={() => api.decide(id, true)}>Open a PR</Btn>
            <Btn secondary onClick={() => api.decide(id, false)}>Keep it on the branch</Btn>
          </div>
        )}
        {v.pr && <p className="text-sm">PR opened: <a className="underline" style={{ color: 'var(--accent)' }} href={v.pr} target="_blank">{v.pr}</a></p>}
        {v.state === 'done' && !v.pr && v.branch && <p className="text-sm" style={{ color: 'var(--muted)' }}>Done. The work is committed on {v.branch}.</p>}
        {v.state === 'cancelled' && <p className="text-sm" style={{ color: 'var(--warn)' }}>Stopped. {v.branch ? `Any partial work is left uncommitted on ${v.branch}.` : ''}</p>}
        {v.error && <pre className="r-sm p-3 text-sm" style={{ background: 'var(--code)', color: 'var(--err)' }}>{v.error}</pre>}
      </div>
    </div>
  )
}

function Dots({ label }: { label: string }) {
  return <p className="animate-pulse text-sm" style={{ color: 'var(--muted)' }}>{label}…</p>
}

// ---------- activity steps ----------

function Steps({ agents, v, compact }: { agents: Agent[]; v: View; compact?: boolean }) {
  const kids = (id: string) => v.order.filter((a) => v.agents[a].parent === id).map((a) => v.agents[a])
  return <div className="space-y-1">{agents.map((a) => <AgentSteps key={a.id} a={a} kids={kids(a.id)} compact={compact} />)}</div>
}

function AgentSteps({ a, kids, depth = 0, compact }: { a: Agent; kids: Agent[]; depth?: number; compact?: boolean }) {
  const [open, setOpen] = useState(depth > 0 || !compact)
  const steps = a.turns.reduce((n, t) => n + t.tools.length, 0)
  const running = !a.stop
  if (steps === 0 && !a.turns.some((t) => t.thinking) && compact) return null
  const spawnIds = a.turns.flatMap((t) => t.tools.filter((c) => c.name === 'spawn_worker').map((c) => c.id))
  const childFor = (c: ToolCall) => (c.name === 'spawn_worker' ? kids[spawnIds.indexOf(c.id)] : undefined)
  return (
    <div style={{ marginLeft: depth * 16 }}>
      <button className="flex items-center gap-2 py-0.5 text-sm" style={{ color: 'var(--muted)' }} onClick={() => setOpen(!open)}>
        <span className="inline-block h-2 w-2 rounded-full" style={{ background: running ? 'var(--warn)' : a.stop === 'end_turn' ? 'var(--ok)' : 'var(--err)', boxShadow: running ? '0 0 10px var(--warn)' : undefined }} />
        <span className="font-medium" style={{ color: 'var(--text)' }}>{a.name}</span>
        <span className="mono text-xs" style={{ color: 'var(--dim)' }}>{a.model.replace('claude-', '')} · {steps} step{steps === 1 ? '' : 's'} · {usd(a.cost)}</span>
        <span className="text-xs">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="ml-1 pl-3" style={{ boxShadow: 'inset 1px 0 0 var(--line)' }}>
          {a.turns.map((t) => <TurnSteps key={t.n} t={t} childFor={childFor} />)}
        </div>
      )}
    </div>
  )
}

function TurnSteps({ t, childFor }: { t: Turn; childFor: (c: ToolCall) => Agent | undefined }) {
  return (
    <div className="my-1">
      {t.thinking && <Thinking text={t.thinking} />}
      {t.tools.map((c) => <Tool key={c.id} c={c} child={childFor(c)} />)}
    </div>
  )
}

function Thinking({ text }: { text: string }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="my-1 text-sm">
      <button className="italic" style={{ color: 'var(--dim)' }} onClick={() => setOpen(!open)}>{open ? 'Hide thinking' : 'Thought about it'} {open ? '▾' : '▸'}</button>
      {open && <pre className="mt-1 italic" style={{ color: 'var(--muted)' }}>{text}</pre>}
    </div>
  )
}

const brief = (c: ToolCall): string => {
  const i = c.input || {}
  switch (c.name) {
    case 'read_file': return `Read ${i.path}`
    case 'write_file': return `Write ${i.path}`
    case 'edit_file': return `Edit ${i.path}`
    case 'glob': return `Find ${i.pattern}`
    case 'grep': return `Search /${i.pattern}/ in ${i.path}`
    case 'bash': return `$ ${i.command}`
    case 'spawn_worker': return `Delegate: ${String(i.task).split('\n')[0]}`
    default: return `${c.name} ${JSON.stringify(i)}`
  }
}

function Tool({ c, child }: { c: ToolCall; child?: Agent }) {
  const [open, setOpen] = useState(false)
  const pending = c.output === undefined
  return (
    <div className="my-1">
      <button className="flex w-full items-start gap-2 r-sm px-1.5 py-0.5 text-left text-sm hover:bg-white/5" onClick={() => setOpen(!open)}>
        <span className="mono mt-0.5 shrink-0 text-xs" style={{ color: c.is_error ? 'var(--err)' : pending ? 'var(--warn)' : 'var(--accent)' }}>{pending ? '○' : c.is_error ? '✕' : '●'}</span>
        <span className="mono min-w-0 flex-1 truncate" style={{ color: 'var(--muted)' }}>{brief(c)}</span>
      </button>
      {open && (
        <div className="ml-5 mt-1 r-sm text-xs" style={{ background: 'var(--code)', boxShadow: 'inset 0 0 0 1px rgba(255,255,255,.07)' }}>
          <pre className="max-h-48 overflow-auto p-3">{JSON.stringify(c.input, null, 2)}</pre>
          {c.output !== undefined && <pre className="max-h-64 overflow-auto p-3" style={{ boxShadow: 'inset 0 1px 0 rgba(255,255,255,.07)', color: c.is_error ? 'var(--err)' : 'var(--text)' }}>{c.output}</pre>}
        </div>
      )}
      {child && <div className="ml-5"><AgentSteps a={child} kids={[]} depth={0} /></div>}
    </div>
  )
}

// ---------- cost strip ----------

function CostStrip({ views }: { views: View[] }) {
  const [open, setOpen] = useState(false)
  const rows = views.flatMap((v) => v.order.map((id) => v.agents[id]))
  const cost = views.reduce((n, v) => n + v.cost, 0)
  const tok = views.reduce((t, v) => ({ in: t.in + v.tokens.in, out: t.out + v.tokens.out, cache: t.cache + v.tokens.cache }), { in: 0, out: 0, cache: 0 })
  if (!rows.length) return null
  return (
    <div className="mt-2 text-xs" style={{ color: 'var(--dim)' }}>
      <button className="mono w-full py-1 text-center hover:text-white/80" onClick={() => setOpen(!open)}>
        {usd(cost)} · {k(tok.in)} in · {k(tok.out)} out · {k(tok.cache)} cached{views.length > 1 ? ` · ${views.length} runs` : ''} {open ? '▾' : '▸'}
      </button>
      {open && (
        <div className="glass r-md p-3">
          {rows.map((a) => (
            <div key={a.id} className="flex items-center gap-2 py-0.5" style={{ color: 'var(--muted)' }}>
              <span className="w-24 truncate">{a.name}</span>
              <span className="mono w-28">{a.model.replace('claude-', '')}</span>
              <div className="h-1 flex-1 rounded" style={{ background: 'rgba(255,255,255,.08)' }}><div className="h-1 rounded" style={{ width: `${cost ? (a.cost / cost) * 100 : 0}%`, background: 'linear-gradient(90deg,#5fb2f2,#e8f6ff)' }} /></div>
              <span className="mono w-16 text-right">{k(a.tokens.in + a.tokens.out)}</span>
              <span className="mono w-16 text-right">{usd(a.cost)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
