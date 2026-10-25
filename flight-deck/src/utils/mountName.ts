// The default name of a Google Drive or OneDrive mount (its agents' vfs:
// address), from where the folder is.

const cleanSeg = (s: string) =>
  s.replace(/[^a-zA-Z0-9._-]/g, '-').replace(/-+/g, '-').replace(/^[-._]+|[-._]+$/g, '')

/** Shortest suffix of the folder path that doesn't collide with an existing
 * project: "VC", else "Performance-VC", else "Startup-reports-Performance-VC",
 * … — a short but unambiguous default the user can still edit. */
export function uniqueMountName(segments: string[], taken: Set<string>): string {
  const segs = segments.map(cleanSeg).filter(Boolean)
  if (segs.length === 0) return 'drive'
  for (let n = 1; n <= segs.length; n++) {
    const cand = segs.slice(segs.length - n).join('-')
    if (!taken.has(cand)) return cand
  }
  const base = segs.join('-')
  let i = 2
  while (taken.has(`${base}-${i}`)) i++
  return `${base}-${i}`
}
