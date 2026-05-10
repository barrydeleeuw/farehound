# FareHound Mini Web App — Design Notes

## The one-line concept

**FareHound is a flight ledger, not a flight booker.** Every screen is a balance sheet. Numbers are the hero, typography is editorial, chrome is invisible.

## Why this and not something else

The brief said "Linear / Things 3, calm and data-dense, not playful, not corporate-cold." Three plausible directions sat in that envelope:

| Direction | What it'd look like | Why I rejected it |
|---|---|---|
| **A. Refined SaaS minimal** (Linear-tinted) | Soft greys, rounded cards, subtle gradients, one accent colour. | The brief explicitly warns against "shadcn-card-with-rounded-corners-and-tiny-icons." That direction is exactly that. |
| **B. Aviation cockpit** | Gauges, callsign typography, status-light greens. | Domain-on-the-nose. Gimmicky. Reads as theme-park, not as serious tooling. |
| **C. Editorial ledger** ✅ | Newsprint serif heads, monospace tabular numbers, hairline rules, no rounded corners, generous negative space. | Honest and forensic — matches the "lowest REAL cost" mission. Distinctive enough to remember. Calm enough to use daily. |

**Picking C.** It treats each deal page like a bank statement or a P&L. The visual claim is: *we are accountable to this number*.

## Aesthetic commitments (intentionally narrow)

1. **No rounded corners anywhere.** Cards, buttons, inputs — all razor-edged. This is the single biggest signal that this is not generic SaaS.
2. **Hairline rules instead of boxes.** Sections are separated by 1px lines and whitespace, not by elevated shadow-cards.
3. **Three typefaces, each with one job:**
   - **Display:** Fraunces (variable serif) — page titles, route names. Editorial, characterful, soft contrast on the wide axis.
   - **UI/Body:** Hanken Grotesk — section labels, button text, paragraphs. Distinctive Söhne-relative, free.
   - **Numbers:** JetBrains Mono — every price, every duration, every airport code. Tabular figures, characterful zeroes.
4. **Section labels are all-caps, 11px, letter-spaced, 65% opacity.** They sit above content like ledger column headers.
5. **Tabular alignment.** Right-align prices. Decimal-align where possible. The € symbol is slightly dimmed and offset from the digits.
6. **Colour is rationed.** One green for savings/positive deltas. One red for "skip route" / negative. Everything else is in two-tone (ink + bone). Telegram theme variables override this when present.
7. **Sparkline only.** No fullscreen charts. A 60-px-tall single-stroke polyline with a translucent band for the typical-low/high range. That's it.

## Palette

```
DARK (default — most Telegram users in dark mode)
  bg          #0d0e10   deep ink, slightly warm
  bg-elev     #16181c   elevated surfaces (rare — used sparingly)
  text        #e8e3d6   bone
  text-dim    #8b8478   secondary metadata
  rule        #1f2227   hairline dividers
  positive    #5fdc8b   savings, downward price moves (mint, calm not garish)
  warning     #f0a55a   amber — "watching" state
  negative    #d96666   muted brick — skip / above range

LIGHT (paper)
  bg          #f5f1e8   warm paper
  bg-elev     #ffffff
  text        #1a1a1d
  text-dim    #6b6962
  rule        #e8e3d6
  positive    #2a8050
  warning     #b86d1c
  negative    #a83333
```

Inside Telegram, `--tg-theme-bg-color`, `--tg-theme-text-color`, `--tg-theme-button-color` etc. override our defaults — the app blends with the user's chat theme.

## Tech stack — pushback on the brief

The brief suggested **FastAPI + HTMX**. For the eventual production wiring that's the right call — server-rendered, no JS build, fits the Python codebase. But for **this design pass**, I'm shipping **plain HTML + CSS + ~80 lines of vanilla JS**, no framework, no templating engine.

Rationale:
- Each page must render standalone via `python3 -m http.server` per the brief.
- The HTML structure is intentionally thin and unopinionated — it's trivially portable to FastAPI/Jinja templates later. (`<div data-route-id="…">` etc., easy to bind.)
- HTMX would force me to design with `hx-get` placeholders, which makes the static preview less honest.
- A full demo with mock data inline is a more reviewable artefact than a half-built HTMX app pointing at endpoints that don't exist yet.

When the backend lands, ~30% of the templates become Jinja partials and the rest of the markup stays.

## Page sketches (what was committed before code)

### Page 1 — `/deal/{deal_id}` (the most important page)

