// REST client for WhatsApp Web — the user's own WhatsApp account linked as a
// device, the way WhatsApp Web is (/fd/whatsapp-web), and the deck's switch
// (/fd/admin/whatsapp-web) — captain_claw/flight_deck/whatsapp_web_routes.py.
// Phase 0: linking only; agents read nothing yet.

import { useAuthStore, refreshAccessToken } from '../stores/authStore'

export type WhatsAppWebState =
  | 'off'
  | 'pairing'
  | 'connecting'
  | 'connected'
  | 'disconnected'
  | 'expired'
  | 'failed'
  | 'logged_out'
  | 'stream_replaced'
  | 'temporary_ban'
  | 'outdated'

export interface WhatsAppWebSync {
  chunks: number
  messages: number
  progress: number
  last_type: string
  last_at: string
}

export interface WhatsAppWebLink {
  state: WhatsAppWebState
  // The service has a client running for this link.
  running: boolean
  jid?: string
  // "+385…" once paired.
  number?: string
  linked_at?: string | null
  connected_at?: string | null
  detail?: string
  ban_expires_at?: string | null
  sync?: WhatsAppWebSync
  // While pairing: the QR as an SVG data URI (it changes every ~20 s) …
  qr_svg?: string
  // … or the code to type on the phone instead.
  pair_code?: string
  pairing_expires_at?: string
  has_session?: boolean
}

export interface MyWhatsAppWeb {
  available: boolean
  reason: string
  allowed: boolean
  can_admin: boolean
  link: WhatsAppWebLink
  // After a disconnect: whether the device also left the phone's Linked devices.
  removed_from_phone?: boolean
}

export interface WhatsAppWebAdmin {
  allowed: boolean
  installed: boolean
  kill_switch: boolean
  service: { ok: boolean; available: boolean; reason: string; neonize: string; links: Record<string, number> } | null
}

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
    let msg = body.detail
    if (Array.isArray(msg)) {
      msg = msg.map((e: { loc?: string[]; msg?: string }) => `${(e.loc || []).join('.')}: ${e.msg}`).join('; ')
    }
    throw new Error(typeof msg === 'string' && msg ? msg : `HTTP ${res.status}`)
  }
  return res.json()
}

const post = <T>(path: string, body?: unknown) =>
  fdFetch<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })

export const getMyWhatsAppWeb = () => fdFetch<MyWhatsAppWeb>('/fd/whatsapp-web')
// Every new link needs the user's consent (the card's checkbox).
export const linkWhatsAppWeb = () => post<MyWhatsAppWeb>('/fd/whatsapp-web/link', { consent: true })
export const whatsAppWebPairCode = (phone: string) => post<MyWhatsAppWeb>('/fd/whatsapp-web/pair-code', { phone })
export const reconnectWhatsAppWeb = () => post<MyWhatsAppWeb>('/fd/whatsapp-web/reconnect')
export const cancelWhatsAppWeb = () => post<MyWhatsAppWeb>('/fd/whatsapp-web/cancel')
export const unlinkWhatsAppWeb = () => fdFetch<MyWhatsAppWeb>('/fd/whatsapp-web', { method: 'DELETE' })

export const getWhatsAppWebAdmin = () => fdFetch<WhatsAppWebAdmin>('/fd/admin/whatsapp-web')
export const setWhatsAppWebAllowed = (allowed: boolean) =>
  fdFetch<WhatsAppWebAdmin>('/fd/admin/whatsapp-web', { method: 'PUT', body: JSON.stringify({ allowed }) })
