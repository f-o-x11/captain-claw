/**
 * Shared folders — the kiosk's "Folders": the file tree every agent of the
 * user reads and writes as `vfs:<folder>/<path>` (`captain_claw/flight_deck/
 * vfs_routes.py`), with the folders linked from disk, mounted from Google
 * Drive and shared by other people. The full layout's VFS page keeps its own
 * store (stores/vfsStore.ts); the kiosk's dialog calls the routes directly.
 */

import { useAuthStore, refreshAccessToken } from '../stores/authStore'
import type { DriveFolder, DriveMountEvent, FsListing, VFSEntry, VFSProject } from '../stores/vfsStore'
import { api } from './fileLibraryApi'

/** A folder as the routes name it: its name and, when someone shared it, its
 *  owner's id ('' = the caller's own). */
export interface FolderRef { project: string; owner: string }

/** A file found by the search (or among the newest files). */
export interface FolderHit {
  project: string
  owner: string
  /** Relative to the folder. */
  path: string
  name: string
  size: number
  /** Epoch seconds. */
  mtime: number
  /** The agent that last wrote it, when one did. */
  author?: string
}

export interface FolderSearch { files: FolderHit[]; total: number; truncated: boolean }

const qs = (o: Record<string, string | number | undefined>) => {
  const q = new URLSearchParams()
  for (const [k, v] of Object.entries(o)) if (v !== undefined && v !== '') q.set(k, String(v))
  return q.toString()
}

const json = (body: unknown): RequestInit => ({ method: 'POST', body: JSON.stringify(body) })

/** A file URL a preview, the viewer or a link opens directly: the FD token
 *  rides along. `version` (its change time) gives each version its own URL. */
function fileUrl(what: 'view' | 'download' | 'thumb', ref: FolderRef, path: string, version?: number): string {
  const { token, authEnabled } = useAuthStore.getState()
  return `/fd/vfs/${what}?${qs({
    project: ref.project, path, owner: ref.owner,
    fd_token: authEnabled && token ? token : undefined,
    v: version === undefined ? undefined : Math.floor(version),
  })}`
}

/** POSTs a mount and reads its progress lines until the stream ends; an
 *  `error` line (or a refused request) throws its detail. */
async function streamMount(url: string, body: unknown, onProgress?: (ev: DriveMountEvent) => void): Promise<void> {
  const send = () => {
    const jwt = useAuthStore.getState().token
    return fetch(url, {
      method: 'POST', body: JSON.stringify(body), credentials: 'include',
      headers: { 'Content-Type': 'application/json', ...(jwt ? { Authorization: `Bearer ${jwt}` } : {}) },
    })
  }
  let r = await send()
  if (r.status === 401 && useAuthStore.getState().authEnabled && (await refreshAccessToken())) r = await send()
  if (!r.ok || !r.body) {
    let detail = ''
    try { const b = await r.json(); detail = typeof b?.detail === 'string' ? b.detail : '' } catch { /* not JSON */ }
    throw new Error(detail || `${r.status} ${r.statusText}`)
  }
  const reader = r.body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  let failed: string | null = null
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    let nl: number
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl).trim()
      buf = buf.slice(nl + 1)
      if (!line) continue
      let ev: DriveMountEvent
      try { ev = JSON.parse(line) as DriveMountEvent } catch { continue }
      if (ev.event === 'error') failed = ev.detail || 'Could not connect the folder.'
      else if (ev.event === 'progress') onProgress?.(ev)
    }
  }
  if (failed) throw new Error(failed)
}

