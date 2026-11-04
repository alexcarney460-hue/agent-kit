---
name: liquid-glass
description: Use when building premium UI with Apple visionOS-style glass effects — glass cards, floating buttons, overlays, or any frosted-glass aesthetic using liquid-glass-js (WebGL, vanilla JS, no build step).
---

# Liquid Glass JS

## Overview

WebGL-powered Apple-inspired liquid glass effects. No npm, no build step — pure vanilla JS with real-time refraction, blur, and masking. Requires WebGL 2.0.

**Source:** https://github.com/dashersw/liquid-glass-js

## Setup (vanilla / CDN)

```html
<link rel="stylesheet" href="styles.css" />
<link rel="stylesheet" href="glass.css" />
<script src="https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js"></script>
<script src="container.js"></script>
<script src="button.js"></script>
```

Files `container.js`, `button.js`, `styles.css`, `glass.css` must be copied from the repo into the project — they are not on npm.

## Next.js Integration

Client-only. Use dynamic import with `ssr: false`:

```ts
const GlassButton = dynamic(() => import('@/components/GlassButton'), { ssr: false })
```

Do not import liquid-glass-js files in server components or at module top-level.

## Core API

### Container

Base glass element. Wrap any DOM node in a glass layer.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `borderRadius` | number | 48 | Corner radius (px) |
| `type` | string | `'rounded'` | `'rounded'` \| `'circle'` \| `'pill'` |
| `tintOpacity` | number | 0.2 | Overlay transparency (0–1) |

```js
const card = new Container({
  borderRadius: 32,
  type: 'rounded',
  tintOpacity: 0.15,
})
document.body.appendChild(card.element)
card.addChild(myContentElement)
```

**Methods:** `addChild(el)`, `removeChild(el)`, `updateSizeFromDOM()`

### Button

Extends Container. Adds text, click handling, and warp distortion.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `text` | string | `'Button'` | Label text |
| `size` | number | 48 | Font size (px) |
| `type` | string | `'rounded'` | Same as Container |
| `onClick` | function | null | `(text) => void` |
| `warp` | boolean | false | Center distortion effect |
| `tintOpacity` | number | 0.2 | Same as Container |

```js
const btn = new Button({
  text: 'Save Changes',
  size: 28,
  type: 'pill',
  tintOpacity: 0.4,
  warp: true,
  onClick: (text) => console.log(`${text} clicked`),
})
document.body.appendChild(btn.element)
```

## Advanced: Global Effect Tuning

Override defaults via `window.glassControls` before instantiating:

```js
window.glassControls = {
  edgeIntensity: 0.02,  // 0–0.1  — refraction at edges
  rimIntensity: 0.08,   // 0–0.2  — rim lighting
  blurRadius: 7.0,      // 1–15   — background blur
  tintOpacity: 0.3,     // 0–1.0  — gradient overlay
}
```

## Shape Types

- `'rounded'` — rounded rectangle, use `borderRadius` to control corners
- `'circle'`  — perfect circle, ignores `borderRadius`
- `'pill'`    — full-radius capsule, auto-sizes

## Constraints

- **WebGL 2.0 required** — Chrome 80+, Firefox 75+, Safari 14+, Edge 80+
- **No SSR** — must run in browser only
- **No npm package** — copy files from repo directly
- **html2canvas dependency** — needed for page sampling (background capture)
- Not compatible with `canvas` or `offscreenCanvas`-restricted environments
