# MDX Animation System — Data-Attribute Reference

This is Marcelo's declarative animation harness reverse-engineered from `mdx.so/dist/js/pages/homepage.min.js`. Each attribute is a contract: tag the HTML, the boot script wires it.

## Boot Order

```js
// 1. Register GSAP plugins
gsap.registerPlugin(ScrollTrigger, SplitText, Flip, Draggable, InertiaPlugin, TextPlugin, CustomEase);

// 2. Define his signature ease
CustomEase.create("mdx", "M0,0 C0.6,0.01 0.05,0.95 1,1");

// 3. Initialize Lenis
const lenis = new Lenis({
  lerp: 0.1,
  smoothWheel: true,
  syncTouch: false,    // touch stays native — Marcelo does this
  wheelMultiplier: 1,
});
lenis.on('scroll', ScrollTrigger.update);
gsap.ticker.add((time) => lenis.raf(time * 1000));
gsap.ticker.lagSmoothing(0);

// 4. Run loader, THEN init scroll-driven animations after loader finishes.
//    Don't initialize ScrollTrigger before the loader exits or measurements are wrong.
runLoader().then(() => {
  initFades();
  initButtons();
  initFloating();
  initFilter();
  initMenu();
  initForm();
  ScrollTrigger.refresh();
});
```

## Loader (`data-loader-*`)

```html
<div class="loader" data-loader>
  <div class="loader__counter" data-loader-counter>0</div>
  <div class="loader__bar" data-loader-bar></div>
  <div class="loader__copy" data-loader-copy>Brewing your experience</div>
</div>
```

```js
async function runLoader() {
  const counter = document.querySelector('[data-loader-counter]');
  const bar     = document.querySelector('[data-loader-bar]');
  const copy    = document.querySelector('[data-loader-copy]');
  const lines = ["Perfection takes a moment", "Brewing your experience", "Worth the wait"];

  // Cycle copy lines every 700ms
  let i = 0;
  const cycle = setInterval(() => { copy.textContent = lines[++i % lines.length]; }, 700);

  // Drive counter + bar from 0 → 100 over 2s
  const obj = { v: 0 };
  await gsap.to(obj, {
    v: 100, duration: 2.0, ease: "mdx",
    onUpdate: () => {
      counter.textContent = Math.round(obj.v);
      bar.style.transform = `scaleX(${obj.v / 100})`;
    }
  });
  clearInterval(cycle);

  // Exit: counter rises out, bar wipes, content reveals (positive/negative split)
  const tl = gsap.timeline();
  tl.to('[data-loader-counter]', { y: -40, autoAlpha: 0, duration: 0.5, ease: "mdx" })
    .to('[data-loader-bar]', { scaleX: 0, transformOrigin: "100% 50%", duration: 0.5, ease: "mdx" }, "<")
    .to('[data-loader]', { autoAlpha: 0, duration: 0.4 })
    .from('[data-loader-fade-element-positive]', { y: 40, autoAlpha: 0, stagger: 0.05, duration: 0.7, ease: "mdx" }, "-=0.2")
    .from('[data-loader-fade-element-negative]', { y: -40, autoAlpha: 0, stagger: 0.05, duration: 0.7, ease: "mdx" }, "<");
}
```

## Fade Variants (entrance on viewport enter)

The HTML opts in to a variant — JS finds them and animates with ScrollTrigger.

```html
<h2 data-fade-y data-fade-delay="0.1">Every experience begins with a feeling</h2>
<p data-fade-clip>Some descriptive line that wipes in from below</p>
<img data-fade-scale src="..." />
<div data-fade-autoalpha>...</div>
<div data-fade-y data-fade-start="top 90%" data-fade-end-mobile="top 95%">...</div>
```

```js
function initFades() {
  // 1. data-fade-y — translate up + autoAlpha
  document.querySelectorAll('[data-fade-y]').forEach(el => {
    const delay  = parseFloat(el.dataset.fadeDelay)  || 0;
    const offset = parseFloat(el.dataset.fadeInOffset) || 60;
    const start  = el.dataset.fadeStart || "top 85%";
    const startMobile = el.dataset.fadeStartMobile || start;

    gsap.from(el, {
      y: offset, autoAlpha: 0, duration: 1.0, delay, ease: "mdx",
      scrollTrigger: {
        trigger: el,
        start: window.matchMedia('(max-width: 991px)').matches ? startMobile : start,
        toggleActions: "play none none none"
      }
    });
  });

  // 2. data-fade-clip — clip-path reveal from bottom
  document.querySelectorAll('[data-fade-clip]').forEach(el => {
    gsap.from(el, {
      clipPath: "inset(100% 0 0 0)",
      duration: 1.1, ease: "mdx",
      scrollTrigger: { trigger: el, start: "top 88%" }
    });
  });

  // 3. data-fade-scale — scale from 0.96 + fade
  document.querySelectorAll('[data-fade-scale]').forEach(el => {
    gsap.from(el, {
      scale: 0.96, autoAlpha: 0, duration: 1.0, ease: "mdx",
      scrollTrigger: { trigger: el, start: "top 85%" }
    });
  });

  // 4. data-fade-autoalpha — pure opacity fade for atmospheric layers
  document.querySelectorAll('[data-fade-autoalpha]').forEach(el => {
    gsap.from(el, {
      autoAlpha: 0, duration: 1.4, ease: "power2.out",
      scrollTrigger: { trigger: el, start: "top 92%" }
    });
  });

  // 5. SplitText headlines — character stagger
  document.querySelectorAll('[data-split-headline]').forEach(el => {
    const split = new SplitText(el, { type: "chars,words" });
    gsap.from(split.chars, {
      y: 40, autoAlpha: 0, duration: 0.8, ease: "mdx",
      stagger: 0.025,
      scrollTrigger: { trigger: el, start: "top 80%" }
    });
  });
}
```

