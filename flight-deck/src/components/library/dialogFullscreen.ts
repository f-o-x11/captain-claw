import { createContext } from 'react'

/** The host dialog's fullscreen state — the kiosk's wide dialog provides it
 *  where it can grow (not on a phone, where it already fills the screen).
 *  FullscreenButton (libraryParts) is its switch. */
export const DialogFullscreen = createContext<{ fullscreen: boolean; toggle: () => void } | null>(null)
