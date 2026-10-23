// The Captain Claw desktop app (desktop/, Electron): its preload script
// exposes `window.captainClawDesktop` to the page. Absent in a browser.
//
// To try the desktop app's behaviour in a browser without building the app,
// open any deck address with `?app=desktop` — the tab then acts as the desktop
// app until it closes (`?app=web` turns it back off). Only the layout follows
// it: sign-in popups still go by the real shell.

interface DesktopBridge {
  isDesktop?: boolean
  platform?: string
  version?: string
  // Hands a Google / Microsoft sign-in to the system browser (main.js checks
  // it is this deck's /fd/<provider>/login). Absent in older builds.
  openSignIn?: (url: string) => Promise<boolean>
}

function bridge(): DesktopBridge | undefined {
  return (window as Window & { captainClawDesktop?: DesktopBridge }).captainClawDesktop
}

const SIMULATE_KEY = 'fd:simulate-desktop'

function simulated(): boolean {
  try {
    const ask = new URLSearchParams(window.location.search).get('app')
    if (ask === 'desktop') sessionStorage.setItem(SIMULATE_KEY, '1')
    else if (ask === 'web') sessionStorage.removeItem(SIMULATE_KEY)
    return sessionStorage.getItem(SIMULATE_KEY) === '1'
  } catch { return false }
}

// Read once, at load: the app rewrites the address (e.g. to /console/…) later.
const SIMULATED = simulated()

/** Running inside the desktop app (or a tab simulating it) rather than a browser tab. */
export function isDesktopApp(): boolean {
  return SIMULATED || !!bridge()?.isDesktop
}

/** The real desktop app (its preload bridge), never a tab simulating it: only
 * the shell sends a sign-in to the system browser — a browser tab keeps its
 * popup, which holds the ticket's cookie. */
export function isDesktopShell(): boolean {
  return !!bridge()?.isDesktop
}

/** Open a Google / Microsoft sign-in (`/fd/<provider>/login?ticket=…`) in the
 * system browser — Google refuses sign-in inside the app's own window. A shell
 * without the bridge call gets window.open, which it sends there too. */
export async function openInSystemBrowser(url: string): Promise<void> {
  const open = bridge()?.openSignIn
  if (open) {
    await open(url)
    return
  }
  window.open(url, '_blank', 'noopener')
}

const BROWSER_POLL_MS = 2000
const BROWSER_WATCH_MAX_MS = 10 * 60_000  // Flight Deck forgets the pending sign-in by then

/** Follow a sign-in that finishes in the system browser. There is no popup to
 * watch and no message from the callback page, so `done` is asked every couple
 * of seconds until it says yes, the sign-in has expired, or the returned
 * function cancels it. `onEnd` runs once, however it ends. */
export function watchBrowserSignIn(
  done: () => Promise<boolean>,
  onEnd: (connected: boolean) => void,
): () => void {
  const started = Date.now()
  let stopped = false
  let timer: ReturnType<typeof setTimeout> | null = null
  const stop = (connected: boolean) => {
    if (stopped) return
    stopped = true
    if (timer) clearTimeout(timer)
    onEnd(connected)
  }
  const tick = async () => {
    timer = null
    let ok = false
    try { ok = await done() } catch { /* asked again next time */ }
    if (stopped) return
    if (ok) return stop(true)
    if (Date.now() - started > BROWSER_WATCH_MAX_MS) return stop(false)
    timer = setTimeout(tick, BROWSER_POLL_MS)
  }
  timer = setTimeout(tick, BROWSER_POLL_MS)
  return () => stop(false)
}
