// Where the money goes: totals, spend per day, per stage, per model, per repository and requester, and the costliest
// requests. Data: GET /api/costs?days=N (costs.summarize on the server).
import { useEffect, useMemo, useRef, useState } from 'react'
import { Coins, ExternalLink, TriangleAlert } from 'lucide-react'
import { team, type CostSummary, type Costs as CostsT } from './api'

const STAGES = [
  { key: 'plan', label: 'Planning', color: 'var(--stage-plan)' },
  { key: 'work', label: 'Implementation', color: 'var(--stage-work)' },
  { key: 'review', label: 'Review', color: 'var(--stage-review)' },
  { key: 'other', label: 'Other', color: 'var(--stage-other)' },
] as const
const money = (x: number) => (x >= 100 ? `$${x.toFixed(0)}` : x >= 1 ? `$${x.toFixed(2)}` : `$${x.toFixed(3)}`)
const num = (x: number) => x.toLocaleString()

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null)
  const [w, setW] = useState(600)
  useEffect(() => {
    if (!ref.current) return
    const ro = new ResizeObserver(([e]) => setW(Math.max(240, Math.floor(e.contentRect.width))))
    ro.observe(ref.current)
    return () => ro.disconnect()
  }, [])
  return [ref, w] as const
}

function Tile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="glass r-md px-4 py-3">
      <div className="text-xs" style={{ color: 'var(--muted)' }}>{label}</div>
      <div className="tnum mt-1 text-2xl font-semibold tracking-tight">{value}</div>
      {hint && <div className="mt-0.5 text-[11px]" style={{ color: 'var(--dim)' }}>{hint}</div>}
    </div>
  )
}

/** Spend per day: one series, so no legend; the title names it. Hover (or focus) a bar for its value. */
function DailyBars({ days, span }: { days: CostSummary['by_day']; span: number }) {
  const [ref, width] = useWidth<HTMLDivElement>()
  const [tip, setTip] = useState<{ x: number; y: number; day: string; v: number } | null>(null)
  const series = useMemo(() => {
    const by = new Map(days.map((d) => [d.day, d.cost_usd]))
    const out: Array<{ day: string; v: number }> = []
    const now = new Date()
    for (let i = span - 1; i >= 0; i--) {
      const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - i)).toISOString().slice(0, 10)
      out.push({ day: d, v: by.get(d) ?? 0 })
    }
    return out
  }, [days, span])
  const H = 160, padL = 44, padB = 22, padT = 8
  const max = Math.max(...series.map((d) => d.v), 0.0001)
  const nice = (() => { const p = 10 ** Math.floor(Math.log10(max)); const m = max / p; return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p })()
  const plotW = width - padL - 4, plotH = H - padB - padT
  const slot = plotW / series.length, bw = Math.max(2, slot - 2)
  const y = (v: number) => padT + plotH - (v / nice) * plotH
  const labelEvery = Math.ceil(series.length / Math.max(2, Math.floor(plotW / 64)))
  return (
    <div ref={ref} className="relative">
      <svg width={width} height={H} role="img" aria-label={`Spend per day over the last ${span} days`}>
        {[0, 0.5, 1].map((f) => (
          <g key={f}>
            <line x1={padL} x2={width - 4} y1={y(nice * f)} y2={y(nice * f)} stroke="var(--grid)" />
            <text x={padL - 6} y={y(nice * f) + 4} textAnchor="end" fontSize="10" fill="var(--dim)" className="tnum">{money(nice * f)}</text>
          </g>
        ))}
        {series.map((d, i) => {
          const x = padL + i * slot + 1, top = y(d.v), h = Math.max(0, padT + plotH - top)
          const r = Math.min(4, bw / 2, h)
          return (
            <g key={d.day}>
              {h > 0 && <path d={`M${x},${padT + plotH} V${top + r} Q${x},${top} ${x + r},${top} H${x + bw - r} Q${x + bw},${top} ${x + bw},${top + r} V${padT + plotH} Z`} fill="var(--bar)" />}
              <rect x={padL + i * slot} y={padT} width={slot} height={plotH} fill="transparent" tabIndex={0}
                aria-label={`${d.day}: ${money(d.v)}`}
                onPointerEnter={() => setTip({ x: x + bw / 2, y: top, day: d.day, v: d.v })} onPointerLeave={() => setTip(null)}
                onFocus={() => setTip({ x: x + bw / 2, y: top, day: d.day, v: d.v })} onBlur={() => setTip(null)} />
              {i % labelEvery === 0 && <text x={x + bw / 2} y={H - 6} textAnchor="middle" fontSize="10" fill="var(--dim)">{d.day.slice(5)}</text>}
            </g>
          )
        })}
      </svg>
      {tip && (
        <div className="glass r-sm pointer-events-none absolute z-10 px-2.5 py-1.5 text-xs whitespace-nowrap"
          style={{ left: Math.min(Math.max(tip.x, 50), width - 50), top: Math.max(0, tip.y - 44), transform: 'translateX(-50%)' }}>
          <strong className="tnum">{money(tip.v)}</strong> <span style={{ color: 'var(--muted)' }}>{tip.day}</span>
        </div>
      )}
    </div>
  )
}

