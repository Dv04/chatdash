# Design: DHI Orbit v2 mission board

Recorded from the built UI (dhi_orbit/ui/css/app.css, dhi_orbit/ui/js), not from intentions. Mode: Operate. Desk-first,
phone second.

## World
A flight-strip board for one operator: graphite surfaces, hairline rules, strips with a wait column on the left. Color
is reserved for state; everything else is ink on neutral. Quiet by default, unmissable when something waits.

## Tokens (oklch, both themes defined; dark follows the system unless the theme toggle pins one)
| Token | Role | Light | Dark |
|---|---|---|---|
| --bg / --panel / --panel-2 | page / strips and groups / wait column, hover | 97.5 / 99.5 / 95 L | 17.5 / 21 / 24.5 L |
| --line / --line-strong | hairlines / button borders (decorative: buttons carry text) | 88 / 78 L | 30 / 40 L |
| --field | text-field borders (3:1, WCAG 1.4.11) | 62 L | 56 L |
| --ink / --ink-2 / --ink-3 | text / secondary / hints | 22 / 40 / 50 L | 93 / 78 / 68 L |
| --action, --focus | links, primary buttons, recommended option, focus ring | hue 255 | hue 255 |
| --ok, --warn, --bad (+ -bg tints) | measured ok / unknown or near / blocked, high risk, needs you | hues 155 / 70 / 25 | same hues, lighter |
| --idle | idle and stopped marks | 62 L | 58 L |
Contrast is computed, not eyeballed: tools/acceptance/contrast.py (instrument checked against WebAIM: white on #777 4.48,
white on #00f 8.59), 0 failures in both themes.

## State language (the one rule that matters)
- Absence is never green. Unknown = amber, dashed outline or hatched gauge, and the word UNKNOWN; a reading past its
  reset shows "?" and "reset since the last reading", never 0%.
- OK green appears only when the server's health AND the client's own freshness check (refresh under 20 s) both pass.
- Needs you = red ring (graph) or a strip in NEEDS YOU; blocked seat = red dot and bar; high risk = red chip.
- Never color alone: every state also has a word, a chip label, or an sr-only text.

## Type and spacing
System UI face (-apple-system / SF), one family; mono only for verbatim data (test lines, diffs, state docs).
Fixed rem scale 0.75 / 0.8125 / 0.9375 / 1.125 / 1.375; tabular numerals everywhere. Spacing steps 4 8 12 16 24 32.
Radius 6 px for controls and strips, 10 to 14 px for floating panels and the phone sheet.

## Components
- Status rail (sticky): health pill, "Waiting longest: <item>, <time>", counts, 7 seat chips (dot, 5h and 7d gauges).
- Strip: wait column (age, unit, kind) + body (title, chips, question or numbered parts, option buttons with kbd hints,
  recommended outlined, "Other" field, Send only when every part is answered, timeout line, last message, receipt).
- Receipt chips: verified / not verified, files (expandable, source per file), +/- from git, verbatim test line in mono,
  PR links, commits, units. "No receipt" is amber text with "Ask for evidence".
- Capacity panel: per-seat bars with reset countdowns in US Central, resume chips, next-24h planner (SVG lanes: ticks
  5h reset, diamonds 7d reset, red spans blocked, dashed check-in lines, legend).
- Graph (canvas): seat = rounded rect sized to its label, work item = diamond, session = circle (area = spend today),
  job/subagent = small square, idle cluster = hollow circle "N idle"; edges faint (runs_on, belongs_to dashed),
  collides_with red, waits_on amber dashed. Minimap, filters, table view, replay scrubber.
- Floating: send bar (sends at once since 2026-10-04; the 5 s undo delay was removed for speed), focus panel, palette (dialog), spawn dialog (dialog: starting a session is a
  consequential, protected step), voice panel, phone bottom sheet for #/decision/<id>.

## Speed (measured 2026-10-04)
- Updates are pushed: the server re-reads chats every 1.5 s (one `ps` per snapshot, about 0.1 s) and /api/events
  tells the page to fetch at once; the 5 s / 10 s polls are only the fallback. overview and graph are cached per
  snapshot (p50 1 to 2 ms); any POST clears the cache.
- A reply reaches the chat in about 2.5 s including the read-back (`claude attach` is ready in about 0.4 s; the old
  fixed 6 s wait is gone). An answer shows on the board about 2.5 s after the chat writes it.
- A refresh never moves a focused field: unchanged strips stay in place, typing updates only the Send state, and a
  poll with no data change repaints only the rail, ages and badge.
- The WebGL2 nebula (board field and Sky) draws in a Web Worker on an OffscreenCanvas (js/gl-worker.js) at full
  frame rate with the frosted glass kept; the main thread only posts the scene and moves visible labels when they
  shift a pixel. Measured in Chrome per 10 s: board 741 -> 289 ms main-thread time, Sky 627 -> 327 ms, same image
  (worker and main-thread screenshots identical). No OffscreenCanvas WebGL2: it falls back to the main thread.
- Claude's messages render as markdown (js/md.js: DOM nodes only, no innerHTML; links only http(s)).
- Pull into Sky: at the board's top, scrolling up (or pulling down on a phone) brings the nebula closer and past
  260 px dives into Sky; transforms only.

## Motion
150 to 250 ms state transitions only; the graph layout is the one moving element and stops when settled; no ring pulse;
prefers-reduced-motion removes all animation and transitions.

## Responsive
Breakpoints 1180 (rail wraps seats), 900 (one column), 560 (phone: stacked strips, 44 px targets, compact seats,
bottom sheet, minimap hidden). Verified at 390 px: innerWidth = scrollWidth = 390 on board, graph and work item.

## Nebula look (opt-in: Settings > Look, or ?look=nebula; the current look above stays the default)
Recorded from the built UI (ui/css/nebula.css, js/gl-nebula.js, field.js, sky.js, nebula.js) on 2026-10-04.
- World: the fleet as a night sky. One WebGL2 gas cluster per seat (size = week left, pink = used up or nearly out,
  hatched = unknown), one light per running chat, needs-you lights white with a ring. Views: Sky (opens first), Board,
  Graph, Chats, Settings, switched from a glass capsule (left rail on desk, bottom bar on phone).
- Light theme = "Night window" (a design choice): pale sky-blue page and frosted glass; the board field is a
  rounded night-sky window and Sky stays full night. Mechanism: the dark token blocks also match .nb-field, .nb-band,
  .skyview and the capsule on Sky; the renderers read tokens from their own element. Toggles are a white chip on a
  grey track (--seg-*), never a black pill.
- Field pause rule: frames only while the view is active, the tab visible and focused, the band on screen and motion
  allowed; a data change while paused paints one still frame.
- Phone (max-width 760 px): rail scrolls away, board cards are one scroll (no nested scroll), seat labels are one-line
  chips (name and week left; the full reading is in Capacity or the Sky panel), Sky lays seats top to bottom, graph
  controls are four swipeable lines above the map, Chats rows are cards, every tap target at least 32 px.
  Verified in WebKit (iPhone 15 device profile, the engine every iPhone browser uses), not only Chrome emulation.
- A live terminal dialog (permission prompt or AskUserQuestion) shows first in NEEDS YOU with exactly the options the
  terminal shows, read from the chat's screen (same numbers, "Type something." as a text field, Esc); nothing is
  generated for it. Picking one presses that option in the chat.
- NEEDS YOU leaves out chats whose seat is blocked (5h or 7d window full, or a limit banner) and lists them as parked,
  each with "Continue there" on the seat with the most spare 7-day headroom for its reset.

## Not used, on purpose
Card grids of equal tiles, hero metrics, eyebrow labels, colored border-left accents, gradient text, emoji icons
(icons are authored SVG paths), decorative glass (the rail blur is functional: content scrolls under it), any CDN or
web font.
