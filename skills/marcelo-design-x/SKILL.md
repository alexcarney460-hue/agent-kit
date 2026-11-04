---
name: marcelo-design-x
description: Replicate the Marcelo Design X (MDX / mdx.so) website aesthetic — vanilla JS + GSAP + Lenis hand-coded sites with cinematic loaders, declarative data-attribute animations, blur-layered atmospheres, and emotional flythrough scroll experiences. Use when the user asks for "mdx.so quality," "MDX style," "Marcelo Design X feel," cinematic flythrough scrolling, premium agency-tier sites, or references mdx.so as the bar to hit. Apply to any portfolio, agency, product, or brand site that needs to feel handmade and Awwwards-tier — not Framer/Webflow templated.
---

# Marcelo Design X — The MDX Build Plan

> This skill is a **forcing function**, not a reference. If you skip phases, you produce AI slop. The phases run in order. Each phase has a gate — you don't advance until the gate is satisfied.

## Why the previous version failed

The earlier skill was an attribute dictionary. It let the model ship "premium minimal" pages that all look the same: centered hero, three-column features, fade-up everywhere, generic "elevate your brand" copy. That's slop. Marcelo's sites feel handmade because every section had a **specific human decision** — a copy line, an asymmetric offset, a custom SVG, a non-obvious ease curve. This skill forces those decisions, and gives you cloneable templates from his real client work as starting points.

## The Designer (one-paragraph reference)

Marcelo Alejandro Cedeño Velasco. Founder/CEO of MDX (mdx.so), Houston-based but global remote. Solo-led shop, $5K–$30K range. Awwwards Honorable Mentions: Drip Aqua, 73 Strings, Flight Path, MDX. TikTok ~493K followers (`@marcelodesignx`). Hand-codes everything: static HTML + GSAP + Lenis + Vimeo. No Framer, no Webflow, no Next.js.

---

# PHASE 0 — Is MDX even right for this?

**Stop and answer before anything else.**

| If the project is… | Use MDX? |
|---|---|
| Agency / portfolio / brand-led marketing site | YES |
| Product launch / single-page brand drop | YES |
| B2B SaaS marketing site (homepage + product pages) | YES — see 73 Strings template |
| Consumer beverage / lifestyle product brand | YES — see Drip Aqua template |
| SaaS dashboard, admin panel, internal tool | NO — utility wins |
| Wholesale marketplace UI / buyer flow | NO — task completion wins |
| Blog / docs / content-heavy | NO — Lenis breaks ⌘F |
| Site that non-devs must edit | NO — use Framer/Webflow, then steal the moves |

If NO, exit this skill and pick another. If YES, continue.

---

# PHASE 1 — Pick the template archetype + write the thesis

Two outputs from this phase: **(a) which template you're cloning**, and **(b) the thesis sentence + feeling word**. You do not move to Phase 2 without both.

## 1.1 — Pick the template archetype

Marcelo has shipped ten distinguishable site archetypes across his real client work and Behance portfolio. Pick the one that fits, then read the matching template in `templates.md`:

| Archetype | Best for | Reference site | Template |
|---|---|---|---|
| **Agency / Studio** | Multi-service creative agency, dev shop, design studio | mdx.so ★ | § A |
| **Solo Portfolio** | Designer / dev personal site, single-operator brand | marcelodesignx.com ★ | § B |
| **Consumer Brand Product** | Beverage, apparel, beauty, single-product DTC | Drip Aqua ★ | § C |
| **B2B SaaS / Enterprise** | Operational SaaS, AI platform, vertical SaaS | 73 Strings ★ | § D |
| **Creative Specialty Studio** | Niche craft studio (animation, sound, branding-only) | Flight Path ★ | § E |
| **FinTech / Capital / Wealth** | Lending, wealth management, advisory, capital firms | Swanson Reserve Capital, Future Mortgage | § F |
| **Web3 / Crypto Product** | Wallets, exchanges, dapps, on-chain platforms | Cirus Web3 Wallet, Fedorgost | § G |
| **AI / Data Product Landing** | Single-product AI tool, demo-led launch page | AI Landing Page concept | § H |
| **Sports / Energy / Performance** | Athletic apparel, energy drinks, performance hardware | Nike, Red Bull, Logitech (concept) | § I |
| **Multi-SKU Fashion / Lifestyle E-Commerce** | Apparel storefront, beauty catalog, lifestyle retail | Lapin e-Commerce | § J |