/** Share of spend per stage: a segmented bar with a 2px surface gap, every segment labeled in the legend below. */
export function StageBar({ by, height = 10, legend = true }: { by: CostsT['by_stage']; height?: number; legend?: boolean }) {
  const total = STAGES.reduce((n, s) => n + (by[s.key]?.cost_usd ?? 0), 0)
  if (total <= 0) return <span className="text-xs" style={{ color: 'var(--dim)' }}>no model spend</span>
  const present = STAGES.filter((s) => (by[s.key]?.cost_usd ?? 0) > 0)
  return (
    <div>
      <div className="flex w-full gap-[2px] overflow-hidden" style={{ height, borderRadius: 4 }} role="img"
        aria-label={present.map((s) => `${s.label} ${money(by[s.key]!.cost_usd)}`).join(', ')}>
        {present.map((s) => (
          <span key={s.key} title={`${s.label}: ${money(by[s.key]!.cost_usd)}`} style={{ flex: by[s.key]!.cost_usd / total, background: s.color, minWidth: 3 }} />
        ))}
      </div>
      {legend && (
        <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs">
          {present.map((s) => (
            <li key={s.key} className="flex items-center gap-1.5">
              <span className="h-2.5 w-2.5 rounded-sm" style={{ background: s.color }} aria-hidden />
              <span style={{ color: 'var(--muted)' }}>{s.label}</span>
              <span className="tnum font-medium">{money(by[s.key]!.cost_usd)}</span>
              <span className="tnum" style={{ color: 'var(--dim)' }}>{Math.round((by[s.key]!.cost_usd / total) * 100)}%</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function Table({ title, head, rows }: { title: string; head: string[]; rows: Array<Array<React.ReactNode>> }) {
  return (
    <section className="glass r-md min-w-0 p-4">
      <h3 className="mb-2 text-sm font-medium">{title}</h3>
      {rows.length ? (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead><tr className="text-left text-xs" style={{ color: 'var(--dim)' }}>{head.map((h, i) => <th key={h} className={`pb-1.5 font-normal ${i ? 'text-right' : ''}`}>{h}</th>)}</tr></thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i} style={{ boxShadow: 'inset 0 1px 0 var(--line)' }}>
                  {r.map((c, j) => <td key={j} className={`py-1.5 ${j ? 'tnum text-right' : 'max-w-[16rem] truncate'}`}>{c}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <p className="text-sm" style={{ color: 'var(--dim)' }}>Nothing in this range.</p>}
    </section>
  )
}

export function CostsView() {
  const [days, setDays] = useState(Number(localStorage.getItem('pw.costDays') ?? 30))
  const [data, setData] = useState<CostSummary | null>(null)
  const [err, setErr] = useState('')
  useEffect(() => {
    let stop = false
    setData(null); setErr('')
    team.costs(days).then((d) => !stop && setData(d)).catch((e) => !stop && setErr(String(e)))
    return () => { stop = true }
  }, [days])
  const pick = (d: number) => { localStorage.setItem('pw.costDays', String(d)); setDays(d) }
  const sorted = (o: Record<string, { cost_usd: number }>) => Object.entries(o).sort((a, b) => b[1].cost_usd - a[1].cost_usd)

  return (
    <div className="mx-auto max-w-5xl px-4 pt-12 pb-10">
      <div className="mb-5 flex flex-wrap items-end gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-xl font-semibold"><Coins size={18} /> Costs</h2>
          <p className="text-sm" style={{ color: 'var(--muted)' }}>Model spend for every request: planning, the runner and review follow-ups.</p>
        </div>
        <div className="ml-auto flex gap-1" role="group" aria-label="Range">
          {[7, 30, 90].map((d) => (
            <button key={d} className="chip r-sm px-3 py-1 text-xs" aria-pressed={days === d} onClick={() => pick(d)}>{d} days</button>
          ))}
        </div>
      </div>
      {err && <p className="text-sm" style={{ color: 'var(--err)' }}>Could not load costs: {err}</p>}
      {!data && !err && (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">{[0, 1, 2, 3].map((i) => <div key={i} className="skeleton h-20" />)}</div>
      )}
      {data && (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Tile label="Total spend" value={money(data.total_usd)} hint={`last ${data.days} days`} />
            <Tile label="Requests" value={num(data.requests)} />
            <Tile label="Per request" value={data.requests ? money(data.total_usd / data.requests) : '—'} hint="average" />
            <Tile label="Model calls" value={num(data.calls)} />
          </div>
          {data.unpriced.length > 0 && (
            <p className="glass r-md flex items-start gap-2 px-4 py-2.5 text-sm" style={{ color: 'var(--warn)' }}>
              <TriangleAlert size={16} className="mt-0.5 shrink-0" />
              <span>No price for {data.unpriced.map((m) => <code key={m} className="mono mx-0.5">{m}</code>)}: its calls count as $0. Add it to PULLWRIGHT_PRICES.</span>
            </p>
          )}
          <section className="glass r-md p-4">
            <h3 className="mb-2 text-sm font-medium">Spend per day</h3>
            <DailyBars days={data.by_day} span={data.days} />
          </section>
          <section className="glass r-md p-4">
            <h3 className="mb-3 text-sm font-medium">By stage</h3>
            <StageBar by={data.by_stage} height={14} />
          </section>
          <div className="grid gap-4 md:grid-cols-2">
            <Table title="By model" head={['Model', 'Cost', 'Calls', 'Tokens in']}
              rows={sorted(data.by_model).map(([m, b]) => [<span key="m" className="mono text-xs">{m}{data.unpriced.includes(m) && <span style={{ color: 'var(--warn)' }}> · unpriced</span>}</span>,
                money(b.cost_usd), num((b as any).calls), num((b as any).input_tokens)])} />
            <Table title="By repository" head={['Repository', 'Cost', 'Requests']}
              rows={sorted(data.by_repo).map(([r, b]) => [<span key="r" className="mono text-xs">{r}</span>, money(b.cost_usd), num((b as any).requests)])} />
            <Table title="By requester" head={['Requester', 'Cost', 'Requests']}
              rows={sorted(data.by_requester).map(([r, b]) => [`@${r}`, money(b.cost_usd), num((b as any).requests)])} />
            <section className="glass r-md min-w-0 p-4">
              <h3 className="mb-2 text-sm font-medium">Costliest requests</h3>
              {data.top.length ? (
                <ul className="space-y-2.5">
                  {data.top.map((t) => (
                    <li key={t.id} className="min-w-0">
                      <div className="flex min-w-0 items-baseline gap-2 text-sm">
                        <span className="truncate">{t.task}</span>
                        {(t.pr_url || t.issue_url) && <a href={t.pr_url || t.issue_url} target="_blank" rel="noreferrer" aria-label="open on GitHub" style={{ color: 'var(--accent)' }}><ExternalLink size={12} /></a>}
                        <span className="tnum ml-auto shrink-0 font-medium">{money(t.cost_usd)}</span>
                      </div>
                      <div className="mt-1"><StageBar by={t.by_stage} height={6} legend={false} /></div>
                    </li>
                  ))}
                </ul>
              ) : <p className="text-sm" style={{ color: 'var(--dim)' }}>Nothing in this range.</p>}
            </section>
          </div>
        </div>
      )}
    </div>
  )
}
