// Deterministic motion graphics: every frame is a pure function of t (seconds). No CSS transitions, no timers,
// no randomness except a seeded PRNG. render/render.mjs calls window.__scene.render(t) and screenshots the stage.
// URL params: ?format=landscape|vertical  &t=<seconds> (preview a still in a normal browser).
;(() => {
  const params = new URLSearchParams(location.search)
  const V = params.get('format') === 'vertical'
  const W = V ? 1080 : 1920
  const H = V ? 1920 : 1080
  const C = window.CUES
  const ENV = window.VOICE_ENV || { fps: 60 }
  const L = (land, vert) => (V ? vert : land)

  // ------------------------------------------------------------------ math
  const clamp = (x, a = 0, b = 1) => (x < a ? a : x > b ? b : x)
  const lerp = (a, b, p) => a + (b - a) * p
  const E = {
    lin: (p) => p,
    out2: (p) => 1 - (1 - p) * (1 - p),
    out3: (p) => 1 - (1 - p) ** 3,
    out4: (p) => 1 - (1 - p) ** 4,
    outExpo: (p) => (p >= 1 ? 1 : 1 - Math.pow(2, -10 * p)),
    in2: (p) => p * p,
    in3: (p) => p * p * p,
    inExpo: (p) => (p <= 0 ? 0 : Math.pow(2, 10 * p - 10)),
    inOut2: (p) => (p < 0.5 ? 2 * p * p : 1 - (-2 * p + 2) ** 2 / 2),
    inOut3: (p) => (p < 0.5 ? 4 * p * p * p : 1 - (-2 * p + 2) ** 3 / 2),
    inOutExpo: (p) => (p <= 0 ? 0 : p >= 1 ? 1 : p < 0.5 ? Math.pow(2, 20 * p - 10) / 2 : (2 - Math.pow(2, -20 * p + 10)) / 2),
    outBack: (p) => 1 + 2.4 * (p - 1) ** 3 + 1.4 * (p - 1) ** 2,
    spring: (p) => (p >= 1 ? 1 : 1 - Math.exp(-6.5 * p) * Math.cos(11 * p)),
  }
  // The app's own easing curve (--ease-out: cubic-bezier(0.22, 1, 0.36, 1)).
  E.app = bezier(0.22, 1, 0.36, 1)
  E.swift = bezier(0.65, 0, 0.1, 1)
  function bezier(x1, y1, x2, y2) {
    const cx = 3 * x1, bx = 3 * (x2 - x1) - cx, ax = 1 - cx - bx
    const cy = 3 * y1, by = 3 * (y2 - y1) - cy, ay = 1 - cy - by
    const sx = (u) => ((ax * u + bx) * u + cx) * u
    const sy = (u) => ((ay * u + by) * u + cy) * u
    const dx = (u) => (3 * ax * u + 2 * bx) * u + cx
    return (x) => {
      if (x <= 0) return 0
      if (x >= 1) return 1
      let u = x
      for (let i = 0; i < 8; i++) {
        const d = dx(u)
        if (Math.abs(d) < 1e-6) break
        u -= (sx(u) - x) / d
      }
      return sy(clamp(u))
    }
  }
  const P = (t, a, b, e = E.outExpo) => e(clamp((t - a) / (b - a)))
  /** Keyframes [[t, value, easeIntoThisKey?], ...] -> value at t (numbers or flat objects of numbers). */
  function kf(t, keys) {
    const cp = (v) => (typeof v === 'number' ? v : { ...v })
    if (t <= keys[0][0]) return cp(keys[0][1])
    for (let i = 1; i < keys.length; i++) {
      if (t <= keys[i][0]) {
        const [t0, v0] = keys[i - 1]
        const [t1, v1, e] = keys[i]
        const p = (e || E.inOut3)(clamp((t - t0) / (t1 - t0)))
        if (typeof v0 === 'number') return lerp(v0, v1, p)
        const o = {}
        for (const k in v1) o[k] = lerp(v0[k] ?? v1[k], v1[k], p)
        return o
      }
    }
    return cp(keys[keys.length - 1][1])
  }
  /** Carry every property forward so camera keys can list only what changes. */
  function track(keys) {
    let prev = {}
    return keys.map(([t, v, e]) => {
      prev = { ...prev, ...v }
      return [t, prev, e]
    })
  }
  function rng(seed) {
    let s = seed >>> 0
    return () => {
      s = (s + 0x6d2b79f5) >>> 0
      let r = Math.imul(s ^ (s >>> 15), 1 | s)
      r = (r + Math.imul(r ^ (r >>> 7), 61 | r)) ^ r
      return ((r ^ (r >>> 14)) >>> 0) / 4294967296
    }
  }
  const env = (name, tt) => {
    const a = ENV[name]
    if (!a || tt < 0) return 0
    const i = tt * ENV.fps
    const i0 = Math.floor(i)
    if (i0 + 1 >= a.length) return 0
    return lerp(a[i0], a[i0 + 1], i - i0)
  }

  // ------------------------------------------------------------------ DOM helpers
  const stage = document.getElementById('stage')
  stage.style.width = `${W}px`
  stage.style.height = `${H}px`
  function h(tag, cls, parent, html) {
    const el = document.createElement(tag)
    if (cls) el.className = cls
    if (html != null) el.innerHTML = html
    if (parent) parent.appendChild(el)
    return el
  }
  const css = (el, o) => {
    for (const k in o) el.style[k] = o[k]
  }
  const show = (el, on) => {
    el.style.display = on ? '' : 'none'
  }
  const MIC_SVG = (size, color, sw = 2) =>
    `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="${color}" stroke-width="${sw}" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3Zm-7 9a7 7 0 0 0 14 0M12 19v3"/></svg>`

  /**
   * Kinetic text block. lines: array of strings; words wrapped in masks. Markup inside a word:
   *   [hl]word  -> highlighted with a brand box that wipes in
   *   {c:#hex}word -> coloured word
   */
  function textBlock(parent, cls, lines, x, y, opts = {}) {
    const el = h('div', `t ${cls}`, parent)
    css(el, { left: `${x}px`, top: `${y}px`, textAlign: opts.align || 'left' })
    if (opts.align === 'center') css(el, { transform: 'translateX(-50%)' })
    if (opts.color) el.style.color = opts.color
    const ws = []
    const boxes = []
    lines.forEach((line) => {
      const ln = h('span', 'line', el)
      line.split(' ').forEach((word, i, arr) => {
        let color = null
        let hl = false
        if (word.startsWith('[hl]')) {
          hl = true
          word = word.slice(4)
        }
        const m = word.match(/^\{c:(#[0-9a-f]+)\}(.*)$/i)
        if (m) {
          color = m[1]
          word = m[2]
        }
        const mask = h('span', 'mask', ln)
        const w = h('span', 'w', mask)
        if (hl) {
          const wrap = h('span', 'hl', w)
          const box = h('span', 'box', wrap)
          h('span', null, wrap, word)
          wrap.style.color = '#fff'
          boxes.push(box)
        } else w.textContent = word
        if (color) w.style.color = color
        ws.push(w)
        if (i < arr.length - 1) ln.appendChild(document.createTextNode(' '))
      })
    })
    return { el, ws, boxes }
  }
  /** Masked word reveal in (slide up from the mask) and out (slide up and away). */
  function animWords(b, t, a) {
    const { tin, stagger = 0.055, dur = 0.8, from = 105, tout = 1e9, ostagger = 0.025, odur = 0.4, to = -105, ein = E.outExpo, rot = 0 } = a
    b.ws.forEach((w, i) => {
      const pi = P(t, tin + i * stagger, tin + i * stagger + dur, ein)
      const po = P(t, tout + i * ostagger, tout + i * ostagger + odur, E.inExpo)
      const y = (1 - pi) * from + po * to
      w.style.transform = `translate3d(0,${y}%,0) rotate(${(1 - pi) * rot}deg)`
    })
    show(b.el, t >= tin - 0.01 && t < tout + b.ws.length * ostagger + odur + 0.01)
  }
  const fade = (el, t, a, b, c, d, e = E.out3) => {
    const o = Math.min(P(t, a, b, e), 1 - P(t, c, d, E.in2))
    el.style.opacity = o
    show(el, o > 0.001)
    return o
  }

  // ------------------------------------------------------------------ scaffold
  const bg = h('div', 'full', stage)
  const glows = [0, 1, 2].map(() => h('div', 'glow', bg))
  const warm = h('div', 'full', stage)
  warm.style.background = 'radial-gradient(120% 90% at 30% 40%, #fdf1d4 0%, #f8ecd0 45%, #f6f5f1 100%)'
  const dark = h('div', 'full', stage)
  dark.style.background = 'radial-gradient(90% 80% at 70% 45%, #262a4a 0%, #1d1c1a 62%)'
  const world = h('div', null, stage)
  world.id = 'world'
  world.style.zIndex = 2
  // Vertical cut: a paper scrim keeps the headline area clean when a device is zoomed in below it.
  const scrim = h('div', 'full', stage)
  css(scrim, { zIndex: 3, height: '900px', background: 'linear-gradient(180deg, #f6f5f1 0%, #f6f5f1 74%, rgb(246 245 241 / 0%) 100%)' })
  const textLayer = h('div', 'full', stage)
  textLayer.style.zIndex = 4
  const lines = h('div', 'full', stage)
  lines.innerHTML = `<svg class="lines" width="${W}" height="${H}"></svg>`
  const lineSvg = lines.firstChild
  lines.style.zIndex = 5
  const callLayer = h('div', 'full', stage)
  callLayer.style.zIndex = 6

  // ------------------------------------------------------------------ devices
  function makePhone(parent) {
    const el = h('div', 'phone', parent)
    const btns = [
      ['left', -3, 170, 34],
      ['left', -3, 232, 66],
      ['left', -3, 312, 66],
      ['right', 420, 262, 104],
    ]
    btns.forEach(([, x, y, hh]) => css(h('i', 'btn-side', el), { left: `${x}px`, top: `${y}px`, height: `${hh}px` }))
    const screen = h('div', 'screen', el)
    const sb = h('div', 'statusbar', screen)
    sb.innerHTML = `<span class="time">10:24</span><span class="island"></span><span class="icons"><span class="sig"><i style="height:5px"></i><i style="height:7px"></i><i style="height:9px"></i><i style="height:11px"></i></span><span class="batt"><i></i></span></span>`
    const view = h('div', 'view', screen)
    h('div', 'home-ind', screen)
    const glare = h('div', 'glare', screen)
    const imgs = {}
    const overlay = h('div', 'full', view) // UI-space overlays (rings, highlights) above the images
    overlay.style.zIndex = 2
    return {
      el,
      view,
      overlay,
      glare,
      imgs,
      add(name) {
        const im = h('img', null, view)
        im.src = `media/${name}.webp`
        im.style.zIndex = 1
        imgs[name] = im
        return im
      },
      /** Screen-space point (CSS px of the 390x844 capture) -> phone-local point. */
      pt: (x, y) => [16 + x, 16 + 47 + y],
      center: [211, 461.5],
    }
  }
  function makeLaptop(parent) {
    const el = h('div', 'laptop', parent)
    h('div', 'deck', el)
    h('div', 'lid', el)
    const scr = h('div', 'lscreen', el)
    h('i', 'cam', el)
    const imgs = {}
    const overlay = h('div', 'full', scr)
    overlay.style.zIndex = 2
    return {
      el,
      scr,
      overlay,
      imgs,
      add(name) {
        const im = h('img', null, scr)
        im.src = `media/${name}.webp`
        im.style.zIndex = 1
        imgs[name] = im
        return im
      },
      pt: (x, y) => [24 + x, 24 + y],
      center: [744, 490],
    }
  }
  /** focus point f (device-local) lands at stage (x, y), scaled by s, rotated in 3D around it. */
  function place(dev, c) {
    const [fx, fy] = c.fx != null ? [c.fx, c.fy] : dev.center
    dev.el.style.transform = `translate3d(${c.x}px,${c.y}px,0) rotateX(${c.rx || 0}deg) rotateY(${c.ry || 0}deg) rotateZ(${c.rz || 0}deg) scale(${c.s}) translate3d(${-fx}px,${-fy}px,0)`
    dev.el.style.opacity = c.op ?? 1
    show(dev.el, (c.op ?? 1) > 0.001)
  }
  /** Show exactly these screen images with these opacities. */
  function screens(dev, list) {
    for (const k in dev.imgs) dev.imgs[k].style.display = 'none'
    list.forEach(([name, o]) => {
      const im = dev.imgs[name]
      if (!im || o <= 0.001) return
      im.style.display = ''
      im.style.opacity = o
    })
  }
  /** Anchor inside a device: returns a function giving its projected stage position. */
  function anchor(dev, x, y) {
    const a = h('i', null, dev.overlay)
    css(a, { position: 'absolute', left: `${x}px`, top: `${y}px`, width: '1px', height: '1px' })
    return () => {
      const r = a.getBoundingClientRect()
      return [r.left, r.top]
    }
  }

  const laptop = makeLaptop(world)
  const phone = makePhone(world)
  ;['m-ai-speaking', 'm-barge', 'm-feedback', 'm-result-pron', 'm-summary', 'm-home'].forEach((n) => phone.add(n))
  for (let i = 0; i < 14; i++) phone.add(`m-live-${String(i).padStart(2, '0')}`)
  ;['d-home', 'd-t1', 'd-t2', 'd-hint1', 'd-t4'].forEach((n) => laptop.add(n))

  // Scenario cards floating behind the phone in the intro (depth parallax).
  const cards = [0, 1, 2, 3].map((i) => {
    const c = h('div', 'card', world)
    css(c, { width: '238px', height: '334px' })
    h('img', null, c).src = `media/c-card${i}.webp`
    return c
  })

  // Orb rings (AI speaking) inside the phone UI, driven by the voice envelope.
  const orbRings = [0, 1, 2].map(() => {
    const r = h('div', 'ring', phone.overlay)
    css(r, { width: '56px', height: '56px', left: '29px', top: '640px', '--c': '#6a3fb5' })
    return r
  })
  // Word highlight gliding over the real word chips (recorded result).
  const wordHi = h('div', null, phone.overlay)
  css(wordHi, { position: 'absolute', left: 0, top: 0, borderRadius: '999px', border: '3px solid #3043c4', boxShadow: '0 0 0 6px rgb(48 67 196 / 18%)' })
  // Contour draw-on: a white sheet over the chart that slides away left to right.
  const contourSheet = h('div', null, phone.overlay)
  css(contourSheet, { position: 'absolute', left: '30px', top: '528px', width: '332px', height: '112px', background: '#fff' })
  // Goal tick bursts on the laptop (desktop goal list check icons).
  const GOAL_PTS = [
    [198.5, 255],
    [198.5, 310],
    [198.5, 390],
  ]
  const goalBursts = GOAL_PTS.map(([x, y]) => {
    const r = h('div', 'ring', laptop.overlay)
    css(r, { width: '26px', height: '26px', left: `${x - 13}px`, top: `${y - 13}px`, '--c': '#1d6f47' })
    return r
  })

  // Hint ladder cards (real crops of the hint panel pieces).
  const HINTS = [
    { hdr: 'c-hdr1', body: 'c-tip', bw: 400, bh: 64, step: 'Step 1 · Meaning' },
    { hdr: 'c-hdr2', body: 'c-kw', bw: 380, bh: 43, step: 'Step 2 · Key words' },
    { hdr: 'c-hdr3', body: 'c-ex', bw: 330, bh: 39, step: 'Step 3 · Example' },
  ]
  const hintCards = HINTS.map((d) => {
    const c = h('div', 'hintcard', world)
    const k = 2 // crops are CSS px captured at 2x; show them at 2x CSS size (1:1 source pixels)
    const hi = h('img', null, c)
    hi.src = `media/${d.hdr}.webp`
    css(hi, { width: `${240 * k}px`, height: `${38 * k}px`, marginLeft: '-8px' })
    const bi = h('img', null, c)
    bi.src = `media/${d.body}.webp`
    css(bi, { width: `${d.bw * k}px`, height: `${d.bh * k}px`, marginTop: '6px', marginLeft: '-8px' })
    h('div', 'step', c, d.step)
    return c
  })

  // ------------------------------------------------------------------ callouts
  function callout(label, small, color) {
    const pin = h('div', 'pin', callLayer)
    pin.style.setProperty('--c', color)
    const box = h('div', 'callout', callLayer, `<span class="dot"></span><span>${label}${small ? ` <small>${small}</small>` : ''}</span>`)
    box.style.setProperty('--c', color)
    const ln = document.createElementNS('http://www.w3.org/2000/svg', 'path')
    ln.setAttribute('stroke', '#1d1c1a')
    ln.setAttribute('stroke-width', '2.5')
    ln.setAttribute('fill', 'none')
    ln.setAttribute('stroke-linecap', 'round')
    lineSvg.appendChild(ln)
    return { pin, box, ln, w: null }
  }
  /** Draw a callout: pin at anchor point, label at pin + (dx, dy) (label side from dx sign). */
  function drawCallout(c, t, t0, t1, at, dx, dy) {
    const vis = t >= t0 && t < t1 + 0.3
    show(c.pin, vis)
    show(c.box, vis)
    c.ln.style.display = vis ? '' : 'none'
    if (!vis) return
    if (c.w == null) {
      c.w = c.box.offsetWidth
      c.h = c.box.offsetHeight
    }
    const [px, py] = at()
    const out = P(t, t1, t1 + 0.28, E.in3)
    const pp = P(t, t0, t0 + 0.5, E.spring) * (1 - out)
    c.pin.style.transform = `translate(${px}px,${py}px) scale(${pp})`
    const lx = px + dx
    const ly = py + dy
    const bx = dx >= 0 ? lx : lx - c.w
    const by = ly - c.h / 2
    const pb = P(t, t0 + 0.12, t0 + 0.62, E.outExpo)
    const sc = lerp(0.6, 1, pb) * (1 - out * 0.3)
    c.box.style.transform = `translate(${bx + (dx >= 0 ? -1 : 1) * (1 - pb) * 30}px,${by}px) scale(${sc})`
    c.box.style.transformOrigin = dx >= 0 ? '0 50%' : '100% 50%'
    c.box.style.opacity = pb * (1 - out)
    // Elbow leader: from the pin, out along dy, then to the label edge.
    const pl = P(t, t0 + 0.04, t0 + 0.4, E.out3) * (1 - out)
    const mx = px + dx * 0.45
    const d = `M${px},${py} L${mx},${ly} L${lx},${ly}`
    c.ln.setAttribute('d', d)
    const len = Math.hypot(mx - px, ly - py) + Math.abs(lx - mx)
    c.ln.setAttribute('stroke-dasharray', `${len}`)
    c.ln.setAttribute('stroke-dashoffset', `${len * (1 - pl)}`)
  }

  // ------------------------------------------------------------------ HOOK (0 - 3)
  const hook = h('div', 'full', stage)
  hook.style.background = 'radial-gradient(80% 70% at 50% 45%, #23264a 0%, #1d1c1a 70%)'
  hook.style.zIndex = 20
  hook.innerHTML = `<svg class="lines" width="${W}" height="${H}"><path id="wave" fill="none" stroke="#dfe3fd" stroke-width="3" stroke-linecap="round"/><path id="wave2" fill="none" stroke="#6a3fb5" stroke-width="2" stroke-linecap="round" opacity="0.8"/></svg>`
  const wave = hook.querySelector('#wave')
  const wave2 = hook.querySelector('#wave2')
  const hookSize = L(150, 138)
  const H1 = textBlock(hook, 'head', L(['Speak English out [hl]loud.'], ['Speak English', 'out [hl]loud.']), W / 2, L(360, 640), { align: 'center', color: '#fff' })
  H1.el.style.fontSize = `${hookSize}px`
  const H2 = textBlock(hook, 'head', L(["Nobody's listening."], ["Nobody's", 'listening.']), W / 2, L(380, 660), { align: 'center', color: '#fff' })
  H2.el.style.fontSize = `${hookSize}px`
  const H3 = textBlock(hook, 'sub', ['Not even the cloud.'], W / 2, L(575, 1000), { align: 'center', color: 'rgb(255 255 255 / 62%)' })
  H3.el.style.fontSize = `${L(40, 44)}px`

  function hookFrame(t) {
    show(hook, t < 3.3)
    if (t >= 3.3) return
    // Waveform: syllable bursts at the word cues, collapsing into the centre before the iris opens.
    const N = 160
    const wy = L(760, 1260)
    const span = W * 0.84 * (1 - P(t, 2.55, 2.95, E.inExpo) * 0.999)
    const draw = P(t, 0.0, 0.7, E.outExpo)
    let amp = 0
    for (const w of C.words) amp += Math.exp(-(((t - w - 0.08) / 0.12) ** 2))
    amp = 0.12 + Math.min(1.2, amp) * (1 - P(t, 2.5, 2.9))
    const mk = (ph, k, a0) => {
      let d = ''
      for (let i = 0; i <= N; i++) {
        const u = i / N
        if (u > draw) break
        const x = W / 2 - span / 2 + span * u
        const win = Math.sin(Math.PI * u) ** 2
        const y = wy + win * a0 * amp * (Math.sin(u * 38 * k + t * 9 + ph) * 0.6 + Math.sin(u * 91 * k - t * 13 + ph) * 0.4)
        d += `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`
      }
      return d
    }
    wave.setAttribute('d', mk(0, 1, 70))
    wave2.setAttribute('d', mk(1.7, 1.3, 45))
    wave.style.opacity = wave2.style.opacity = 1 - P(t, 2.85, 3.0, E.lin)
    animWords(H1, t, { tin: 0.28, stagger: 0.12, dur: 0.75, tout: 1.5, ostagger: 0.03, odur: 0.35 })
    H1.boxes.forEach((b) => (b.style.transform = `scaleX(${P(t, 0.85, 1.25, E.app)})`))
    animWords(H2, t, { tin: 1.68, stagger: 0.14, dur: 0.75, tout: 2.62, ostagger: 0.03, odur: 0.3 })
    animWords(H3, t, { tin: 2.2, stagger: 0.05, dur: 0.6, tout: 2.62, ostagger: 0.02, odur: 0.3 })
    const blurOut = P(t, 2.5, 2.9, E.in2)
    hook.style.filter = blurOut > 0 ? `blur(${blurOut * 6}px)` : ''
    // Iris: a hole opens in the dark hook from the collapsed waveform point, revealing the next act.
    const r = P(t, 2.78, 3.22, E.inOutExpo) * Math.hypot(W, H) * 1.02
    const m = r > 0.5 ? `radial-gradient(circle at ${W / 2}px ${wy}px, transparent ${r}px, #000 ${r + 1.5}px)` : 'none'
    hook.style.webkitMaskImage = m
    hook.style.maskImage = m
  }

  // ------------------------------------------------------------------ scene text
  const col = L({ left: 150, right: 1060 }, { left: 90, right: 90 })
  const TY = L(290, 170) // top of the text column
  function sceneText(eyebrow, num, head, sub, side, opts = {}) {
    const x = opts.x ?? col[side]
    const y = opts.y ?? TY
    const eb = h('div', 't eyebrow', textLayer, num ? `<span class="num">${num}</span>${eyebrow}` : eyebrow)
    css(eb, { left: `${x}px`, top: `${y}px` })
    const hd = textBlock(textLayer, 'head', head, x, y + 58)
    if (opts.size) hd.el.style.fontSize = `${opts.size}px`
    const sb = sub ? textBlock(textLayer, 'sub', sub, x, y + 58 + (opts.size || 104) * 1.02 * head.length + 30) : null
    return { eb, hd, sb }
  }
  function animScene(s, t, tin, tout) {
    const pe = P(t, tin, tin + 0.6, E.outExpo)
    const po = P(t, tout, tout + 0.35, E.inExpo)
    s.eb.style.opacity = pe * (1 - po)
    s.eb.style.transform = `translate3d(${(1 - pe) * -40 + po * -30}px,0,0)`
    show(s.eb, t > tin - 0.01 && t < tout + 0.4)
    s.eb.style.clipPath = `inset(0 ${(1 - pe) * 100}% 0 0)`
    animWords(s.hd, t, { tin: tin + 0.08, stagger: 0.07, dur: 0.8, tout, ostagger: 0.02, odur: 0.35 })
    if (s.sb) animWords(s.sb, t, { tin: tin + 0.35, stagger: 0.02, dur: 0.7, tout, ostagger: 0.01, odur: 0.3 })
  }

  // Intro title
  const introEb = h('div', 't eyebrow', textLayer, 'Introducing')
  css(introEb, { left: `${L(150, 90)}px`, top: `${L(330, 190)}px`, color: 'var(--ink-3)' })
  const introTitle = textBlock(textLayer, 'head', ['말하기 연습'], L(150, 90), L(385, 245))
  introTitle.el.style.fontSize = `${L(150, 150)}px`
  introTitle.el.style.fontWeight = 800
  introTitle.el.style.letterSpacing = '-0.05em'
  const introSub = textBlock(textLayer, 'sub', L(['An AI English speaking partner', 'that runs entirely on your Mac.'], ['An AI English speaking partner', 'that runs entirely on your Mac.']), L(154, 94), L(575, 430))
  introSub.el.style.fontSize = `${L(36, 38)}px`
  const introPill = h('div', 't', textLayer, '<span style="display:inline-flex;align-items:center;gap:10px;padding:10px 20px;border-radius:999px;background:#dff3ef;color:#0b6e63;font-size:24px;font-weight:650">● Private · Offline · On-device</span>')
  css(introPill, { left: `${L(154, 94)}px`, top: `${L(700, 560)}px` })

  const S1 = sceneText('Real-time roleplay', '01', ['Talk like', "it's real."], ['Live captions as you speak.', 'Replies in a natural voice.'], 'left')
  const S2 = sceneText('Barge-in', '02', ['Cut in', 'anytime.'], ['The AI stops mid-sentence', 'and listens.'], 'right')
  const S3 = sceneText('Hint ladder', '03', ['Stuck?', 'Take a hint.'], ['Korean meaning, then key words,', 'then a full example.'], 'left')
  const S4 = sceneText('Goals', '04', ['Goals tick', 'as you talk.'], null, 'left', { size: L(96, 104) })
  const S5 = sceneText('Session summary', '05', ['Say it', 'better.'], null, 'right')
  const S6 = sceneText('Recorded practice', '06', ['Hear every', 'word.'], ['Your take vs. the model voice,', 'plus a pitch contour. No scores.'], 'left')

  // Goals counter
  const counter = h('div', 't counter', textLayer)
  css(counter, { left: `${L(150, 90)}px`, top: `${L(560, 470)}px` })
  const counterLbl = h('div', 't sub', textLayer, 'goals done · <span style="font-weight:650;color:#1d6f47">목표 달성</span>')
  css(counterLbl, { left: `${L(160, 100)}px`, top: `${L(790, 695)}px` })

  // Summary sentence morph
  const said = h('div', 't', textLayer)
  const fixed = h('div', 't', textLayer)
  const sx = L(1060, 90)
  const sy = L(640, 485)
  said.innerHTML = `<div class="eyebrow" style="color:var(--ink-3);font-size:19px;margin-bottom:10px">You said</div><div style="position:relative;display:inline-block;font-size:${L(46, 48)}px;font-weight:600;letter-spacing:-0.02em;color:var(--ink-3)">“I want pay with my phone.”<span class="strike" style="width:104%"></span></div>`
  fixed.innerHTML = `<div class="eyebrow" style="color:var(--ok);font-size:19px;margin-bottom:10px">Try saying</div><div style="font-size:${L(52, 54)}px;font-weight:760;letter-spacing:-0.03em;color:var(--ok)">“I'd like to pay with my phone.”</div>`
  css(said, { left: `${sx}px`, top: `${sy}px` })
  css(fixed, { left: `${sx}px`, top: `${sy + 150}px` })
  const strike = said.querySelector('.strike')

  // Private / on-device statements (dark)
  const PRIV = ['Runs on your Mac.', 'Works offline.', 'Audio never saved.']
  const priv = PRIV.map((s, i) => {
    const b = textBlock(textLayer, 'head', [s], L(150, 90), L(270 + i * 116, 190 + i * 116))
    b.el.style.color = '#fff'
    b.el.style.fontSize = `${L(96, 96)}px`
    return b
  })
  const chipTxt = ['Qwen3-ASR', 'CosyVoice3', 'Qwen3-4B · llama.cpp']
  const chips = chipTxt.map((s) => h('div', 'chip', textLayer, s))
  const chipCap = h('div', 't', textLayer, 'Speech recognition, voice and conversation — all local models.')
  css(chipCap, { fontSize: '22px', color: 'rgb(255 255 255 / 55%)', fontWeight: 500 })

  // Callouts
  const coLive = callout('Live caption', 'as you speak', '#3043c4')
  const coReply = callout('AI replies out loud', null, '#6a3fb5')
  const coVoice = callout('Natural AI voice', null, '#6a3fb5')
  const coBarge = callout('AI stopped here', '말하다 멈춤', '#b0261d')
  const coDrill = callout('Drill it right away', '다시 말하기', '#3043c4')
  const coCompare = callout('My take ↔ model voice', null, '#3043c4')
  const coContour = callout('Pitch contour', 'reference only', '#0b6e63')
  const aVoice = anchor(phone, 57, 668)
  const aLive = anchor(phone, 92, 372)
  const aReply = anchor(phone, 48, 452)
  const aBarge = anchor(phone, 215, 470)
  const aDrill = anchor(phone, 36, 805)
  const aCompare = anchor(phone, 168, 381)
  const aContour = anchor(phone, 340, 575)

  // ------------------------------------------------------------------ BRAND (27 - 30)
  const brand = h('div', 'full', stage)
  brand.style.zIndex = 30
  brand.style.background = 'radial-gradient(90% 90% at 50% 38%, #3a4fd6 0%, #3043c4 45%, #22329a 100%)'
  const mark = h('div', 'logo-mark', brand, MIC_SVG(96, '#3043c4', 2.2))
  const bTitle = textBlock(brand, 'head', ['말하기 연습'], W / 2, L(470, 830), { align: 'center', color: '#fff' })
  bTitle.el.style.fontSize = `${L(128, 132)}px`
  bTitle.el.style.fontWeight = 800
  bTitle.el.style.letterSpacing = '-0.05em'
  const bName = textBlock(brand, 'sub', ['voice-roleplay'], W / 2, L(617, 985), { align: 'center', color: 'rgb(255 255 255 / 70%)' })
  css(bName.el, { fontSize: '34px', fontWeight: 600, letterSpacing: '0.02em' })
  const bTag = textBlock(brand, 'sub', L(['Private English speaking practice.'], ['Private English', 'speaking practice.']), W / 2, L(690, 1075), { align: 'center', color: '#fff' })
  css(bTag.el, { fontSize: `${L(44, 54)}px`, fontWeight: 600, letterSpacing: '-0.02em' })
  const cta = h('div', 'cta', brand, '<span class="prompt">$</span><span>./app start</span><span class="caret"></span>')
  const caret = cta.querySelector('.caret')
  const bFoot = h('div', 't', brand, '한국어 학습자를 위한 영어 말하기 연습 · 100% local')
  css(bFoot, { fontSize: `${L(24, 28)}px`, color: 'rgb(255 255 255 / 60%)', fontWeight: 500 })

  // Grain (seeded noise canvas, shifted per frame).
  const grain = h('canvas', null, stage)
  grain.id = 'grain'
  grain.style.zIndex = 40
  {
    const gw = W + 128, gh = H + 128
    grain.width = gw / 2
    grain.height = gh / 2
    css(grain, { width: `${gw}px`, height: `${gh}px` })
    const g = grain.getContext('2d')
    const img = g.createImageData(grain.width, grain.height)
    const r = rng(7)
    for (let i = 0; i < img.data.length; i += 4) {
      const v = 128 + (r() - 0.5) * 255
      img.data[i] = img.data[i + 1] = img.data[i + 2] = v
      img.data[i + 3] = 255
    }
    g.putImageData(img, 0, 0)
  }

  // ------------------------------------------------------------------ camera tracks
  const pc = phone.center
  const P_ = (x, y) => phone.pt(x, y)
  const [lfx, lfy] = P_(215, 395) // live captions focus
  const [bfx, bfy] = P_(200, 500) // barge focus
  const [dfx, dfy] = P_(190, 700) // summary feedback focus
  const [wfx, wfy] = P_(195, 205) // result words
  const [cfx, cfy] = P_(195, 520) // result contour
  const phoneKeys = track(
    L(
      [
        [0, { x: 1330, y: 1750, s: 0.85, rx: 34, ry: -28, rz: 8, fx: pc[0], fy: pc[1], op: 0 }],
        [3.0, { op: 1 }, E.lin],
        [3.95, { x: 1330, y: 545, s: 1, rx: 6, ry: -14, rz: 0 }, E.outExpo],
        [6.35, { x: 1310, y: 540, s: 1.04, rx: 3, ry: -9 }, E.inOut2],
        [7.05, { x: 1330, y: 520, s: 1.88, rx: 0, ry: -3, fx: lfx, fy: lfy }, E.inOutExpo],
        [9.85, { x: 1320, y: 530, s: 1.96, ry: -5 }, E.inOut2],
        [10.35, { x: 600, y: 540, s: 2.05, ry: 6, fx: bfx, fy: bfy }, E.inOutExpo],
        [12.3, { x: 610, y: 545, s: 2.18, ry: 4 }, E.inOut2],
        [12.75, { x: 610, y: 1900, s: 1.9, rx: 22, ry: 10 }, E.inExpo],
        [12.76, { op: 0 }, E.lin],
        [17.95, { x: -500, y: 560, s: 1.35, rx: 4, ry: 35, fx: dfx, fy: dfy, op: 0 }],
        [17.96, { op: 1 }, E.lin],
        [18.6, { x: 560, y: 540, s: 1.45, rx: 2, ry: 9 }, E.outExpo],
        [20.85, { x: 575, y: 535, s: 1.52, ry: 6 }, E.inOut2],
        [21.3, { x: 1330, y: 530, s: 1.62, rx: 0, ry: -7, fx: wfx, fy: wfy }, E.inOutExpo],
        [22.35, { x: 1325, y: 540, s: 1.66, ry: -6 }, E.inOut2],
        [22.95, { y: 540, s: 1.62, fx: cfx, fy: cfy }, E.inOutExpo],
        [23.85, { s: 1.66, ry: -4 }, E.inOut2],
        [24.55, { x: 1700, y: 640, s: 0.66, rx: 4, ry: -18, fx: pc[0], fy: pc[1] }, E.inOutExpo],
        [26.35, { x: 1690, y: 630, s: 0.69, rx: 2, ry: -14 }, E.inOut2],
        [26.95, { x: 960, y: 540, s: 0.12, rx: 0, ry: 0, op: 0 }, E.inExpo],
      ],
      [
        [0, { x: 540, y: 2600, s: 0.95, rx: 34, ry: -20, rz: 6, fx: pc[0], fy: pc[1], op: 0 }],
        [3.0, { op: 1 }, E.lin],
        [3.95, { x: 540, y: 1330, s: 1.12, rx: 6, ry: -10, rz: 0 }, E.outExpo],
        [6.35, { x: 540, y: 1320, s: 1.15, rx: 3, ry: -6 }, E.inOut2],
        [7.05, { x: 540, y: 1290, s: 2.0, rx: 0, ry: -2, fx: lfx, fy: lfy }, E.inOutExpo],
        [9.85, { x: 540, y: 1300, s: 2.08, ry: -3 }, E.inOut2],
        [10.35, { x: 540, y: 1300, s: 2.15, ry: 5, fx: bfx, fy: bfy }, E.inOutExpo],
        [12.3, { x: 540, y: 1305, s: 2.25, ry: 3 }, E.inOut2],
        [12.75, { x: 540, y: 3000, s: 1.9, rx: 22, ry: 10 }, E.inExpo],
        [12.76, { op: 0 }, E.lin],
        [17.95, { x: -600, y: 1700, s: 1.5, rx: 4, ry: 35, fx: dfx, fy: dfy, op: 0 }],
        [17.96, { op: 1 }, E.lin],
        [18.6, { x: 540, y: 1700, s: 1.62, rx: 2, ry: 6 }, E.outExpo],
        [20.85, { x: 540, y: 1690, s: 1.7, ry: 4 }, E.inOut2],
        [21.3, { x: 540, y: 1260, s: 1.9, rx: 0, ry: -5, fx: wfx, fy: wfy }, E.inOutExpo],
        [22.35, { s: 1.94, ry: -4 }, E.inOut2],
        [22.95, { y: 1300, s: 1.9, fx: cfx, fy: cfy }, E.inOutExpo],
        [23.85, { s: 1.94, ry: -3 }, E.inOut2],
        [24.55, { x: 770, y: 1420, s: 0.78, rx: 4, ry: -16, fx: pc[0], fy: pc[1] }, E.inOutExpo],
        [26.35, { x: 760, y: 1410, s: 0.8, rx: 2, ry: -12 }, E.inOut2],
        [26.95, { x: 540, y: 960, s: 0.12, rx: 0, ry: 0, op: 0 }, E.inExpo],
      ],
    ),
  )
  const [gfx, gfy] = laptop.pt(312, 330)
  const lc = laptop.center
  const laptopKeys = track(
    L(
      [
        [0, { x: 2700, y: 560, s: 0.5, rx: 8, ry: -40, rz: 0, fx: lc[0], fy: lc[1], op: 0 }],
        [15.3, { op: 0 }],
        [15.31, { op: 1 }, E.lin],
        [15.95, { x: 1210, y: 560, s: 0.62, rx: 4, ry: -14 }, E.outExpo],
        [16.05, { x: 1200, y: 560, s: 0.63, ry: -13 }, E.lin],
        [16.55, { x: 1360, y: 520, s: 1.85, rx: 1, ry: -5, fx: gfx, fy: gfy }, E.inOutExpo],
        [17.75, { x: 1370, y: 530, s: 1.95, ry: -4 }, E.inOut2],
        [18.15, { x: 2900, y: 500, s: 1.8, ry: -30 }, E.inExpo],
        [18.16, { op: 0 }, E.lin],
        [23.95, { x: 900, y: 1500, s: 0.5, rx: 25, ry: 16, fx: lc[0], fy: lc[1], op: 0 }],
        [23.96, { op: 1 }, E.lin],
        [24.6, { x: 1400, y: 520, s: 0.44, rx: 5, ry: 16 }, E.outExpo],
        [26.35, { x: 1410, y: 510, s: 0.46, rx: 3, ry: 12 }, E.inOut2],
        [26.95, { x: 960, y: 540, s: 0.08, rx: 0, ry: 0, op: 0 }, E.inExpo],
      ],
      [
        [0, { x: 1900, y: 1300, s: 0.5, rx: 8, ry: -40, rz: 0, fx: lc[0], fy: lc[1], op: 0 }],
        [15.3, { op: 0 }],
        [15.31, { op: 1 }, E.lin],
        [15.95, { x: 540, y: 1280, s: 0.62, rx: 4, ry: -12 }, E.outExpo],
        [16.05, { x: 540, y: 1280, s: 0.63, ry: -11 }, E.lin],
        [16.55, { x: 560, y: 1320, s: 2.1, rx: 1, ry: -4, fx: gfx, fy: gfy }, E.inOutExpo],
        [17.75, { x: 560, y: 1320, s: 2.2, ry: -3 }, E.inOut2],
        [18.15, { x: 2400, y: 1300, s: 2.0, ry: -30 }, E.inExpo],
        [18.16, { op: 0 }, E.lin],
        [23.95, { x: 460, y: 2400, s: 0.5, rx: 25, ry: 16, fx: lc[0], fy: lc[1], op: 0 }],
        [23.96, { op: 1 }, E.lin],
        [24.6, { x: 470, y: 1240, s: 0.56, rx: 5, ry: 14 }, E.outExpo],
        [26.35, { x: 480, y: 1230, s: 0.58, rx: 3, ry: 10 }, E.inOut2],
        [26.95, { x: 540, y: 960, s: 0.08, rx: 0, ry: 0, op: 0 }, E.inExpo],
      ],
    ),
  )

  // ------------------------------------------------------------------ render
  const FLIP = C.flip
  function render(t) {
    // Backgrounds and glows
    const g = [
      [0.28 + 0.05 * Math.sin(t * 0.35), 0.3 + 0.06 * Math.cos(t * 0.3), 0.55, '#dfe3fd'],
      [0.78 + 0.05 * Math.cos(t * 0.28), 0.72 + 0.05 * Math.sin(t * 0.33), 0.5, '#efe8fb'],
      [0.62 + 0.06 * Math.sin(t * 0.22 + 2), 0.18 + 0.04 * Math.cos(t * 0.4), 0.35, '#dff3ef'],
    ]
    glows.forEach((el, i) => {
      const [gx, gy, gr, col] = g[i]
      const r = gr * Math.max(W, H)
      css(el, { left: `${gx * W - r}px`, top: `${gy * H - r}px`, width: `${2 * r}px`, height: `${2 * r}px`, background: `radial-gradient(closest-side, ${col} 0%, ${col}00 100%)`, opacity: 0.9 })
    })
    fade(warm, t, 12.35, 12.8, 15.35, 15.8, E.inOut2)
    // Dark stage for the private act: wipes up from the bottom with a rounded edge.
    const dIn = P(t, 23.8, 24.35, E.inOutExpo)
    show(dark, dIn > 0)
    dark.style.clipPath = `inset(${(1 - dIn) * 100}% 0 0 0 round ${(1 - dIn) * 120}px ${(1 - dIn) * 120}px 0 0)`

    hookFrame(t)
    if (V) {
      const so = Math.min(P(t, 6.5, 7.0, E.inOut2), 1 - P(t, 12.62, 12.8, E.inOut2)) + Math.min(P(t, 15.6, 16.1, E.inOut2), 1 - P(t, 23.7, 24.0, E.inOut2))
      scrim.style.opacity = clamp(so)
      show(scrim, so > 0.001)
    } else show(scrim, false)

    // Phone
    const pcam = kf(t, phoneKeys)
    // Barge-in jolt: the phone snaps when the AI is cut off.
    if (t > C.cut) pcam.x += 16 * Math.exp(-(t - C.cut) * 9) * Math.sin((t - C.cut) * 55)
    place(phone, pcam)
    phone.glare.style.backgroundPosition = `${lerp(120, -20, P(t, 3.0, 4.2, E.out3))}% 0`
    phone.glare.style.opacity = t < 5 ? 1 : 0
    // Phone screen content by time
    let scr
    if (t < 7.02) scr = [['m-ai-speaking', 1]]
    else if (t < 10.12) {
      const i = clamp(Math.floor((t - FLIP.start) / FLIP.step), 0, 13)
      scr = [[`m-live-${String(i).padStart(2, '0')}`, 1]]
    } else if (t < 15) scr = [['m-barge', 1]]
    else if (t < 20.95) scr = [['m-feedback', 1]]
    else if (t < 24.05) scr = [['m-result-pron', 1]]
    else scr = [['m-summary', 1]]
    screens(phone, scr)

    // Orb rings follow the AI voice (intro)
    const ev = env('opening', t - C.voice.opening)
    orbRings.forEach((r, i) => {
      const ph = (t * 1.6 + i / 3) % 1
      const on = t > 3.2 && t < 7.1
      show(r, on)
      if (!on) return
      r.style.transform = `scale(${1 + ph * (0.5 + ev * 1.4)})`
      r.style.opacity = (1 - ph) * (0.25 + ev * 0.9)
    })

    // Result: word highlight glides with the model voice; contour draws on.
    const RW = [
      [33, 131, 44, 40, 0.1],
      [83, 131, 50, 40, 0.34],
      [139, 131, 35, 40, 0.55],
      [180, 131, 62, 40, 0.68],
      [33, 177, 110, 40, 1.02],
      [149, 177, 56, 40, 1.68],
      [212, 177, 49, 40, 1.9],
      [267, 177, 55, 40, 2.15],
      [33, 223, 73, 40, 2.55],
    ]
    const vt = t - C.voice.model_read
    const onW = t > 21.1 && t < 24.05
    show(wordHi, onW)
    if (onW) {
      let k = 0
      for (let i = 0; i < RW.length; i++) if (vt >= RW[i][4]) k = i
      const a = RW[Math.max(0, k)]
      const b = RW[Math.min(RW.length - 1, k + 1)]
      const seg = k + 1 < RW.length ? P(vt, b[4] - 0.12, b[4], E.inOut3) : 0
      const x = lerp(a[0], b[0], seg), y = lerp(a[1], b[1], seg), w = lerp(a[2], b[2], seg), hh = lerp(a[3], b[3], seg)
      css(wordHi, { transform: `translate(${x - 3}px,${y - 3}px)`, width: `${w + 6}px`, height: `${hh + 6}px`, opacity: P(t, 21.2, 21.5) * (1 - P(t, 23.3, 23.7)) })
    }
    const cr = P(t, 22.55, 23.55, E.inOut2)
    show(contourSheet, t > 20.9 && t < 24.05 && cr < 1)
    css(contourSheet, { transform: `translateX(${cr * 332}px)`, width: `${332 * (1 - cr)}px` })

    // Laptop
    place(laptop, kf(t, laptopKeys))
    let ls
    if (t < 20) {
      const g1 = P(t, 16.22, 16.3, E.lin), g2 = P(t, 16.72, 16.8, E.lin), g3 = P(t, 17.22, 17.3, E.lin)
      ls = [['d-t1', 1], ['d-t2', g1], ['d-hint1', g2], ['d-t4', g3]]
    } else ls = [['d-home', 1]]
    screens(laptop, ls)
    goalBursts.forEach((r, i) => {
      const t0 = C.dings[i]
      const p = P(t, t0, t0 + 0.6, E.out3)
      show(r, t > t0 && t < t0 + 0.6)
      r.style.transform = `scale(${1 + p * 2.2})`
      r.style.opacity = 1 - p
      r.style.borderWidth = `${3 - p * 2}px`
    })

    // Floating scenario cards (intro), depth parallax.
    const CP = L(
      [
        [860, 250, 0.72, -14, 0.25],
        [1780, 300, 0.8, 12, 0.35],
        [900, 820, 0.66, 8, 0.2],
        [1760, 860, 0.86, -8, 0.3],
      ],
      [
        [150, 980, 0.7, -12, 0.25],
        [940, 1060, 0.78, 10, 0.35],
        [170, 1690, 0.66, 8, 0.2],
        [930, 1740, 0.84, -8, 0.3],
      ],
    )
    cards.forEach((c, i) => {
      const [x, y, s, rz, depth] = CP[i]
      const pin = P(t, 3.15 + i * 0.09, 4.15 + i * 0.09, E.outExpo)
      const pout = P(t, 6.35, 6.95, E.inExpo)
      const drift = (t - 3) * 14 * depth
      const vis = t > 3.1 && t < 7
      show(c, vis)
      if (!vis) return
      const blur = (1 - depth) * 3 * (1 - pin * 0.4)
      c.style.transform = `translate3d(${x - 119 + (i % 2 ? drift : -drift) + pout * (x < W / 2 ? -400 : 400)}px,${y - 167 + (1 - pin) * 260 - drift * 0.6}px,${-400 + depth * 600}px) rotate(${rz * (1 - pin * 0.3)}deg) scale(${s * lerp(0.7, 1, pin)})`
      c.style.opacity = pin * (1 - pout) * 0.95
      c.style.filter = `blur(${blur.toFixed(2)}px)`
    })

    // Hint cards (act: hints)
    const HP = L(
      [
        [880, 205],
        [940, 490],
        [1000, 735],
      ],
      [
        [60, 880],
        [95, 1190],
        [130, 1460],
      ],
    )
    hintCards.forEach((c, i) => {
      const t0 = C.hintCards[i]
      const p = P(t, t0 - 0.05, t0 + 0.6, E.outExpo)
      const out = P(t, 14.85 + i * 0.04, 15.2 + i * 0.04, E.inExpo)
      const focus = i === 2 ? P(t, 14.3, 14.8, E.inOut3) : 0
      const vis = t > t0 - 0.06 && t < 15.3
      show(c, vis)
      if (!vis) return
      const [x, y] = HP[i]
      const lift = t > 14.3 && i < 2 ? P(t, 14.3, 14.8, E.inOut3) : 0
      c.style.transform = `translate3d(${x + out * -120}px,${y + (1 - p) * 140 - out * 60}px,0) rotateX(${(1 - p) * -40}deg) scale(${lerp(0.92, 1, p) * (1 + focus * 0.08)})`
      c.style.opacity = p * (1 - out) * (1 - lift * 0.45)
      c.style.clipPath = `inset(0 0 ${(1 - p) * 100}% 0 round 26px)`
    })

    // Scene text
    fade(introEb, t, 3.4, 3.9, 6.35, 6.7)
    introEb.style.letterSpacing = `${lerp(0.5, 0.16, P(t, 3.4, 4.4, E.outExpo))}em`
    animWords(introTitle, t, { tin: 3.5, stagger: 0.1, dur: 0.9, tout: 6.4, from: 110 })
    animWords(introSub, t, { tin: 3.85, stagger: 0.025, dur: 0.8, tout: 6.42, ostagger: 0.008 })
    const ip = fade(introPill, t, 4.3, 4.8, 6.4, 6.7)
    introPill.style.transform = `translateY(${(1 - ip) * 20}px)`
    animScene(S1, t, 7.05, 9.75)
    animScene(S2, t, 10.2, 12.3)
    animScene(S3, t, 12.55, 14.9)
    animScene(S4, t, 15.55, 17.8)
    animScene(S5, t, 18.3, 20.7)
    animScene(S6, t, 21.2, 23.7)

    // Goal counter: 0/3 -> 3/3 on the dings
    const n = C.dings.filter((d) => t >= d).length
    counter.textContent = `${n}/3`
    const bump = C.dings.reduce((a, d) => a + (t >= d ? Math.exp(-(t - d) * 9) : 0), 0)
    const cp = fade(counter, t, 15.8, 16.2, 17.75, 18.05)
    counter.style.transform = `translateY(${(1 - cp) * 40}px) scale(${1 + bump * 0.08})`
    counter.style.transformOrigin = '0 70%'
    fade(counterLbl, t, 15.95, 16.4, 17.75, 18.05)

    // Summary: strike the slip, reveal the fix
    const sp = fade(said, t, 18.55, 19.0, 20.65, 20.95)
    said.style.transform = `translateY(${(1 - sp) * 30}px)`
    strike.style.transform = `scaleX(${P(t, C.strike, C.strike + 0.35, E.inOut3)})`
    const fp = fade(fixed, t, C.fix - 0.05, C.fix + 0.45, 20.65, 20.95)
    fixed.style.transform = `translateY(${(1 - fp) * 36}px)`
    fixed.style.clipPath = `inset(0 ${(1 - P(t, C.fix - 0.05, C.fix + 0.6, E.app)) * 100}% 0 0)`

    // Private statements (dark act)
    priv.forEach((b, i) => {
      const t0 = C.priv[i]
      animWords(b, t, { tin: t0 - 0.05, stagger: 0.06, dur: 0.7, tout: 26.45 + i * 0.04, ostagger: 0.02, odur: 0.3 })
      const cur = C.priv.filter((x) => t >= x).length - 1
      b.el.style.opacity = t < 25.85 ? (i === cur ? 1 : 0.38) : 1
    })
    const chipY = L(700, 560)
    let cx = L(154, 94)
    chips.forEach((c, i) => {
      const t0 = C.chips[i] - 0.05
      const p = P(t, t0, t0 + 0.5, E.outExpo)
      const out = P(t, 26.45, 26.75, E.inExpo)
      show(c, t > t0 && t < 26.8)
      c.style.transform = `translate(${cx}px,${chipY + (1 - p) * 30}px) scale(${lerp(0.8, 1, p)})`
      c.style.opacity = p * (1 - out)
      cx += c.offsetWidth + 14
    })
    const ccp = fade(chipCap, t, 26.0, 26.4, 26.45, 26.75)
    chipCap.style.transform = `translate(${L(156, 96)}px,${chipY + 82}px)`
    chipCap.style.opacity = ccp

    // Callouts
    drawCallout(coVoice, t, C.callouts.voice, 6.3, aVoice, L(-120, 140), L(90, 110))
    drawCallout(coLive, t, C.callouts.live, 9.8, aLive, L(-210, 60), L(-120, -130))
    drawCallout(coReply, t, C.callouts.reply, 9.8, aReply, L(-150, 60), L(70, 150))
    drawCallout(coBarge, t, C.callouts.barge, 12.25, aBarge, L(-70, -70), L(115, 115))
    drawCallout(coDrill, t, C.callouts.drill, 20.75, aDrill, L(110, 250), L(110, -70))
    drawCallout(coCompare, t, C.callouts.compare, 22.4, aCompare, L(-230, 40), L(120, 150))
    drawCallout(coContour, t, C.callouts.contour, 23.75, aContour, L(-400, -170), L(120, 150))

    // Brand
    const bIn = P(t, 26.92, 27.4, E.inOutExpo)
    show(brand, bIn > 0)
    const cyL = L(300, 640)
    brand.style.clipPath = `circle(${bIn * Math.hypot(W, H) * 0.62}px at ${W / 2}px ${L(540, 960)}px)`
    const mp = P(t, 27.0, 27.9, E.spring)
    mark.style.transform = `translate(${W / 2 - 84}px,${cyL - 84 + (1 - P(t, 27.0, 27.6, E.outExpo)) * 60}px) scale(${mp}) rotate(${(1 - mp) * -30}deg)`
    animWords(bTitle, t, { tin: 27.25, stagger: 0.09, dur: 0.9 })
    animWords(bName, t, { tin: 27.5, stagger: 0.05, dur: 0.8 })
    animWords(bTag, t, { tin: 27.75, stagger: 0.05, dur: 0.8 })
    const ctaP = P(t, C.cta, C.cta + 0.6, E.outExpo)
    const ctaW = cta.offsetWidth
    cta.style.transform = `translate(${W / 2 - ctaW / 2}px,${L(800, 1260) + (1 - ctaP) * 40}px) scale(${lerp(0.85, 1, ctaP)})`
    cta.style.opacity = ctaP
    caret.style.opacity = Math.floor((t - C.cta) / 0.5) % 2 === 0 ? 1 : 0
    const ffp = fade(bFoot, t, 28.55, 29.1, 99, 100)
    const fw = bFoot.offsetWidth
    bFoot.style.transform = `translate(${W / 2 - fw / 2}px,${L(960, 1720) + (1 - ffp) * 16}px)`
    // Slow push on the brand stack.
    const push = 1 + P(t, 27.4, 30, E.lin) * 0.03
    brand.style.transform = `scale(${push})`
    brand.style.transformOrigin = `50% ${L(50, 50)}%`

    // Grain shifts every frame (deterministic).
    const fr = Math.round(t * 60)
    const r = rng(fr + 11)
    grain.style.transform = `translate(${Math.floor(r() * 64)}px,${Math.floor(r() * 64)}px)`
    grain.style.opacity = t < 3 || (t > 24 && t < 27) ? 0.07 : 0.03
  }

  async function ready() {
    await document.fonts.ready
    await Promise.all([...document.images].map((im) => (im.complete ? im.decode().catch(() => {}) : new Promise((r) => (im.onload = im.onerror = r)))))
    render(0)
  }
  window.__scene = { W, H, duration: C.duration, ready: ready(), render }
  const tq = params.get('t')
  if (tq != null) window.__scene.ready.then(() => render(parseFloat(tq)))
})()
