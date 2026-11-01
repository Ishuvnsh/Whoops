"use strict";
/*
 * WHOOPS cinematic layer.
 *
 * Draws the idle ambience and the "fingerprint signal rain" on two dedicated
 * canvases (one behind the page, one in front). It never touches the spiral
 * drawing itself: the spiral stays the original SVG built by collection_module.js,
 * and only its CSS transform is animated from outside.
 *
 * Data integrity:
 *  - Labelled fragments (key: value) come only from values this browser really
 *    exposes for the five signal groups WHOOPS actually collects
 *    (Navigator, Screen, Canvas, WebGL availability, Timezone).
 *  - Unlabelled hex / binary / pixel fragments are purely decorative noise.
 *  - This file never reads or writes the fingerprint record that is sent to /collect.
 *
 * Public API (window.WhoopsCinema):
 *   begin()  -> controller | null   (null = reduced motion / no canvas: use the plain flow)
 *   idle()   -> start faint entrance ambience
 *   controller.feedRecord(record), controller.finish(), controller.abort()
 */
(function () {
  const doc = document;
  const $ = (id) => doc.getElementById(id);
  const mq = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };

  const TAU = Math.PI * 2;
  const ROW = 22; // row height of the coordinated "data stream" layout
  const MONO = "'IBM Plex Mono',ui-monospace,Menlo,Consolas,monospace";
  const HEXCH = '0123456789abcdef';
  const TINT = ['rgb(205,212,222)', 'rgb(160,210,230)']; // cool grey, rare ice-blue accent
  const FONT = [];
  for (let s = 8; s <= 13; s++) FONT[s] = s + 'px ' + MONO;

  const PIXEL = 0, HEX = 1, BIN = 2, KV = 3, LINE = 4, CLUSTER = 5, LABEL = 6;
  const CATS = ['CANVAS', 'WEBGL', 'SCREEN', 'NAVIGATOR', 'TIMEZONE'];

  const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);
  const rnd = (a, b) => a + Math.random() * (b - a);
  const easeIO = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
  const easeOut = (t) => 1 - Math.pow(1 - t, 3);
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));

  function rndHex(n) { let s = ''; for (let i = 0; i < n; i++) s += HEXCH[(Math.random() * 16) | 0]; return s; }
  function rndBin(n) { let s = ''; for (let i = 0; i < n; i++) s += Math.random() < 0.5 ? '0' : '1'; return s; }

  function canRun() {
    try {
      if (mq.matches) return false;
      if (!$('collect') || !$('origin')) return false;
      const c = doc.createElement('canvas');
      return !!(c.getContext && c.getContext('2d')) && typeof requestAnimationFrame === 'function';
    } catch (e) { return false; }
  }

  const newParticle = () => ({
    on: false, k: 0, layer: 0, tint: 0, x: 0, y: 0, vx: 0, vy: 0, s: 2, a: 0.15,
    age: 0, life: 5, text: '', fs: 10, conv: 0, seed: 0, tag: '', bright: false
  });

  const E = {
    back: null, front: null, bx: null, fx: null,
    W: 0, H: 0, dpr: 1, cx: 0, cy: 0, sr: 150, vl: 0, vr: 0, vt: 0, vb: 0,
    pool: [], max: 100, active: 0, cursor: 0,
    raf: 0, last: 0, running: false, attached: false, dirty: false,
    mode: 'off', t0: 0, t: 0, released: false, dead: false,
    intensity: 0, zoom: 1, coord: 0, spread: 0, grid: 0, fade: 1, glow: 0, p4Start: 0,
    tw: [], ripples: [{ t: -1 }, { t: -1 }],
    sweeps: [{ on: false, y: 0, a: 0 }, { on: false, y: 0, a: 0 }], nextSweep: 0,
    links: [], nextLink: 0, nextRandLink: 0,
    labels: [], sig: { CANVAS: [], WEBGL: [], SCREEN: [], NAVIGATOR: [], TIMEZONE: [] },
    hex: [], bin: []
  };
  for (let i = 0; i < 6; i++) E.links.push({ on: false, a: null, b: null, sa: 0, sb: 0, t: 0, ttl: 1.6 });

  /* ---------- setup / teardown ---------- */

  function makeCanvas(z) {
    const c = doc.createElement('canvas');
    c.className = 'cinema-canvas';
    c.setAttribute('aria-hidden', 'true');
    c.style.zIndex = z;
    doc.body.appendChild(c);
    return c;
  }

  function onResize() { resize(); }
  function onScroll() { E.dirty = true; }
  function onVisibility() {
    if (doc.hidden) { stopLoop(); }
    else if (E.mode !== 'off' && !E.dead) { startLoop(); }
  }

  function attach() {
    if (E.attached) return;
    if (!E.back) {
      E.back = makeCanvas(0); E.front = makeCanvas(3);
      E.bx = E.back.getContext('2d'); E.fx = E.front.getContext('2d');
      if ('letterSpacing' in E.fx) { E.fx.letterSpacing = '1.2px'; }
    }
    E.back.style.display = E.front.style.display = 'block';
    window.addEventListener('resize', onResize);
    window.addEventListener('scroll', onScroll, { passive: true });
    doc.addEventListener('visibilitychange', onVisibility);
    E.attached = true;
    E.W = E.H = 0;
    resize();
  }

  function detach() {
    if (!E.attached) return;
    window.removeEventListener('resize', onResize);
    window.removeEventListener('scroll', onScroll);
    doc.removeEventListener('visibilitychange', onVisibility);
    if (E.back) { E.back.style.display = E.front.style.display = 'none'; }
    E.attached = false;
  }

  function resize() {
    const W = window.innerWidth, H = window.innerHeight, dpr = Math.min(window.devicePixelRatio || 1, 2);
    if (W === E.W && H === E.H && dpr === E.dpr) { measure(); return; }
    E.W = W; E.H = H; E.dpr = dpr;
    for (const c of [E.back, E.front]) { c.width = Math.round(W * dpr); c.height = Math.round(H * dpr); }
    const area = W * H;
    E.max = Math.round(clamp(area / 6500, 90, W < 640 ? 120 : 240));
    const need = Math.round(E.max * 1.5);
    while (E.pool.length < need) E.pool.push(newParticle());
    measure();
  }

  // Reads the spiral's position (layout read) only on setup, resize and scroll.
  function measure() {
    const el = $('collect');
    const r = el ? el.getBoundingClientRect() : null;
    if (r && r.width > 0) { E.cx = r.left + r.width / 2; E.cy = r.top + r.height / 2; E.sr = Math.min(r.width, r.height) / 2; }
    else { E.cx = E.W / 2; E.cy = E.H / 2; E.sr = 140; }
  }

  function resetState() {
    for (const p of E.pool) p.on = false;
    E.active = 0; E.cursor = 0; E.tw.length = 0;
    E.intensity = 0; E.zoom = 1; E.coord = 0; E.spread = 0; E.grid = 0; E.fade = 1; E.glow = 0; E.p4Start = 0;
    E.released = false; E.dead = false;
    for (const s of E.sweeps) s.on = false;
    for (const l of E.links) l.on = false;
    for (const r of E.ripples) r.t = -1;
    E.labels.length = 0;
  }

  function startLoop() { if (E.running) return; E.running = true; E.last = 0; E.raf = requestAnimationFrame(frame); }
  function stopLoop() { E.running = false; if (E.raf) cancelAnimationFrame(E.raf); E.raf = 0; }

  function clearCanvases() {
    if (!E.bx) return;
    for (const c of [E.bx, E.fx]) { c.setTransform(1, 0, 0, 1, 0, 0); c.clearRect(0, 0, c.canvas.width, c.canvas.height); }
  }

  function shutdown() {
    stopLoop(); E.mode = 'off'; resetState(); clearCanvases(); detach();
  }

  // Something went wrong in the animation: stop it quietly. The page keeps working.
  function kill(err) {
    if (window.console) console.warn('WHOOPS cinema stopped:', err);
    E.dead = true; stopLoop(); clearCanvases();
    doc.body.classList.remove('cinema-on');
    const sp = $('collect'); if (sp) sp.classList.remove('activating');
  }

  /* ---------- signal pools ---------- */

  function buildSignals() {
    if (!E.hex.length) {
      for (let i = 0; i < 320; i++) E.hex.push(Math.random() < 0.3 ? rndHex(4) + '…' + rndHex(4) : rndHex((6 + Math.random() * 5) | 0));
      for (let i = 0; i < 80; i++) E.bin.push(rndBin(8) + ' ' + rndBin(8));
    }
    const sg = { CANVAS: [], WEBGL: [], SCREEN: [], NAVIGATOR: [], TIMEZONE: [] };
    try {
      const n = navigator, s = window.screen;
      if (n.platform) sg.NAVIGATOR.push('platform: ' + n.platform);
      if (n.language) sg.NAVIGATOR.push('language: ' + n.language);
      if (n.hardwareConcurrency != null) sg.NAVIGATOR.push('cores: ' + n.hardwareConcurrency);
      if (n.maxTouchPoints != null) sg.NAVIGATOR.push('touch_points: ' + n.maxTouchPoints);
      if (n.cookieEnabled != null) sg.NAVIGATOR.push('cookies: ' + (n.cookieEnabled ? 'enabled' : 'disabled'));
      if (n.userAgent) sg.NAVIGATOR.push('ua: ' + n.userAgent.slice(0, 26) + '…');
      sg.SCREEN.push('screen: ' + s.width + 'x' + s.height);
      sg.SCREEN.push('viewport: ' + window.innerWidth + 'x' + window.innerHeight);
      sg.SCREEN.push('pixel_ratio: ' + window.devicePixelRatio);
      if (s.colorDepth != null) sg.SCREEN.push('color_depth: ' + s.colorDepth);
      const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
      if (tz) sg.TIMEZONE.push('timezone: ' + tz);
    } catch (e) { /* missing values are simply not shown */ }
    E.sig = sg;
  }

  function pickCat() {
    let tries = 0;
    while (tries++ < 6) {
      const c = CATS[(Math.random() * CATS.length) | 0];
      if (E.sig[c].length) return c;
    }
    return '';
  }

  /* ---------- particles ---------- */

  function acquire() {
    const n = E.pool.length;
    for (let i = 0; i < n; i++) {
      const idx = (E.cursor + i) % n;
      if (!E.pool[idx].on) { E.cursor = (idx + 1) % n; E.active++; return E.pool[idx]; }
    }
    return null;
  }
  function release(p) { if (p.on) { p.on = false; E.active--; } }

  function spawn(p) {
    const idle = E.mode === 'idle';
    const vl = E.vl, vr = E.vr, vt = E.vt, vb = E.vb, vw = vr - vl, vh = vb - vt;
    const r = Math.random();
    let k;
    if (idle) k = r < 0.72 ? PIXEL : r < 0.9 ? HEX : LINE;
    else k = r < 0.40 ? PIXEL : r < 0.60 ? HEX : r < 0.68 ? BIN : r < 0.82 ? KV : r < 0.92 ? LINE : CLUSTER;

    let text = '', tag = '';
    if (k === KV) { tag = pickCat(); if (tag) text = E.sig[tag][(Math.random() * E.sig[tag].length) | 0]; else k = HEX; }
    if (k === HEX) text = E.hex[(Math.random() * E.hex.length) | 0];
    if (k === BIN) text = E.bin[(Math.random() * E.bin.length) | 0];

    p.k = k; p.text = text; p.tag = tag; p.age = 0; p.conv = 0;
    p.seed = (Math.random() * 0x7fffffff) | 0;
    p.layer = !idle && Math.random() < 0.22 ? 1 : 0;
    const d = p.layer ? rnd(0.7, 1.25) : rnd(0.3, 1);
    p.tint = 0; p.bright = false;

    let a = rnd(0.07, 0.22) * (p.layer ? 0.9 : 1);
    if (!idle && Math.random() < 0.07) { a = rnd(0.5, 0.8); p.bright = true; p.tint = 1; }
    else if (k === KV) { a = Math.max(a, 0.26); if (Math.random() < 0.3) p.tint = 1; }
    p.a = idle ? a * 0.45 : a;
    p.fs = Math.round(clamp(8 + 3.4 * d + (p.bright ? 1 : 0), 8, 13));
    p.s = k === PIXEL ? (d > 0.9 ? 3 : d > 0.6 ? 2 : 1.4) : k === LINE ? rnd(18, 90) * d : k === CLUSTER ? 2 : 0;
    p.life = idle ? rnd(10, 20) : rnd(4.5, 9);

    const m = Math.random(), sp = 35 + 130 * d;
    if (idle) {
      p.x = rnd(vl, vr); p.y = rnd(vt, vb); p.vx = rnd(-5, 5); p.vy = rnd(3, 12) * d;
    } else if (m < 0.16 && E.intensity > 0.25) {
      // convergence: drawn toward the spiral
      const ang = rnd(0, TAU), R = Math.max(vw, vh) * rnd(0.35, 0.6);
      p.x = clamp(E.cx + Math.cos(ang) * R, vl - 10, vr + 10);
      p.y = clamp(E.cy + Math.sin(ang) * R, vt - 10, vb + 10);
      const dx = E.cx - p.x, dy = E.cy - p.y, dd = Math.hypot(dx, dy) || 1, v = rnd(40, 80);
      p.vx = dx / dd * v; p.vy = dy / dd * v; p.conv = rnd(80, 170);
    } else if (m < 0.55 || E.intensity < 0.5) {
      p.x = rnd(vl, vr); p.y = vt - 10; p.vx = rnd(-12, 12) * d; p.vy = sp * rnd(0.8, 1.2);
    } else if (m < 0.67) {
      p.x = vl - 10; p.y = rnd(vt, vb * 0.8); p.vx = 40 + 90 * d; p.vy = rnd(-40, 50);
    } else if (m < 0.79) {
      p.x = vr + 10; p.y = rnd(vt, vb * 0.8); p.vx = -(40 + 90 * d); p.vy = rnd(-40, 50);
    } else {
      p.x = rnd(vl, vr); p.y = rnd(vt, vb); p.vx = rnd(-20, 20); p.vy = rnd(20, 90) * d;
    }
    p.on = true;
  }

  function addLink(a, b) {
    const l = E.links[E.nextLink++ % E.links.length];
    l.on = true; l.a = a; l.b = b; l.sa = a.seed; l.sb = b ? b.seed : 0; l.t = performance.now(); l.ttl = 1.6;
  }

  function spawnLabel(cat, i) {
    const p = acquire(); if (!p) return;
    const ang = (i / CATS.length) * TAU - Math.PI / 2 + rnd(-0.25, 0.25), rad = E.sr * rnd(1.2, 1.55);
    p.k = LABEL; p.text = '+ ' + cat; p.tag = cat; p.age = 0; p.conv = 0; p.seed = (Math.random() * 0x7fffffff) | 0;
    p.layer = 1; p.tint = 1; p.bright = true; p.a = 0.62; p.fs = 9; p.life = 2.1; p.s = 0;
    p.x = clamp(E.cx + Math.cos(ang) * rad, 14, E.W - 120); p.y = clamp(E.cy + Math.sin(ang) * rad, 24, E.H - 24);
    p.vx = 0; p.vy = rnd(-4, 4); p.on = true;
    addLink(p, null);
    const pool = E.sig[cat];
    if (pool.length) {
      const q = acquire();
      if (q) {
        q.k = KV; q.text = pool[(Math.random() * pool.length) | 0]; q.tag = cat; q.age = 0; q.conv = 0;
        q.seed = (Math.random() * 0x7fffffff) | 0; q.layer = 1; q.tint = 0; q.bright = false; q.a = 0.46; q.fs = 10;
        q.life = 2.1; q.s = 0; q.x = p.x; q.y = p.y + 14; q.vx = p.vx; q.vy = p.vy; q.on = true;
      }
    }
  }

  /* ---------- tweens ---------- */

  function tween(key, to, dur, delay, fn) {
    E.tw = E.tw.filter((t) => t.key !== key);
    E.tw.push({ key, from: null, to, dur, delay: delay || 0, start: performance.now(), fn: fn || easeIO });
  }
  function runTweens(now) {
    for (let i = E.tw.length - 1; i >= 0; i--) {
      const t = E.tw[i];
      if (now < t.start + t.delay) continue;
      if (t.from === null) t.from = E[t.key];
      const u = clamp((now - t.start - t.delay) / t.dur, 0, 1);
      E[t.key] = t.from + (t.to - t.from) * t.fn(u);
      if (u >= 1) E.tw.splice(i, 1);
    }
  }

  /* ---------- frame loop ---------- */

  function frame(now) {
    E.raf = 0;
    if (!E.running) return;
    try {
      if (E.mode === 'idle' && E.last && now - E.last < 33) { E.raf = requestAnimationFrame(frame); return; }
      const dt = Math.min(0.05, (E.last ? now - E.last : 16) / 1000);
      E.last = now;
      if (E.dirty) { measure(); E.dirty = false; }
      runTweens(now);
      update(dt, now);
      draw(now);
    } catch (err) { kill(err); return; }
    E.raf = requestAnimationFrame(frame);
  }

  function update(dt, now) {
    const idle = E.mode === 'idle', z = E.zoom;
    E.vl = E.cx - E.cx / z; E.vr = E.cx + (E.W - E.cx) / z;
    E.vt = E.cy - E.cy / z; E.vb = E.cy + (E.H - E.cy) / z;
    E.t = now - E.t0;

    if (idle) E.intensity = 0.05;
    else if (!E.released) {
      const t = E.t;
      E.intensity = t < 500 ? 0.12 + 0.2 * (t / 500)
        : t < 1500 ? 0.32 + 0.33 * ((t - 500) / 1000)
        : t < 3000 ? 0.65 + 0.35 * easeIO((t - 1500) / 1500) : 1;
    }
    if (E.p4Start) E.spread = Math.sin(Math.PI * clamp((now - E.p4Start) / 1000, 0, 1));
    E.glow = Math.max(0, E.glow - dt * 0.9);

    // phase 2: category labels
    if (!idle) {
      while (E.labels.length && E.t >= E.labels[0].at) { const l = E.labels.shift(); spawnLabel(l.cat, l.i); }
    }

    // spawn up to the target density
    let desired;
    if (idle) desired = E.W < 640 ? 8 : 14;
    else desired = Math.min(E.pool.length, Math.round(E.max * Math.pow(E.intensity, 1.3) * (1 + (1 - E.zoom) * 1.9)));
    for (let i = 0; i < 8 && E.active < desired; i++) { const p = acquire(); if (!p) break; spawn(p); }

    // scan lines
    if (!idle) {
      if (now >= E.nextSweep && E.intensity > 0.12 && E.fade > 0.5) {
        const s = E.sweeps[E.sweeps[0].on ? 1 : 0];
        if (!s.on) { s.on = true; s.y = E.vt - 20; s.a = 1; }
        E.nextSweep = now + (E.t < 1500 ? 900 : 1300) + rnd(0, 400);
      }
      for (const s of E.sweeps) {
        if (!s.on) continue;
        s.y += (E.vb - E.vt) * 0.9 * dt;
        if (s.y > E.vb + 30) s.on = false;
      }
      if (now >= E.nextRandLink && E.intensity > 0.4) {
        E.nextRandLink = now + 700;
        let a = null, b = null;
        for (let i = 0; i < 8 && !(a && b); i++) {
          const p = E.pool[(Math.random() * E.pool.length) | 0];
          if (p.on && p.k >= HEX && p.k <= KV) { if (!a) a = p; else if (p !== a) b = p; }
        }
        if (a && b) addLink(a, b);
      }
    }

    // motion
    const coord = E.coord, spread = E.spread, cx = E.cx, cy = E.cy;
    for (let i = 0, n = E.pool.length; i < n; i++) {
      const p = E.pool[i];
      if (!p.on) continue;
      p.age += dt;
      if (p.age >= p.life) { release(p); continue; }
      if (p.conv > 0) {
        const dx = cx - p.x, dy = cy - p.y, d = Math.hypot(dx, dy) || 1;
        const k = p.conv * (1 - coord) * dt / d, damp = 1 - Math.min(0.9, 0.8 * dt);
        p.vx = (p.vx + dx * k) * damp; p.vy = (p.vy + dy * k) * damp;
        if (d < E.sr * 0.55) { E.glow = Math.min(1, E.glow + 0.05); release(p); continue; }
      }
      if (coord > 0) {
        const ri = Math.round(p.y / ROW), ty = ri * ROW, dirx = ri & 1 ? 1 : -1;
        const tvx = dirx * (30 + (p.seed & 3) * 14), tvy = (ty - p.y) * 2.5, b = Math.min(1, coord * 3 * dt);
        p.vx += (tvx - p.vx) * b; p.vy += (tvy - p.vy) * b;
      }
      if (spread > 0) {
        const dx = p.x - cx, dy = p.y - cy, d = Math.hypot(dx, dy) || 1;
        p.vx += dx / d * 70 * spread * dt; p.vy += dy / d * 70 * spread * dt;
      }
      p.x += p.vx * dt; p.y += p.vy * dt;
      if (p.x < E.vl - 40 || p.x > E.vr + 40 || p.y < E.vt - 40 || p.y > E.vb + 40) release(p);
    }
  }

  function draw(now) {
    const bx = E.bx, fx = E.fx, W = E.W, H = E.H, dpr = E.dpr, z = E.zoom, cx = E.cx, cy = E.cy, F = E.fade;
    const idle = E.mode === 'idle';
    for (const c of [bx, fx]) {
      c.setTransform(dpr, 0, 0, dpr, 0, 0); c.clearRect(0, 0, W, H);
      c.setTransform(dpr * z, 0, 0, dpr * z, dpr * cx * (1 - z), dpr * cy * (1 - z));
    }

    // soft halo behind the spiral (the spiral itself is never redrawn)
    const ha = (idle ? 0.035 + 0.025 * Math.sin(now / 1800) : 0.05 + 0.07 * E.glow + 0.03 * E.intensity) * F;
    if (ha > 0.001) {
      const g = bx.createRadialGradient(cx, cy, E.sr * 0.6, cx, cy, E.sr * 2.1);
      g.addColorStop(0, 'rgba(215,225,240,' + ha.toFixed(4) + ')');
      g.addColorStop(1, 'rgba(215,225,240,0)');
      bx.globalAlpha = 1; bx.fillStyle = g;
      bx.fillRect(cx - E.sr * 2.2, cy - E.sr * 2.2, E.sr * 4.4, E.sr * 4.4);
    }

    // faint structure the data settles into (phase 5)
    if (E.grid > 0.01) {
      bx.fillStyle = TINT[0];
      bx.globalAlpha = 0.05 * E.grid * F;
      for (let i = 1; i < 12; i++) bx.fillRect(i * W / 12, E.vt, 1 / z, E.vb - E.vt);
      bx.globalAlpha = 0.035 * E.grid * F;
      for (let y = Math.ceil(E.vt / (ROW * 4)) * ROW * 4; y < E.vb; y += ROW * 4) bx.fillRect(E.vl, y, E.vr - E.vl, 1 / z);
    }

    // links between related fragments (and label -> spiral)
    bx.lineWidth = 1 / z; bx.strokeStyle = TINT[1];
    for (const l of E.links) {
      if (!l.on) continue;
      const u = (now - l.t) / 1000 / l.ttl;
      if (u >= 1 || !l.a.on || l.a.seed !== l.sa || (l.b && (!l.b.on || l.b.seed !== l.sb))) { l.on = false; continue; }
      let ex, ey;
      if (l.b) { ex = l.b.x; ey = l.b.y; }
      else { const dx = l.a.x - cx, dy = l.a.y - cy, d = Math.hypot(dx, dy) || 1; ex = cx + dx / d * E.sr * 1.05; ey = cy + dy / d * E.sr * 1.05; }
      bx.globalAlpha = 0.16 * (1 - u) * (u < 0.15 ? u / 0.15 : 1) * F;
      bx.beginPath(); bx.moveTo(l.a.x, l.a.y); bx.lineTo(ex, ey); bx.stroke();
    }

    // particles
    for (let i = 0, n = E.pool.length; i < n; i++) {
      const p = E.pool[i];
      if (!p.on) continue;
      const age = p.age, left = p.life - age;
      let a = p.a * (age < 0.35 ? age / 0.35 : 1) * (left < 0.6 ? left / 0.6 : 1) * F;
      for (const s of E.sweeps) { if (s.on) { const dd = Math.abs(p.y - s.y); if (dd < 70) a += (1 - dd / 70) * 0.45 * s.a; } }
      if (a <= 0.01) continue;
      const L = p.layer ? fx : bx;
      L.globalAlpha = a > 1 ? 1 : a;
      L.fillStyle = TINT[p.tint];
      switch (p.k) {
        case PIXEL: L.fillRect(p.x, p.y, p.s, p.s); break;
        case LINE: L.fillRect(p.x, p.y, p.s, 1); break;
        case CLUSTER:
          for (let j = 0; j < 6; j++) L.fillRect(p.x + ((p.seed >> (j * 2)) & 3) * 3, p.y + ((p.seed >> (j * 2 + 1)) & 3) * 3, 2, 2);
          break;
        default:
          L.font = FONT[p.fs];
          L.fillText(p.text, p.x, p.y);
      }
      if (p.conv > 0 && E.coord < 0.5) { // short streak toward the spiral
        L.strokeStyle = TINT[p.tint]; L.lineWidth = 1 / z;
        L.beginPath(); L.moveTo(p.x, p.y); L.lineTo(p.x - p.vx * 0.12, p.y - p.vy * 0.12); L.stroke();
      }
    }

    // scan lines
    for (const s of E.sweeps) {
      if (!s.on) continue;
      fx.fillStyle = TINT[1];
      fx.globalAlpha = 0.045 * F; fx.fillRect(E.vl, s.y - 14, E.vr - E.vl, 28);
      fx.globalAlpha = 0.2 * F; fx.fillRect(E.vl, s.y, E.vr - E.vl, 1 / z);
    }

    // activation ripples
    fx.strokeStyle = TINT[0]; fx.lineWidth = 1 / z;
    for (const r of E.ripples) {
      if (r.t < 0 || now < r.t) continue;
      const u = (now - r.t) / 1100;
      if (u > 1) { r.t = -1; continue; }
      fx.globalAlpha = 0.35 * (1 - u) * F;
      fx.beginPath(); fx.arc(cx, cy, E.sr * (0.95 + 0.95 * easeOut(u)), 0, TAU); fx.stroke();
    }
    fx.globalAlpha = 1; bx.globalAlpha = 1;
  }

  /* ---------- page choreography ---------- */

  function setBodyState(on) {
    doc.body.classList.toggle('cinema-on', on);
  }

  function restoreEntrance() {
    const origin = $('origin'), spiral = $('collect'), loading = $('loading');
    if (origin) {
      if (origin.getAnimations) origin.getAnimations({ subtree: true }).forEach((a) => a.cancel());
      origin.removeAttribute('style');
    }
    if (spiral) spiral.classList.remove('activating');
    if (loading) loading.classList.remove('as-caption', 'caption-out');
    setBodyState(false);
  }

  function revealResultsNow() {
    const results = $('results');
    $('origin').classList.add('hidden');
    $('loading').classList.add('hidden');
    results.classList.remove('hidden');
  }

  function focusResults() {
    try {
      const h = $('results').querySelector('h2');
      if (h) { h.setAttribute('tabindex', '-1'); h.focus({ preventScroll: true }); }
    } catch (e) { /* focus is a nicety only */ }
  }

  const controller = {
    // Adds the two signals that need the page's own measurement. Labels only, never sent anywhere.
    feedRecord(rec) {
      try {
        if (!rec) return;
        E.sig.CANVAS = ['canvas: ' + (rec.canvasChecksum == null ? 'unavailable' : rec.canvasChecksum)];
        E.sig.WEBGL = ['webgl: ' + (rec.webglAvailable ? 'available' : 'unavailable')];
      } catch (e) { /* ignore */ }
    },

    async finish() {
      try {
        if (E.dead || !E.running) { shutdown(); restoreEntrance(); revealResultsNow(); focusResults(); return; }

        // let the storm reach its peak (3 s) before pulling back
        const remain = 3000 - (performance.now() - E.t0);
        if (remain > 0) await wait(remain);
        if (E.dead) { shutdown(); restoreEntrance(); revealResultsNow(); focusResults(); return; }

        const origin = $('origin'), spiral = $('collect'), loading = $('loading'), results = $('results');

        // Phase 4: zoom out and coordinate the field
        E.released = true;
        E.p4Start = performance.now();
        tween('zoom', 0.68, 1000);
        tween('coord', 1, 900, 150);
        loading.classList.add('caption-out');
        spiral.animate([{ transform: 'scale(1)' }, { transform: 'scale(.5)' }],
          { duration: 1000, easing: 'cubic-bezier(.6,0,.3,1)', fill: 'forwards' });
        await wait(1000);
        if (E.dead) { shutdown(); restoreEntrance(); revealResultsNow(); focusResults(); return; }

        // Phase 5: lift the entrance out of the flow (same place on screen), reveal the dashboard beneath it
        const r0 = origin.getBoundingClientRect();
        origin.style.cssText = 'position:fixed;margin:0;z-index:5;pointer-events:none;left:' + r0.left + 'px;top:' + r0.top +
          'px;width:' + r0.width + 'px;height:' + r0.height + 'px;';
        loading.classList.add('hidden');
        results.classList.add('emerging');
        results.classList.remove('hidden');
        window.scrollTo(0, 0);

        const art = results.querySelector('.result-art .spiral');
        const r1 = art ? art.getBoundingClientRect() : null;
        const tsize = r1 ? Math.min(r1.width, r1.height) : 0;
        if (tsize > 0) {
          const k = tsize / (2 * E.sr), dx = r1.left + r1.width / 2 - E.cx, dy = r1.top + r1.height / 2 - E.cy;
          spiral.animate([{ transform: 'scale(.5)' }, { transform: 'translate(' + dx + 'px,' + dy + 'px) scale(' + k + ')' }],
            { duration: 900, easing: 'cubic-bezier(.65,0,.25,1)', fill: 'forwards' });
        }
        origin.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 260, delay: 700, easing: 'ease-out', fill: 'forwards' });
        tween('grid', 1, 600);
        tween('fade', 0, 700, 300);
        await wait(1000);

        shutdown();
        restoreEntrance();
        origin.classList.add('hidden');
        loading.classList.add('hidden');
        focusResults();
        setTimeout(() => results.classList.remove('emerging'), 600);
      } catch (err) {
        if (window.console) console.warn('WHOOPS transition fallback:', err);
        shutdown(); restoreEntrance(); revealResultsNow(); focusResults();
        $('results').classList.remove('emerging');
      }
    },

    // Collection failed: calm the field and hand the entrance back.
    abort() {
      try {
        setBodyState(false);
        tween('fade', 0, 250);
        const spiral = $('collect'); if (spiral) spiral.classList.remove('activating');
        setTimeout(() => {
          shutdown(); restoreEntrance();
          if (!$('origin').classList.contains('hidden')) api.idle();
        }, 280);
      } catch (e) { shutdown(); restoreEntrance(); }
    }
  };

  const api = {
    supported: canRun,

    idle() {
      try {
        if (!canRun() || E.mode === 'storm') return;
        if (E.mode === 'idle') return;
        attach(); resetState(); E.mode = 'idle'; E.t0 = performance.now();
        buildSignals(); startLoop();
      } catch (e) { shutdown(); }
    },

    begin() {
      if (!canRun()) return null;
      try {
        $('results').classList.add('hidden');
        $('origin').classList.remove('hidden');
        window.scrollTo(0, 0);
        if (E.mode !== 'off') shutdown();
        attach(); resetState(); buildSignals();
        E.mode = 'storm'; E.t0 = performance.now(); E.nextSweep = E.t0 + 80; E.nextRandLink = E.t0 + 900;
        E.ripples[0].t = E.t0; E.ripples[1].t = E.t0 + 200;
        CATS.forEach((cat, i) => E.labels.push({ at: 520 + i * 240, cat, i }));
        E.glow = 0.35;

        const spiral = $('collect'), loading = $('loading');
        setBodyState(true);
        spiral.classList.remove('activating'); void spiral.offsetWidth; spiral.classList.add('activating');
        loading.classList.remove('hidden', 'caption-out'); loading.classList.add('as-caption');
        measure();
        startLoop();
        return controller;
      } catch (err) {
        if (window.console) console.warn('WHOOPS cinema unavailable:', err);
        shutdown(); restoreEntrance();
        return null;
      }
    }
  };

  window.WhoopsCinema = api;
  window.addEventListener('load', () => { E.dirty = true; }); // web fonts can shift the spiral's position

  // Quiet ambience on the entrance only; nothing is collected or sent.
  api.idle();
})();
