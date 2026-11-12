// Reduce the event log into a view model: agents (tree), turns, pending decision, cost.
import type { Ev } from './api'

export type ToolCall = { id: string; name: string; input: any; output?: string; is_error?: boolean }
export type Turn = { n: number; thinking: string; text: string; tools: ToolCall[]; usage?: Record<string, any> }
export type Agent = {
  id: string
  name: string
  model: string
  effort: string
  parent: string | null
  turns: Turn[]
  stop?: string
  cost: number
  tokens: { in: number; out: number; cache: number }
}

export type View = {
  state: string
  agents: Record<string, Agent>
  order: string[]
  pending: Record<string, any> | null
  intake: any
  diagnosis: any
  summary: string
  branch: string
  pr: string
  error: string
  cost: number
  tokens: { in: number; out: number; cache: number }
  lastId: number
}

export const empty = (): View => ({
  state: 'created', agents: {}, order: [], pending: null, intake: null, diagnosis: null, summary: '', branch: '', pr: '', error: '',
  cost: 0, tokens: { in: 0, out: 0, cache: 0 }, lastId: 0,
})

const turn = (a: Agent, n: number | null): Turn => {
  const k = n ?? (a.turns.length || 1)
  let t = a.turns.find((x) => x.n === k)
  if (!t) { t = { n: k, thinking: '', text: '', tools: [] }; a.turns.push(t) }
  return t
}

export function reduce(v: View, e: Ev): View {
  if (e.id <= v.lastId) return v
  const n = { ...v, lastId: e.id, agents: { ...v.agents } }
  const p = e.payload
  const agent = (): Agent => {
    const a = n.agents[e.agent_id]
    if (!a) return (n.agents[e.agent_id] = { id: e.agent_id, name: e.agent_id, model: '', effort: '', parent: e.parent_id, turns: [], cost: 0, tokens: { in: 0, out: 0, cache: 0 } })
    return (n.agents[e.agent_id] = { ...a, turns: a.turns.map((t) => ({ ...t, tools: [...t.tools] })) })
  }
  switch (e.type) {
    case 'state':
      n.state = p.state
      if (p.intake) n.intake = p.intake
      if (p.summary) n.summary = p.summary
      if (p.error) n.error = p.error
      if (p.state === 'done' || p.state === 'failed' || p.state === 'cancelled') n.pending = null
      break
    case 'agent_start': {
      const a = agent(); a.name = p.name; a.model = p.model; a.effort = p.effort
      if (!n.order.includes(a.id)) n.order = [...n.order, a.id]
      break
    }
    case 'agent_end': agent().stop = p.stop; break
    case 'thinking': turn(agent(), e.turn).thinking += p.delta; break
    case 'text': turn(agent(), e.turn).text += p.delta; break
    case 'tool_call': turn(agent(), e.turn).tools.push({ id: p.tool_use_id, name: p.name, input: p.input }); break
    case 'tool_result': {
      const t = turn(agent(), e.turn)
      const c = t.tools.find((x) => x.id === p.tool_use_id)
      if (c) { c.output = p.output; c.is_error = p.is_error }
      break
    }
    case 'usage': {
      const a = agent(); turn(a, e.turn).usage = p
      a.cost += p.cost_usd; a.tokens = { in: a.tokens.in + p.input_tokens, out: a.tokens.out + p.output_tokens, cache: a.tokens.cache + p.cache_read_input_tokens }
      n.cost += p.cost_usd; n.tokens = { in: n.tokens.in + p.input_tokens, out: n.tokens.out + p.output_tokens, cache: n.tokens.cache + p.cache_read_input_tokens }
      break
    }
    case 'decision_needed': n.pending = p; break
    case 'decision': n.pending = null; break
    case 'branch': n.branch = p.branch; break
    case 'pr': n.pr = p.url; break
    case 'diagnosis': n.diagnosis = p; break
    case 'error': if (e.agent_id === 'run') n.error = JSON.stringify(p); break
  }
  return n
}

export const usd = (x: number) => (x === 0 ? '$0' : `$${x.toFixed(x < 0.1 ? 4 : 3)}`)
export const k = (x: number) => (x >= 1000 ? `${(x / 1000).toFixed(1)}k` : String(x))
