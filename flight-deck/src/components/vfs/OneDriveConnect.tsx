import { useEffect, useState, type ReactNode } from 'react'
import { ArrowLeft, ChevronRight, Cloudy, Folder, Loader2, Sparkles, Users, X } from 'lucide-react'
import type { DriveFolder, DriveMountEvent } from '../../stores/vfsStore'
import { vfsApi } from '../../services/vfsApi'
import { uniqueMountName } from '../../utils/mountName'

/** Connect a OneDrive folder as a read-only VFS mount — the OneDrive twin of
 *  DriveConnect: browse folders (your own, and at the top the ones people
 *  shared with you), name the mount, choose Markdown copies, mount. Files stay
 *  in OneDrive and are fetched when read, unless Markdown copies are on.
 */
interface Frame { id: string; name: string }

export function OneDriveConnect({ taken, onClose, onDone, onConnectMicrosoft }: {
  /** Folder names already in use (a new mount gets another). */
  taken: Set<string>
  onClose: () => void
  onDone: (name: string) => void
  /** Opens Connections → Microsoft: offered when the account isn't connected. */
  onConnectMicrosoft?: () => void
}) {
  const [stack, setStack] = useState<Frame[]>([{ id: 'root', name: 'My files' }])
  const [folders, setFolders] = useState<DriveFolder[]>([])
  const [shared, setShared] = useState<DriveFolder[]>([])
  const [loading, setLoading] = useState(false)
  const [truncated, setTruncated] = useState(false)
  const [err, setErr] = useState('')
  const [name, setName] = useState('')
  const [clonemd, setClonemd] = useState(false)
  const [mounting, setMounting] = useState(false)
  const [status, setStatus] = useState('')

  const here = stack[stack.length - 1]
  const atRoot = stack.length === 1
  // Where the folder is, below "My files" (or the shared folder it's in).
  const pathSegments = stack.slice(1).map((f) => f.name)
  const fullPath = pathSegments.join(' / ')

  const load = async (frame: Frame) => {
    setLoading(true)
    setErr('')
    try {
      const d = await vfsApi.oneDriveBrowse(frame.id)
      setFolders(d.folders)
      setShared(d.shared || [])
      setTruncated(d.truncated)
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Could not reach OneDrive')
      setFolders([])
      setShared([])
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load(stack[0]) }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const enter = (f: DriveFolder) => {
    const next = [...stack, { id: f.id, name: f.name }]
    setStack(next)
    setName(uniqueMountName(next.slice(1).map((fr) => fr.name), taken))
    load(next[next.length - 1])
  }
  const up = () => {
    if (stack.length < 2) return
    const next = stack.slice(0, -1)
    setStack(next)
    setName(next.length > 1 ? uniqueMountName(next.slice(1).map((fr) => fr.name), taken) : '')
    load(next[next.length - 1])
  }

  const mount = async () => {
    const n = name.trim()
    if (!n) return
    setMounting(true)
    setErr('')
    setStatus('Connecting to OneDrive…')
    const describe = (ev: DriveMountEvent): string => {
      if (ev.phase === 'cloning') {
        return `Converting to Markdown… ${ev.done ?? 0} file${ev.done === 1 ? '' : 's'}${ev.name ? ` · ${ev.name}` : ''}`
      }
      return `Reading OneDrive… ${ev.folders ?? 0} folder${ev.folders === 1 ? '' : 's'}, ${ev.files ?? 0} file${ev.files === 1 ? '' : 's'}`
    }
    try {
      await vfsApi.mountOneDrive(n, here.id, clonemd, atRoot ? 'My files' : pathSegments.join('/'), (ev) => setStatus(describe(ev)))
      onDone(n)
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Could not connect the folder')
      setMounting(false)
      setStatus('')
    }
  }

  const row = (f: DriveFolder, icon: ReactNode) => (
    <div key={f.id} className="flex items-center gap-2 rounded px-2 py-1.5 hover:bg-zinc-900">
      {icon}
      <button onClick={() => enter(f)} className="min-w-0 flex-1 truncate text-left text-sm text-zinc-200">{f.name}</button>
      <button onClick={() => enter(f)} className="rounded p-0.5 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200" title="Open">
        <ChevronRight className="h-3.5 w-3.5" />
      </button>
    </div>
  )

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-8" onClick={onClose}>
      <div className="flex max-h-full w-full max-w-2xl flex-col rounded-lg border border-zinc-800 bg-zinc-950" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-2 border-b border-zinc-800 px-4 py-3">
          <Cloudy className="h-4 w-4 text-indigo-500 dark:text-indigo-400" />
          <span className="text-sm font-semibold text-zinc-200">Connect OneDrive</span>
          <button onClick={onClose} className="ml-auto text-zinc-500 hover:text-zinc-200" aria-label="Close"><X className="h-4 w-4" /></button>
        </div>

        <div className="flex items-center gap-1 border-b border-zinc-800 px-4 py-2 text-xs text-zinc-400">
          {stack.length > 1 && (
            <button onClick={up} className="mr-1 rounded p-0.5 hover:bg-zinc-800 hover:text-zinc-200" title="Up">
              <ArrowLeft className="h-3.5 w-3.5" />
            </button>
          )}
          {stack.map((f, i) => (
            <span key={`${i}:${f.id}`} className="flex min-w-0 items-center gap-1">
              {i > 0 && <ChevronRight className="h-3 w-3 shrink-0 text-zinc-600" />}
              <span className={`truncate ${i === stack.length - 1 ? 'text-zinc-200' : ''}`}>{f.name}</span>
            </span>
          ))}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-2" style={{ minHeight: 220 }}>
          {loading && (
            <div className="flex items-center justify-center gap-2 py-10 text-xs text-zinc-500">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading…
            </div>
          )}
          {!loading && err && (
            <div className="m-2 rounded border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-600 dark:text-red-300">
              {err}
              {onConnectMicrosoft && /connect your microsoft account/i.test(err) && (
                <button
                  onClick={onConnectMicrosoft}
                  className="mt-2 flex items-center gap-1.5 rounded-md bg-indigo-600 px-2.5 py-1.5 text-xs font-medium text-white hover:bg-indigo-500"
                >
                  <Cloudy className="h-3.5 w-3.5" /> Connect Microsoft
                </button>
              )}
            </div>
          )}
          {!loading && !err && folders.length === 0 && shared.length === 0 && (
            <div className="py-10 text-center text-xs text-zinc-500">No folders inside. Connect this folder itself below.</div>
          )}
          {!loading && atRoot && shared.length > 0 && (
            <>
              <div className="px-2 pb-1 pt-2 text-[10px] font-medium uppercase tracking-wide text-zinc-500">Shared with you</div>
              {shared.map((f) => row(f, <Users className="h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />))}
              {folders.length > 0 && (
                <div className="px-2 pb-1 pt-3 text-[10px] font-medium uppercase tracking-wide text-zinc-500">My files</div>
              )}
            </>
          )}
          {!loading && folders.map((f) => row(f, <Folder className="h-4 w-4 shrink-0 text-indigo-500 dark:text-indigo-400" />))}
          {truncated && <div className="px-2 py-1 text-[11px] text-amber-600 dark:text-amber-400">Showing the first 500 folders here.</div>}
        </div>

        <div className="border-t border-zinc-800 px-4 py-3">
          <div className="mb-2 text-[11px] text-zinc-500">
            Connects <span className="text-zinc-300">{atRoot ? 'all of My files' : fullPath}</span> as a read-only folder.
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="folder name"
              className="w-40 rounded bg-zinc-900 px-2 py-1 text-xs text-zinc-100 outline-none ring-1 ring-zinc-800 focus:ring-zinc-600"
            />
            <label className="flex items-center gap-1.5 text-xs text-zinc-400" title="Keep a Markdown copy of each file here, so agents read and search it faster. Off: each file is fetched from OneDrive when it's read.">
              <input type="checkbox" checked={clonemd} onChange={(e) => setClonemd(e.target.checked)} className="h-3.5 w-3.5 accent-indigo-600" />
              <Sparkles className="h-3 w-3" /> Markdown copies
            </label>
            <button
              onClick={mount}
              disabled={!name.trim() || mounting || !!(err && !folders.length && !shared.length)}
              className="ml-auto flex items-center gap-1.5 rounded bg-indigo-600/90 px-3 py-1 text-xs font-medium text-white hover:bg-indigo-600 disabled:opacity-40"
            >
              {mounting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Cloudy className="h-3.5 w-3.5" />}
              Connect folder
            </button>
          </div>
          {mounting && status && (
            <div className="mt-2 flex items-center gap-1.5 text-[11px] text-indigo-600 dark:text-indigo-400">
              <Loader2 className="h-3 w-3 shrink-0 animate-spin" />
              <span className="truncate">{status}</span>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