```
┌────────────────────────────────────────┐
│   FAREHOUND  •  DEAL                    │  hairline at top, 11px label
│                                         │
│   AMS → NRT                             │  Fraunces 44px, weight 400
│   Tokyo · 15 Oct → 5 Nov · 2 pax        │  metadata, dim
│                                         │
│   €  1,820 / pp        ▼ €40            │  JetBrains Mono 64px, € dimmed
│                        savings vs alert │
│                                         │
│   ── COST BREAKDOWN ────────────        │  all-caps section label
│   flights         €  3,640              │  decimal-aligned
│   baggage      +  €    120              │
│   transport    +  €     90              │
│   parking      +  €      0              │
│   ─────────────                          │  short rule
│   total           €  3,850   €1,925/pp   │  bolder
│                                         │
│   ── WHY THIS IS THE BEST ──────        │
│   ✓  Cheapest of 4 dates polled         │
│      Mar 12 saves €60/pp vs others      │
│   ✓  €80 below Google's typical low     │
│      Range €620–€780, you're at €540   │
│   ✓  Yours is best — €40 cheaper        │
│      door-to-door than EIN              │
│                                         │
│   ── PRICE · 90 DAYS ───────────        │
│   ┌──────────── typical-band ─────┐     │  60px sparkline, with band
│   │     ╱╲    ╱╲       ╱╲          │     │
│   │ ╱╲╱  ╲  ╱  ╲   ╱╲╱  ╲╱╲___ ●   │     │
│   └──────────────────────────────┘     │
│   €620                       €780       │  axis
│                                         │
│   ── ALTERNATIVES · AIRPORTS ───        │
│   AMS    € 1,820   Uber 34m    1,890 ●  │  best marker
│   EIN    € 1,650   Car 50m     1,950    │
│   BRU    € 1,580   Train 2.5h  2,010    │
│   DUS    € 1,720   Car 2h      2,050    │
│                                         │
│   ── ALTERNATIVES · DATES ──────        │
│   Mar 8     € 1,880                     │
│   Mar 12    € 1,820  ●                  │  current highlight
│   Mar 15    € 1,920                     │
│   Mar 22    € 1,950                     │
│                                         │
│   ── BAGGAGE · KLM longhaul ────        │
│   carry-on              included        │
│   1× checked 23kg       included        │
│   2× checked 23kg    + €60 each way     │
│                                         │
│   [ B O O K   N O W ]   ← MainButton    │  via Telegram MainButton
│   watching · skip route                 │  secondary, text-link
└────────────────────────────────────────┘
```

### Page 2 — `/routes`

```
┌────────────────────────────────────────┐
│   FAREHOUND  •  ROUTES                  │
│   3 monitored                           │
│                                         │
│   ┌─ Add a trip ───────────────────┐   │
│   │ Tokyo for 2 weeks in October   │   │  free-text NL input
│   └────────────────────────────────┘   │
│                                         │
│   ── 01 ─────────────────────────       │
│   AMS → NRT                             │  Fraunces, link
│   Tokyo · 15 Oct → 5 Nov                │
│                                         │
│   €  1,820 / pp           ▼ €40         │  current price, delta
│   last poll 2h ago · 1 deal this week   │
│                                         │
│   snooze 7d   snooze 30d   edit         │  text-link row
│                                         │
│   ── 02 ─────────────────────────       │
│   AMS → BKK                             │
│   Bangkok · 5 Jan → 12 Jan              │
│                                         │
│   €    620 / pp        ●  NEW LOW       │  positive accent badge
│   last poll 5h ago · 2 deals this week  │
│                                         │
│   snooze 7d   snooze 30d   edit         │
│                                         │
│   ── 03 ─── snoozed ─────────────       │  dim treatment
│   AMS → LIS                             │
│   Lisbon · 10 Jul → 17 Jul              │
│                                         │
│   €    410 / pp                          │
│   resume in 5 days                      │
│                                         │
│   unsnooze                              │
└────────────────────────────────────────┘
```

### Page 3 — `/settings`

```
┌────────────────────────────────────────┐
│   FAREHOUND  •  PREFERENCES             │
│                                         │
│   ── BAGGAGE NEEDS ──────────────       │
│   ○ carry-on only                       │
│   ● one checked bag                     │  selected: filled circle, ink
│   ○ two checked bags                    │
│                                         │
│   ── TRANSPORT TO AIRPORT ───────       │
│   AMS   Uber    €  45   34m   p €  0   │  inline-edit table
│   EIN   car     €  10   50m   p € 80   │
│   RTM   car     €  10   21m   p € 80   │
│   BRU   train   €  70   2h24  p €  0   │
│   + add airport                         │  text-link only
│                                         │
│   ── QUIET HOURS ────────────────       │
│   no alerts between [22:00] and [07:00] │
│                                         │
│   ── DAILY DIGEST ───────────────       │
│   send at [08:00]                       │
│                                         │
│   ── CONNECTED ──────────────────       │
│   Telegram · @bdeleeuw1987              │
└────────────────────────────────────────┘
```

## Notable interaction details

1. **Cost-breakdown rows are clickable.** Tapping a row opens a one-line explainer below it (e.g. tapping `transport` shows "Uber from The Hague to AMS, €45 × 2 (round trip)"). Renders as inline expansion, not as a modal.
2. **Sparkline current-point is interactive.** Tap = tooltip with the date and exact price. No tooltip on hover (mobile).
3. **Action buttons go to Telegram MainButton.** The page itself has no fixed-bottom button bar; instead, JS calls `Telegram.WebApp.MainButton.setText('Book Now').show()` and wires its click. Outside Telegram, a fallback button appears in the page.
4. **Snooze chips animate to confirmed state.** Tap "snooze 7d" → ink-fill from left + label changes to "snoozed 7d ↩" (undo). Pure CSS, no spinner.
5. **The `/routes` list uses ordinal numbers** (01, 02, 03) as a calm form of identity — no avatars, no icons. Editorial.

## What I did NOT include

- No filter/search on `/routes` (3 routes, doesn't need it).
- No charts on `/settings` (it's a form).
- No success-toast component — Telegram's own `showAlert` API is used.
- No "edit dates" modal in this pass — `edit` is a stub link until the backend supports inline patch.
- No keyboard navigation polish — phone-first, real keyboard nav can come later.
