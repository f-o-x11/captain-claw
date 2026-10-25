// REST client for agent links — systems a person connected to one of their
// agents (captain_claw/flight_deck/agent_link.py). The system itself talks
// over the /link/ws WebSocket with the link's key, or over HTTP and a webhook
// (agent_link_http.py); this is the owner's side: /fd/links to list, create,
// change and delete links, a new key, a new webhook signing secret.
//
// A key is in an answer only when it was just made (create, new key); it is
// kept as a hash, and the list shows its last 4 characters. A webhook's
// signing secret likewise, when the webhook is set or the secret renewed.

import { useAuthStore, refreshAccessToken } from '../stores/authStore'

function _authHeaders(): Record<string, string> {
  const { token, authEnabled } = useAuthStore.getState()
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (authEnabled && token) headers['Authorization'] = `Bearer ${token}`
  return headers
}

async function fdFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const _state = useAuthStore.getState()
  if (_state.authEnabled === true && !_state.token) {
    const refreshed = await refreshAccessToken()
    if (!refreshed) throw new Error('Not authenticated')
  }
  const build = (): RequestInit => ({ headers: _authHeaders(), credentials: 'include', ...init })
  let res = await fetch(path, build())
  // Only a failed refresh ends the session (see docker.ts fdFetch).
  if (res.status === 401 && useAuthStore.getState().authEnabled) {
    if (!(await refreshAccessToken())) {
      useAuthStore.getState().clearAuth()
      throw new Error('Session expired')
    }
    res = await fetch(path, build())
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }))
    const msg = body.detail
    throw new Error(typeof msg === 'string' && msg ? msg : `HTTP ${res.status}`)
  }
  return res.json()
}

export interface AgentLink {
  id: string
  name: string
  agent_slug: string // "" = the owner's first running agent
  instructions: string
  key_hint: string
  created_at: string
  updated_at: string
  last_used_at: string | null
  connected: boolean
  agent_may_send: boolean // the agent may message it on its own (the linked_system tool)
  webhook_url: string // "" = none
}

// What an answer carries once: a new key (create, new key) or a webhook's
// new signing secret (create / change with a new webhook, new secret).
export interface AgentLinkSecrets {
  key?: string
  webhook_secret?: string
}

export interface AgentLinkFields {
  name?: string
  agent_slug?: string
  instructions?: string
  agent_may_send?: boolean
  webhook_url?: string
}

// An agent the link can talk to (GET /fd/fleet, owner-scoped).
export interface LinkableAgent {
  name: string
  slug: string
  kind: string
  status: string
}

// The limit the server keeps (agent_link.MAX_INSTRUCTIONS_CHARS).
export const LINK_INSTRUCTIONS_MAX = 4000

export async function listLinks(): Promise<AgentLink[]> {
  const data = await fdFetch<{ links?: AgentLink[] }>('/fd/links')
  return Array.isArray(data.links) ? data.links : []
}

export function createLink(fields: AgentLinkFields): Promise<AgentLink & AgentLinkSecrets> {
  return fdFetch('/fd/links', { method: 'POST', body: JSON.stringify(fields) })
}

export function changeLink(id: string, fields: AgentLinkFields): Promise<AgentLink & AgentLinkSecrets> {
  return fdFetch(`/fd/links/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(fields) })
}

export function newLinkKey(id: string): Promise<AgentLink & AgentLinkSecrets> {
  return fdFetch(`/fd/links/${encodeURIComponent(id)}/key`, { method: 'POST' })
}

export function newWebhookSecret(id: string): Promise<AgentLink & AgentLinkSecrets> {
  return fdFetch(`/fd/links/${encodeURIComponent(id)}/webhook-secret`, { method: 'POST' })
}

export async function deleteLink(id: string): Promise<void> {
  await fdFetch(`/fd/links/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

// The person's own agents a link can name: docker and process agents
// (the ones Flight Deck runs), running first.
export async function linkableAgents(): Promise<LinkableAgent[]> {
  const fleet = await fdFetch<LinkableAgent[]>('/fd/fleet')
  return (Array.isArray(fleet) ? fleet : [])
    .filter((a) => a.slug && (a.kind === 'docker' || a.kind === 'process'))
    .sort((a, b) => Number(b.status === 'running') - Number(a.status === 'running') || a.name.localeCompare(b.name))
}

// Where a system connects: this deck's address as a WebSocket URL.
export function linkSocketUrl(): string {
  const { protocol, host } = window.location
  return `${protocol === 'https:' ? 'wss' : 'ws'}://${host}/link/ws`
}

// …or over HTTP: this deck's address.
export function linkHttpBase(): string {
  return window.location.origin
}
