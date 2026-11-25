// Rig runtime for a layered character SVG (<id>-rig.svg) driven by its rig file (<id>-rig.json). Shared ancestor: CertLingo
// design/cast/rig/rig.js (2026-11-25), with a live (audio-driven) mouth, per-side brows and lids, pitch parallax and IK arms.
// Every part id, pivot, pose, clip, idle action, talk gesture, spring and
// spine is data in the JSON; this file only knows the channel grammar.
//
// One Rig = one inline copy of the SVG. Every frame it builds one set of channels
//   hold pose -> main clip -> idle life (waves, micro actions, blinks) -> talk layer -> blend -> secondary springs
// and writes them as group transforms, opacities and (for spines) path data. Nothing else in the DOM changes.

// ---------- easing / tracks ----------
const E = {
  lin: (t) => t,
  in: (t) => t * t,
  out: (t) => 1 - (1 - t) * (1 - t),
  inout: (t) => (t < 0.5 ? 2 * t * t : 1 - 2 * (1 - t) * (1 - t)),
  back: (t) => 1 + 2.70158 * (t - 1) ** 3 + 1.70158 * (t - 1) ** 2,
  // slow in-out for heavy, calm moves (Neuru's nods, Siru's slow blink)
  sine: (t) => 0.5 - 0.5 * Math.cos(Math.PI * t),
};
/** Keyed track [[ms, value, ease?], ...]; the ease names the curve into that key (default inout). */
function track(keys, t) {
  if (t <= keys[0][0]) return keys[0][1];
  for (let i = 1; i < keys.length; i++) {
    const [t1, v1, e = 'inout'] = keys[i];
    if (t <= t1) {
      const [t0, v0] = keys[i - 1];
      return v0 + (v1 - v0) * E[e]((t - t0) / (t1 - t0 || 1));
    }
  }
  return keys[keys.length - 1][1];
}
/** Discrete track [[ms, value], ...]: the last key at or before t. */
function step(keys, t) {
  let v = keys[0][1];
  for (const [k, val] of keys) if (t >= k) v = val;
  return v;
}
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const SCALE = /\.(sx|sy|s|len)$/;

// ---------- seeded random ----------
function rng(seed) {
  let a = seed >>> 0;
  const next = () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  return { next, range: (a0, b0) => a0 + (b0 - a0) * next(), pick: (list) => list[Math.floor(next() * list.length)] };
}

