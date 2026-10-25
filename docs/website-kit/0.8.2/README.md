# Website kit: Captain Claw 0.8.2

The 0.8.2 sections for captain-claw.com: BotPort (Flight Decks that work
together), the kiosk, and the devices (StopWatch and PaperColor), with the
screenshots and the BotPort clip they use.

| File | What it is |
|------|------------|
| `index.html` | The whole thing as one page. Open it in a browser to see it. |
| `CaptainClaw082.astro` | The same sections as an Astro component. |
| `assets/` | Screenshots (WebP), the BotPort clip (`botport-live.mp4`, 1.4 MB) and its poster. |

## Put it on the site (Astro)

1. Copy `assets/` to the site's `public/media/0.8.2/`.
2. Copy `CaptainClaw082.astro` to `src/components/`.
3. Use it on a page:

   ```astro
   ---
   import CaptainClaw082 from '../components/CaptainClaw082.astro';
   ---
   <CaptainClaw082 />
   ```

   Assets somewhere else? Pass the folder: `<CaptainClaw082 base="/img/release" />`.
4. Add the fonts to the layout's `<head>` (without them the sections fall back
   to system fonts and still look right):

   ```html
   <link rel="preconnect" href="https://fonts.googleapis.com">
   <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
   <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=JetBrains+Mono:wght@500;600;700&family=Sora:wght@600;700&display=swap">
   ```

All styles sit under `.cc082`, so nothing leaks into the rest of the site. The
sections have ids for menu links: `#botport`, `#add-deck`, `#wall`, `#kiosk`,
`#devices`, `#upgrade`.

## What's in it

1. **Hero**: "Your agents. Every deck. Every device." The hub's live network
   (the clip, muted, looping; paused for visitors who ask for reduced motion),
   with the phone kiosk, the StopWatch and the PaperColor around it.
2. **BotPort**: one deck's agent asking another's through the hub, the four
   promises (Agent@deck, dial-out only, safe by construction, built not to lose
   work) and the hub's Decks page.
3. **Add a Flight Deck in a minute**: name it on the hub, copy the join code,
   paste it into the deck.
4. **On the wall**: the hub dashboard in kiosk mode.
5. **The kiosk**: the chat-first view for the team, the share-on-network dialog,
   the kiosk on a phone.
6. **Devices**: two StopWatch faces and two PaperColor screens, with specs.
7. **Upgrade**: the hub first, then the decks, with copy buttons.

## Where the pictures come from

Every screenshot is the real 0.8.2 hub and Flight Deck UI. The busy network on
the hub is a test network: a real Flight Deck plus simulated decks that connect
and answer like real ones. The device screens come from the firmware's own renderer
(`firmware/device/preview`), so they are pixel for pixel what the devices show.
To refresh a picture, replace the file in `assets/` with one of the same name
and size.