**Free fallback for SplitText (paid):** use [`splitting.js`](https://splitting.js.org/) — it adds `<span>` per char with CSS variables, then animate via GSAP staggers the same way.

## Buttons (`data-button-animate-chars` / `data-btn-hover`)

The signature is: hover, the existing label rolls up out of the button, a duplicate label rolls up into place. Like a slot machine.

```html
<button class="btn" data-btn-hover data-button-animate-chars>
  <span class="btn__label" data-label>Let's Talk</span>
  <span class="btn__label btn__label--ghost" aria-hidden="true">Let's Talk</span>
  <span class="btn__icon">›</span>
</button>
```

```css
.btn { position: relative; overflow: hidden; height: 3rem; padding: 0 1.5rem; }
.btn__label { display: inline-block; will-change: transform; }
.btn__label--ghost { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; transform: translateY(110%); }
```

```js
function initButtons() {
  document.querySelectorAll('[data-btn-hover]').forEach(btn => {
    const real  = btn.querySelector('[data-label]');
    const ghost = btn.querySelector('.btn__label--ghost');
    const tl = gsap.timeline({ paused: true })
      .to(real,  { y: "-110%", duration: 0.4, ease: "mdx" })
      .to(ghost, { y: "0%",     duration: 0.4, ease: "mdx" }, "<");
    btn.addEventListener('mouseenter', () => tl.play());
    btn.addEventListener('mouseleave', () => tl.reverse());
  });
}
```

## Floating / Magnetic (`data-floating`)

```html
<button data-floating data-floating-strength="20" class="cta-orb">→</button>
```

```js
function initFloating() {
  document.querySelectorAll('[data-floating]').forEach(el => {
    const strength = parseFloat(el.dataset.floatingStrength) || 18;
    const setX = gsap.quickTo(el, "x", { duration: 0.45, ease: "mdx" });
    const setY = gsap.quickTo(el, "y", { duration: 0.45, ease: "mdx" });
    el.addEventListener('mousemove', (e) => {
      const r = el.getBoundingClientRect();
      const dx = (e.clientX - (r.left + r.width / 2)) / (r.width / 2);
      const dy = (e.clientY - (r.top  + r.height / 2)) / (r.height / 2);
      setX(dx * strength);
      setY(dy * strength);
    });
    el.addEventListener('mouseleave', () => { setX(0); setY(0); });
  });
}
```

## Project Filter (`data-filter-*`) — uses Flip

```html
<div class="filter">
  <button data-filter-target="all" data-filter-status="active">All</button>
  <button data-filter-target="ui">UI/UX</button>
  <button data-filter-target="dev">Development</button>
</div>
<div class="grid" data-filter-group>
  <article data-filter-tags="ui,dev">...</article>
  <article data-filter-tags="ui">...</article>
  ...
</div>
```

```js
function initFilter() {
  const buttons = document.querySelectorAll('[data-filter-target]');
  const items   = document.querySelectorAll('[data-filter-tags]');

  buttons.forEach(btn => btn.addEventListener('click', () => {
    const target = btn.dataset.filterTarget;
    buttons.forEach(b => b.dataset.filterStatus = b === btn ? 'active' : '');

    // Capture state before mutations
    const state = Flip.getState(items);

    items.forEach(item => {
      const tags = (item.dataset.filterTags || '').split(',');
      item.style.display = (target === 'all' || tags.includes(target)) ? '' : 'none';
    });

    Flip.from(state, {
      duration: 0.7, ease: "mdx", absolute: true,
      onEnter: el => gsap.fromTo(el, { autoAlpha: 0, scale: 0.92 }, { autoAlpha: 1, scale: 1, duration: 0.4 }),
      onLeave: el => gsap.to(el, { autoAlpha: 0, duration: 0.3 })
    });
  }));
}
```

**Free fallback for Flip (paid):** keep the same data-attrs but transition with FLIP technique by hand using `getBoundingClientRect()` + transforms, or fall back to a CSS opacity/scale fade only.

## Menu (`data-menu-*`)

```html
<button data-menu-toggle aria-label="Open menu">
  <span class="hamburger"><span></span><span></span></span>
</button>
<nav class="menu" data-menu>
  <a data-menu-heading>Home</a>
  <a data-menu-heading>Projects</a>
  <a data-menu-heading>Services</a>
  <a data-menu-heading>Contact</a>
</nav>
```

```js
function initMenu() {
  const toggle = document.querySelector('[data-menu-toggle]');
  const menu   = document.querySelector('[data-menu]');
  const items  = menu.querySelectorAll('[data-menu-heading]');
  let open = false;

  const tl = gsap.timeline({ paused: true })
    .to(menu, { autoAlpha: 1, duration: 0.4, ease: "mdx" })
    .from(items, { y: 60, autoAlpha: 0, stagger: 0.06, duration: 0.7, ease: "mdx" }, "<0.1");

  toggle.addEventListener('click', () => {
    open = !open;
    open ? tl.play()    : tl.reverse();
    open ? lenis.stop() : lenis.start();   // freeze scroll while menu open
    document.body.dataset.navClear = open ? 'true' : 'false';
  });
}
```

## Form (`data-form-*`)

```html
<form data-form-validate>
  <input name="email" required data-error-message="Please enter a valid email" />
  <button type="submit">Send</button>
  <p data-form-status-message></p>
</form>
```

```js
function initForm() {
  document.querySelectorAll('[data-form-validate]').forEach(form => {
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const status = form.querySelector('[data-form-status-message]');

      // Native HTML5 validation first
      if (!form.checkValidity()) {
        const bad = form.querySelector(':invalid');
        const msg = bad?.dataset.errorMessage || 'Please complete required fields.';
        status.textContent = msg;
        gsap.fromTo(status, { y: -8, autoAlpha: 0 }, { y: 0, autoAlpha: 1, duration: 0.4, ease: "mdx" });
        return;
      }

      // Submit
      try {
        await fetch(form.action, { method: 'POST', body: new FormData(form) });
        status.textContent = 'Sent. We\'ll be in touch.';
        form.reset();
      } catch {
        status.textContent = 'Something went wrong. Try again.';
      }
    });
  });
}
```

## Lenis Scroll Control

- `data-lenis-prevent` on a scrollable child (modal, dropdown) — Lenis ignores wheel events inside it.
- `data-force-scroll` / `data-force-sroll-to="#section"` (yes, his typo) — anchor links Lenis-routed: `lenis.scrollTo('#section', { duration: 1.2, easing: t => 1 - Math.pow(1 - t, 3) })`.

```js
document.querySelectorAll('[data-force-sroll-to], [data-force-scroll]').forEach(a => {
  a.addEventListener('click', (e) => {
    const target = a.dataset.forceSrollTo || a.dataset.forceScroll || a.getAttribute('href');
    if (!target) return;
    e.preventDefault();
    lenis.scrollTo(target, { duration: 1.2, easing: t => 1 - Math.pow(1 - t, 3) });
  });
});
```

## Section Observer (`data-leave-section`, `data-active-bar`)

Drives the dynamic top-bar appearance / accent change as you scroll between sections.

```js
document.querySelectorAll('[data-leave-section]').forEach(section => {
  ScrollTrigger.create({
    trigger: section,
    start: "top 50%", end: "bottom 50%",
    onEnter: () => document.body.dataset.activeBar = section.dataset.leaveSection,
    onEnterBack: () => document.body.dataset.activeBar = section.dataset.leaveSection,
  });
});
```

CSS reacts:
```css
body[data-active-bar="dark"]  .topbar { color: #f5f2ec; mix-blend-mode: difference; }
body[data-active-bar="light"] .topbar { color: #0a0a0a; }
```

## Scroll Direction Tracking

```js
let lastY = 0;
lenis.on('scroll', ({ scroll }) => {
  document.body.dataset.scrollingDirection = scroll > lastY ? 'down' : 'up';
  document.body.dataset.scrollingStarted = scroll > 100 ? 'true' : 'false';
  lastY = scroll;
});
```

CSS hides the topbar on scroll-down, reveals on scroll-up:
```css
body[data-scrolling-direction="down"][data-scrolling-started="true"] .topbar {
  transform: translateY(-100%);
}
.topbar { transition: transform 0.4s cubic-bezier(0.6, 0.01, 0.05, 0.95); }
```

## Free-Plugin Substitution Map

| Marcelo uses | Free swap |
|---|---|
| GSAP SplitText | [Splitting.js](https://splitting.js.org/) |
| GSAP Flip | hand-rolled FLIP with `getBoundingClientRect()` |
| GSAP InertiaPlugin | drop it; use plain `gsap.to({ duration, ease: "power2.out" })` |
| GSAP Draggable | native `pointermove` listeners |
| GSAP CustomEase | inline cubic-bezier `"cubic-bezier(0.6, 0.01, 0.05, 0.95)"` |

If the user has a Club GSAP license, prefer the originals — same final feel.