★ = Awwwards-recognized real client win. Other entries are concept/spec work from Behance — Marcelo's archetype thinking, not necessarily a finished production site. Treat them as visual/structural templates, not as proof points.

**Rule:** if your project doesn't fit any archetype, you are probably not in MDX territory. Re-check Phase 0. If two archetypes seem to fit, pick the one whose **feeling** matches better, not the one whose **industry** matches better.

## 1.2 — Write the thesis sentence

The thesis is what the site is supposed to make a visitor *feel* in their body in the first 5 seconds. Not what the company does. Not the value prop. The **feeling**.

- mdx.so thesis: *"A first impression that feels like opening an expensive object."*
- Drip Aqua thesis (inferred): *"Cold water hitting your face after a long day."*
- Flight Path thesis (inferred): *"The instant runway lights flick on at dusk."*
- 73 Strings thesis (inferred): *"The quiet authority of a senior partner at the head of the table."*

**Rules for the thesis:**
- Concrete, sensory, present tense
- Names a physical experience the visitor would recognize
- Has nothing to do with the product category
- Fits in one breath

**Banned thesis patterns (these guarantee slop):**
- "Premium / luxury / high-end [anything]"
- "Modern, clean, minimal"
- "Trustworthy and professional"
- Anything with "experience," "journey," or "solution" as the noun
- Anything that could equally apply to 5 other companies

## 1.3 — Pick the feeling word

One word. Governs every subsequent decision. If a section doesn't serve the feeling word, it gets cut.

Examples: *anticipation, gravity, stillness, breath, pressure, warmth, wake, hush, charge*.

**Gate:** Read the archetype, thesis, and feeling word out loud. If the thesis sounds like a brand brief, throw it out and try again. It should sound like the start of a poem.

---

# PHASE 2 — The Asset Manifest (lock decisions before HTML exists)

You are not allowed to write HTML in Phase 2. You are listing the specific, named assets the page will use. **Every entry must be a decision, not a placeholder.** The chosen template in `templates.md` gives you defaults — override only with intent.

## 2.1 — Typography (pick exactly two faces)

```
Sans (primary):    [name] — [why this face fits the feeling word]
Display (accent):  [name] — [why this face fits the feeling word]
```