// ---------- spines (bendable tails): FK joints -> Catmull-Rom centre line -> width profile -> round caps ----------
// Keep in step with the Python port used to fit rest spines to the art (README "Spines").
const SPINE_K = 6; // samples per segment
function spineJoints(sp, v, name) {
  const g = (k, d = 0) => (k in v ? v[k] : d);
  const shapes = Object.entries(sp.shapes ?? {});
  let a = sp.dir + g(`${name}.a`);
  let x = sp.base[0] + g(`${name}.x`);
  let y = sp.base[1] + g(`${name}.y`);
  // a shape with `retract` pulls the tail in while it is half way between poses (sin(pi w) peaks at w = 0.5), so a tail
  // moving between far-apart shapes slides in and grows out instead of sweeping through the floor or the body
  let ls = g(`${name}.len`, 1);
  for (const [s, def] of shapes) if (def.retract) ls *= 1 - def.retract * Math.sin(Math.PI * clamp(g(`${name}.${s}`), 0, 1));
  const pts = [[x, y]];
  sp.segs.forEach((L, i) => {
    a += sp.bend[i] + g(`${name}.b${i}`);
    for (const [s, def] of shapes) {
      const w = g(`${name}.${s}`);
      if (w && def.bend) a += w * (def.bend[i] ?? 0);
    }
    const r = (a * Math.PI) / 180;
    x += L * ls * Math.cos(r);
    y += L * ls * Math.sin(r);
    pts.push([x, y]);
  });
  const width = sp.width.map((w0, i) => {
    let w = w0;
    for (const [s, def] of shapes) {
      const k = g(`${name}.${s}`);
      if (k && def.width) w += k * (def.width[i] ?? 0);
    }
    return Math.max(2, w);
  });
  return { pts, width };
}
function spineSamples(sp, v, name) {
  const { pts: P, width: W } = spineJoints(sp, v, name);
  const n = P.length;
  const X = [[2 * P[0][0] - P[1][0], 2 * P[0][1] - P[1][1]], ...P, [2 * P[n - 1][0] - P[n - 2][0], 2 * P[n - 1][1] - P[n - 2][1]]];
  const pts = [];
  const ws = [];
  for (let i = 0; i < n - 1; i++) {
    const [p0, p1, p2, p3] = [X[i], X[i + 1], X[i + 2], X[i + 3]];
    for (let k = 0; k < SPINE_K; k++) {
      const t = k / SPINE_K;
      const t2 = t * t;
      const t3 = t2 * t;
      pts.push([0, 1].map((j) => 0.5 * (2 * p1[j] + (-p0[j] + p2[j]) * t + (2 * p0[j] - 5 * p1[j] + 4 * p2[j] - p3[j]) * t2 + (-p0[j] + 3 * p1[j] - 3 * p2[j] + p3[j]) * t3)));
      ws.push(W[i] + (W[i + 1] - W[i]) * t);
    }
  }
  pts.push(P[n - 1]);
  ws.push(W[n - 1]);
  const ns = pts.map((_, i) => {
    const a = pts[Math.max(0, i - 1)];
    const b = pts[Math.min(pts.length - 1, i + 1)];
    const dx = b[0] - a[0];
    const dy = b[1] - a[1];
    const L = Math.hypot(dx, dy) || 1;
    return [-dy / L, dx / L];
  });
  return { pts, ws, ns };
}
const f1 = (n) => Math.round(n * 10) / 10;
/** Catmull-Rom through points as cubic Beziers (the leading M is the caller's). */
function bez(q) {
  let s = '';
  for (let i = 0; i < q.length - 1; i++) {
    const p0 = q[Math.max(0, i - 1)];
    const p1 = q[i];
    const p2 = q[i + 1];
    const p3 = q[Math.min(q.length - 1, i + 2)];
    s += `C${f1(p1[0] + (p2[0] - p0[0]) / 6)} ${f1(p1[1] + (p2[1] - p0[1]) / 6)} ${f1(p2[0] - (p3[0] - p1[0]) / 6)} ${f1(p2[1] - (p3[1] - p1[1]) / 6)} ${f1(p2[0])} ${f1(p2[1])}`;
  }
  return s;
}
function spinePaths(sp, v, name) {
  const { pts, ws, ns } = spineSamples(sp, v, name);
  const off = (s) => pts.map((p, i) => [p[0] + ns[i][0] * ws[i] * 0.5 * s, p[1] + ns[i][1] * ws[i] * 0.5 * s]);
  const left = off(1);
  const right = off(-1);
  const m = pts.length - 1;
  const r1 = f1(ws[m] / 2);
  const r0 = f1(ws[0] / 2);
  const rev = right.slice().reverse();
  const d = `M${f1(left[0][0])} ${f1(left[0][1])}${bez(left)}A${r1} ${r1} 0 0 0 ${f1(right[m][0])} ${f1(right[m][1])}${bez(rev)}A${r0} ${r0} 0 0 0 ${f1(left[0][0])} ${f1(left[0][1])}Z`;
  let rim = null;
  if (sp.rimLine) {
    const { side, at, from, to } = sp.rimLine;
    const q = off(side * at).slice(Math.round(from * m), Math.round(to * m) + 1);
    rim = `M${f1(q[0][0])} ${f1(q[0][1])}${bez(q)}`;
  }
  return { d, rim };
}

