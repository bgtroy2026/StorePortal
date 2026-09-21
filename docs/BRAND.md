# Brand in the portal

Source of truth: *Big Grove Brewery — Brand Identity Standards* (January 2024). The guide and the logo source
files live in `images/` on Troy's Mac and are **git-ignored** (the repo is public; only web-sized derivatives in
`site/images/` are published).

## What is used where

| Place | Treatment | Why |
|---|---|---|
| Sign-in | Primary Rooted Medallion, full colour / white type, on navy Hopleaf, soft vignette | The standards' preferred treatment; the medallion is never shown under 1.5 in (144 px) |
| Top bar | One Line Wordmark, white, on **solid** BGB Navy | The primary marks cannot fit a 52 px bar; the wordmark exists for exactly that case. Small marks never sit on Hopleaf |
| Navigation | Light Navy bar, Bright Green rule and active marker | Light Navy only ever alongside Navy; green only as an accent |
| Page band | Navy Hopleaf (tone-on-tone), page title in bold caps, filters on the band | Hopleaf behind plain type is permitted; the band also names the page, taproom and dates for screenshots |
| Footer | Primary Rooted Medallion at 150 px on navy Hopleaf | Same preferred treatment as sign-in |
| Weekly one-pager (print) | Medallion, full colour / black type, on white, exactly 1.5 in | Backgrounds may only be navy, white or black |
| Morning email | Medallion on a navy band | Hosted from `site/images/` |

## Assets (`site/images/`, `site/fonts/`)

- `bgb-medallion-white.png`, `bgb-medallion-black.png` — resized from the supplied PNGs, never recoloured.
- `bgb-wordmark-white.svg` — the One Line Wordmark, lifted as vector from page 9 of the standards PDF.
- `hopleaf-navy.webp` — the navy Hopleaf rendering from the PDF's cover, re-mapped to the exact hexes
  (`#1b2449` leaves on `#313757` ground — not inverted). It is a crop, not the seamless tile, so it is only
  ever used with `background-size: cover`. **Replace with the designer's seamless file when available**
  (Design@BigGrove.com).
- `fonts/jost-*.woff2` — Jost (SIL OFL). Body type in the standards is **Brandon Grotesque**, a licensed face
  that is not shipped here; the font stack names it first, so machines that have it use it. To go fully on
  brand, add the licensed web font's `@font-face` above the Jost ones in `site/index.html`.

## Colour

`--navy #1b2449` (prevalent), `--navy2 #313757` (only beside navy), `--green #94bb36` (accent only — rules,
active states, never a field). Text is navy, not black.

Chart series (`--s1`…`--s8`) are anchored on the brand — navy, the acorn cap, the hop — and were run through the
data-viz palette validator: all adjacent pairs clear the colour-blind (ΔE ≥ 8) and normal-vision (ΔE ≥ 15)
floors on white. BGB Navy itself fails the lightness and chroma checks for a series colour (it reads as black
among other lines), so slot 1 is the same hue lifted to `#3d4e9a`. Sequential heat scales use true BGB Navy at
rising opacity. Do not reorder the slots: the order is what keeps neighbours distinguishable.

## Not done, and why

- **No logo in the browser tab.** Nothing in the standards is legible at 16–32 px, and cropping the Hopcorn out
  of a logomark is on the "incorrect use" page.
- **No taproom-specific marks** (Tier 4): the files were not supplied. They would suit the one-pager header.
- **Monthoers / Vodka Brush / Brandon Printed** title faces are licensed and not shipped.
