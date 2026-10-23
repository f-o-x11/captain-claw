# StopWatch — UX review and what would make it superb

*2026-10-23, firmware 0.13.2. A walk through every screen and gesture of the
round client (`firmware/device`, env `stopwatch`), with what to change and
roughly what each takes. Ordered by how much each adds to the feel.*

## What's already right

The colour language is consistent and learnable (green ready, red listening,
amber working, cyan speaking), the ring is a good state indicator, Inter on
the circle reads well, pictures arrive in parts, offline notes, OTA, the
background and the pause button are in. The bones are right. What's missing
is motion, sync between sound and screen, and touch-first navigation — the
watch still behaves like a two-button device that happens to have a screen.

## 1. Feel — the five that matter most

1. **Speak as it thinks** *(deck + watch, the biggest win).* The watch gets
   the answer only once it's complete, then one TTS pass. Stream the agent's
   text to the deck as it's written and speak it sentence by sentence: first
   words ~1–2 s after the agent starts, not after it finishes. The watch's
   audio path already streams; the text would arrive in pieces.
2. **The text follows the voice** *(watch).* Turn pages as the speech reaches
   them and highlight the sentence being spoken. No word timings needed: the
   watch knows how far into the clip the speaker is (it drives the mouth with
   it), and text position ≈ audio position. Pause then shows where you are.
3. **Motion** *(watch).* Every change is a hard cut. 3–4-frame slides on page
   swipes, a fade answer → home, scale-in for notices, the badge glow
   breathing while the agent works. ~150–200 ms each at ~50 ms a frame, from
   canvas offsets — no assets.
4. **A haptic vocabulary** *(watch).* One motor, so timing is everything: a
   12 ms tick on a page turn or menu step, a double tick at the last page,
   40 ms for a confirm, three ticks for an error, a soft one when the answer
   starts. Today: record start (35 ms), pause (25), the 160 ms pre-speech
   buzz.
5. **Raise to wake** *(watch).* The IMU is already read for the context line.
   Wake the screen from off/dim on a wrist raise, dim again when lowered.
   From deep sleep only if the IMU's interrupt reaches an RTC pin — check the
   board.

## 2. Navigation and gestures

- **Swipe everywhere.** Swipes only page answers. The Menu, Agent and
  Character pickers should swipe left/right, the chevrons should be tappable,
  and the "Saved voice notes" yes/no should be two tappable pills instead of
  "Yellow: send / Other: later".
- **Slim the menu.** 16 rows one at a time. Keep six on the watch — Replay,
  Status, Agent, Send notes, Tap to talk, Wi-Fi & settings — and move the rest
  to the phone page, as the PaperColor does (or a second *Settings* card).
- **Stop the agent.** Nothing stops a working agent from the watch (the stop
  only silences audio). A *stop* button on the working screen, with the deck
  forwarding `/stop`.
- **Volume by gesture.** A vertical swipe on an answer sets the volume, shown
  as a ring filling round the edge; horizontal keeps paging.
- **Cancel while holding.** Slide a finger away while the yellow button is
  held → cancel (the WhatsApp gesture). Letting go always sends today.

## 3. Screen by screen

- **Home.** It's a watch: idle should be a clock face — big time, small badge,
  a battery arc — not a badge with a hint. Show "Hold the yellow button to
  talk" for the first ~5 turns, then drop it.
- **Listening.** Fine. The red ring fills toward the 110 s cap, which reads as
  progress; elapsed alone is clearer.
- **Working.** The timer is the largest thing on screen and the least useful.
  Swap: the current activity in body size, the timer small; keep the step
  ticks on the ring.
- **Answer.** Auto-paging and the spoken-sentence highlight (above). A replay
  glyph next to the page dots once speech ends — replay by double-click is
  hidden.
- **Pictures.** A picture is a 4:3 rectangle with black corners inside the
  circle. Crop to fill (the deck already does this for backgrounds), tap to
  toggle to fit.
- **Character mode.** Subtitles: the sentence being spoken under the face,
  from the same position estimate.
- **Status.** "-54 dBm" → bars and a word; the dBm can stay on page 2.
- **Notices.** A tap dismisses; minor ones go by themselves after 4 s.
- **Background.** A lightly blurred copy behind text screens (one-time blur at
  load), so busy photos don't fight the words; the sharp one stays for the
  picture-only screen.
- **Battery.** One buzz and a toast at 10 %.

## 4. Small polish

- "yellow = change" → "press to change" with a small yellow dot.
- The pause/play button could also show on the picture screen while it's
  spoken.
- Page dots tappable (left half = back, right half = on).

## 5. A sensible order

| PR | Scope | Size |
|---|---|---|
| A | swipes in pickers, tappable chevrons and yes/no, haptic vocabulary, hint that fades, menu slimmed, pictures fill the circle | watch, half a day |
| B | transitions (slides, fades, breathing glow) | watch, half a day |
| C | auto-paging, sentence highlight, character subtitles, replay glyph | watch, a day |
| D | streamed answers with sentence-by-sentence speech; stop the agent | deck + watch, a day |
| E | clock-face idle, raise to wake, frosted background | watch, a day (IMU wiring to check) |