**Allowed:**
- Sans: Aventa (Marcelo's, paid), Geist Sans, Satoshi, General Sans, Söhne (paid), Neue Haas Grotesk (paid)
- Display: Orlean (Marcelo's), Cabinet Grotesk, PP Editorial New, PP Neue Montreal (paid), Tobias (paid)

**Banned:** Inter, Roboto, Open Sans, Helvetica, Arial, Poppins, Montserrat, system-ui. These kill the feel on contact. **No exceptions.**

## 2.2 — Color (exactly 4 values, no more)

```
Base dark:   #0A0A0A or #070707  (NOT #000 — too harsh)
Off-white:   #F5F2EC (warm) or #F0F0F0 (cool)
Signal:      ONE accent — used on ≤3% of pixels
Mute:        ONE muted neutral for body text and dividers
```

Marcelo's mdx.so signal: `#FF8200` / `#FF6711` orange. Pick one signal — name it, hex it, commit. **The blur atmosphere layers carry the rest of the color richness; the palette stays at 4.**

## 2.3 — The blur atmosphere plan

2–4 stacked blur PNG/WebP layers at varying parallax speeds. This is the "color richness" without breaking the 4-color rule.

For each layer:
```
Layer 1: [hue gradient, e.g., "purple→pink iridescent at 50% opacity"]
         position: [top-right / bottom-left / etc.]
         parallax speed: [0.2 / 0.5 / 0.8]
         file: hero-layer-1.webp (~60–70 quality)
         mobile variant: hero-layer-1-mobile.webp
```

You need at least one blur atmosphere section. If you don't, the page reads as flat slop.

## 2.4 — The 3D / decorative orb plan

One signature decorative object per page. **Renderable offline as WebP, not live WebGL.**

Pick one:
- Glass orb / globe with internal refraction
- Particle field (still WebP, animated only via slow CSS rotate/translate)
- Liquid metal blob
- Volumetric cloud
- Rendered product (only if the site has a product to show)

**Banned:** Spinning 3D `.glb` models loaded with three.js. They tank performance and feel try-hard. Render once, ship as WebP.

## 2.5 — Copy lines (write them now, not later)

Write these exact strings in advance. Do not wait until the HTML phase.

```
Loader cycle (5 lines, each ≤4 words):
  1. ____________________
  2. ____________________
  3. ____________________
  4. ____________________
  5. ____________________

Hero headline (two clauses, each ≤4 words):
  Line A: ____________________
  Line B: ____________________

Hero subhead (≤22 words, sentence case):
  ____________________

Atmosphere section headline:
  ____________________

CTA primary (1–3 words, all caps when on a button):
  ____________________

Contact section hook (one question):
  ____________________

Footer one-liner (one wistful sentence):
  ____________________
```

**Allowed verbs:** feel, breathe, resonate, evoke, brew, craft, shape, build, hold, wake.
**Banned verbs:** leverage, optimize, scale, transform, empower, unlock, elevate, drive, enable.
**Banned phrases:** "next-generation," "AI-powered," "world-class," "innovative solutions," "cutting-edge," "best-in-class," "-as-a-Service," "industry-leading."

If a copy slot reads like it could be on any other site, rewrite it.

## 2.6 — The custom moments list (his real signature)

Marcelo's sites have **3–5 unmistakably handmade interactions** per page. Pick at least 3:

- [ ] **Custom-drawn SVG hover** in nav (e.g., the wave that draws on hover, mdx.so's `waveButton`)
- [ ] **Two-bar SVG close-X** that rotates from `+` to `×` with each bar on its own timeline (50ms offset so the X "snaps")
- [ ] **Loader copy mask** — 5 lines stacked in a fixed-height mask, scrolling vertically as the counter ticks
- [ ] **Character-roll CTA hover** — every character translates up, replaced by the same character entering from below, staggered 20ms
- [ ] **Magnetic floating cursor target** — a button drifts toward the cursor inside a 60px radius, eased back on leave
- [ ] **Asymmetric navbar with vertical divider** — logo-left, divider, custom SVG, then center, then CTA + menu toggle on right
- [ ] **Filter pill row** that reflows the gallery via GSAP `Flip` (not opacity fade)
- [ ] **Vimeo lightbox with custom chrome** — never default Vimeo or YouTube
- [ ] **Section-anchored ellipse blurs** drifting on slow CSS-driven rotation, layered behind menu and atmosphere panels
- [ ] **Hover-reveal video preview on project cards** — autoplay/muted, swaps the still on hover

**Gate:** thesis ✓, feeling word ✓, archetype ✓, two named typefaces ✓, four hex codes ✓, blur layer plan ✓, decorative orb decision ✓, all copy lines written ✓, ≥3 custom moments locked ✓. Only then continue.

---

# PHASE 3 — The Page Scaffold (HTML first, no animation yet)

You write the HTML with the data-attribute system already wired, but the JS hasn't been written. **The page should already render correctly without JS** — animations are an enhancement layer.

## 3.1 — File structure (mirror Marcelo's exactly)

```
/index.html
/dist/css/homepage.min.css         (one CSS file per page)
/dist/js/pages/homepage.min.js     (one JS file per page)
/dist/js/shared.min.js             (loader, lenis, button hover, fades — shared)
/assets/images/                    (logos, icons inline as SVG, not <img>)
/assets/atmosphere/                (blur layer WebPs)
/assets/render/                    (3D-rendered orbs/products as WebP)
/fonts/                            (self-hosted .woff2 — never Google Fonts CDN)
```

## 3.2 — Page recipe

Use the section order from your chosen template in `templates.md`. The default order (Agency archetype) is:

1. **Loader** (Phase 4 will animate it)
2. **Navbar** — three regions: left (logo + divider + custom SVG hover), center (3 links max), right (CTA + menu toggle). All wrapped in `data-loader-fade-element-negative`.
3. **Hero** — full viewport, two-clause headline, one subhead, one CTA, blur layers stacked behind. NOT centered if the feeling word is `gravity`, `wake`, or `pressure` — offset the headline left or right.
4. **Atmosphere section** — short emotional headline + decorative orb. The breath between hero and content.
5. **Showreel / signature media** — Vimeo lightbox triggered by a small `watch ›` link.
6. **Project / service showcase** — filterable. Filter pills use Flip layout. Cards reveal video on hover.
7. **Philosophy / about beat** — one bold headline, one paragraph, one tertiary CTA.
8. **Contact** — emotional hook headline, form with service-interest checkboxes, character-roll send button.
9. **Footer** — wistful one-liner, email, social row, legal small caps, scroll-top right.

Other archetypes vary the order — see `templates.md`.

**Fail-fast check at end of Phase 3:**
- [ ] Page renders correctly with JavaScript disabled
- [ ] No `<img>` for logos or UI icons — all inline SVG
- [ ] No Tailwind utility classes (use BEM-named classes: `.hero__layer--mobile`, `.navbar-left__logo-wrapper`)
- [ ] No `100vh` on iOS-targeted styles (use `100dvh`)
- [ ] Three navlinks max in primary nav

---

# PHASE 4 — Motion Choreography

This is where slop is most likely to creep in. Rule: **vary the easing and the variant across sections.** If every element fades up with `power3.out`, you've made a templated site.

## 4.1 — The five entrance variants (use at least 3)

| Attribute | What it does | When to use |
|---|---|---|
| `data-fade-y` | Translate up + autoAlpha | Body paragraphs, supporting text |
| `data-fade-clip` | Mask reveal from below (clip-path inset) | Headlines — feels like a curtain rising |
| `data-fade-scale` | Scale 0.96 → 1 + autoAlpha | Images, cards, decorative objects |
| `data-fade-autoalpha` | Pure opacity | Backgrounds, blur layers |
| `data-fade-stagger` | Children stagger 30ms | Filter pills, nav links, footer columns |

**Rule:** every section uses a different variant for its primary element. Don't fade-up everything.

## 4.2 — The signature ease

Define once, reuse forever:
```js
CustomEase.create("mdx", "M0,0 C0.6,0.01 0.05,0.95 1,1");
```

- Hero / loader: `mdx`
- Section reveals: `expo.out`
- Hover micro-interactions: `power2.out`
- Cursor magnet return: `power3.inOut`
- **Never** `linear`, `ease-in-out`, or default JS easings.

## 4.3 — The loader sequence (this IS the brand)

```
0.0s   → black frame, logo small at top
0.0s   → counter starts ticking 0 → 100, eased "mdx" over ~2.0s
0.0s   → 5-line copy cycler in masked container, line changes every 700ms
2.0s   → counter exits up (-40px, autoAlpha 0)
2.0s   → progress bar wipes from 100% to 0% (transformOrigin right)
2.4s   → loader fades out (autoAlpha 0)
2.6s   → [data-loader-fade-element-positive] elements rise (y: 40→0, stagger 0.05)
2.6s   → [data-loader-fade-element-negative] elements descend (y: -40→0, stagger 0.05)
2.9s   → ScrollTrigger.refresh() — only NOW do scroll animations measure
```

**Gate:** loader plays on first visit. On internal navigation, it's suppressed (sessionStorage flag).

## 4.4 — Hover and scroll fidelity

- Lenis: `lerp: 0.1`, `smoothWheel: true`, `syncTouch: false` (touch stays native)
- Buttons with `data-button-animate-chars`: each char translates `y: -100%` while a duplicate enters from `y: 100%`, staggered 20ms.
- Magnetic `data-floating` elements: track cursor inside bounding rect, move toward it eased `power2.out`, return on leave at `power3.inOut`.
- Scroll observer writes `data-scrolling-direction="up|down"` on `<html>` so the navbar can hide/show.

## 4.5 — The custom SVG moments

For each item from your Phase 2.6 list, write the actual SVG and wire its animation. See `animation-system.md` for working JS.

---

# PHASE 5 — Slop Audit (gate before declaring done)

Run this checklist. Any **NO** is a hard block. Fix before shipping.

## 5.1 — Copy audit

- [ ] No banned verbs (leverage, optimize, scale, transform, empower, unlock, elevate, drive, enable)
- [ ] No banned phrases ("next-generation," "world-class," etc.)
- [ ] Hero headline could not be moved to another company's site without modification
- [ ] At least one copy line uses the feeling word from Phase 1
- [ ] Footer one-liner makes the reader pause for half a second

## 5.2 — Visual audit

- [ ] Exactly 4 colors on the page (count them — if 5+, kill one)
- [ ] No `box-shadow` on cards (depth comes from blur layers, not shadows)
- [ ] No emoji anywhere in copy or UI
- [ ] No icon library defaults (Lucide / Heroicons / Material). Custom SVG only.
- [ ] At least one section is asymmetric — content offset left or right, not centered
- [ ] At least 3 different fade variants used across the page

## 5.3 — Motion audit

- [ ] Loader plays on first visit, suppresses on internal nav
- [ ] At least one custom CustomEase, used at least 4 times
- [ ] Lenis scroll feels weightless on trackpad AND mouse wheel
- [ ] At least 3 of the Phase 2.6 custom moments are present
- [ ] Headlines animate via clip-path or mask reveal — not plain fade-up
- [ ] Section transitions feel different from each other

## 5.4 — Performance audit

- [ ] Page weight under 2MB total
- [ ] All images WebP, quality 60–72
- [ ] First Contentful Paint under 1.5s on 4G
- [ ] No `<link href="fonts.googleapis.com">` — fonts self-hosted as `.woff2`
- [ ] Fonts loaded with `font-display: swap`
- [ ] Loader HTML inline in document, visible before any external CSS or JS

## 5.5 — The 5-second feel test

Open the URL on a fresh device. Watch for 5 seconds. Without reading any copy, can you name the feeling word from Phase 1?

If no, the design is wrong. Not the copy. Go back to Phase 4.

---

# PHASE 6 — Ship

- Final file structure matches Phase 3.1 exactly
- One `homepage.min.css` and one `homepage.min.js` per route
- Loader inline in `<body>` before any other element
- Vimeo Player API loaded only if a showreel exists
- All paid GSAP plugins (SplitText, Flip, InertiaPlugin, Draggable, CustomEase) either licensed or replaced with documented free fallbacks (`animation-system.md`)
- Mobile tested at 375×667 — blur layers swap to `-mobile` variants
- Scroll-top button right of footer, fixed, fades in after 30vh scroll

---

# Reference companions

- `templates.md` — five cloneable site templates by archetype (Agency, Solo Portfolio, Consumer Brand, B2B SaaS, Creative Specialty)
- `animation-system.md` — full data-attribute reference + working JS for every handler
- `starter-template.html` — drop-in HTML scaffold with all data-attributes pre-wired

---

# The Stack (exact, from production mdx.so source)

```
GSAP 3.13.0  + plugins: ScrollTrigger, SplitText, Flip, Draggable,
                        InertiaPlugin, TextPlugin, CustomEase
Lenis 1.3.4  (smooth scroll)
Vimeo Player API  (showreel lightbox — never YouTube)

No framework. Hand-rolled HTML/CSS/JS.
Build artifacts at /dist/css/{page}.min.css and /dist/js/pages/{page}.min.js
One CSS + one JS per page route. ES modules.
```

CDN sources:
```html
<script src="https://cdn.jsdelivr.net/npm/gsap@3.13.0/dist/gsap.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/gsap@3.13.0/dist/ScrollTrigger.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/gsap@3.13.0/dist/SplitText.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/gsap@3.13.0/dist/Flip.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/gsap@3.13.0/dist/Draggable.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/gsap@3.13.0/dist/InertiaPlugin.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/gsap@3.13.0/dist/TextPlugin.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/gsap@3.13.0/dist/CustomEase.min.js"></script>
<script src="https://unpkg.com/lenis@1.3.4/dist/lenis.min.js"></script>
<script src="https://player.vimeo.com/api/player.js"></script>
```

---

# The tl;dr the model can fall back to

When a user says "make it like mdx.so":

1. **Phase 0:** confirm MDX fits the project type.
2. **Phase 1:** pick template archetype from `templates.md`. Write thesis sentence + feeling word.
3. **Phase 2:** lock fonts, colors, blur layers, decorative orb, copy lines, ≥3 custom moments.
4. **Phase 3:** build HTML with data-attrs, no animation yet.
5. **Phase 4:** choreograph motion with varied variants and ≥1 CustomEase.
6. **Phase 5:** run the slop audit. Fix every NO.
7. **Phase 6:** ship to file structure spec.

Skip any phase, you ship slop. Run them in order, you ship something that feels handmade.