export const vfsApi = {
  projects: () => api<{ projects: VFSProject[] }>('/fd/vfs/projects').then((r) => r.projects),
  list: (ref: FolderRef, path: string) =>
    api<{ entries: VFSEntry[] }>(`/fd/vfs/list?${qs({ project: ref.project, path, owner: ref.owner })}`).then((r) => r.entries),
  /** Files whose path holds every word of `q`, newest first — in every folder,
   *  or in `within`. No words: the newest files. */
  search: (q: string, within?: FolderRef, limit = 120) =>
    api<FolderSearch>(`/fd/vfs/search?${qs({ q, project: within?.project, owner: within?.owner, limit })}`),

  viewUrl: (ref: FolderRef, path: string, version?: number) => fileUrl('view', ref, path, version),
  downloadUrl: (ref: FolderRef, path: string) => fileUrl('download', ref, path),
  thumbUrl: (ref: FolderRef, path: string, version?: number) => fileUrl('thumb', ref, path, version),
  zipUrl: (ref: FolderRef) => {
    const { token, authEnabled } = useAuthStore.getState()
    return `/fd/vfs/download-zip?${qs({ project: ref.project, owner: ref.owner, fd_token: authEnabled && token ? token : undefined })}`
  },

  /** A new folder: at the top (`path` '') a folder of its own, else inside one. */
  mkdir: (ref: FolderRef, path: string) => api('/fd/vfs/mkdir', json({ ...ref, path })),
  write: (ref: FolderRef, path: string, content: string) => api('/fd/vfs/write', json({ ...ref, path, content })),
  rename: (ref: FolderRef, path: string, to: string) => api('/fd/vfs/rename', json({ ...ref, path, to })),
  remove: (ref: FolderRef, path: string) =>
    api(`/fd/vfs/entry?${qs({ project: ref.project, path, owner: ref.owner, recursive: 'true' })}`, { method: 'DELETE' }),
  /** Deletes one of the caller's folders — a linked one is only unlinked. */
  removeFolder: (project: string) => api(`/fd/vfs/project?${qs({ project })}`, { method: 'DELETE' }),
  copyShared: (ref: FolderRef) => api<{ project: string }>('/fd/vfs/copy-shared', json(ref)),
  exportToDrive: (ref: FolderRef, path: string) =>
    api<{ link?: string; name?: string }>('/fd/vfs/export-to-drive', json({ ...ref, path, folder_id: '' })),

  // A Google Drive or OneDrive mount, either one.
  driveRefresh: (name: string) =>
    api<{ files: number; cloned?: number; truncated?: boolean }>(`/fd/vfs/links/mount/${encodeURIComponent(name)}/refresh`, { method: 'POST' }),
  driveClone: (name: string, clonemd: boolean) =>
    api<{ cloned?: number }>(`/fd/vfs/links/mount/${encodeURIComponent(name)}/clonemd`, json({ clonemd })),
  driveUnmount: (name: string, keepCloned: boolean) =>
    api(`/fd/vfs/links/mount/${encodeURIComponent(name)}?${qs({ keep_cloned: String(keepCloned) })}`, { method: 'DELETE' }),

  /** OneDrive folders under *folderId* for the mount picker; at the root also
   *  the folders other people shared with the user (`shared`). */
  oneDriveBrowse: (folderId: string) =>
    api<{ folders: DriveFolder[]; shared: DriveFolder[]; truncated: boolean }>(
      `/fd/vfs/onedrive/browse?${qs({ folder_id: folderId || 'root' })}`),
  /** Mounts a OneDrive folder, reporting the walk as it goes (NDJSON). */
  mountOneDrive: (name: string, folderId: string, clonemd: boolean, path: string,
    onProgress?: (ev: DriveMountEvent) => void) =>
    streamMount('/fd/vfs/links/onedrive/stream', { name, folder_id: folderId, clonemd, path }, onProgress),

  addLink: (name: string, path: string, mode: 'rw' | 'ro') => api('/fd/vfs/links', json({ name, path, mode })),
  browseFs: (path: string) => api<FsListing>(`/fd/vfs/browse-fs?${qs({ path })}`),

  /** Files into a folder: multipart, so the browser sets the boundary. */
  upload: async (ref: FolderRef, path: string, files: File[]): Promise<void> => {
    const form = new FormData()
    form.append('project', ref.project)
    form.append('path', path)
    if (ref.owner) form.append('owner', ref.owner)
    for (const f of files) form.append('files', f)
    const send = () => {
      const jwt = useAuthStore.getState().token
      return fetch('/fd/vfs/upload', {
        method: 'POST', body: form, credentials: 'include',
        headers: jwt ? { Authorization: `Bearer ${jwt}` } : {},
      })
    }
    let r = await send()
    if (r.status === 401 && useAuthStore.getState().authEnabled && (await refreshAccessToken())) r = await send()
    if (!r.ok) {
      let detail = ''
      try { const b = await r.json(); detail = typeof b?.detail === 'string' ? b.detail : '' } catch { /* not JSON */ }
      throw new Error(detail || `${r.status} ${r.statusText}`)
    }
  },
}