// ---------- the rig ----------
export class Rig {
  /**
   * @param host element whose content becomes the SVG
   * @param prefix unique per instance on the page (prefixes every SVG id)
   * @param opts.rig result of loadRig(); opts.seed idle randomness; opts.size CSS px of the square art box
   */
  constructor(host, prefix, { rig, seed = 1, size = 200 } = {}) {
    if (!rig) throw new Error('Rig: pass { rig } from loadRig(id)');
    this.def = rig.def;
    this.id = rig.id;
    host.innerHTML = rig.svg
      .replace(/\bid="([^"]+)"/g, `id="${prefix}-$1"`)
      .replace(/href="#([^"]+)"/g, `href="#${prefix}-$1"`)
      .replace(/url\(#([^)]+)\)/g, `url(#${prefix}-$1)`);
    this.svg = host.querySelector('svg');
    this.svg.setAttribute('width', size);
    this.svg.setAttribute('height', size);
    this.svg.setAttribute('aria-hidden', 'true');
    this.svg.setAttribute('focusable', 'false');
    this.prefix = prefix;
    this.els = new Map();
    this.r = rng(seed);
    this.kind = size <= 64 ? 'small' : size <= 128 ? 'medium' : 'large';
    const idle = this.def.idle ?? {};
    this.amp = (idle.sizeAmp ?? { small: 1.8, medium: 1.25, large: 1 })[this.kind]; // idle amplitudes grow as the rig shrinks
    this.cache = new Map();
    this.piv = {};
    for (const [id, p] of Object.entries(this.def.pivots ?? {})) {
      this.piv[id] = Array.isArray(p) ? { at: [p[0], p[1]], local: !!p[2] } : { at: p.at, axis: p.axis ?? 0, mode: p.mode, local: !!p.local };
    }
    // ids the generic part pass skips: they get their transform from a dedicated pass below (eyes, brows, mouth, ...)
    const D = this.def;
    const special = new Set([
      D.shadow?.id ?? 'shadow',
      ...['L', 'R'].flatMap((s) => [D.eyes?.[s]?.eye, D.eyes?.[s]?.pupil, D.eyes?.[s]?.hl]),
      D.brows?.L, D.brows?.R, D.mouth?.group, D.blush?.L, D.blush?.R,
      ...Object.keys(D.sparkles?.items ?? {}),
    ].filter(Boolean));
    this.parts = (D.parts ?? Object.keys(this.piv)).filter((id) => !special.has(id));
    this.spines = this.def.spines ?? {};
    this.discrete = new Set(['mouth', 'eyes', ...Object.keys(this.def.swaps ?? {})]);
    this.defaults = this.def.defaults ?? {};
    this.poses = { idle: {}, ...(this.def.poses ?? {}) };
    this.stills = this.def.stills ?? {};
    this.pose = this.poseOf('idle');
    this.clip = null;
    this.liveFn = null;
    this.env = 0;
    this.micro = null;
    this.blink = null;
    this.reduced = false;
    this.ghost = null;
    this.blend = null;
    this.last = null;
    this.prev = null;
    this.springs = {};
    this.nextMicro = 0;
    this.nextBlink = 0;
    this.mouthShape = null;
    this.mouthAt = -1e9;
    this.now = 0;
    this.log = []; // [ms, what] of idle actions and blinks, read by frame checks
  }

  el(id) {
    if (!this.els.has(id)) this.els.set(id, this.svg.getElementById(`${this.prefix}-${id}`));
    return this.els.get(id);
  }

  /** Value of a channel when nothing drives it. */
  restOf(k) {
    if (k in this.defaults) return this.defaults[k];
    return k === 'idleW' || k === 'all.o' || SCALE.test(k) ? 1 : 0;
  }

  /** Hold pose: the JSON rest values plus the pose's own values (absolute channel values). */
  poseOf(name) {
    return { ...(this.def.rest ?? {}), ...(this.poses[name] ?? this.poses.idle) };
  }

  stillOf(name) {
    return { ...(this.def.rest ?? {}), ...(this.stills[name] ?? this.poses[name] ?? this.stills.idle ?? {}) };
  }

  has(clip) {
    return !!this.def.clips?.[clip];
  }

  // --- commands ---
  setReduced(on, now) {
    this.reduced = on;
    if (!on) this.dropGhost();
    this.clip = null;
    this.micro = null;
    this.blink = null;
    this.springs = {};
    this.snap(now, 0);
  }

  /** Hold a pose (idle / correct / wrong / any key of `poses`) without a clip. */
  hold(name, now, blendMs = 220) {
    this.snap(now, blendMs);
    this.clip = null;
    this.pose = this.poseOf(name);
  }

  /** Play a clip from `clips` (correct, wrong, cheerRise, cheerHop, signature, ...). Unknown names hold idle. */
  play(name, now, onEnd) {
    const def = this.def.clips?.[name];
    if (!def) {
      this.hold('idle', now);
      onEnd?.();
      return;
    }
    this.snap(now, 140);
    this.micro = null;
    this.clip = { def, t0: now, onEnd };
    // a clip that drives the lids owns them: no idle blink on top that would keep the eyes shut longer
    if (def.tracks?.lidU) {
      this.blink = null;
      this.nextBlink = Math.max(this.nextBlink, now + def.dur);
    }
  }

  /** Reduced motion: the new still shows at once and a copy of the old frame fades out over it (120 ms cross-fade). */
  still(name, now) {
    this.clip = null;
    this.dropGhost();
    const el = this.svg.cloneNode(true);
    el.removeAttribute('id');
    el.classList.add('ghost');
    el.style.opacity = '1';
    this.svg.after(el);
    this.ghost = { el, t0: now };
    this.stillTo = this.stillOf(name);
  }

  dropGhost() {
    this.ghost?.el.remove();
    this.ghost = null;
  }

  /** 실시간 말하기: fn(now) -> { shape, level } 또는 null(끝). 입 모양은 실제 음성에서 온다. */
  live(fn) {
    this.liveFn = fn;
  }

  snap(now, ms) {
    this.blend = this.last && ms > 0 ? { from: this.last, t0: now, ms } : null;
  }

  // --- per frame ---
  tick(now) {
    const dt = clamp(this.prev == null ? 1 / 60 : (now - this.prev) / 1000, 1 / 240, 1 / 20);
    this.prev = now;
    this.now = now;
    const D = this.def;
    const v = { ...(this.reduced ? this.stillTo ?? this.stillOf('idle') : this.pose) };
    const get = (k, d = this.restOf(k)) => (k in v ? v[k] : d);

    // main clip
    if (this.clip) {
      const { def, t0 } = this.clip;
      const t = now - t0;
      for (const [k, keys] of Object.entries(def.tracks ?? {})) v[k] = track(keys, t);
      for (const [k, keys] of Object.entries(def.steps ?? {})) v[k] = step(keys, t);
      if (t >= def.dur) {
        const { onEnd } = this.clip;
        this.clip = null;
        this.pose = this.poseOf(def.end ?? 'idle');
        onEnd?.();
      }
    }
    // keep volume: a squashed body widens unless the clip sets the width itself
    const vol = D.volume;
    if (vol && !(`${vol.part}.sx` in v) && `${vol.part}.sy` in v) v[`${vol.part}.sx`] = 1 + (1 - v[`${vol.part}.sy`]) * vol.k;

    if (!this.reduced) {
      const idle = D.idle ?? {};
      const w = get('idleW', 1);
      const A = this.amp;
      const s = now / 1000;
      // breathing and other slow loops
      for (const wv of idle.waves ?? []) {
        const a = wv.amp * (wv.scaled ? A : 1);
        let x = Math.sin((s * 2 * Math.PI * 1000) / wv.period + (wv.phase ?? 0));
        if (wv.abs) x = Math.abs(x);
        v[wv.ch] = get(wv.ch) + a * x * w;
      }

      // idle micro actions only when no clip or line is playing
      if (!this.clip && !this.liveFn && !this.hush) {
        if (!this.micro && now >= this.nextMicro) {
          const bags = idle.bags ?? {};
          const { bag = [], gap = [3000, 8000] } = bags[this.kind] ?? bags.large ?? {};
          if (this.nextMicro && bag.length) {
            const name = this.r.pick(bag);
            const m = this.microOf(name);
            if (m) {
              this.micro = { ...m, t0: now };
              this.log.push([now, name]);
            }
          }
          // the first action comes soon after the page opens, then every few seconds
          this.nextMicro = now + (this.nextMicro ? this.r.range(...gap) : this.r.range(...(idle.first ?? [1200, 2400])));
        }
      } else this.micro = null;
      if (this.micro) {
        const t = now - this.micro.t0;
        for (const [k, keys] of Object.entries(this.micro.tracks)) v[k] = get(k) + track(keys, t) * (this.micro.gain[k] ?? 1);
        for (const [k, keys] of Object.entries(this.micro.steps ?? {})) v[k] = step(keys, t);
        if (t >= this.micro.dur) this.micro = null;
      }
      // blinks on their own clock (sometimes double), not while the eyes are closed by a pose
      const bl = idle.blink;
      if (bl) {
        if (!this.blink && now >= this.nextBlink) {
          if (this.nextBlink && (v.eyes ?? 'open') === 'open') {
            this.blink = { t0: now, twice: this.r.next() < (bl.double ?? 0) };
            this.log.push([now, this.blink.twice ? 'blink2' : 'blink']);
          }
          this.nextBlink = now + (this.nextBlink ? this.r.range(...bl.gap) : this.r.range(...(bl.first ?? [600, 1500])));
        }
        if (this.blink) {
          const t = now - this.blink.t0;
          const gap2 = bl.doubleGap ?? 230;
          const t2 = this.blink.twice && t > gap2 ? t - gap2 : t;
          v.lidU = Math.max(get('lidU'), track(bl.lidU, t2));
          v.lidD = Math.max(get('lidD'), track(bl.lidD, t2));
          if (t >= (this.blink.twice ? gap2 + bl.dur : bl.dur)) this.blink = null;
        }
      }
      // live talk layer: mouth shape and loudness come from the audio that is playing now
      const lv = this.liveFn?.(now);
      if (this.liveFn && !lv) this.liveFn = null;
      this.env += ((lv ? 1 : 0) - this.env) * (1 - Math.exp(-dt / (lv ? 0.06 : 0.16)));
      if (lv || this.env > 0.02) {
        const T = D.talk ?? {};
        const env = this.env;
        const lev = lv?.level ?? 0;
        this.lev = (this.lev ?? 0) + (lev - (this.lev ?? 0)) * (1 - Math.exp(-dt / 0.05));
        if (lv) v.mouth = lv.shape === 'rest' ? get('mouth', D.mouth?.rest) : T.visemes?.[lv.shape] ?? lv.shape;
        if (T.browY) v.browY = get('browY') + T.browY * env * (0.4 + 0.6 * this.lev);
        for (const gs of T.gestures ?? []) v[gs.ch] = get(gs.ch) + gs.amp * Math.sin((now / gs.period) * 2 * Math.PI + (gs.phase ?? 0)) * env;
        for (const ls of T.loud ?? []) v[ls.ch] = get(ls.ch) + ls.amp * this.lev * env;
      }
    }

    // blend away a discontinuity (a command arrived mid-motion): on the first frame keep the gap between where each
    // channel was and where the new motion starts, then fade that gap out over the blend. The new motion itself plays at
    // full speed. Channels only one side has use their rest value.
    if (this.blend) {
      const b = this.blend;
      if (!b.off) {
        const tracks = this.clip && this.clip.t0 === b.t0 ? this.clip.def.tracks ?? {} : {};
        const t1 = now - b.t0;
        b.off = {};
        for (const k of new Set([...Object.keys(b.from), ...Object.keys(v)])) {
          if (this.discrete.has(k) || (k in v && typeof v[k] !== 'number')) continue;
          const moved = tracks[k] ? track(tracks[k], t1) - track(tracks[k], 0) : 0;
          b.off[k] = (k in b.from ? b.from[k] : this.restOf(k)) - get(k) + moved;
        }
      }
      const u = clamp((now - b.t0) / b.ms, 0, 1);
      if (u >= 1) this.blend = null;
      else {
        const keep = 1 - E.inout(u);
        for (const [k, o] of Object.entries(b.off)) if (o) v[k] = get(k) + o * keep;
      }
    }
    this.last = Object.fromEntries(Object.entries(v).filter(([, x]) => typeof x === 'number'));

    if (!this.reduced) this.secondary(v, dt);
    this.apply(v);
    if (this.ghost) {
      const o = 1 - (now - this.ghost.t0) / 120;
      if (o <= 0) this.dropGhost();
      else this.ghost.el.style.opacity = String(Math.round(o * 1000) / 1000);
    }
  }

  /** An idle action from the JSON `idle.micro` table, with its random side / variant picked now. */
  microOf(name) {
    const m = this.def.idle?.micro?.[name];
    if (!m) return null;
    const base = m.variants ? this.r.pick(m.variants) : m;
    const gain = {};
    if (m.mirror) {
      const d = this.r.pick([-1, 1]);
      for (const k of m.mirror === true ? Object.keys(base.tracks) : m.mirror) gain[k] = d;
    }
    for (const [k, list] of Object.entries(m.pick ?? {})) gain[k] = (gain[k] ?? 1) * this.r.pick(list);
    return { dur: m.dur ?? base.dur, tracks: base.tracks, steps: base.steps, gain };
  }

  /** Follow-through: springs on root height velocity (vy), head and body turn speed (hr, br), plus feet that dangle. */
  secondary(v, dt) {
    const S = this.def.secondary;
    if (!S) return;
    const g = (k, d = this.restOf(k)) => (k in v ? v[k] : d);
    const bodyId = this.def.bodyPart ?? 'body';
    const rootY = g('root.y');
    const p = this.prevVals ?? { rootY, headR: g('head.r'), bodyR: g(`${bodyId}.r`) };
    let vy = (rootY - p.rootY) / dt;
    if (Math.abs(rootY - p.rootY) > 300) {
      this.springs = {}; // a teleport (cheer reset), not motion
      vy = 0;
    }
    const src = { vy, hr: (g('head.r') - p.headR) / dt, br: (g(`${bodyId}.r`) - p.bodyR) / dt };
    this.prevVals = { rootY, headR: g('head.r'), bodyR: g(`${bodyId}.r`) };
    for (const s of S.springs ?? []) {
      const target = clamp((s.vy ?? 0) * src.vy + (s.hr ?? 0) * src.hr + (s.br ?? 0) * src.br, -s.clamp, s.clamp);
      const st = (this.springs[s.ch] ??= { x: 0, v: 0 });
      st.v += ((s.k ?? 200) * (target - st.x) - (s.c ?? 13) * st.v) * dt;
      st.x += st.v * dt;
      v[s.ch] = g(s.ch) + st.x;
    }
    for (const s of S.stretch ?? []) v[s.ch] = g(s.ch, 1) * (1 + clamp(s.vy * src.vy, -s.clamp, s.clamp));
    if (S.air) {
      const air = clamp(-rootY / S.air.lift, 0, 1);
      for (const [k, a] of Object.entries(S.air.add)) v[k] = g(k) + a * air;
    }
  }

  apply(v) {
    const D = this.def;
    const g = (k, d = this.restOf(k)) => (k in v ? v[k] : d);
    const f = (n) => Math.round(n * 100) / 100;
    const yaw = clamp(g('yaw'), -1, 1);
    const pitch = clamp(g('pitch'), -1, 1);
    const par = D.parallax ?? {};
    const follow = Object.fromEntries((D.follow ?? []).map((x) => [x.id, x]));
    const ride = (x) => {
      const pv = this.piv[x.on];
      const [hx, hy] = pv.at;
      const w = x.w;
      return `translate(${f(hx + w * g(`${x.on}.x`))} ${f(hy + w * g(`${x.on}.y`))}) rotate(${f(w * g(`${x.on}.r`))}) translate(${-hx} ${-hy}) `;
    };
    const T = (id, x = 0, y = 0, r = 0, sx = 1, sy = 1) => {
      const pv = this.piv[id];
      if (!pv) return;
      const [px, py] = pv.at;
      const pre = follow[id] ? ride(follow[id]) : '';
      let s = `${pre}translate(${f(px + x)} ${f(py + y)})`;
      if (pv.mode === 'skew') {
        // a part joined along a base line (ears): shear along that line so the line itself never moves (no seams)
        if (r || sx !== 1 || sy !== 1) s += ` rotate(${pv.axis}) skewX(${f(-r)}) scale(${f(sx)} ${f(sy)}) rotate(${-pv.axis})`;
      } else {
        if (r) s += ` rotate(${f(r)})`;
        if (sx !== 1 || sy !== 1) s += ` scale(${f(sx)} ${f(sy)})`;
      }
      if (!pv.local) s += ` translate(${-px} ${-py})`;
      this.set(id, 'transform', s);
    };

    // ground shadow stays on the floor: shrinks and fades with height, widens on a squash, hides below the edge
    const sh = D.shadow;
    const rootY = g('root.y');
    if (sh && this.piv[sh.id ?? 'shadow']) {
      const id = sh.id ?? 'shadow';
      const h = clamp(-rootY / sh.lift, 0, 1);
      const bx = g(`${D.volume?.part ?? 'body'}.sx`, 1);
      const ss = (1 - sh.shrink * h) * (1 + (bx - 1) * 0.8);
      T(id, g('root.x') * (sh.followX ?? 1), 0, 0, ss, 1 - sh.shrink * h);
      this.set(id, 'opacity', rootY > 0 ? clamp(1 - rootY / 120, 0, 1) : 1 - sh.fade * h);
    }

    // every other part: its own channels plus parallax from yaw
    for (const id of this.parts) {
      if (id === (sh?.id ?? 'shadow')) continue;
      const p = par[id];
      const px = p ? (typeof p === 'number' ? p : p.x ?? 0) * yaw : 0;
      let sx = g(`${id}.sx`, 1) * g(`${id}.s`, 1);
      const sy = g(`${id}.sy`, 1) * g(`${id}.s`, 1);
      if (p && typeof p === 'object' && p.squash) sx *= 1 - p.squash * Math.max(0, p.side * yaw);
      const py = p && typeof p === 'object' ? (p.y ?? 0) * pitch : 0;
      T(id, g(`${id}.x`) + px, g(`${id}.y`) + py, g(`${id}.r`), sx, sy * (1 + (p?.sq ?? 0) * pitch));
    }

    // eyes: look moves the eye, the pupil inside it and the highlight against it; lids close from both sides
    const EY = D.eyes;
    if (EY) {
      const lk = EY.look ?? {};
      const lx = g('lookX');
      const ly = g('lookY');
      const lu = clamp(g('lidU'), 0, 1);
      const ld = clamp(g('lidD'), 0, 1);
      const mode = v.eyes ?? 'open';
      for (const side of ['L', 'R']) {
        const e = EY[side];
        if (!e) continue;
        const [ex, ey] = lk.eye ?? [0, 0];
        const [qx, qy] = lk.pupil ?? [0, 0];
        const [hx, hy] = lk.hl ?? [0, 0];
        const sq = EY.yawSquash ? 1 - EY.yawSquash * Math.max(0, (side === 'L' ? -1 : 1) * yaw) : 1;
        if (e.eye) T(e.eye, lx * ex + g(`${e.eye}.x`), ly * ey + g(`${e.eye}.y`), 0, sq, 1);
        if (e.pupil) T(e.pupil, lx * qx, ly * qy);
        if (e.hl) T(e.hl, lx * hx, ly * hy);
        const lo = EY.lidOpen ?? { U: -80, D: 50 };
        const lus = clamp(lu + g(`lidU${side}`), 0, 1);
        // lid edge angle (lidrL / lidrR, degrees) turns around the eye's own pin, so a flat lid can look bored, sharp or soft
        const lr = g(`lidr${side}`);
        const pin = EY.pin?.[side];
        const ly0 = Math.round(lo.U * (1 - lus) * 10) / 10;
        if (e.lidU) this.set(e.lidU, 'transform', `translate(0 ${ly0})${lr && pin ? ` rotate(${f(lr * (1 - lus * lus))} ${pin[0]} ${pin[1]})` : ''}`);
        if (e.lidD) this.set(e.lidD, 'transform', `translate(0 ${Math.round(lo.D * (1 - ld) * 10) / 10})`);
        // eye states: 'open' plus any closed shape the SVG draws (e.g. happy)
        for (const [state, id] of Object.entries(e.states ?? {})) this.set(id, 'opacity', state === mode ? 1 : 0);
      }
    }

    // brows: hidden unless brows.o; browY lifts both, knit turns them worried (never angry)
    const BR = D.brows;
    if (BR) {
      const knit = g('knit');
      const by = g('browY');
      this.set(BR.group, 'opacity', clamp(g('brows.o'), 0, 1));
      const [ax, ay, ar] = BR.knit?.L ?? [6, -6, -20];
      const [bx, bby, br] = BR.knit?.R ?? [-6, -6, 20];
      T(BR.L, ax * knit, by + ay * knit + g('browLy'), ar * knit + g('browLr'));
      T(BR.R, bx * knit, by + bby * knit + g('browRy'), br * knit + g('browRr'));
    }

    // mouth: one shape visible; an opening shape pops in from 80% height
    const M = D.mouth;
    if (M) {
      const shape = v.mouth ?? M.rest;
      if (shape !== this.mouthShape) {
        this.mouthShape = shape;
        this.mouthAt = this.now;
      }
      for (const [name, id] of Object.entries(M.shapes)) this.set(id, 'opacity', name === shape ? 1 : 0);
      const pops = (M.pop ?? ['small', 'wide', 'round']).includes(shape) && !this.reduced;
      const pop = pops ? 0.8 + 0.2 * E.out(clamp((this.now - this.mouthAt) / 70, 0, 1)) : 1;
      T(M.group, g(`${M.group}.x`), g(`${M.group}.y`), 0, g(`${M.group}.sx`, 1), pop * g(`${M.group}.sy`, 1));
    }

    // cheeks
    const BL = D.blush;
    if (BL) {
      const bs = g('blush.s', 1);
      const q = BL.yawSquash ?? 0.2;
      if (BL.L) T(BL.L, 0, 0, 0, bs * (1 - q * Math.max(0, -yaw)), bs);
      if (BL.R) T(BL.R, 0, 0, 0, bs * (1 - q * Math.max(0, yaw)), bs);
    }

    // discrete swaps (hands, book open/closed, ...): exactly one child visible
    for (const [ch, map] of Object.entries(D.swaps ?? {})) {
      const cur = v[ch] ?? this.defaults[ch] ?? Object.keys(map)[0];
      for (const [name, ids] of Object.entries(map)) for (const id of [].concat(ids)) this.set(id, 'opacity', name === cur ? 1 : 0);
    }
    // continuous fades
    for (const [ch, ids] of Object.entries(D.fades ?? {})) {
      const o = Math.round(clamp(g(ch), 0, 1) * 1000) / 1000;
      for (const id of [].concat(ids)) this.set(id, 'opacity', o);
    }

    // spines: rebuild the path data from the joint channels
    for (const [name, sp] of Object.entries(this.spines)) {
      const { d, rim } = spinePaths(sp, v, name);
      this.set(sp.path, 'd', d);
      if (rim && sp.rim) this.set(sp.rim, 'd', rim);
    }

    // limbs: shoulder -> elbow -> wrist by IK on the target channels, drawn as one smooth tube; the hand rides the wrist
    for (const [name, lb] of Object.entries(D.limbs ?? {})) {
      const [sx0, sy0] = lb.shoulder;
      const tx = g(`${name}.tx`);
      const ty = g(`${name}.ty`);
      const dx = tx - sx0;
      const dy = ty - sy0;
      const reach = Math.hypot(dx, dy) || 1;
      const k = clamp(reach / ((lb.l1 + lb.l2) * 0.985), 1, lb.stretch ?? 1.3);
      const l1 = lb.l1 * k;
      const l2 = lb.l2 * k;
      const d = Math.min(reach, l1 + l2 - 0.5);
      const base = Math.atan2(dy, dx);
      const a = Math.acos(clamp((l1 * l1 + d * d - l2 * l2) / (2 * l1 * d), -1, 1));
      const bend = clamp(g(`${name}.bend`), -1, 1);
      const ex = sx0 + Math.cos(base + a * bend) * l1 * (1 - 0.25 * (1 - Math.abs(bend)));
      const ey = sy0 + Math.sin(base + a * bend) * l1 * (1 - 0.25 * (1 - Math.abs(bend)));
      // quadratic through the elbow: control point pushed past it so the tube really bends there
      const cx = 2 * ex - (sx0 + tx) / 2;
      const cy = 2 * ey - (sy0 + ty) / 2;
      const q = 0.82;
      const c = `M${f1(sx0)} ${f1(sy0)}Q${f1(ex + (cx - ex) * q)} ${f1(ey + (cy - ey) * q)} ${f1(tx)} ${f1(ty)}`;
      for (const id of lb.paths) this.set(id, 'd', c);
      const ang = (Math.atan2(ty - (ey + (cy - ey) * 0.4), tx - (ex + (cx - ex) * 0.4)) * 180) / Math.PI + 90;
      this.set(lb.hand, 'transform', `translate(${f1(tx)} ${f1(ty)}) rotate(${f1(ang + g(`${name}.hr`))})`);
      if (lb.cuff) this.set(lb.cuff, 'transform', `translate(${f1(tx)} ${f1(ty)}) rotate(${f1(ang)})`);
    }

    // sparkles: pop in, slow spin
    const SPK = D.sparkles;
    if (SPK) {
      const sk = clamp(g('sparks'), 0, 1);
      this.set(SPK.group, 'opacity', sk > 0.01 ? 1 : 0);
      const spin = this.reduced ? 0 : (this.now / (SPK.spin ?? 12)) % 360;
      for (const [id, base] of Object.entries(SPK.items)) T(id, 0, 0, spin * (SPK.ccw?.includes(id) ? -1 : 1), base * sk, base * sk);
    }

    this.svg.style.opacity = String(Math.round(clamp(g('all.o', 1), 0, 1) * 1000) / 1000);
  }

  set(id, attr, value) {
    const key = `${id}|${attr}`;
    const s = String(value);
    if (this.cache.get(key) === s) return;
    this.cache.set(key, s);
    this.el(id)?.setAttribute(attr, s);
  }
}
