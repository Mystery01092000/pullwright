// Hand-written mirror of server/pullwright/{api,web,events,store}.py. Regenerate with openapi-typescript if it drifts.
const BASE = import.meta.env.VITE_API ?? '/api'

export type Ev = {
  id: number
  type: string
  run_id: string
  agent_id: string
  parent_id: string | null
  turn: number | null
  ts: number
  payload: Record<string, any>
}

export type RunRow = {
  id: string
  task: string
  cwd: string
  state: string
  cost_usd: number
  branch: string
  commit_sha: string
  pr_url: string
  created_at: number
  thread_id: string
  pending?: Record<string, any> | null
}

export type Cost = {
  total: Record<string, number>
  by_agent: Array<Record<string, any>>
  by_model: Array<Record<string, any>>
}

const j = async <T,>(r: Promise<Response>): Promise<T> => {
  const res = await r
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`)
  return res.json()
}

export const api = {
  runs: () => j<RunRow[]>(fetch(`${BASE}/runs`)),
  run: (id: string) => j<RunRow>(fetch(`${BASE}/runs/${id}`)),
  start: (body: { task: string; cwd?: string; main_model?: string; worker_model?: string; max_cost_usd?: number; continue_from?: string }) =>
    j<{ id: string }>(fetch(`${BASE}/runs`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) })),
  decide: (id: string, approved: boolean, feedback = '') =>
    j<{ ok: true }>(fetch(`${BASE}/runs/${id}/decide`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ approved, feedback }) })),
  cost: (id: string) => j<Cost>(fetch(`${BASE}/runs/${id}/cost`)),
  cancel: (id: string) => j<{ ok: true }>(fetch(`${BASE}/runs/${id}/cancel`, { method: 'POST' })),
  events: (id: string, types?: string[]) => j<Ev[]>(fetch(`${BASE}/runs/${id}/events${types ? `?types=${types.join(',')}` : ''}`)),
  stream(id: string, onEvent: (e: Ev) => void): () => void {
    const es = new EventSource(`${BASE}/runs/${id}/stream`)
    const handle = (m: Event) => {
      const data = (m as MessageEvent).data
      if (typeof data === 'string' && data.startsWith('{')) onEvent(JSON.parse(data))
    }
    es.onmessage = handle
    // sse-starlette sends named events; EventSource only fires onmessage for unnamed ones.
    const types = ['state', 'agent_start', 'agent_end', 'thinking', 'text', 'tool_call', 'tool_result', 'usage', 'error', 'decision_needed', 'decision', 'branch', 'pr', 'diagnosis']
    for (const t of types) es.addEventListener(t, handle)
    es.onerror = () => es.close()
    return () => es.close()
  },
}

// --- the team flow: chat requests -> approved issues -> runner PRs (server/pullwright/web.py) ---

export type Me = { login: string; name: string }
export type RepoInfo = { repo: string; description: string; languages: string[] }

export type RequestRow = {
  id: string
  channel: string
  repo: string
  task: string
  requester_login: string
  requester_name: string
  thread_ref: Record<string, any>
  thread_url: string
  state: string // planning | awaiting_approval | queued | pr_open | pr_draft | cancelled | failed
  plan: Record<string, any> | null
  issue_number: number | null
  issue_url: string
  pr_url: string
  error: string
  cost_usd: number
  created_at: number
}

export type PlanCardBody = { request_id: string; repo: string; task: string; plan_markdown: string; revision: number; cost_usd: number; questions: string[] }
export type ThreadMessage = { id: number; kind: 'user' | 'reply' | 'plan'; body: any; ts: number }

const json = (body: unknown): RequestInit => ({ method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) })

export const team = {
  me: async (): Promise<Me | null> => {
    const r = await fetch(`${BASE}/me`)
    return r.status === 401 ? null : j<Me>(Promise.resolve(r))
  },
  loginUrl: (next = '/') => `/auth/github/login?next=${encodeURIComponent(next)}`,
  logout: () => j<{ ok: true }>(fetch('/auth/logout', { method: 'POST' })),
  requests: () => j<RequestRow[]>(fetch(`${BASE}/requests`)),
  create: (repo: string, task: string) => j<{ thread: string; request_id: string | null }>(fetch(`${BASE}/requests`, json({ repo, task }))),
  thread: (thread: string, after = 0) => j<{ request: RequestRow | null; messages: ThreadMessage[] }>(fetch(`${BASE}/threads/${thread}?after=${after}`)),
  decide: (id: string, decision: 'approve' | 'revise' | 'cancel', feedback = '') =>
    j<{ message: string }>(fetch(`${BASE}/requests/${id}/decision`, json({ decision, feedback }))),
  queue: () => j<RequestRow[]>(fetch(`${BASE}/queue`)),
  repos: () => j<RepoInfo[]>(fetch(`${BASE}/repos`)),
}

