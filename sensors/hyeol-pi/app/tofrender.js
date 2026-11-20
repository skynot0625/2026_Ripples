// Shared renderer for every visual output (2026-10-15): the 시각화 작업실 preview (/), the device screens
// (/display/hyeol, /display/maek) and the blind outputs (/display/blind?space=a|b) draw with this one file, so a
// composition looks the same wherever it is assigned. Output differences live in the page's output profile only
// (sensor-state overlay, blind mapping, request pacing), never in a copy of this code.
// Inside: source → renderer (layer type) → layer → output. Users do not wire anything: each layer picks its input and
// its renderer. To add an input or a layer type, add one entry to SOURCES or RENDERERS below — nothing else changes.
//   RipplesRender.createTof(canvas)          ToF packet (server.build_packet) → canvas (the old index.html code, unchanged).
//   RipplesRender.createComposition(canvas)  one composition (ordered layers, visual_store.py) → canvas.
//   RipplesRender.SOURCES / RENDERERS / hub  the registries and the shared input poller.
//   RipplesRender.connectVisual(cb)          /ws/visual: compositions + the four output slots, send(op).
(() => {
  const MODES = ['sensor', 'contour', 'side', 'relief'];
  const SLAT_PITCH = 32 / 720, SLAT_GAP = 8 / 720;  // fraction of image height, same as server.py

  function parse(buf) {
    const v = new DataView(buf);
    const f = {
      mode: MODES[v.getUint8(4)], slats: Boolean(v.getUint8(5) & 1), vertical: Boolean(v.getUint8(5) & 2), nodata: Boolean(v.getUint8(5) & 4),
      w: v.getUint16(6, true), h: v.getUint16(8, true), id: v.getUint32(10, true), capture: v.getFloat64(14, true),
      center: v.getFloat32(22, true), near: v.getUint16(26, true), far: v.getUint16(28, true),
      line: v.getUint8(30), row: v.getUint8(31), gain: v.getUint16(32, true) / 100, interval: v.getUint16(34, true),
      base: v.getUint8(36), flow: v.getUint16(38, true) / 100,
    };
    if (f.mode === 'sensor' || f.mode === 'side') {
      f.depth = new Uint16Array(buf, 40, f.w * f.h);
      const end = 40 + 2 * f.w * f.h;   // 혈 움직임 추정 (server odometry.py), when the server sends it: "POSE" x y heading quality
      // trailers after the depth: "POSE" (혈 움직임 추정, 20 bytes), "EDGE" (적외선 윤곽: n, x[n], y[n], depth mm[n], server IrEdges)
      let o = end;
      while (buf.byteLength >= o + 8) {
        const tag = v.getUint32(o, false);
        if (tag === 0x504F5345 && buf.byteLength >= o + 20) {
          f.pose = {x: v.getFloat32(o + 4, true), y: v.getFloat32(o + 8, true), heading: v.getFloat32(o + 12, true), quality: v.getFloat32(o + 16, true)};
          o += 20;
        } else if (tag === 0x4544474C || tag === 0x49534F4C) {   // "EDGL" 적외선 윤곽 / "ISOL" 거리 등고선: strokes of points x·8, y·8, depth mm
          const ns = v.getUint32(o + 4, true), np = v.getUint32(o + 8, true), b0 = o + 12;
          if (buf.byteLength < b0 + 2 * ns + 6 * np) break;
          f[tag === 0x4544474C ? 'strokes' : 'isolines'] = {counts: new Uint16Array(buf, b0, ns), x: new Uint16Array(buf, b0 + 2 * ns, np), y: new Uint16Array(buf, b0 + 2 * ns + 2 * np, np), z: new Uint16Array(buf, b0 + 2 * ns + 4 * np, np)};
          o = b0 + 2 * ns + 6 * np;
        } else if (tag === 0x45444745) {
          const n = v.getUint32(o + 4, true);
          if (buf.byteLength < o + 8 + 6 * n) break;
          f.edges = {n, x: new Uint16Array(buf, o + 8, n), y: new Uint16Array(buf, o + 8 + 2 * n, n), z: new Uint16Array(buf, o + 8 + 4 * n, n)};
          o += 8 + 6 * n;
        } else break;
      }
    } else {
      const n = v.getUint32(40, true);
      f.counts = new Uint32Array(buf, 44, n);
      f.levels = new Uint16Array(buf, 44 + 4 * n, n);
      f.coords = new Uint16Array(buf, 44 + 6 * n);
    }
    return f;
  }

  function formatDistance(mm) {
    if (!Number.isFinite(mm)) return '-- mm';
    return mm < 1000 ? `${Math.round(mm)} mm` : `${(mm / 1000).toFixed(2)} m`;
  }

  function heightFrac(f, d) { return Math.min(1, Math.max(0, (f.far - d) / Math.max(1, f.far - f.near))); }

  // 화소 표현 (2026-11-02): how a layer drawn per sensor pixel (240×180) is enlarged. 'off' = each sensor pixel a
  // square (as before, and for saved layers without the setting). 'smooth' = the finished picture scaled by the browser
  // (bilinear: edges blur toward black). 'contour' = the DEPTH is interpolated to UPSCALE× first and the layer computes
  // its colours / bands / range at that size, so outlines follow the interpolated depth instead of the sensor grid.
  // Interpolation uses measured neighbours only (black where less than half of the neighbourhood was measured) and does
  // not blend across a depth step larger than STEP_MM (there it takes the nearest measured neighbour), so it never
  // makes a depth between a person and the wall behind them.
  const UPSCALE = 4, STEP_MM = 150;
  function upTables(w, h) {
    const axis = (n, N) => {
      const i0 = new Int32Array(N), i1 = new Int32Array(N), t = new Float32Array(N);
      for (let o = 0; o < N; o++) {
        const sx = Math.min(n - 1, Math.max(0, (o + 0.5) / UPSCALE - 0.5)), a = Math.floor(sx);
        i0[o] = a; i1[o] = Math.min(n - 1, a + 1); t[o] = sx - a;
      }
      return {i0, i1, t};
    };
    return {w, h, x: axis(w, w * UPSCALE), y: axis(h, h * UPSCALE)};
  }
  function depthUpsampler() {
    let tab = null, out = null;
    return f => {
      const w = f.w, h = f.h, W = w * UPSCALE, H = h * UPSCALE, dp = f.depth;
      if (!tab || tab.w !== w || tab.h !== h) { tab = upTables(w, h); out = new Uint16Array(W * H); }
      const X = tab.x, Y = tab.y;
      for (let oy = 0, q = 0; oy < H; oy++) {
        const r0 = Y.i0[oy] * w, r1 = Y.i1[oy] * w, fy = Y.t[oy];
        for (let ox = 0; ox < W; ox++, q++) {
          const fx = X.t[ox], da = dp[r0 + X.i0[ox]], db = dp[r0 + X.i1[ox]], dc = dp[r1 + X.i0[ox]], dd = dp[r1 + X.i1[ox]];
          const wa = da ? (1 - fx) * (1 - fy) : 0, wb = db ? fx * (1 - fy) : 0, wc = dc ? (1 - fx) * fy : 0, wd = dd ? fx * fy : 0;
          const cov = wa + wb + wc + wd;
          if (cov < 0.5) { out[q] = 0; continue; }
          let lo = 65535, hi = 0;
          if (da) { lo = Math.min(lo, da); hi = Math.max(hi, da); }
          if (db) { lo = Math.min(lo, db); hi = Math.max(hi, db); }
          if (dc) { lo = Math.min(lo, dc); hi = Math.max(hi, dc); }
          if (dd) { lo = Math.min(lo, dd); hi = Math.max(hi, dd); }
          if (hi - lo > STEP_MM) {   // a depth step: the nearest measured neighbour, no blend
            let best = wa, v = da;
            if (wb > best) { best = wb; v = db; }
            if (wc > best) { best = wc; v = dc; }
            if (wd > best) { best = wd; v = dd; }
            out[q] = v;
          } else out[q] = Math.round((wa * da + wb * db + wc * dc + wd * dd) / cov);
        }
      }
      return Object.assign({}, f, {w: W, h: H, depth: out});
    };
  }
  // the setting on the layer types drawn per sensor pixel; new layers start at 'contour', saved ones without it = 'off'
  const UPSCALE_PARAM = {key: 'upscale', label: '화소 표현', type: 'select', default: 'contour', legacy: 'off',
    options: [['contour', '매끈하게 (깊이 보간 후 그림)'], ['smooth', '부드럽게 (그림 번짐)'], ['off', '센서 칸 그대로']]};
  const smoothed = p => p && (p.upscale === 'smooth' || p.upscale === 'contour') ? p.upscale : null;

  // size(): the canvas's pixel size; default = its CSS size × devicePixelRatio (an on-screen canvas). Off-screen layer
  // canvases pass their own size; the drawing code itself is unchanged.
  function createTof(canvas, opts = {}) {
    const ctx = canvas.getContext('2d');
    const sizeOf = opts.size || (() => { const dpr = window.devicePixelRatio || 1; return [Math.round(canvas.clientWidth * dpr), Math.round(canvas.clientHeight * dpr)]; });
    const small = document.createElement('canvas');
    const smallCtx = small.getContext('2d');
    const state = {axisDrag: false};
    const upsample = depthUpsampler();

    function fit(w, h) {
      const dpr = window.devicePixelRatio || 1;
      const [cw, ch] = sizeOf();
      if (canvas.width !== cw || canvas.height !== ch) { canvas.width = cw; canvas.height = ch; }
      const s = Math.min(cw / w, ch / h);
      return {dpr, s, x: (cw - w * s) / 2, y: (ch - h * s) / 2, iw: w * s, ih: h * s};
    }

    // Depth 0 = no measurement (NaN, <= 0, beyond range or low confidence): always black, never a colour.
    function grayImage(f) {
      if (small.width !== f.w || small.height !== f.h) { small.width = f.w; small.height = f.h; }
      const img = smallCtx.createImageData(f.w, f.h);
      const px = img.data;
      for (let i = 0, j = 0; i < f.depth.length; i++, j += 4) {
        const d = f.depth[i];
        px[j] = px[j + 1] = px[j + 2] = d === 0 ? 0 : 255 * heightFrac(f, d);
        px[j + 3] = 255;
      }
      smallCtx.putImageData(img, 0, 0);
      return small;
    }

    function drawSlats(r) {
      ctx.fillStyle = '#000';
      const pitch = r.ih * SLAT_PITCH, gap = r.ih * SLAT_GAP;
      for (let y = r.y + pitch - gap; y < r.y + r.ih; y += pitch) ctx.fillRect(r.x, y, r.iw, gap);
    }

    function drawSensor(f, r, _inset, o) {
      ctx.imageSmoothingEnabled = false;
      const mode = smoothed(o);
      ctx.imageSmoothingEnabled = Boolean(mode);
      if (mode) ctx.imageSmoothingQuality = 'high';
      ctx.drawImage(grayImage(mode === 'contour' ? upsample(f) : f), r.x, r.y, r.iw, r.ih);
      ctx.imageSmoothingEnabled = false;
      const cx = Math.floor(f.w / 2) - 2, cy = Math.floor(f.h / 2) - 2;
      ctx.lineWidth = 3 * r.dpr; ctx.strokeStyle = '#000';
      ctx.strokeRect(r.x + cx * r.s, r.y + cy * r.s, 5 * r.s, 5 * r.s);
      ctx.lineWidth = r.dpr; ctx.strokeStyle = '#fff';
      ctx.strokeRect(r.x + cx * r.s, r.y + cy * r.s, 5 * r.s, 5 * r.s);
      // (2026-10-15) no centre-distance number drawn on the picture any more; the value itself (f.center) is unchanged
      // and the diagnostic status lines (센싱 모니터 / display "중앙 …") still show it
    }

    function drawContour(f, r) {
      const lw = f.line * r.dpr, inset = r.s * 0.5 + lw;
      ctx.save();
      ctx.beginPath(); ctx.rect(r.x + inset, r.y + inset, r.iw - 2 * inset, r.ih - 2 * inset); ctx.clip();
      ctx.strokeStyle = '#fff'; ctx.lineWidth = lw; ctx.lineJoin = 'round';
      ctx.beginPath();
      let p = 0;
      for (const n of f.counts) {
        for (let i = 0; i < n; i++, p += 2) {
          const x = r.x + (f.coords[p] + 0.5) * r.s, y = r.y + (f.coords[p + 1] + 0.5) * r.s;
          i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
        }
        ctx.closePath();
      }
      ctx.stroke();
      ctx.restore();
      if (f.slats) drawSlats(r);
    }

    // Scan line index: a row (horizontal line) or, with 세로 스캔, a column (vertical line).
    function sideLine(f) {
      const n = f.vertical ? f.w : f.h;
      return Math.min(n - 1, Math.round(f.row / 100 * (n - 1)));
    }

    // The live profile sits on the baseline. Every SIDE_STEP px of flow a snapshot is kept, and snapshots
    // slide away by f.flow px per frame — down by default; "흐르는 방향" (layer setting, 0° = up, clockwise) turns
    // the direction they move in (the profiles themselves, the baseline and the scan line stay as they are).
    // Later (older) ones hide the ones before them; the live profile is stroked last.
    const SIDE_STEP = 4, SIDE_MAX = 250;
    const sideHistory = [];
    let sideFlow = 0, sideFrame = null;

    function sideBaseY(base, r) { return r.y + r.ih * (1 - base / 100); }

    function drawProfile(prof, y0, r, scale, lw, fill) {
      const curve = new Path2D(), under = new Path2D();
      let start = -1;
      for (let x = 0; x <= prof.length; x++) {
        const v = x < prof.length ? prof[x] : NaN;
        if (Number.isNaN(v)) {
          if (start >= 0) {
            const dx = r.iw / prof.length, xs = r.x + (start + 0.5) * dx, xe = r.x + (x - 0.5) * dx;
            if (x - 1 === start) curve.rect(xs - lw / 2, y0 - prof[start] * scale - lw / 2, lw, lw);
            under.lineTo(xe, y0); under.lineTo(xs, y0); under.closePath();
            start = -1;
          }
          continue;
        }
        const px = r.x + (x + 0.5) * r.iw / prof.length, py = y0 - v * scale;
        if (start < 0) { start = x; curve.moveTo(px, py); under.moveTo(px, py); }
        else { curve.lineTo(px, py); under.lineTo(px, py); }
      }
      if (fill) ctx.fill(under);
      ctx.stroke(curve);
    }

    // opt.base: the layer's 기준선 (% from the bottom, its slider / number), opt.direction: flow direction in degrees,
    // opt.vanish: 소멸 기준선 — how far along the flow the snapshots disappear, in % of the way from the baseline to the
    // picture edge in that direction (100 = the picture edge = where they always disappeared)
    function drawSide(f, r, inset = true, opt = {}) {
      const line = sideLine(f), base = sideBaseY(opt.base ?? f.base, r), scale = r.ih * 0.5 * f.gain;
      const dir = opt.direction ?? 180, down = dir === 180;   // 180 = the original downward flow, exactly
      const ux = down ? 0 : Math.sin(dir * Math.PI / 180), uy = down ? 1 : -Math.cos(dir * Math.PI / 180);
      const lw = f.line * r.dpr, step = SIDE_STEP * r.dpr;
      const n = f.vertical ? f.h : f.w;
      const prof = new Float32Array(n);
      for (let i = 0; i < n; i++) {
        const d = f.depth[f.vertical ? i * f.w + line : line * f.w + i];
        prof[i] = d ? heightFrac(f, d) : NaN;
      }
      if (sideHistory.length && sideHistory[0].prof.length !== n) sideHistory.length = 0;
      // the flow advances once per new frame (a redraw of the same frame after a setting change does not)
      if (f.id !== sideFrame || !sideHistory.length) {
        sideFrame = f.id;
        sideFlow += f.flow;
        if (!sideHistory.length || sideFlow - sideHistory[0].at >= SIDE_STEP) sideHistory.unshift({prof, at: sideFlow});
      }
      const dOf = item => (sideFlow - item.at) * r.dpr;   // how far a snapshot has moved from the baseline
      // the vanishing line: perpendicular to the flow, at vanish % of the way from the baseline (its centre) to the
      // picture edge along the flow. At 100 % it is the picture edge: the clip rectangle and the removal below as before.
      const vanish = Math.min(100, Math.max(5, opt.vanish ?? 100)) / 100, cx = r.x + r.iw / 2;
      const toEdge = Math.min(ux > 1e-9 ? (r.x + r.iw - cx) / ux : ux < -1e-9 ? (cx - r.x) / -ux : Infinity,
                              uy > 1e-9 ? (r.y + r.ih - base) / uy : uy < -1e-9 ? (base - r.y) / -uy : Infinity);
      const vline = vanish < 1 ? vanish * toEdge : null;   // distance from the baseline centre along the flow
      const gone = d => (vline !== null ? d > vline + scale + step   // past the line (+ a profile's height): drawn nowhere any more
        : down ? base + d > r.y + r.ih + step
        : Math.abs(ux * d) >= r.iw + step || base + uy * d > r.y + r.ih + step || base + uy * d < r.y - scale - step);
      while (sideHistory.length > SIDE_MAX || (sideHistory.length && gone(dOf(sideHistory[sideHistory.length - 1])))) sideHistory.pop();
      ctx.save();
      ctx.beginPath(); ctx.rect(r.x, r.y, r.iw, r.ih); ctx.clip();
      ctx.fillStyle = '#000'; ctx.strokeStyle = '#fff'; ctx.lineWidth = lw; ctx.lineJoin = 'round';
      ctx.save();
      if (vline !== null) {   // keep only the side of the vanishing line the flow comes from
        const big = 4 * (r.iw + r.ih), lx = cx + ux * vline, ly = base + uy * vline, vx = -uy, vy = ux;
        ctx.beginPath();
        ctx.moveTo(lx + vx * big, ly + vy * big); ctx.lineTo(lx - vx * big, ly - vy * big);
        ctx.lineTo(lx - vx * big - ux * big, ly - vy * big - uy * big); ctx.lineTo(lx + vx * big - ux * big, ly + vy * big - uy * big);
        ctx.closePath(); ctx.clip();
      }
      // lower on screen = in front (as the original downward flow): flowing upward, the newer snapshots are the lower ones
      for (const item of uy < -1e-9 ? [...sideHistory].reverse() : sideHistory) {
        const d = dOf(item);
        drawProfile(item.prof, base + uy * d, ux ? Object.assign({}, r, {x: r.x + ux * d}) : r, scale, lw, true);
      }
      ctx.restore();
      ctx.fillStyle = state.axisDrag ? '#fff' : '#555';
      ctx.fillRect(r.x, base - r.dpr / 2, r.iw, r.dpr);
      drawProfile(prof, base, r, scale, lw, false);
      ctx.restore();
      // the ToF picture in the corner: drawn here only for SIDE layers made before 2026-10-15 evening (no "inset" setting);
      // newer SIDE layers show the flow only and get the ToF picture as their own linked layer ("ToF 원본 보기")
      if (!inset) return;

      const pw = r.iw * 0.3, ph = pw * f.h / f.w, m = 8 * r.dpr, px0 = r.x + m, py0 = r.y + m;
      ctx.fillStyle = '#000'; ctx.fillRect(px0 - 1, py0 - 1, pw + 2, ph + 2);
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(grayImage(f), px0, py0, pw, ph);
      ctx.lineWidth = r.dpr; ctx.strokeStyle = '#fff'; ctx.strokeRect(px0, py0, pw, ph);
      if (f.vertical) {
        const lx = px0 + (line + 0.5) * pw / f.w;
        ctx.fillStyle = '#000'; ctx.fillRect(lx - 1.5 * r.dpr, py0, 3 * r.dpr, ph);
        ctx.fillStyle = '#fff'; ctx.fillRect(lx - 0.5 * r.dpr, py0, r.dpr, ph);
      } else {
        const ly = py0 + (line + 0.5) * ph / f.h;
        ctx.fillStyle = '#000'; ctx.fillRect(px0, ly - 1.5 * r.dpr, pw, 3 * r.dpr);
        ctx.fillStyle = '#fff'; ctx.fillRect(px0, ly - 0.5 * r.dpr, pw, r.dpr);
      }
    }

    // Orthographic view tilted 45°: each contour level becomes a terrace at its own height, drawn
    // lowest first with a black-filled extrusion so higher terraces hide what is behind them.
    function reliefView(f) {
      const dpr = window.devicePixelRatio || 1, c = Math.SQRT1_2, top = f.h * 0.5 * f.gain;
      const [cw, ch] = sizeOf();
      if (canvas.width !== cw || canvas.height !== ch) { canvas.width = cw; canvas.height = ch; }
      const s = Math.min(cw / f.w, ch / ((f.h + top) * c));
      const iw = f.w * s, ih = (f.h + top) * c * s, x = (cw - iw) / 2, y = (ch - ih) / 2;
      return {dpr, s, c, top, x, y, iw, ih, sx: u => x + (u + 0.5) * s, sy: (v, z) => y + (top + v + 0.5 - z) * c * s};
    }

    function drawRelief(f, r) {
      const zOf = d => heightFrac(f, d) * r.top;
      const starts = [];
      for (let i = 0, p = 0; i < f.counts.length; p += 2 * f.counts[i++]) starts.push(p);
      const order = [...f.counts.keys()].sort((a, b) => f.levels[b] - f.levels[a]);
      const levels = [...new Set(order.map(i => f.levels[i]))];
      ctx.lineWidth = f.line * r.dpr; ctx.lineCap = ctx.lineJoin = 'round'; ctx.strokeStyle = '#fff'; ctx.fillStyle = '#000';
      const W = f.w - 1, H = f.h - 1;
      let k = 0;
      levels.forEach((level, li) => {
        const z = zOf(level), zLow = li ? zOf(levels[li - 1]) : 0;
        const topPath = new Path2D(), bottom = new Path2D(), walls = new Path2D(), edges = new Path2D();
        for (; k < order.length && f.levels[order[k]] === level; k++) {
          const i = order[k], n = f.counts[i], p0 = starts[i];
          for (let j = 0; j < n; j++) {
            const au = f.coords[p0 + 2 * j], av = f.coords[p0 + 2 * j + 1], ax = r.sx(au);
            const b = j + 1 < n ? j + 1 : 0, bu = f.coords[p0 + 2 * b], bv = f.coords[p0 + 2 * b + 1], bx = r.sx(bu);
            j ? topPath.lineTo(ax, r.sy(av, z)) : topPath.moveTo(ax, r.sy(av, z));
            j ? bottom.lineTo(ax, r.sy(av, zLow)) : bottom.moveTo(ax, r.sy(av, zLow));
            const onFrame = (au === bu && (au === 0 || au === W)) || (av === bv && (av === 0 || av === H));
            if (!onFrame) { edges.moveTo(ax, r.sy(av, z)); edges.lineTo(bx, r.sy(bv, z)); }
            if (ax === bx || z === zLow) continue;
            const [lx, lv, rx, rv] = ax < bx ? [ax, av, bx, bv] : [bx, bv, ax, av];
            walls.moveTo(lx, r.sy(lv, z)); walls.lineTo(rx, r.sy(rv, z));
            walls.lineTo(rx, r.sy(rv, zLow)); walls.lineTo(lx, r.sy(lv, zLow)); walls.closePath();
          }
          topPath.closePath(); bottom.closePath();
        }
        ctx.fill(walls); ctx.fill(bottom, 'evenodd'); ctx.fill(topPath, 'evenodd');
        ctx.stroke(edges);
      });
      if (f.slats) drawSlats(r);
    }

    // One packet → the canvas. Returns {f, r} (the parsed frame and the drawn image rect).
    function draw(buf, layer) {
      const f = parse(buf);
      const r = f.mode === 'relief' ? reliefView(f) : fit(f.w, f.h);
      ctx.fillStyle = '#000'; ctx.fillRect(0, 0, canvas.width, canvas.height);
      if (f.mode !== 'side') sideHistory.length = 0;
      const p = (layer && layer.params) || {};
      ({sensor: drawSensor, contour: drawContour, side: drawSide, relief: drawRelief})[f.mode](f, r, p.inset !== false, {base: p.base, direction: p.direction, vanish: p.vanish, upscale: p.upscale});
      return {f, r};
    }
    function clear() {
      sideHistory.length = 0; sideFrame = null;
      const [cw, ch] = sizeOf();
      if (canvas.width !== cw || canvas.height !== ch) { canvas.width = cw; canvas.height = ch; }
      ctx.fillStyle = '#000'; ctx.fillRect(0, 0, canvas.width, canvas.height);
    }
    return {draw, clear, sideBaseY, state, kind: 'tof'};
  }

  // ============================================================================================================
  // EXPERIMENT MODES (2026-10-15): SILHOUETTE / DEPTH BANDS / POINTS / MOTION draw the per-pixel depth the server
  // already streams for SIDE (temporal + median filtered, 0 = no measurement, cut at "far"), so the sensor format and
  // the API stay as they are. No measurement is always black. They are layer types only: no built-in composition and
  // no output uses them unless someone picks them in the 작업실.
  const ART_DEFAULTS = {
    silhouette: {near_t: 500, far_t: 2500, invert: false},
    bands: {bands: 6, color_near: '#ffffff', color_far: '#333333', edges: false},
    points: {density: 2, size: 1.5, size_by_distance: true, alpha_by_distance: true},
    motion: {sensitivity: 40, decay: 0.6},
    numbers: {cell: 10, unit: 'cm', shade: true, grid: false},
  };
  const hexRgb = h => [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)];
  function createDepthArt(canvas, kind, opts = {}) {
    const ctx = canvas.getContext('2d');
    const sizeOf = opts.size || (() => { const dpr = window.devicePixelRatio || 1; return [Math.round(canvas.clientWidth * dpr), Math.round(canvas.clientHeight * dpr)]; });
    const small = document.createElement('canvas'), smallCtx = small.getContext('2d');
    let prev = null, energy = null, lastAt = 0;   // MOTION: the previous depth frame and how lit each pixel still is
    const upsample = depthUpsampler();
    let upscale = null;   // this layer's 화소 표현 (set in draw)
    function fit(w, h) {
      const [cw, ch] = sizeOf();
      if (canvas.width !== cw || canvas.height !== ch) { canvas.width = cw; canvas.height = ch; }
      const s = Math.min(cw / w, ch / h);
      return {s, x: (cw - w * s) / 2, y: (ch - h * s) / 2, iw: w * s, ih: h * s};
    }
    // per-pixel image at sensor resolution (240×180) → the fitted rect, without smoothing (like SENSOR)
    function pixels(f, r, colour) {
      if (small.width !== f.w || small.height !== f.h) { small.width = f.w; small.height = f.h; }
      const img = smallCtx.createImageData(f.w, f.h), px = img.data;
      for (let i = 0, j = 0; i < f.depth.length; i++, j += 4) {
        const c = f.depth[i] ? colour(i, f.depth[i]) : null;   // no measurement → black, whatever the settings
        if (c) { px[j] = c[0]; px[j + 1] = c[1]; px[j + 2] = c[2]; }
        px[j + 3] = 255;
      }
      smallCtx.putImageData(img, 0, 0);
      ctx.imageSmoothingEnabled = Boolean(upscale);
      if (upscale) ctx.imageSmoothingQuality = 'high';
      ctx.drawImage(small, r.x, r.y, r.iw, r.ih);
      ctx.imageSmoothingEnabled = false;
    }
    const WHITE = [255, 255, 255];
    const draws = {
      // filled shape of what lies between near_t and far_t (invert: the measured pixels outside that range)
      silhouette(f, r, p) {
        const lo = Math.min(p.near_t, p.far_t), hi = Math.max(p.near_t, p.far_t);
        pixels(f, r, (i, d) => ((d >= lo && d <= hi) !== Boolean(p.invert) ? WHITE : null));
      },
      // distance in discrete bands, coloured from far (band 1) to near (last band); edges = black line between bands
      bands(f, r, p) {
        const n = Math.max(2, Math.round(p.bands)), cn = hexRgb(p.color_near), cf = hexRgb(p.color_far);
        const colours = Array.from({length: n}, (_, b) => cf.map((v, k) => Math.round(v + (cn[k] - v) * b / (n - 1))));
        const band = d => Math.min(n - 1, Math.floor(heightFrac(f, d) * n));
        pixels(f, r, (i, d) => {
          const b = band(d);
          if (p.edges) {
            const x = i % f.w, right = x + 1 < f.w ? f.depth[i + 1] : 0, down = i + f.w < f.depth.length ? f.depth[i + f.w] : 0;
            if ((right && band(right) !== b) || (down && band(down) !== b)) return null;
          }
          return colours[b];
        });
      },
      // every density-th pixel as a square dot at its image position; nearer = bigger / more opaque (options)
      points(f, r, p) {
        const step = Math.max(1, Math.round(p.density)), BUCKETS = 8;
        const paths = Array.from({length: BUCKETS + 1}, () => new Path2D());
        for (let y = 0; y < f.h; y += step) {
          for (let x = 0; x < f.w; x += step) {
            const d = f.depth[y * f.w + x];
            if (!d) continue;
            const t = heightFrac(f, d);
            if (t <= 0) continue;
            const s = p.size * r.s * (p.size_by_distance ? 0.35 + 0.65 * t : 1);
            const a = p.alpha_by_distance ? 0.2 + 0.8 * t : 1;
            paths[Math.round(a * BUCKETS)].rect(r.x + (x + 0.5) * r.s - s / 2, r.y + (y + 0.5) * r.s - s / 2, s, s);
          }
        }
        ctx.fillStyle = '#fff';
        paths.forEach((path, b) => { if (b) { ctx.globalAlpha = b / BUCKETS; ctx.fill(path); } });
        ctx.globalAlpha = 1;
      },
      // only where the depth changed by more than "sensitivity" mm since the previous frame (or a measurement
      // appeared); a lit pixel fades linearly to black within "decay" seconds. Pixels with no measurement now stay black.
      motion(f, r, p) {
        const n = f.w * f.h, now = performance.now();
        if (!prev || prev.length !== n) { prev = new Uint16Array(n); energy = new Float32Array(n); prev.set(f.depth); lastAt = now; }
        const fade = p.decay > 0 ? (now - lastAt) / 1000 / p.decay : 1;
        lastAt = now;
        for (let i = 0; i < n; i++) {
          const d = f.depth[i], o = prev[i];
          energy[i] = d && (!o || Math.abs(d - o) > p.sensitivity) ? 1 : Math.max(0, energy[i] - fade);
        }
        prev.set(f.depth);
        pixels(f, r, i => { const v = Math.round(255 * energy[i]); return v ? [v, v, v] : null; });
      },
      // NUMBERS (2026-11-02): the depth of one sensor pixel per cell (the centre pixel of every cell × cell block), as
      // text in mm / cm / m. No measurement = no number. shade: nearer = brighter text (same scale as SENSOR).
      numbers(f, r, p) {
        const step = Math.max(2, Math.round(p.cell)), cw = step * r.s;
        const fmt = p.unit === 'mm' ? d => String(d) : p.unit === 'm' ? d => (d / 1000).toFixed(2) : d => String(Math.round(d / 10));
        const chars = p.unit === 'cm' ? 3 : 4;   // up to 4000 mm: "4000" / "400" / "4.00"
        const size = Math.min(cw * 0.5, cw / (chars * 0.72));   // a gap between neighbouring numbers
        if (p.grid) {
          ctx.strokeStyle = '#222'; ctx.lineWidth = 1; ctx.beginPath();
          for (let x = 0; x <= f.w; x += step) { const X = Math.round(r.x + x * r.s) + 0.5; ctx.moveTo(X, r.y); ctx.lineTo(X, r.y + r.ih); }
          for (let y = 0; y <= f.h; y += step) { const Y = Math.round(r.y + y * r.s) + 0.5; ctx.moveTo(r.x, Y); ctx.lineTo(r.x + r.iw, Y); }
          ctx.stroke();
        }
        if (size < 4) return;   // too small to read on this canvas
        ctx.font = `${size.toFixed(1)}px ui-monospace, Menlo, Consolas, monospace`;
        ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        for (let y = 0; y < f.h; y += step) {
          for (let x = 0; x < f.w; x += step) {
            const cx = Math.min(f.w - 1, x + (step >> 1)), cy = Math.min(f.h - 1, y + (step >> 1)), d = f.depth[cy * f.w + cx];
            if (!d) continue;
            const v = p.shade ? Math.round(70 + 185 * heightFrac(f, d)) : 255;
            ctx.fillStyle = `rgb(${v},${v},${v})`;
            ctx.fillText(fmt(d), r.x + (x + step / 2) * r.s, r.y + (y + step / 2) * r.s);
          }
        }
      },
    };
    function draw(buf, layer) {
      const f = parse(buf), r = fit(f.w, f.h);
      ctx.fillStyle = '#000'; ctx.fillRect(0, 0, canvas.width, canvas.height);
      upscale = smoothed(layer && layer.params);   // saved layers without the setting: as before
      // 'contour': the layer is computed on the interpolated depth (r stays the sensor-sized rect on screen)
      if (f.depth) draws[kind](upscale === 'contour' && kind !== 'points' && kind !== 'numbers' ? upsample(f) : f, r, Object.assign({}, ART_DEFAULTS[kind], layer && layer.params));
      return {f, r};
    }
    function clear() {
      const [cw, ch] = sizeOf();
      if (canvas.width !== cw || canvas.height !== ch) { canvas.width = cw; canvas.height = ch; }
      ctx.fillStyle = '#000'; ctx.fillRect(0, 0, canvas.width, canvas.height);
    }
    return {draw, clear, reset() { prev = null; energy = null; }, kind: 'art'};
  }

  // ============================================================================================================
  // VISUAL EXPERIMENTS (2026-10-15) — per ToF layer, all off by default; off = the layer is drawn exactly as before
  // (no extra canvas, no extra work). One fixed order: renderer → colour mapping / steps / pixelation → blur / glow →
  // symmetry → afterimage → the layer's transform / blend. Sizes are in sensor pixels (1 = one of the 240×180 pixels
  // on screen), so a setting looks the same on a phone and on a projector. visual_store.py checks the same ranges.
  const EFFECTS = {
    color: {label: '거리별 색상 매핑', note: '밝기(가까움)를 색으로 · 측정값 없음은 항상 검정 · 기본 색 = 지금 흑백 그대로',
      params: [{key: 'near', label: '가까움', type: 'color', default: '#ffffff'}, {key: 'mid', label: '중간', type: 'color', default: '#808080'},
               {key: 'far', label: '멀어짐', type: 'color', default: '#000000'},
               {key: 'midpoint', label: '중간 지점', min: 0.05, max: 0.95, step: 0.05, default: 0.5},
               {key: 'invert', label: '반전', type: 'bool', default: false}]},
    steps: {label: '거리 단계화', params: [{key: 'levels', label: '단계 수', min: 2, max: 32, step: 1, default: 6}]},
    pixelate: {label: '픽셀화', params: [{key: 'block', label: '블록 크기', min: 2, max: 24, step: 1, unit: ' px', default: 4}]},
    blur: {label: '블러 / 글로우', params: [{key: 'kind', label: '방식', type: 'select', options: [['glow', '글로우 (더하기)'], ['blur', '블러 (번짐)']], default: 'glow'},
                                         {key: 'radius', label: '번짐 반경', min: 1, max: 24, step: 1, unit: ' px', default: 4},
                                         {key: 'strength', label: '강도', min: 0.1, max: 2, step: 0.1, default: 1}]},
    mirror: {label: '대칭', always: true, params: [{key: 'mode', label: '대칭', type: 'select',
      options: [['none', '없음'], ['lr', '좌우'], ['tb', '상하'], ['quad', '4방향']], default: 'none'}]},
    trail: {label: '잔상', note: '지속시간 안에 완전히 사라짐 · 입력이 끊겨도 남지 않음 · 잔상 간격 0 = 매 화면 프레임(연속)',
      params: [{key: 'amount', label: '잔상 양', min: 0.05, max: 1, step: 0.05, default: 0.7},
               {key: 'duration', label: '지속시간', min: 0.1, max: 5, step: 0.1, unit: ' s', default: 1},
               // time between kept samples (0 = every display frame, the original continuous trail)
               {key: 'interval', label: '잔상 간격', min: 0, max: 1, step: 0.02, unit: ' s', default: 0}]},
  };
  function fxDefaults() {
    const out = {};
    for (const [id, e] of Object.entries(EFFECTS)) {
      out[id] = e.always ? {} : {on: false};
      for (const p of e.params) out[id][p.key] = p.default;
    }
    return out;
  }
  const FX_DEFAULTS = fxDefaults();
  function fxOf(layer) {
    const e = layer.effects || {}, out = {};
    for (const id of Object.keys(FX_DEFAULTS)) out[id] = Object.assign({}, FX_DEFAULTS[id], e[id]);
    return out;
  }
  const fxStatic = e => e.color.on || e.steps.on || e.pixelate.on || e.blur.on || e.mirror.mode !== 'none';
  const fxRenderer = R => Boolean(R && (R.tofMode || R.fx));   // ToF layers + the types that opt in (PULSE)
  const fxActive = layer => Boolean(layer.effects) && fxRenderer(RENDERERS[layer.renderer]) && (fxStatic(fxOf(layer)) || fxOf(layer).trail.on);
  const canFilter = typeof CanvasRenderingContext2D !== 'undefined' && 'filter' in CanvasRenderingContext2D.prototype;

  // 색 매핑 on the GPU (2026-11-10, Pi: the per-pixel loop below took ~85 % of the CPU on a 1766×1280 공간선 layer).
  // One shared WebGL canvas: source canvas → texture, brightness v = max(r, g, b) → the same 256-entry colour table as
  // mapPixels (v = 0 stays as it is), alpha kept. Same 8-bit results; null = not available (the CPU loop is used).
  const gpuMap = (() => {
    let gl = null, failed = false, prog, srcTex, lutTex, lutKey = null;
    function init() {
      const c = document.createElement('canvas');
      gl = c.getContext('webgl', {premultipliedAlpha: false, preserveDrawingBuffer: true, antialias: false, depth: false});
      if (!gl) throw new Error('no webgl');
      const sh = (type, src) => { const x = gl.createShader(type); gl.shaderSource(x, src); gl.compileShader(x); if (!gl.getShaderParameter(x, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(x)); return x; };
      prog = gl.createProgram();
      gl.attachShader(prog, sh(gl.VERTEX_SHADER, 'attribute vec2 p; varying vec2 uv; void main() { uv = p * 0.5 + 0.5; gl_Position = vec4(p, 0.0, 1.0); }'));
      gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, `precision mediump float; uniform sampler2D src, lut; varying vec2 uv;
        void main() { vec4 c = texture2D(src, uv); float v = floor(max(c.r, max(c.g, c.b)) * 255.0 + 0.5);
          gl_FragColor = v < 0.5 ? c : vec4(texture2D(lut, vec2((v + 0.5) / 256.0, 0.5)).rgb, c.a); }`));
      gl.linkProgram(prog);
      if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog));
      gl.useProgram(prog);
      const buf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buf);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
      const loc = gl.getAttribLocation(prog, 'p'); gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
      const tex = unit => { const t = gl.createTexture(); gl.activeTexture(gl.TEXTURE0 + unit); gl.bindTexture(gl.TEXTURE_2D, t);
        for (const [k, v] of [[gl.TEXTURE_MIN_FILTER, gl.NEAREST], [gl.TEXTURE_MAG_FILTER, gl.NEAREST], [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE]]) gl.texParameteri(gl.TEXTURE_2D, k, v);
        return t; };
      srcTex = tex(0); lutTex = tex(1);
      gl.uniform1i(gl.getUniformLocation(prog, 'src'), 0); gl.uniform1i(gl.getUniformLocation(prog, 'lut'), 1);
      gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);   // canvas rows top-down → texture bottom-up, so uv matches the output
    }
    return function map(srcCanvas, lut) {   // → the WebGL canvas holding the mapped picture, or null
      if (failed) return null;
      try {
        if (!gl) init();
        if (gl.isContextLost()) { failed = true; return null; }
        const c = gl.canvas, w = srcCanvas.width, h = srcCanvas.height;
        if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
        gl.viewport(0, 0, w, h);
        if (lutKey !== lut) {
          const data = new Uint8Array(256 * 4);
          for (let v = 0; v < 256; v++) { data[v * 4] = lut.rgb[v * 3]; data[v * 4 + 1] = lut.rgb[v * 3 + 1]; data[v * 4 + 2] = lut.rgb[v * 3 + 2]; data[v * 4 + 3] = 255; }
          gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, lutTex);
          gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
          gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 256, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, data);
          gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
          lutKey = lut;
        }
        gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, srcTex);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, srcCanvas);
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
        return c;
      } catch (err) {
        failed = true; console.warn('색 매핑 GPU 사용 불가, CPU로 처리', err); return null;
      }
    };
  })();

  // one layer's effect canvases and state (created only when an effect is on)
  function createFx() {
    const cv = {}, ctxs = {};
    let lut = null, lutKey = '', acc = null, accAt = 0, carry = 0, stampAt = -Infinity;
    function canvasOf(name, w, h, read) {
      let c = cv[name];
      if (!c) { c = cv[name] = document.createElement('canvas'); ctxs[name] = c.getContext('2d', read ? {willReadFrequently: true} : undefined); }
      if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
      return [c, ctxs[name]];
    }
    // brightness v = max(r, g, b) (0 = nothing / no measurement, stays black) → steps → colour (far → mid → near)
    function buildLut(e) {
      const key = JSON.stringify([e.color, e.steps.on && e.steps.levels]);
      if (key === lutKey) return lut;
      const n = e.steps.on ? e.steps.levels : 0, m = e.color.midpoint * 255;
      const [far, mid, near] = [e.color.far, e.color.mid, e.color.near].map(hexRgb);
      const rgb = new Uint8ClampedArray(256 * 3), scale = new Float32Array(256);
      for (let v = 1; v < 256; v++) {
        const q = n ? Math.ceil(v * n / 255) * 255 / n : v;
        scale[v] = q / v;
        const u = e.color.invert ? 255 - q : q;
        for (let k = 0; k < 3; k++) {
          const a = u <= m ? far[k] + (mid[k] - far[k]) * (u / m) : mid[k] + (near[k] - mid[k]) * ((u - m) / (255 - m));
          rgb[v * 3 + k] = Math.floor(a + 1e-6);   // floor: the default colours give back every grey level exactly
        }
      }
      lutKey = key; lut = {rgb, scale, colour: e.color.on};
      return lut;
    }
    function mapPixels(c, x) {
      const t = buildLut(x.e), w = c.width, h = c.height, img = x.ctx.getImageData(0, 0, w, h), px = img.data;
      for (let j = 0; j < px.length; j += 4) {
        const r = px[j], g = px[j + 1], b = px[j + 2], v = r > g ? (r > b ? r : b) : (g > b ? g : b);
        if (!v) continue;
        if (t.colour) { px[j] = t.rgb[v * 3]; px[j + 1] = t.rgb[v * 3 + 1]; px[j + 2] = t.rgb[v * 3 + 2]; }
        else { const s = t.scale[v]; px[j] = r * s; px[j + 1] = g * s; px[j + 2] = b * s; }
      }
      x.ctx.putImageData(img, 0, 0);
    }
    // src: the layer canvas as the renderer drew it; unit: size of one sensor pixel on it. Returns the processed canvas.
    function still(src, e, unit) {
      const w = src.width, h = src.height;
      let cur = src;
      if (e.color.on || e.steps.on || e.pixelate.on) {
        if (e.pixelate.on) {
          const block = Math.max(1, e.pixelate.block * unit), sw = Math.max(1, Math.round(w / block)), sh = Math.max(1, Math.round(h / block));
          const [s, sctx] = canvasOf('small', sw, sh, true);
          sctx.imageSmoothingEnabled = true; sctx.imageSmoothingQuality = 'high';
          sctx.fillStyle = '#000'; sctx.fillRect(0, 0, sw, sh);
          sctx.drawImage(cur, 0, 0, sw, sh);
          if (e.color.on || e.steps.on) mapPixels(s, {e, ctx: sctx});
          const [o, octx] = canvasOf('pix', w, h);
          octx.imageSmoothingEnabled = false;
          octx.drawImage(s, 0, 0, w, h);
          cur = o;
        } else {
          const t = e.color.on ? buildLut(e) : null, g = t && t.colour && w * h > 65536 && !window.RIPPLES_FX_CPU ? gpuMap(cur, t) : null;
          if (g) {   // the GPU did it: copy into this layer's own canvas (the WebGL canvas is shared by all layers)
            const [o, octx] = canvasOf('map', w, h);
            octx.clearRect(0, 0, w, h); octx.drawImage(g, 0, 0);
            cur = o;
          } else {
            const [o, octx] = canvasOf('mapCpu', w, h, true);
            octx.drawImage(cur, 0, 0);
            mapPixels(o, {e, ctx: octx});
            cur = o;
          }
        }
      }
      if (e.blur.on) {
        const px = Math.max(0.5, e.blur.radius * unit);
        const [b, bctx] = canvasOf('blur', w, h);
        bctx.clearRect(0, 0, w, h);
        if (canFilter) { bctx.filter = `blur(${px}px)`; bctx.drawImage(cur, 0, 0); bctx.filter = 'none'; }
        else {   // no canvas filter (older Safari): shrink and enlarge with smoothing (same idea, softer edges)
          const k = Math.max(1, px / 2), [s, sctx] = canvasOf('blurSmall', Math.max(1, Math.round(w / k)), Math.max(1, Math.round(h / k)));
          sctx.imageSmoothingEnabled = true; sctx.imageSmoothingQuality = 'high';
          sctx.clearRect(0, 0, s.width, s.height); sctx.drawImage(cur, 0, 0, s.width, s.height);
          bctx.imageSmoothingEnabled = true; bctx.drawImage(s, 0, 0, w, h);
        }
        const [o, octx] = canvasOf('blurOut', w, h);
        octx.globalCompositeOperation = 'source-over'; octx.globalAlpha = 1;
        octx.drawImage(cur, 0, 0);
        if (e.blur.kind === 'blur') { octx.globalAlpha = Math.min(1, e.blur.strength); octx.drawImage(b, 0, 0); }
        else {
          octx.globalCompositeOperation = 'lighter';
          for (let left = e.blur.strength; left > 0.001; left -= 1) { octx.globalAlpha = Math.min(1, left); octx.drawImage(b, 0, 0); }
        }
        octx.globalCompositeOperation = 'source-over'; octx.globalAlpha = 1;
        cur = o;
      }
      if (e.mirror.mode !== 'none') {
        const flip = (name, source, horizontal) => {
          const [o, octx] = canvasOf(name, w, h);
          octx.drawImage(source, 0, 0);
          octx.save();
          if (horizontal) { const hw = Math.floor(w / 2); octx.translate(w, 0); octx.scale(-1, 1); octx.drawImage(source, 0, 0, hw, h, 0, 0, hw, h); }
          else { const hh = Math.floor(h / 2); octx.translate(0, h); octx.scale(1, -1); octx.drawImage(source, 0, 0, w, hh, 0, 0, w, hh); }
          octx.restore();
          return o;
        };
        if (e.mirror.mode === 'lr' || e.mirror.mode === 'quad') cur = flip('mirrorX', cur, true);
        if (e.mirror.mode === 'tb' || e.mirror.mode === 'quad') cur = flip('mirrorY', cur, false);
      }
      return cur;
    }
    // afterimage, every display frame: acc = max(acc − fade, current); shown = current + amount × (acc − current).
    // The fade subtracts a fixed amount (255 per "duration"), so a trail always reaches black within the duration —
    // also when frames stop coming. Subtraction on the GPU: invert, add, invert (exact, clamped at 0).
    // "잔상 간격" (interval, s): the current picture goes into acc only every interval seconds (0 = every display frame,
    // a continuous trail as before), so larger values leave separate, spaced-out echoes; the fade is unchanged.
    function trail(src, e, now) {
      const w = src.width, h = src.height;
      const [a, actx] = canvasOf('acc', w, h);
      if (!acc || acc.w !== w || acc.h !== h) { actx.globalCompositeOperation = 'source-over'; actx.globalAlpha = 1; actx.fillStyle = '#000'; actx.fillRect(0, 0, w, h); acc = {w, h}; accAt = now; carry = 0; }
      carry += 255 * Math.max(0, now - accAt) / (e.trail.duration * 1000);
      accAt = now;
      const fade = Math.floor(carry);
      carry -= fade;
      if (fade >= 255) { actx.globalCompositeOperation = 'source-over'; actx.fillStyle = '#000'; actx.fillRect(0, 0, w, h); }
      else if (fade > 0) {
        actx.globalCompositeOperation = 'difference'; actx.fillStyle = '#fff'; actx.fillRect(0, 0, w, h);
        actx.globalCompositeOperation = 'lighter'; actx.fillStyle = `rgb(${fade},${fade},${fade})`; actx.fillRect(0, 0, w, h);
        actx.globalCompositeOperation = 'difference'; actx.fillStyle = '#fff'; actx.fillRect(0, 0, w, h);
      }
      const every = (e.trail.interval || 0) * 1000;
      if (!every || now - stampAt >= every) {
        actx.globalCompositeOperation = 'lighten'; actx.drawImage(src, 0, 0);
        stampAt = every && now - stampAt < 2 * every ? stampAt + every : now;   // steady spacing, no drift
      }
      actx.globalCompositeOperation = 'source-over';
      const [o, octx] = canvasOf('trailOut', w, h);
      octx.globalCompositeOperation = 'source-over'; octx.globalAlpha = 1; octx.drawImage(src, 0, 0);
      octx.globalCompositeOperation = 'lighten'; octx.globalAlpha = e.trail.amount; octx.drawImage(a, 0, 0);
      octx.globalCompositeOperation = 'source-over'; octx.globalAlpha = 1;
      return o;
    }
    return {still, trail, resetTrail() { acc = null; carry = 0; stampAt = -Infinity; }, get trailing() { return Boolean(acc); }};
  }

  // ============================================================================================================
  // INPUT SOURCES — the one registry of what layers can read. Only values the server really has (/inputs,
  // /api/simulator); inputs that do not exist yet are listed as not connected, with no value. A new source adapter is
  // one entry here: the 작업실 (input list, source pickers, links) and every output pick it up.
  //   kind: 'frame' (ToF depth stream) | 'number'; read(inputs, sim) → {connected, value, rate_hz, mode, seq}
  const ch = name => (d) => {
    const c = d && d.channels && d.channels[name];
    if (!c) return {connected: false, value: null, rate_hz: null, mode: 'none'};
    return {connected: c.connected, value: typeof c.value === 'number' ? c.value : null, rate_hz: c.rate_hz, seq: c.seq, age_s: c.age_s, paused: c.paused,
            mode: c.source === 'mock' ? 'mock' : c.source === 'none' ? '수신기 없음' : 'live', unit: c.unit};
  };
  const pose = (dev, key) => (d, sim) => {
    const p = sim && sim.devices && sim.devices[dev];
    return {connected: Boolean(p), value: p ? p[key] : null, rate_hz: null, mode: 'simulation', seq: sim && sim.revision};
  };
  const SOURCES = {
    'tof.depth': {label: 'ToF depth (240×180, 0.2–4 m)', kind: 'frame', read: d => ({connected: Boolean(d && d.tof && d.tof.connected), value: null,
      rate_hz: d && d.tof ? d.tof.fps : null, mode: d ? (d.mock ? 'mock' : 'live') : 'none'})},
    // the ceiling-facing single-point distance sensor (/inputs channels.distance; mock: the 혈 pose straight up to the
    // venue ceiling, no value when the ceiling is unknown). One distance over time, not an image.
    'sensor.up_distance': {label: '천장 거리 센서 (up_distance, 단일점 · 최대 약 20 m)', kind: 'number', unit: 'm', range: [0, 20], read: ch('distance')},
    'audio.level': {label: 'audio level (현재 값 하나)', kind: 'number', unit: '0–1', range: [0, 1], read: ch('audio')},
    'hyeol.attached': {label: '혈 사용 중 1 / 아이들링 0', kind: 'number', range: [0, 1], read: d => ({connected: Boolean(d && d.hyeol),
      value: d && d.hyeol ? (d.hyeol.attached ? 1 : 0) : null, rate_hz: null, mode: d && d.hyeol && d.hyeol.simulated ? 'simulation' : 'live'})},
    'sim.hyeol.x': {label: '혈 위치 x (mm)', kind: 'number', unit: 'mm', range: [0, 12000], read: pose('hyeol', 'x')},
    'sim.hyeol.y': {label: '혈 위치 y (mm)', kind: 'number', unit: 'mm', range: [0, 20000], read: pose('hyeol', 'y')},
    'sim.hyeol.heading': {label: '혈 방향 (°)', kind: 'number', unit: '°', range: [0, 360], read: pose('hyeol', 'heading')},
    'sim.maek.x': {label: '맥 위치 x (mm)', kind: 'number', unit: 'mm', range: [0, 12000], read: pose('maek', 'x')},
    'sim.maek.y': {label: '맥 위치 y (mm)', kind: 'number', unit: 'mm', range: [0, 20000], read: pose('maek', 'y')},
    'sim.maek.heading': {label: '맥 방향 (°)', kind: 'number', unit: '°', range: [0, 360], read: pose('maek', 'heading')},
    'ai_led.heartbeat': {label: 'AI/LED heartbeat 번호 (데이터 포맷 미정)', kind: 'number', read: d => { const r = ch('ai_led')(d); return Object.assign(r, {value: r.connected ? r.seq : null}); }},
    // 맥: 3 microphones close together (channels.maek_mic, one sample = [mic 1, mic 2, mic 3], 0–1); mock = assumed
    // placement, see inputs.py. One source per mic so any layer setting can follow one of them.
    ...Object.fromEntries([1, 2, 3].map(k => [`maek.mic${k}`, {label: `맥 마이크 ${k} (3개 중 · 0–1)`, kind: 'number', unit: '0–1', range: [0, 1], read: d => {
      const c = d && d.channels && d.channels.maek_mic, v = c && Array.isArray(c.value) ? c.value[k - 1] : null;
      if (!c) return {connected: false, value: null, rate_hz: null, mode: 'none'};
      return {connected: c.connected && typeof v === 'number', value: typeof v === 'number' ? v : null, rate_hz: c.rate_hz, seq: c.seq, age_s: c.age_s,
              paused: c.paused, mode: c.source === 'mock' ? 'mock' : c.source === 'none' ? '수신기 없음' : 'live', unit: c.unit}; }}])),
    'ai.learning': {label: 'AI 학습 상태', kind: 'number', unavailable: '연결 안 됨 / 포맷 미정'},
  };

  // One poller per page for every number source: /inputs (one request in flight, every 50 ms) and /api/simulator
  // (every 250 ms). sample(id) = the latest reading; history(id) = received values (new seq only) of the last 10 s.
  const hub = (() => {
    let inputs = null, sim = null, inflight = false, simInflight = false, started = false, serverOk = false, retryAt = 0;
    const hist = {}, lastSeq = {}, trace = {}, TRACE_MS = 130000;
    const timeoutSignal = ms => (AbortSignal.timeout ? AbortSignal.timeout(ms) : undefined);
    async function poll() {
      if (inflight || Date.now() < retryAt) return;  // after a failure (server restarting), retry once per second
      inflight = true;
      try {
        inputs = await fetch('/inputs', {cache: 'no-store', signal: timeoutSignal(2000)}).then(r => r.json());
        serverOk = true;
        const now = performance.now();
        for (const [id, src] of Object.entries(SOURCES)) {
          if (src.kind !== 'number' || src.unavailable || id.startsWith('sim.')) continue;
          const r = src.read(inputs, sim);
          if (r.connected && r.value !== null && r.seq !== lastSeq[id]) { lastSeq[id] = r.seq; (hist[id] = hist[id] || []).push({t: now, v: r.value}); }
          if (hist[id]) while (hist[id].length && now - hist[id][0].t > 10000) hist[id].shift();
          // trace: every received value at its measurement time, null = a gap (no measurement / not connected)
          const tr = trace[id] = trace[id] || [], last = tr[tr.length - 1];
          if (r.connected && r.value !== null) { if (!last || last.seq !== r.seq) tr.push({t: now - 1000 * (r.age_s || 0), v: r.value, seq: r.seq}); }
          else if (last && last.v !== null) tr.push({t: now, v: null});
          while (tr.length && now - tr[0].t > TRACE_MS) tr.shift();
        }
      } catch (_) {
        serverOk = false; retryAt = Date.now() + 1000;
        for (const tr of Object.values(trace)) if (tr.length && tr[tr.length - 1].v !== null) tr.push({t: performance.now(), v: null});
      } finally { inflight = false; }
    }
    async function pollSim() {
      if (simInflight || Date.now() < retryAt) return;
      simInflight = true;
      try { sim = await fetch('/api/simulator', {cache: 'no-store', signal: timeoutSignal(2000)}).then(r => r.json()); } catch (_) { retryAt = Date.now() + 1000; } finally { simInflight = false; }
    }
    return {
      start() { if (started) return; started = true; poll(); pollSim(); setInterval(poll, 50); setInterval(pollSim, 250); },
      sample(id) {
        const src = SOURCES[id];
        if (!src) return {connected: false, value: null, mode: 'none', unknown: true};
        if (src.unavailable) return {connected: false, value: null, mode: src.unavailable, unavailable: src.unavailable};
        return src.read(inputs, sim);
      },
      history: id => hist[id] || [],
      trace: id => trace[id] || [],
      get serverOk() { return serverOk; },
      get inputs() { return inputs; },
    };
  })();

  // ============================================================================================================
  // RENDERERS (layer types) — the one registry of how a layer draws. accepts: which source kind the layer reads.
  // params: the editable values (the 작업실 builds its controls from these). create(off, layer) → instance with
  // draw(context) that draws the whole layer into its own off-screen canvas. A new renderer is one entry here.
  const tofParam = {
    near: {label: '가까운 거리', min: 0, max: 2000, step: 50, unit: 'mm'}, far: {label: '먼 거리', min: 500, max: 4000, step: 50, unit: 'mm'},
    confidence: {label: 'confidence', min: 0, max: 255, step: 1, unit: ''}, row: {label: '단면 위치', min: 0, max: 100, step: 1, unit: '%'},
    base: {label: '기준선', min: 0, max: 90, step: 1, unit: '%'}, flow: {label: '흐름 속도', min: 0.25, max: 4, step: 0.25, unit: '×'},
    gain: {label: '깊이 강조', min: 0.5, max: 3, step: 0.1, unit: '×'}, interval: {label: '등고선 간격', min: 50, max: 500, step: 50, unit: 'mm'},
    line: {label: '선 두께', min: 1, max: 3, step: 1, unit: 'px'}, slats: {label: '슬랫 미리보기', type: 'bool'},
    vertical: {label: '세로 스캔', type: 'bool'}, temporal: {label: '시간 필터', type: 'bool'},
    direction: {label: '흐르는 방향', type: 'angle'},   // SIDE only, drawn in the browser (not a server setting)
    vanish: {label: '소멸 기준선', min: 5, max: 100, step: 1, unit: '%'},   // SIDE only: where the flow disappears (100 = picture edge)
  };
  const TOF_DEFAULTS = {near: 200, far: 4000, confidence: 30, interval: 200, line: 1, slats: false, vertical: false, temporal: true, row: 50, gain: 1, base: 60, flow: 1};
  const BROWSER_DEFAULTS = {direction: 180, vanish: 100};   // SIDE settings drawn in the browser only
  const tofParams = keys => keys.map(k => Object.assign({key: k, default: k in BROWSER_DEFAULTS ? BROWSER_DEFAULTS[k] : TOF_DEFAULTS[k]}, tofParam[k]));
  // which settings each ToF mode shows (as the old ToF viewer's data-show); all of them are sent to the server
  // make(canvas, size): the drawer ({draw(buf, layer), clear()}) for an output canvas (size omitted) or a layer canvas
  const tofRenderer = (mode, label, keys) => ({label, accepts: 'frame', tofMode: mode, params: tofParams(keys),
    defaults: Object.assign({}, TOF_DEFAULTS),
    make(canvas, size) { return createTof(canvas, size ? {size} : {}); },
    create(off) { return {tof: createTof(off, {size: () => [off.width, off.height]})}; }});
  // experiment modes: stream the filtered depth (the SIDE packet), draw it here; own settings after the ToF ones
  const artRenderer = (kind, label, extra) => ({label, accepts: 'frame', tofMode: 'side', art: kind, params: [...tofParams(['near', 'far', 'confidence', 'temporal']), ...extra],
    defaults: Object.assign({}, TOF_DEFAULTS, ART_DEFAULTS[kind]),
    make(canvas, size) { return createDepthArt(canvas, kind, size ? {size} : {}); },
    create(off) { return {tof: createDepthArt(off, kind, {size: () => [off.width, off.height]})}; }});
  const mm = (key, label, min, max) => ({key, label, min, max, step: 50, unit: 'mm', default: ART_DEFAULTS.silhouette[key]});
  // ============================================================================================================
  // CEILING DISTANCE SENSOR (2026-10-15): one distance (m) over time from sensor.up_distance — drawn as its own kind of
  // picture (never as a fake ToF image). Every layer: input range, smoothing, change gain, invert, near / middle / far
  // colours. No measurement (not connected, no value, server lost) = the whole layer black, no invented value.
  const UPD_COMMON = [
    {key: 'min', label: '입력 최소', min: 0, max: 20, step: 0.1, unit: ' m', default: 0}, {key: 'max', label: '입력 최대', min: 0.5, max: 20, step: 0.1, unit: ' m', default: 20},
    {key: 'smoothing', label: '부드럽게', min: 0, max: 0.95, step: 0.05, default: 0.5}, {key: 'gain', label: '변화량 강조', min: 0, max: 10, step: 0.1, unit: '×', default: 1},
    {key: 'invert', label: '반전 (멀수록 밝게·높게)', type: 'bool', default: false},
    {key: 'color_near', label: '가까움 색', type: 'color', default: '#ffffff'}, {key: 'color_mid', label: '중간 색', type: 'color', default: '#808080'},
    {key: 'color_far', label: '멀어짐 색', type: 'color', default: '#333333'}, {key: 'midpoint', label: '중간 지점', min: 0.05, max: 0.95, step: 0.05, default: 0.5}];
  const DIRECTIONS = [['위', 0], ['오른쪽', 90], ['아래', 180], ['왼쪽', 270]];   // flow angle: 0° = toward the top, clockwise
  // extra: {fx: true} = the shared visual experiments (EFFECTS) apply to this type too. A param with `legacy` is read as
  // that value when a saved layer does not have it (older layers look as before); new layers start from `default`.
  function updistRenderer(kind, label, own, extra) {
    const params = [...own, ...UPD_COMMON];
    return {label, group: 'updist', accepts: 'number', sources: ['sensor.up_distance'], params, updist: kind, ...extra,
      defaults: Object.fromEntries(params.map(p => [p.key, p.default])),
      legacy: Object.fromEntries(params.filter(p => 'legacy' in p).map(p => [p.key, p.legacy])),
      create() { return {s: null, v: 0, seq: null, at: 0, energy: 0, phase: 0, offset: 0, frameAt: 0, strip: null}; },
      draw(off, layer, inst) { drawUpdist(kind, off, layer, inst || {}); }};
  }
  function updistState(layer, inst, now) {
    const R = RENDERERS[layer.renderer], p = Object.assign({}, R.defaults, R.legacy, layer.params);
    const r = layer.source ? hub.sample(layer.source) : {connected: false};
    const ok = hub.serverOk && r.connected && typeof r.value === 'number';
    if (!ok) { inst.s = null; inst.v = 0; inst.seq = null; return {p, ok: false}; }
    if (r.seq !== inst.seq) {   // a new measurement: smooth, change speed from the measurement times (arrival − age_s)
      const at = now - 1000 * (r.age_s || 0);
      if (inst.s === null) { inst.s = r.value; inst.v = 0; }
      else {
        const prev = inst.s, dt = Math.max(0.02, (at - inst.at) / 1000);
        inst.s = prev + (1 - p.smoothing) * (r.value - prev);
        inst.v = (inst.s - prev) / dt;
      }
      inst.seq = r.seq; inst.at = at;
    } else if (now - inst.at > 500) inst.v = 0;   // no new value for 0.5 s: not changing
    if (p.auto_span > 0) {   // 자동 범위 (2026-11-11): the range follows a slow average of the value, ± auto_span m around it
      const dtb = inst.baseAt ? Math.min(1, (now - inst.baseAt) / 1000) : 1;
      inst.base = inst.base == null ? inst.s : inst.base + (inst.s - inst.base) * Math.min(1, dtb / Math.max(1, p.auto_s || 20));
      inst.baseAt = now;
      p.min = inst.base - p.auto_span; p.max = inst.base + p.auto_span;
      if (p.line_mm > p.auto_span * 1000 / 4) p.line_mm = Math.max(20, Math.round(p.auto_span * 1000 / 12));   // keep several lines in the shorter range
    }
    const lo = Math.min(p.min, p.max), hi = Math.max(p.max, p.min + 0.1);
    const n = Math.min(1, Math.max(0, (inst.s - lo) / (hi - lo)));   // 0 = at "min" (near), 1 = at "max" (far)
    const t = p.invert ? n : 1 - n;                                  // brightness / colour position: near = 1 (invert: far = 1)
    return {p, ok: true, d: inst.s, n: p.invert ? 1 - n : n, t, change: Math.abs(inst.v) * p.gain, v: inst.v * p.gain};
  }
  function rampColour(p, t) {   // far → mid → near
    const far = hexRgb(p.color_far), mid = hexRgb(p.color_mid), near = hexRgb(p.color_near), m = p.midpoint;
    return far.map((f, k) => Math.round(t <= m ? f + (mid[k] - f) * (t / m) : mid[k] + (near[k] - mid[k]) * ((t - m) / (1 - m))));
  }
  const rgbStr = (c, a = 1) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;
  // 미터 선 색 (2026-11-12): side_fade > 0 → full at the screen edge, fading toward the centre over that share of the
  // width; meter_alpha dims it — so the bars read as part of the picture, not as separate blocks
  function meterInk(ctx, p, c, bx, bw) {
    const a = p.meter_alpha == null ? 1 : p.meter_alpha, f = p.side_fade || 0;
    if (f <= 0) return rgbStr(c, a);
    const outer = p._side === 'left' ? bx : bx + bw, inner = p._side === 'left' ? bx + bw : bx;
    const g = ctx.createLinearGradient(outer, 0, inner, 0);
    g.addColorStop(0, rgbStr(c, a)); g.addColorStop(Math.max(0, 1 - f), rgbStr(c, a)); g.addColorStop(1, rgbStr(c, 0));
    return g;
  }
  // VERTICAL METER with a scale: the track is the whole range (min at the bottom, max at the top), the bar fills it up
  // to the value, ticks every "tick" metres with their labels, the value in metres above the bar
  function drawMeterScale(ctx, W, H, dpr, p, st) {
    const lo = Math.min(p.min, p.max), hi = Math.max(p.max, p.min + 0.1);
    const fs = Math.max(9 * dpr, Math.min(W * 0.2, H * 0.03)), pad = Math.round(fs * 1.6);   // room for the value text
    const lines = p.style === 'lines';   // lines: the range runs to the bottom edge, only the value text above it
    const marks = Boolean(p.scale);   // lines without marks: only the stacked lines (no side lines, ticks, numbers)
    const top = lines ? (marks ? Math.round(fs * 1.2) : 0) : pad, bot = lines ? H - Math.max(1, dpr) : H - Math.round(fs * 0.8), th = Math.max(1, bot - top);
    const bw = Math.max(2, p.thickness * W), bx = (W - bw) / 2;
    const yOf = v => bot - th * (p.invert ? 1 - (v - lo) / (hi - lo) : (v - lo) / (hi - lo));
    if (lines) {   // thin lines like 공간선: one every line_mm up to the value, coloured far (bottom) → near (the value)
      const lwid = Math.max(1, dpr), stepM = Math.max(0.02, p.line_mm / 1000);
      ctx.lineWidth = lwid;
      if (marks) {   // the range: two hairlines at its sides
        ctx.strokeStyle = 'rgba(255,255,255,0.18)';
        ctx.beginPath(); ctx.moveTo(bx + 0.5, top); ctx.lineTo(bx + 0.5, bot); ctx.moveTo(bx + bw - 0.5, top); ctx.lineTo(bx + bw - 0.5, bot); ctx.stroke();
      }
      if (st.ok && p.line_px > 0) {   // 화소 간격 (2026-11-11): lines every line_px from the bottom up to the value — same density whatever the range
        const v = Math.min(hi, Math.max(lo, st.d)), vy = yOf(v), gap = Math.max(2, p.line_px * dpr);
        for (let ly = bot; ly > vy; ly -= gap) {
          ctx.strokeStyle = meterInk(ctx, p, rampColour(p, (bot - ly) / Math.max(1, bot - vy)), bx, bw);
          ctx.beginPath(); ctx.moveTo(bx, Math.round(ly) + 0.5); ctx.lineTo(bx + bw, Math.round(ly) + 0.5); ctx.stroke();
        }
        ctx.strokeStyle = meterInk(ctx, p, rampColour(p, 1), bx, bw);   // the value line itself
        ctx.beginPath(); ctx.moveTo(bx, Math.round(vy) + 0.5); ctx.lineTo(bx + bw, Math.round(vy) + 0.5); ctx.stroke();
      } else if (st.ok) {
        const v = Math.min(hi, Math.max(lo, st.d)), n = Math.floor((v - lo) / stepM + 1e-6);
        for (let k = 0; k <= n; k++) {
          const lv = k === n ? v : lo + k * stepM, f = v > lo ? (lv - lo) / (v - lo) : 1, ly = Math.round(yOf(lv)) + 0.5;
          ctx.strokeStyle = rgbStr(rampColour(p, f));   // opaque: a 색 매핑 effect then colours it like 공간선
          ctx.beginPath(); ctx.moveTo(bx, ly); ctx.lineTo(bx + bw, ly); ctx.stroke();
        }
      }
    } else {
      ctx.fillStyle = 'rgba(255,255,255,0.07)'; ctx.fillRect(bx, top, bw, th);
      ctx.strokeStyle = 'rgba(255,255,255,0.28)'; ctx.lineWidth = Math.max(1, dpr); ctx.strokeRect(bx + 0.5, top + 0.5, bw - 1, th - 1);
    }
    let y = bot;
    if (st.ok && lines) y = yOf(Math.min(hi, Math.max(lo, st.d)));
    else if (st.ok) {
      y = yOf(Math.min(hi, Math.max(lo, st.d)));
      ctx.fillStyle = rgbStr(rampColour(p, st.t));
      if (p.invert) ctx.fillRect(bx, top, bw, y - top); else ctx.fillRect(bx, y, bw, bot - y);
    }
    if (lines && !marks) return;
    ctx.font = `${Math.round(fs * 0.62)}px ui-monospace, Menlo, monospace`; ctx.textBaseline = 'middle';
    const step = Math.max(0.1, p.tick), n = Math.floor((hi - lo) / step + 1e-6);
    for (let k = 0; k <= n; k++) {
      const v = lo + k * step, ty = Math.round(yOf(v)) + 0.5, whole = Math.abs(v - Math.round(v)) < 1e-6;
      if (lines) {   // outside the lines, to the left: short hairline + whole metres only, faint
        const out = Math.max(3 * dpr, bx * 0.35);
        ctx.strokeStyle = `rgba(255,255,255,${whole ? 0.45 : 0.22})`;
        ctx.beginPath(); ctx.moveTo(bx - out * (whole ? 1 : 0.5), ty); ctx.lineTo(bx - 1, ty); ctx.stroke();
        ctx.fillStyle = 'rgba(255,255,255,0.5)'; ctx.textAlign = 'right';
        if (whole && bx > fs) ctx.fillText(`${Math.round(v)}`, bx - out - 2 * dpr, ty);
        continue;
      }
      ctx.strokeStyle = `rgba(255,255,255,${whole ? 0.75 : 0.4})`;
      ctx.beginPath(); ctx.moveTo(bx, ty); ctx.lineTo(bx + bw * (whole ? 0.45 : 0.25), ty); ctx.stroke();
      ctx.fillStyle = `rgba(255,255,255,${whole ? 0.85 : 0.5})`; ctx.textAlign = 'right';
      if (whole || bw > fs * 3) ctx.fillText(whole ? `${Math.round(v)}` : v.toFixed(1), bx + bw - fs * 0.25, ty);
    }
    ctx.font = `${lines ? 400 : 600} ${Math.round(fs * (lines ? 0.8 : 1))}px ui-monospace, Menlo, monospace`; ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
    ctx.fillStyle = !st.ok ? 'rgba(255,255,255,0.5)' : lines ? rgbStr(hexRgb(p.color_near)) : '#ffffff';
    const label = st.ok ? `${st.d.toFixed(2)}m` : '없음';
    ctx.fillText(label, W / 2, Math.max(fs, Math.min(p.invert ? top + fs * 1.2 + (y - top) : y - fs * 0.4, bot)), W);
  }
  function drawUpdist(kind, off, layer, inst) {
    const ctx = off.getContext('2d'), W = off.width, H = off.height, now = performance.now(), dpr = window.devicePixelRatio || 1;
    const dt = inst.frameAt ? Math.min(0.25, (now - inst.frameAt) / 1000) : 0;
    inst.frameAt = now;
    ctx.globalAlpha = 1; ctx.globalCompositeOperation = 'source-over';
    ctx.fillStyle = '#000'; ctx.fillRect(0, 0, W, H);
    const st = updistState(layer, inst, now), p = st.p;
    p._side = ((layer.x && layer.x.value) || 0) + (layer.w || 0) / 2 < 0.5 ? 'left' : 'right';   // which way the centre is (for 안쪽으로 사라짐)
    if (kind === 'trace') { drawTrace(ctx, W, H, dpr, layer, p, now); return; }   // its history shows gaps itself
    if (kind === 'meter' && (p.scale || p.style === 'lines')) { drawMeterScale(ctx, W, H, dpr, p, st); return; }   // shows "측정값 없음" itself
    if (!st.ok) { inst.energy = 0; return; }   // no measurement: black
    const colour = rampColour(p, st.t);
    if (kind === 'meter') {
      const bw = Math.max(1, p.thickness * W), bh = st.n * H;   // height = distance within the range (invert: near = tall)
      ctx.fillStyle = rgbStr(colour); ctx.fillRect((W - bw) / 2, H - bh, bw, bh);
    } else if (kind === 'pulse') {
      // energy jumps with the change speed and falls back to 0 within "decay" seconds; it sets brightness / opacity and
      // the expansion speed only — the line width is fixed (never from the sensor)
      inst.energy = Math.max(Math.min(1, st.change * p.sensitivity), inst.energy - dt / p.decay);
      const R = p.size * Math.min(W, H) / 2 * (0.3 + 0.7 * st.t), rings = Math.round(p.rings);
      inst.phase = (inst.phase + dt * (0.25 + 1.5 * inst.energy)) % 1;
      const shape = pulseShape(layer, p, now);   // per-angle radius factors (null = a plain circle)
      ctx.lineWidth = Math.max(1, 2 * dpr); ctx.lineJoin = 'round';
      for (let k = 0; k < rings; k++) {
        const f = (k + inst.phase) / rings, r = R * f;
        if (r < 0.5) continue;
        ctx.strokeStyle = rgbStr(colour, (1 - f) * (0.35 + 0.65 * inst.energy) + 0.15 * (1 - f));
        ctx.beginPath();
        if (!shape) ctx.arc(W / 2, H / 2, r, 0, 2 * Math.PI);
        else {
          for (let j = 0; j < shape.length; j++) {   // angle 0 = top, clockwise = further back in time
            const a = 2 * Math.PI * j / shape.length, rr = r * shape[j];
            ctx[j ? 'lineTo' : 'moveTo'](W / 2 + rr * Math.sin(a), H / 2 - rr * Math.cos(a));
          }
          ctx.closePath();
        }
        ctx.stroke();
      }
      ctx.fillStyle = rgbStr(colour, 0.25 + 0.75 * inst.energy);
      ctx.beginPath(); ctx.arc(W / 2, H / 2, Math.max(2 * dpr, R * 0.08), 0, 2 * Math.PI); ctx.fill();
    } else if (kind === 'field') {
      // stripes across the flow direction: spacing from the distance (nearer = denser), moving along the direction at a
      // speed from the change amount. The direction only turns the stripes; spacing / speed responses stay the same.
      const bands = p.bands * (0.5 + st.t);
      inst.offset = (inst.offset + dt * (0.15 + st.change * p.speed)) % 1000;
      const L = 1024;
      if (!inst.strip) { inst.strip = document.createElement('canvas'); inst.strip.width = L; inst.strip.height = 1; }
      const sctx = inst.strip.getContext('2d'), img = sctx.createImageData(L, 1), px = img.data, far = hexRgb(p.color_far);
      const w = 0.5 * (1 - p.sharpness) + 0.002;
      for (let x = 0; x < L; x++) {
        const f = 0.5 + 0.5 * Math.cos(2 * Math.PI * ((x / L) * bands - inst.offset));
        let g = Math.min(1, Math.max(0, (f - (0.5 - w)) / (2 * w))); g = g * g * (3 - 2 * g);
        for (let k = 0; k < 3; k++) px[x * 4 + k] = far[k] + (colour[k] - far[k]) * g;
        px[x * 4 + 3] = 255;
      }
      sctx.putImageData(img, 0, 0);
      const D = Math.hypot(W, H), a = ((Number(p.angle) || 0) - 90) * Math.PI / 180;   // strip x axis → flow vector
      ctx.save(); ctx.translate(W / 2, H / 2); ctx.rotate(a); ctx.imageSmoothingEnabled = true;
      ctx.drawImage(inst.strip, -D / 2, -D / 2, D, D);
      ctx.restore();
    }
  }
  // PULSE outline from the RECENT HISTORY of the one ceiling distance (a single-point sensor: this is how the distance
  // changed over time, NOT the ceiling's shape in any direction). The last shape_s seconds go once around the ring in
  // time order — now at the top, clockwise = older. Each point's radius factor = 1 + shape × tanh(shape_gain × (mean −
  // distance)): a steady input → every factor 1 → a circle; bigger recent changes → a more irregular outline (nearer
  // than the mean = outward, like "nearer = bigger"; 반전 flips it). Same smoothing as the other modes; gaps (no
  // measurement) and times before the first value stay on the circle (no invented value).
  const PULSE_POINTS = 120;
  function pulseShape(layer, p, now) {
    if (!(p.shape > 0)) return null;
    const tr = layer.source ? hub.trace(layer.source) : [], span = p.shape_s * 1000;
    const pts = [];   // smoothed, oldest → newest, null = gap
    let s = null;
    for (const q of tr) {
      if (q.v === null) { s = null; if (now - q.t <= span) pts.push({t: q.t, v: null}); continue; }
      s = s === null ? q.v : s + (1 - p.smoothing) * (q.v - s);
      if (now - q.t <= span + 200) pts.push({t: q.t, v: s});
    }
    const valid = pts.filter(q => q.v !== null);
    if (valid.length < 2) return null;
    const mean = valid.reduce((a, q) => a + q.v, 0) / valid.length, sign = p.invert ? 1 : -1;
    const out = new Float32Array(PULSE_POINTS);
    let i = pts.length - 1;
    for (let j = 0; j < PULSE_POINTS; j++) {
      const t = now - span * j / PULSE_POINTS;   // newest first
      while (i > 0 && pts[i].t > t) i--;
      const a = pts[i], b = pts[i + 1];
      let v = null;
      if (a.t <= t) {
        if (b && a.v !== null && b.v !== null) v = a.v + (b.v - a.v) * Math.min(1, (t - a.t) / Math.max(1, b.t - a.t));
        else if (!b) v = a.v;   // after the latest value: hold it
      }
      out[j] = v === null ? 1 : Math.max(0.05, 1 + p.shape * Math.tanh(p.shape_gain * sign * (v - mean)));
    }
    return out;
  }
  // time runs right (now) → left (window seconds ago); height = distance in the range; gaps stay gaps
  function drawTrace(ctx, W, H, dpr, layer, p, now) {
    const tr = layer.source ? hub.trace(layer.source) : [], span = p.window * 1000;
    const lo = Math.min(p.min, p.max), hi = Math.max(p.max, p.min + 0.1);
    const pts = [];
    let s = null;
    for (const q of tr) {
      if (now - q.t > span + 1000) { s = q.v === null ? null : q.v; continue; }
      if (q.v === null) { s = null; pts.push(null); continue; }
      s = s === null ? q.v : s + (1 - p.smoothing) * (q.v - s);
      const n = Math.min(1, Math.max(0, (s - lo) / (hi - lo))), h = p.invert ? 1 - n : n;
      pts.push({x: W * (1 - (now - q.t) / span), y: H * (1 - h), t: p.invert ? n : 1 - n, age: (now - q.t) / span});
    }
    ctx.lineWidth = p.line * dpr; ctx.lineJoin = ctx.lineCap = 'round';
    for (let i = 1; i < pts.length; i++) {
      const a = pts[i - 1], b = pts[i];
      if (!a || !b) continue;
      const alpha = 1 - p.fade * Math.min(1, b.age), c = rampColour(p, b.t);
      if (p.fill) { ctx.fillStyle = rgbStr(c, alpha * 0.5); ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.lineTo(b.x, H); ctx.lineTo(a.x, H); ctx.closePath(); ctx.fill(); }
      ctx.strokeStyle = rgbStr(c, alpha); ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    }
  }

  // ============================================================================================================
  // 공간선 (SPACE LINES, experiment, 2026-10-15): the ToF depth as real 3D lines. Every record_s seconds one batch of
  // line segments is made from the latest depth frame with ONE method (경계 윤곽 / 거리별 등고선 / 점 연결선) — each depth
  // sample becomes a camera-space point (x right, y up, z forward, metres): the mock ToF measures the distance along the
  // ray, so Z = r / √(1 + xn² + yn²). With the mock simulation the batch is fixed in the venue with 혈's pose at that
  // moment (ToF 1.5 m high, level, 70° wide: mock_tof.py), and the view is 혈's current camera, so older lines stay where
  // they were read and move on screen as the device moves / turns. Without a simulator pose (real input) the lines stay
  // in camera space only (shown as such, never as a record fixed in the room). These are accumulated sensor readings, not
  // learned values. Drawn with WebGL: depth test (nearer lines hide farther ones), then an optional depth-of-field pass
  // that blurs each pixel by how far its Z is from the focus plane (Z of the ToF pixel at the focus point). A batch keeps
  // its place for hold_s, then sags along the gravity direction (screen direction, 0° = up, clockwise) and clumps toward
  // its centre over sag_s, then fades out over fade_s and is removed. Caps: SL_LIMITS (batches, segments).
  const SL_W = 240, SL_H = 180, SL_HFOV = 70, SL_CAM_H = 1.0;   // mock_tof W, H, HFOV_DEG, SIM_TOF_HEIGHT (m, 1.0 since 2026-11-06)
  const SL_FX = (SL_W / 2) / Math.tan(SL_HFOV / 2 * Math.PI / 180);
  const SL_LIMITS = {batches: 160, segPerBatch: 6000, segTotal: 240000};
  const SPACELINE_DEFAULTS = {method: 'edge', edge_mm: 150, interval_mm: 250, step: 4, record_s: 0.25, hold_s: 2, sag_s: 6, fade_s: 4,
    gravity_deg: 180, gravity: 0.6, clump: 0.4, focus_x: 50, focus_y: 50, focus_shift: 0, focus_range: 0.5, blur: 6,
    color_near: '#ffffff', color_far: '#5a6a80', pose_src: 'auto', res: 1, denoise: 0};
  // the simulator pose, live (/ws/simulator) — opened only when a 공간선 layer exists
  const simLive = (() => {
    let pose = null, bounds = null, open = false, started = false;
    function connect() {
      const ws = new WebSocket(`ws://${location.host}/ws/simulator`);
      ws.onopen = () => { open = true; };
      ws.onmessage = e => { const m = JSON.parse(e.data); if (m.type !== 'state') return;
        if (m.tof_pose || (m.devices && m.devices.hyeol)) pose = Object.assign({}, m.tof_pose || m.devices.hyeol);   // the ToF's own pose (back of 혈, looking backward)
        if (m.bounds) bounds = Object.assign({}, m.bounds); };
      ws.onclose = () => { open = false; setTimeout(connect, 1000); };
    }
    return {start() { if (!started) { started = true; connect(); } }, get pose() { return open ? pose : null; }, get bounds() { return bounds; }};
  })();
  // camera point of sensor pixel (u, v) with ray distance r (mm) → [x, y, z] metres
  function slCam(u, v, r) {
    const xn = (u - (SL_W - 1) / 2) / SL_FX, yn = -(v - (SL_H - 1) / 2) / SL_FX, z = r / 1000 / Math.sqrt(1 + xn * xn + yn * yn);
    return [xn * z, yn * z, z];
  }
  // 잡음 거르기 (denoise, 2026-11-10): the real ToF's per-pixel jitter turns 10 cm contours into scattered short pieces.
  // Each pixel becomes the mean of the measured pixels in a (2r+1)² window; a pixel with fewer than 60 % of its window
  // measured becomes "no data" (lone specks and flying pixels at edges vanish), and an unmeasured pixel inside a
  // mostly measured window gets the mean (pinholes filled, so 경계 윤곽 does not outline every hole). r = 0 → unchanged.
  function slDenoise(f, r) {
    r = Math.round(r || 0);
    if (r < 1 || !f.depth) return f;
    const W = f.w, H = f.h, d = f.depth, sx = new Float32Array(W * H), cx = new Uint16Array(W * H), out = new Float32Array(W * H);
    for (let y = 0; y < H; y++) {   // horizontal running sums of depth and of measured pixels
      let s = 0, c = 0;
      for (let x = -r; x < W + r; x++) {
        const xa = x + r, xb = x - r - 1;
        if (xa < W) { const v = d[y * W + xa]; if (v) { s += v; c++; } }
        if (xb >= 0) { const v = d[y * W + xb]; if (v) { s -= v; c--; } }
        if (x >= 0 && x < W) { sx[y * W + x] = s; cx[y * W + x] = c; }
      }
    }
    const need = 0.6 * (2 * r + 1) * (2 * r + 1);
    for (let x = 0; x < W; x++) {   // vertical sums of those
      let s = 0, c = 0;
      for (let y = -r; y < H + r; y++) {
        const ya = y + r, yb = y - r - 1;
        if (ya < H) { s += sx[ya * W + x]; c += cx[ya * W + x]; }
        if (yb >= 0) { s -= sx[yb * W + x]; c -= cx[yb * W + x]; }
        if (y >= 0 && y < H) out[y * W + x] = c >= need ? s / c : 0;   // a hole inside a measured surface is filled too
      }
    }
    return Object.assign({}, f, {depth: out});
  }
  // one batch of segments [x0, y0, z0, x1, y1, z1, …] in camera space from a depth frame (radial mm, 0 = none)
  function slSegments(f, p) {
    const W = f.w, H = f.h, d = f.depth, out = [];
    // (2026-11-11) no temporary arrays per point: on the Pi 4 the garbage of slCam([x, y, z]) per vertex was most of a frame
    const cx0 = (SL_W - 1) / 2, cy0 = (SL_H - 1) / 2, ifx = 1 / SL_FX;
    const cam = (u, v, r) => { const xn = (u - cx0) * ifx, yn = -(v - cy0) * ifx, z = r / 1000 / Math.sqrt(1 + xn * xn + yn * yn); out.push(xn * z, yn * z, z); };
    const push = (u0, v0, r0, u1, v1, r1) => { cam(u0, v0, r0); cam(u1, v1, r1); };
    if (p.method === 'ir_edge' && f.strokes) {   // 적외선 윤곽: smoothed strokes from the server (IrEdges), each point at its own depth
      const st = f.strokes; let k = 0;
      // p._only: 'edges' = outlines only, 'iso' = distance lines only (2026-11-11: drawn as two layers)
      for (let i = 0; i < (p._only === 'iso' ? 0 : st.counts.length); i++) {
        const n = st.counts[i];
        for (let j = 1; j < n; j++) {
          const x0 = st.x[k + j - 1] / 8, y0 = st.y[k + j - 1] / 8, z0 = st.z[k + j - 1], x1 = st.x[k + j] / 8, y1 = st.y[k + j] / 8, z1 = st.z[k + j];
          push(x0, y0, z0, x1, y1, z1);
          for (let b = 1; b <= (p.ir_bold || 0); b++) {   // 윤곽 강조: extra copies a hair apart (≈ 1 screen px each) → a bolder outline than the distance lines
            const d = 0.12 * b; push(x0 + d, y0, z0, x1 + d, y1, z1); push(x0, y0 + d, z0, x1, y1 + d, z1);
          }
        }
        k += n;
      }
      if (p._only === 'edges') return out;
      if (p.ir_rows > 0 && p._only !== 'edges' && f.depth) {   // 면 스캔선 (2026-11-15): every ir_rows sensor rows, the surface
        // profile as one line (smoothed depth), cut where the depth jumps — reads as surfaces, not outlines
        const g = slDenoise(f, Math.max(1, p.denoise || 2)), d = g.depth, Wd = g.w, Hd = g.h, jump = Math.max(60, p.edge_mm), st = Math.max(2, Math.round(p.ir_rows));
        for (let y = Math.floor(st / 2); y < Hd; y += st) for (let x = 0; x + 1 < Wd; x++) {
          const a = d[y * Wd + x], b = d[y * Wd + x + 1];
          if (a && b && a <= f.far && b <= f.far && Math.abs(a - b) < jump) push(x, y, a, x + 1, y, b);
        }
      }
      if (p.ir_contour > 0 && f.isolines) {   // + 거리 등고선 from the server (IsoLines: averaged depth, closed smoothed shapes)
        const st2 = f.isolines; let k2 = 0;
        for (let i = 0; i < st2.counts.length; i++) {
          const n = st2.counts[i];
          for (let j = 1; j < n; j++) push(st2.x[k2 + j - 1] / 8, st2.y[k2 + j - 1] / 8, st2.z[k2 + j - 1], st2.x[k2 + j] / 8, st2.y[k2 + j] / 8, st2.z[k2 + j]);
          k2 += n;
        }
      } else if (p.ir_contour > 0) {   // (older server) + 거리 등고선 in the page: walls have no brightness edges, so add iso-distance lines
        const more = slSegments(f, Object.assign({}, p, {method: 'contour', interval_mm: p.ir_contour}));
        for (let i = 0; i < more.length; i++) out.push(more[i]);
      }
      return out;
    }
    if (p.method === 'ir_edge') {   // (older server: outline pixels, each at its own depth)
      const e = f.edges; if (!e || !e.n) return out;
      const map = new Float32Array(W * H), jump = Math.max(100, p.edge_mm * 2);
      for (let i = 0; i < e.n; i++) map[e.y[i] * W + e.x[i]] = e.z[i];
      for (let i = 0; i < e.n; i++) {
        const x = e.x[i], y = e.y[i], z = e.z[i];
        for (const [dx, dy] of [[1, 0], [0, 1], [1, 1], [-1, 1]]) {
          const nx = x + dx, ny = y + dy;
          if (nx < 0 || nx >= W || ny >= H) continue;
          const z2 = map[ny * W + nx];
          if (z2 && Math.abs(z - z2) < jump) push(x, y, z, nx, ny, z2);
        }
      }
      return out;
    }
    if (p.method === 'contour') {
      const step = Math.max(50, p.interval_mm);
      const lo = Math.ceil(Math.max(f.near, 200) / step) * step;
      for (let y = 0; y + 1 < H; y++) for (let x = 0; x + 1 < W; x++) {
        const a = d[y * W + x], b = d[y * W + x + 1], c = d[(y + 1) * W + x + 1], e = d[(y + 1) * W + x];
        if (!a || !b || !c || !e) continue;
        const mn = Math.min(a, b, c, e), mx = Math.max(a, b, c, e);
        for (let L = Math.max(lo, Math.ceil(mn / step) * step); L <= mx && L <= f.far; L += step) {
          // marching squares on one cell: crossing points on its 4 edges (pixel centres at integer coordinates)
          const pts = [];
          const edge = (x0, y0, v0, x1, y1, v1) => { if ((v0 < L) !== (v1 < L)) { const t = (L - v0) / (v1 - v0); pts.push([x0 + t * (x1 - x0), y0 + t * (y1 - y0)]); } };
          edge(x, y, a, x + 1, y, b); edge(x + 1, y, b, x + 1, y + 1, c); edge(x + 1, y + 1, c, x, y + 1, e); edge(x, y + 1, e, x, y, a);
          if (pts.length >= 2) push(pts[0][0], pts[0][1], L, pts[1][0], pts[1][1], L);
          if (pts.length === 4) push(pts[2][0], pts[2][1], L, pts[3][0], pts[3][1], L);
        }
      }
    } else if (p.method === 'mesh') {
      const s = Math.max(1, Math.round(p.step)), jump = Math.max(100, p.edge_mm * 2);
      for (let y = 0; y < H; y += s) for (let x = 0; x < W; x += s) {
        const a = d[y * W + x];
        if (!a) continue;
        if (x + s < W) { const b = d[y * W + x + s]; if (b && Math.abs(a - b) < jump) push(x, y, a, x + s, y, b); }
        if (y + s < H) { const b = d[(y + s) * W + x]; if (b && Math.abs(a - b) < jump) push(x, y, a, x, y + s, b); }
      }
    } else {   // edge: outlines where the depth jumps (or meets no measurement), drawn on the nearer side
      const thr = Math.max(20, p.edge_mm);
      for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
        const a = d[y * W + x];
        if (x + 1 < W) { const b = d[y * W + x + 1]; if ((a || b) && (!a || !b || Math.abs(a - b) > thr)) { const r = a && b ? Math.min(a, b) : a || b; push(x + 0.5, y - 0.5, r, x + 0.5, y + 0.5, r); } }
        if (y + 1 < H) { const b = d[(y + 1) * W + x]; if ((a || b) && (!a || !b || Math.abs(a - b) > thr)) { const r = a && b ? Math.min(a, b) : a || b; push(x - 0.5, y + 0.5, r, x + 0.5, y + 0.5, r); } }
      }
    }
    return out;
  }
  // sag and clump on the screen (2026-11-11): every point of a batch drops by the same screen distance and moves toward the
  // batch centre's screen position by the same fraction (so points at the same distance from it move at the same speed);
  // before, both happened in metres in 3D, so near points fell faster than far ones (a "waterfall")
  const SL_VS = `attribute vec3 aPos; uniform mat4 uView, uProj; uniform float uK, uClump, uDrop; uniform vec3 uCenter; uniform vec2 uGrav, uOff; varying float vZ;
    void main() { vec4 e = uView * vec4(aPos, 1.0); vZ = -e.z; vec4 c = uProj * e;
      vec4 cc = uProj * (uView * vec4(uCenter, 1.0)); vec2 n = c.xy / c.w, nc = cc.xy / max(cc.w, 1e-3);
      n = mix(n, nc, uClump * uK) + uGrav * uDrop * 0.6 * uK * uK;
      gl_Position = vec4((n + uOff) * c.w, c.z, c.w); }`;
  const SL_FS_COLOR = `precision mediump float; uniform vec3 uNear, uMid, uFar; uniform float uAlpha, uZ0, uZ1, uFog; varying float vZ;
    void main() { float t = clamp((vZ - uZ0) / (uZ1 - uZ0), 0.0, 1.0);
      gl_FragColor = vec4(t < 0.5 ? mix(uNear, uMid, t * 2.0) : mix(uMid, uFar, t * 2.0 - 1.0), uAlpha * (1.0 - uFog * t)); }`;
  const SL_FS_DEPTH = `precision highp float; varying float vZ;
    void main() { float v = clamp(vZ / 20.0, 0.0, 1.0) * 255.0; gl_FragColor = vec4(floor(v) / 255.0, fract(v), 0.0, 1.0); }`;
  const SL_VS_QUAD = `attribute vec2 aXY; varying vec2 vUv; void main() { vUv = aXY * 0.5 + 0.5; gl_Position = vec4(aXY, 0.0, 1.0); }`;
  // depth of field, gather form of scattering: a tap within its own circle of confusion lends it light, spread over
  // that circle's area (so an out-of-focus line becomes wider and dimmer); in-focus pixels stay as they are
  const SL_FS_DOF = `precision highp float; uniform sampler2D uColor, uDepth; uniform vec2 uPx; uniform float uFocus, uRange, uMaxR; varying vec2 vUv;
    float zAt(vec2 uv) { vec4 d = texture2D(uDepth, uv); return d.a < 0.5 ? -1.0 : (d.r * 255.0 + d.g) / 255.0 * 20.0; }
    float coc(float z) { if (z < 0.0) return 0.0; float dz = max(0.0, abs(z - uFocus) - uRange * 0.5); return clamp(dz / max(uFocus, 0.3), 0.0, 1.0) * uMaxR; }
    void main() {
      vec3 c0 = texture2D(uColor, vUv).rgb; float r0 = coc(zAt(vUv));
      if (uMaxR < 0.5) { gl_FragColor = vec4(c0, 1.0); return; }
      float tapArea = 3.14159 * uMaxR * uMaxR / 32.0;
      vec3 acc = r0 < 1.0 ? c0 : c0 * min(1.0, tapArea / (3.14159 * r0 * r0));
      for (int i = 1; i <= 32; i++) {
        float fi = float(i), rad = sqrt(fi / 32.0) * uMaxR, ang = fi * 2.39996;
        vec2 o = vec2(cos(ang), sin(ang)) * rad;
        vec2 uv = vUv + o * uPx;
        float rt = coc(zAt(uv));
        if (rt >= 1.0 && rt >= rad) acc += texture2D(uColor, uv).rgb * min(1.0, tapArea / (3.14159 * rt * rt));
      }
      gl_FragColor = vec4(acc, 1.0);
    }`;
  function createSpaceLines(canvas, opts = {}) {
    const ctx = canvas.getContext('2d');
    const sizeOf = opts.size || (() => { const dpr = window.devicePixelRatio || 1; return [Math.round(canvas.clientWidth * dpr), Math.round(canvas.clientHeight * dpr)]; });
    const glc = document.createElement('canvas');
    const gl = glc.getContext('webgl', {antialias: true, premultipliedAlpha: false, preserveDrawingBuffer: true});
    simLive.start();
    // counters for checks (never drawn): batches / segments made, the last batch before / after the per-batch cap, and
    // what removed batches (life = their time is up, cap = SL_LIMITS, reset = method / space change) with the last reason
    const status = {mode: gl ? 'starting' : 'nogl', batches: 0, segments: 0, method: null, gpu_ms: 0, build_ms: 0,
      made: {batches: 0, segments: 0}, last: {raw: 0, kept: 0}, dropped: {life: 0, cap: 0, reset: 0}, resets: 0, reset_why: null};
    let liveBatch = null, liveIso = null, colorRange = null, batches = [], lastRec = -Infinity, space = null, method = null, focusZ = 2, lastFrame = null, prog = null, fb = null;
    function shader(type, src) { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s)); return s; }
    function program(vs, fs) {
      const p = gl.createProgram(); gl.attachShader(p, shader(gl.VERTEX_SHADER, vs)); gl.attachShader(p, shader(gl.FRAGMENT_SHADER, fs)); gl.linkProgram(p);
      const u = {}; const n = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS); for (let i = 0; i < n; i++) { const a = gl.getActiveUniform(p, i); u[a.name] = gl.getUniformLocation(p, a.name); }
      return {p, u, a: gl.getAttribLocation(p, vs.includes('aPos') ? 'aPos' : 'aXY')};
    }
    if (gl) {
      prog = {color: program(SL_VS, SL_FS_COLOR), depth: program(SL_VS, SL_FS_DEPTH), dof: program(SL_VS_QUAD, SL_FS_DOF)};
      prog.quad = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, prog.quad); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    }
    function target(w, h) {   // two render targets (colour, linear Z) with a depth buffer each
      if (fb && fb.w === w && fb.h === h) return fb;
      if (fb) for (const t of fb.list) { gl.deleteTexture(t.tex); gl.deleteRenderbuffer(t.rb); gl.deleteFramebuffer(t.f); }
      const make = () => {
        const tex = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, tex);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
        for (const [k, v] of [[gl.TEXTURE_MIN_FILTER, gl.NEAREST], [gl.TEXTURE_MAG_FILTER, gl.NEAREST], [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE]]) gl.texParameteri(gl.TEXTURE_2D, k, v);
        const rb = gl.createRenderbuffer(); gl.bindRenderbuffer(gl.RENDERBUFFER, rb); gl.renderbufferStorage(gl.RENDERBUFFER, gl.DEPTH_COMPONENT16, w, h);
        const f = gl.createFramebuffer(); gl.bindFramebuffer(gl.FRAMEBUFFER, f);
        gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0); gl.framebufferRenderbuffer(gl.FRAMEBUFFER, gl.DEPTH_ATTACHMENT, gl.RENDERBUFFER, rb);
        return {tex, rb, f};
      };
      const color = make(), depth = make();
      fb = {w, h, color, depth, list: [color, depth]};
      return fb;
    }
    function dropBatch(b) { gl.deleteBuffer(b.buf); }
    function reset(why) {
      if (gl) batches.forEach(dropBatch);
      status.dropped.reset += batches.length; status.resets++; status.reset_why = why || 'reset';
      batches = []; lastRec = -Infinity; status.batches = status.segments = 0;
    }
    // where 혈 is (2026-11-10): 시뮬레이터 = the mock simulation's pose, ToF 추정 = guessed from the depth itself
    // (odometry.py, sent with each frame; errors add up), 카메라 기준 = none (lines stay in camera space).
    // 자동 = the simulator with the mock, else the ToF estimate when the server sends one (the Pi), else camera space.
    function poseNow(p, f) {
      const inp = hub.inputs, mock = Boolean(inp && inp.mock === true), est = (f && f.pose) || null, src = p.pose_src || 'auto';
      if (src === 'camera') return null;
      if (src === 'sim') return mock ? simLive.pose : null;
      if (src === 'tof') return est;
      return mock ? simLive.pose : est;
    }
    function record(f, p, now, live = false) {
      const t0 = performance.now();
      // 적외선 윤곽 with server lines uses no depth picture: skip the depth denoise (a Pi 4 saving)
      const layered = p.method === 'ir_edge' && f.strokes && p.ghost != null && p.ghost < 1;   // outlines and distance lines apart
      const src = p.method === 'ir_edge' && f.strokes && (!(p.ir_contour > 0) || f.isolines) ? f : slDenoise(f, p.denoise);
      if (layered && live) record2(f, Object.assign({}, p, {_only: 'iso'}), now);   // the distance lines: live only, never ghosts
      let seg = slSegments(src, layered ? Object.assign({}, p, {_only: 'edges'}) : p);
      const n = seg.length / 6;
      status.last = {raw: n, kept: Math.min(n, SL_LIMITS.segPerBatch)};
      if (n > SL_LIMITS.segPerBatch) {   // keep an even spread of the segments, at most segPerBatch
        const keep = new Float32Array(SL_LIMITS.segPerBatch * 6), k = n / SL_LIMITS.segPerBatch;
        for (let i = 0; i < SL_LIMITS.segPerBatch; i++) keep.set(seg.slice(Math.floor(i * k) * 6, Math.floor(i * k) * 6 + 6), i * 6);
        seg = keep;
      }
      if (!seg.length) { if (live && liveBatch) liveBatch.count = 0; status.build_ms = performance.now() - t0; return; }   // nothing read (no measurement): no batch
      const pose = poseNow(p, f), pts = new Float32Array(seg);
      if (pose) {   // camera → venue (x, height, y in metres) with 혈's pose now
        const h = pose.heading * Math.PI / 180, fx = Math.sin(h), fz = Math.cos(h), rx = Math.cos(h), rz = -Math.sin(h);
        for (let i = 0; i < pts.length; i += 3) {
          const X = pts[i], Y = pts[i + 1], Z = pts[i + 2];
          pts[i] = pose.x / 1000 + X * rx + Z * fx; pts[i + 1] = SL_CAM_H + Y; pts[i + 2] = pose.y / 1000 + X * rz + Z * fz;
        }
      }
      let cx = 0, cy = 0, cz = 0; const m = pts.length / 3 || 1;
      for (let i = 0; i < pts.length; i += 3) { cx += pts[i]; cy += pts[i + 1]; cz += pts[i + 2]; }
      if (live) {   // 지금 윤곽 (2026-11-11): redrawn every frame at full brightness, never sags; one buffer, refilled
        if (!liveBatch) liveBatch = {buf: gl.createBuffer()};
        gl.bindBuffer(gl.ARRAY_BUFFER, liveBatch.buf); gl.bufferData(gl.ARRAY_BUFFER, pts, gl.DYNAMIC_DRAW);
        Object.assign(liveBatch, {count: pts.length / 3, center: [cx / m, cy / m, cz / m], born: now});
        return;
      }
      const buf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buf); gl.bufferData(gl.ARRAY_BUFFER, pts, gl.STATIC_DRAW);
      batches.push({buf, count: pts.length / 3, center: [cx / m, cy / m, cz / m], born: now});
      status.made.batches++; status.made.segments += pts.length / 6;
      let total = batches.reduce((s, b) => s + b.count / 2, 0);
      while (batches.length > SL_LIMITS.batches || total > SL_LIMITS.segTotal) { const b = batches.shift(); total -= b.count / 2; dropBatch(b); status.dropped.cap++; }
      status.build_ms = performance.now() - t0;
    }
    function record2(f, p, now) {   // 거리선 live buffer (layered 적외선 윤곽): camera space only (pose transform like record)
      const seg = slSegments(f, p), pose = poseNow(p, f), pts = new Float32Array(seg);
      if (pose) {
        const h = pose.heading * Math.PI / 180, fx = Math.sin(h), fz = Math.cos(h), rx = Math.cos(h), rz = -Math.sin(h);
        for (let i = 0; i < pts.length; i += 3) { const X = pts[i], Y = pts[i + 1], Z = pts[i + 2]; pts[i] = pose.x / 1000 + X * rx + Z * fx; pts[i + 1] = SL_CAM_H + Y; pts[i + 2] = pose.y / 1000 + X * rz + Z * fz; }
      }
      if (!liveIso) liveIso = {buf: gl.createBuffer(), center: [0, 0, 0]};
      gl.bindBuffer(gl.ARRAY_BUFFER, liveIso.buf); gl.bufferData(gl.ARRAY_BUFFER, pts, gl.DYNAMIC_DRAW);
      liveIso.count = pts.length / 3;
      // 색 범위 자동 (auto_color): near/far of what is seen now (5th / 95th percentile of the line depths), followed slowly
      if (p.auto_color && seg.length >= 60) {
        const zs = []; for (let i = 2; i < seg.length; i += 6 * Math.max(1, Math.floor(seg.length / 6 / 400))) zs.push(seg[i]);
        zs.sort((a, b) => a - b);
        const lo = zs[Math.floor(zs.length * 0.05)], hi = zs[Math.floor(zs.length * 0.95)], k = 0.08;
        colorRange = colorRange ? [colorRange[0] + (lo - colorRange[0]) * k, colorRange[1] + (Math.max(hi, lo + 0.3) - colorRange[1]) * k] : [lo, Math.max(hi, lo + 0.3)];
      }
    }
    function focusFrom(f, p) {   // Z (m) of the ToF pixels around the focus point (median of a 7×7 window), else the last one
      const u0 = Math.round(p.focus_x / 100 * (f.w - 1)), v0 = Math.round(p.focus_y / 100 * (f.h - 1)), zs = [];
      for (let v = Math.max(0, v0 - 3); v <= Math.min(f.h - 1, v0 + 3); v++) for (let u = Math.max(0, u0 - 3); u <= Math.min(f.w - 1, u0 + 3); u++) { const r = f.depth[v * f.w + u]; if (r) zs.push(slCam(u, v, r)[2]); }
      if (zs.length) { zs.sort((a, b) => a - b); focusZ = zs[zs.length >> 1]; }
      return Math.max(0.2, focusZ + p.focus_shift);
    }
    function draw(buf, layer) {
      const [cw, ch] = sizeOf();
      if (canvas.width !== cw || canvas.height !== ch) { canvas.width = cw; canvas.height = ch; }
      ctx.fillStyle = '#000'; ctx.fillRect(0, 0, cw, ch);
      const f = parse(buf), p = Object.assign({}, SPACELINE_DEFAULTS, layer && layer.params), now = performance.now();
      const s = Math.min(cw / f.w, ch / f.h), r = {s, x: (cw - f.w * s) / 2, y: (ch - f.h * s) / 2, iw: f.w * s, ih: f.h * s};
      if (!gl) {   // say so instead of drawing something else
        ctx.fillStyle = '#ff8a7a'; ctx.font = `${Math.max(12, ch / 30)}px sans-serif`; ctx.fillText('WebGL을 쓸 수 없어 공간선을 그릴 수 없음', 12, ch / 2);
        return {f, r};
      }
      const pose = poseNow(p, f), mode = pose ? 'world' : 'local';
      status.pose_src = pose ? (pose.quality !== undefined ? 'tof' : 'sim') : 'camera';
      if (mode !== space || p.method !== method) { reset(`${space}→${mode} ${method}→${p.method}`); space = mode; method = p.method; }   // one method, one space at a time
      const life = p.hold_s + p.sag_s + p.fade_s;
      while (batches.length && now - batches[0].born > life * 1000) { dropBatch(batches.shift()); status.dropped.life++; }
      const fresh = f.id !== lastFrame;
      lastFrame = f.id;
      const split = p.ghost != null && p.ghost < 1;   // 잔상 밝기 < 1: the live outline every frame + dim ghosts every record_s
      if (split && fresh && f.depth) record(f, p, now, true);
      if (!split && liveBatch) liveBatch.count = 0;
      if (fresh && f.depth && now - lastRec >= p.record_s * 1000) { record(f, p, now); lastRec = now; }
      const fz = f.depth ? focusFrom(f, p) : focusZ;
      // render at most ~1.5 device px per CSS px so the 1-px lines stay visible on dense screens
      // × 그리기 해상도 (res, 2026-11-10: a slower GPU such as the Pi 5's draws fewer pixels, then the picture is enlarged)
      const dpr = window.devicePixelRatio || 1, k = (dpr > 1.5 ? 1.5 / dpr : 1) * Math.min(1, Math.max(0.25, p.res || 1));
      const w = Math.max(1, Math.round(r.iw * k)), h = Math.max(1, Math.round(r.ih * k));
      if (glc.width !== w || glc.height !== h) { glc.width = w; glc.height = h; }
      const t0 = performance.now();
      // view: 혈's camera now (venue space) or the sensor itself (camera space); projection: the sensor's intrinsics
      let view = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, -1, 0, 0, 0, 0, 1];   // camera space (z forward) → GL eye (−z forward)
      if (pose) {
        const hd = pose.heading * Math.PI / 180, sx = Math.sin(hd), cxh = Math.cos(hd), px = pose.x / 1000, pz = pose.y / 1000;
        // eye = (X, Y, −Z): X = right·(p − c), Y = p.y − h, Z = forward·(p − c); column-major
        view = [cxh, 0, -sx, 0, 0, 1, 0, 0, -sx, 0, -cxh, 0, -(cxh * px - sx * pz), -SL_CAM_H, (sx * px + cxh * pz), 1];
      }
      const n = 0.05, fr = 30, ax = SL_FX / (SL_W / 2), ay = SL_FX / (SL_H / 2);
      const proj = [ax, 0, 0, 0, 0, ay, 0, 0, 0, 0, -(fr + n) / (fr - n), -1, 0, 0, -2 * fr * n / (fr - n), 0];
      const ga = p.gravity_deg * Math.PI / 180, grav = [Math.sin(ga), Math.cos(ga)];
      const near = hexRgb(p.color_near).map(v => v / 255), far = hexRgb(p.color_far).map(v => v / 255);
      const mid = p.color_mid ? hexRgb(p.color_mid).map(v => v / 255) : near.map((v, i) => (v + far[i]) / 2);   // 가운데 색 (없으면 두 색 사이 = 예전과 같음)
      const blur = p.blur * (w / (r.iw / dpr));   // px at CSS size → render px
      // 선 두께 (2026-11-11): WebGL draws 1-px lines only, so a wider line = the same lines again shifted by a pixel
      const lw = Math.max(1, Math.round((p.line_w || 1) * Math.min(1, w / Math.max(1, r.iw)) )), offs = [];
      for (let a = 0; a < lw; a++) for (let b2 = 0; b2 < lw; b2++) offs.push([(a - (lw - 1) / 2) * 2 / w, (b2 - (lw - 1) / 2) * 2 / h]);
      const drawLines = (pr, count) => { for (const o of offs) { gl.uniform2f(pr.u.uOff, o[0], o[1]); gl.drawArrays(gl.LINES, 0, count); } };
      const pass = (pr, toTarget) => {
        gl.bindFramebuffer(gl.FRAMEBUFFER, toTarget ? toTarget.f : null);
        gl.viewport(0, 0, w, h); gl.clearColor(0, 0, 0, toTarget === (fb && fb.depth) ? 0 : 1); gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
        gl.enable(gl.DEPTH_TEST); gl.depthFunc(gl.LEQUAL);
        gl.useProgram(pr.p);
        gl.uniformMatrix4fv(pr.u.uView, false, view); gl.uniformMatrix4fv(pr.u.uProj, false, proj);
        gl.uniform2fv(pr.u.uGrav, grav); gl.uniform1f(pr.u.uDrop, p.gravity); gl.uniform1f(pr.u.uClump, p.clump);
        if (pr === prog.color) { gl.uniform3fv(pr.u.uNear, near); gl.uniform3fv(pr.u.uMid, mid); gl.uniform3fv(pr.u.uFar, far); gl.uniform1f(pr.u.uFog, p.fog || 0); const abs = [f.near / 1000, f.far / 1000], wm = p.auto_mix == null ? 1 : p.auto_mix;
          // 색 범위 = 고정 범위(near–far)와 장면 기준 범위를 auto_mix 만큼 섞음, 최소 폭 1 m (먼 것만 보일 때 흰색이 되지 않게)
          let zr = p.auto_color && colorRange ? [abs[0] + (colorRange[0] - abs[0]) * wm, abs[1] + (colorRange[1] - abs[1]) * wm] : abs;
          if (p.auto_color) zr = [zr[0], Math.max(zr[1], zr[0] + (p.auto_span_min || 1))]; gl.uniform1f(pr.u.uZ0, Math.max(0.2, zr[0])); gl.uniform1f(pr.u.uZ1, Math.max(zr[0] + 0.2, zr[1])); gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA); }
        else gl.disable(gl.BLEND);
        const split = p.ghost != null && p.ghost < 1;
        if (split) gl.depthMask(false);   // ghosts never hide anything (they used to cut dark gaps into brighter lines)
        for (const b of batches) {   // oldest first; the depth test decides what is in front
          const age = (now - b.born) / 1000;
          let kk, alpha;
          if (split) {   // (2026-11-11) a ghost keeps moving away the whole time it fades: one motion over sag + fade, no stop
            const life = Math.max(0.01, p.sag_s + p.fade_s), u = Math.min(1, Math.max(0, (age - p.hold_s) / life));
            kk = Math.sqrt(u) * 1.0;   // the shader drops by kk², so the drop grows linearly in time: a steady drift
            alpha = p.ghost * (1 - u) * (1 - u * 0.3);
          } else {
            kk = Math.min(1, Math.max(0, (age - p.hold_s) / Math.max(0.01, p.sag_s)));
            alpha = age < p.hold_s + p.sag_s ? 1 : Math.max(0, 1 - (age - p.hold_s - p.sag_s) / Math.max(0.01, p.fade_s));
          }
          if (alpha <= 0.02) continue;
          gl.uniform1f(pr.u.uK, kk); gl.uniform3fv(pr.u.uCenter, b.center);
          if (pr.u.uAlpha) gl.uniform1f(pr.u.uAlpha, alpha);
          gl.bindBuffer(gl.ARRAY_BUFFER, b.buf); gl.enableVertexAttribArray(pr.a); gl.vertexAttribPointer(pr.a, 3, gl.FLOAT, false, 0, 0);
          drawLines(pr, b.count);
        }
        gl.depthMask(true);
        if (split && liveIso && liveIso.count && p.method === 'ir_edge') {   // distance lines: dim, under the outlines, no ghosts
          gl.depthMask(false);
          gl.uniform1f(pr.u.uK, 0); gl.uniform3fv(pr.u.uCenter, liveIso.center);
          if (pr.u.uAlpha) gl.uniform1f(pr.u.uAlpha, p.iso_alpha == null ? 1 : p.iso_alpha);
          gl.bindBuffer(gl.ARRAY_BUFFER, liveIso.buf); gl.enableVertexAttribArray(pr.a); gl.vertexAttribPointer(pr.a, 3, gl.FLOAT, false, 0, 0);
          gl.uniform2f(pr.u.uOff, 0, 0); gl.drawArrays(gl.LINES, 0, liveIso.count);
          gl.depthMask(true);
        }
        if (split && liveBatch && liveBatch.count) {   // the live outline last, at full brightness, not sagging
          gl.uniform1f(pr.u.uK, 0); gl.uniform3fv(pr.u.uCenter, liveBatch.center);
          if (pr.u.uAlpha) gl.uniform1f(pr.u.uAlpha, p.edge_alpha == null ? 1 : p.edge_alpha);   // 윤곽 밝기 (2026-11-12)
          gl.bindBuffer(gl.ARRAY_BUFFER, liveBatch.buf); gl.enableVertexAttribArray(pr.a); gl.vertexAttribPointer(pr.a, 3, gl.FLOAT, false, 0, 0);
          drawLines(pr, liveBatch.count);
        }
      };
      if (blur < 0.5) pass(prog.color, null);
      else {
        const t = target(w, h);
        pass(prog.color, t.color); pass(prog.depth, t.depth);
        gl.bindFramebuffer(gl.FRAMEBUFFER, null); gl.viewport(0, 0, w, h); gl.disable(gl.DEPTH_TEST); gl.disable(gl.BLEND);
        const d = prog.dof; gl.useProgram(d.p);
        gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, t.color.tex); gl.uniform1i(d.u.uColor, 0);
        gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, t.depth.tex); gl.uniform1i(d.u.uDepth, 1);
        gl.uniform2f(d.u.uPx, 1 / w, 1 / h); gl.uniform1f(d.u.uFocus, fz); gl.uniform1f(d.u.uRange, p.focus_range); gl.uniform1f(d.u.uMaxR, blur);
        gl.bindBuffer(gl.ARRAY_BUFFER, prog.quad); gl.enableVertexAttribArray(d.a); gl.vertexAttribPointer(d.a, 2, gl.FLOAT, false, 0, 0);
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      }
      status.gpu_ms = performance.now() - t0;
      ctx.imageSmoothingEnabled = true;
      ctx.drawImage(glc, r.x, r.y, r.iw, r.ih);
      Object.assign(status, {mode, method: p.method, batches: batches.length, segments: batches.reduce((s2, b) => s2 + b.count / 2, 0), focus_z: fz, life_s: life});
      return {f, r};
    }
    function clear() {
      const [cw, ch] = sizeOf();
      if (canvas.width !== cw || canvas.height !== ch) { canvas.width = cw; canvas.height = ch; }
      ctx.fillStyle = '#000'; ctx.fillRect(0, 0, canvas.width, canvas.height);
    }
    function destroy() { reset(); const ext = gl && gl.getExtension('WEBGL_lose_context'); if (ext) ext.loseContext(); }
    return {draw, clear, reset, destroy, status, kind: 'spaceline'};
  }

  // ============================================================================================================
  // 평면 점 흔적 (TOP-DOWN TRACE, experiment, 2026-10-30): the ToF depth seen from above. Each depth sample (every
  // "step" px) becomes a camera point (slCam, the same conversion as 공간선); points between z_min and z_max above the
  // floor are laid flat on a venue map with 혈's SIMULATOR pose (ToF 1.5 m high, level) — the pose is the simulation's,
  // not something estimated from the ToF frames (real-sensor localisation does not exist). Every stamp_s the current
  // points are added ("lighter") onto a map canvas fixed in the venue (20 mm per px), so passing the same place again
  // piles new points over the old ones (no exact alignment is attempted); the map fades by fade_s (0 = keeps). The
  // current points are drawn on top, plus 혈's marker and its 70° view. 천장 거리 trace (separate, optional): the
  // up_distance reading (혈 → ceiling) left as coloured dots on the path 혈 travelled (not the time graph). Without a
  // simulator pose (real input) everything is drawn in camera space only (sensor at the bottom, looking up), no map.
  // Colours, piling and lifetimes are experiment settings, not a final design.
  // map resolution: res_mm per map pixel (setting, 10–40 mm, default 10); the current points use the same pixels
  const TOPDOWN_DEFAULTS = {view: 'follow', range_m: 10, step: 3, mark_mode: 'points', contour_mm: 250, z_min: 0.15, z_max: 2.6, stamp_s: 0.2, stamp: 0.25, fade_s: 60,
    size: 1.5, color_now: '#ffffff', color_trace: '#6fa8ff', ceil_trace: true, ceil_max: 6, ceil_near: '#ffd27a', ceil_far: '#6a3fa0',
    still_stop: true, still_mm: 100, still_deg: 5, fov: 'shade',
    trace_style: 'add', fade_style: 'uniform', age_s: 30, color_old: '#5a2d91', drift_mm: 150, res_mm: 10};
  const TD_DIR = Array.from({length: 256}, (_, k) => [Math.cos(k / 256 * 2 * Math.PI), Math.sin(k / 256 * 2 * Math.PI)]);
  // (2026-10-30) the current points are drawn on the same map grid and pixel size as the trace (one map pixel each, the
  // map scaled up without smoothing), so the live points and the trace look like the same pixels in different colours.
  // (2026-10-30, experiments 3 / 4) trace_style 'age' = 시간 색 지층: a map pixel takes the colour of WHEN it was last
  // stamped (new → 흔적 색 → old over age_s) at a fixed brightness, so passing again repaints it new instead of adding
  // light (no grey saturation). fade_style 'dissolve' = 흩어지며 사라짐: each pixel drops out at its own random moment of
  // its life (fade_s) and drifts up to drift_mm outward first, instead of the whole map dimming evenly. Defaults ('add',
  // 'uniform') are the original canvas path, unchanged; any other choice uses per-pixel buffers (time, hits) redrawn
  // into the map every 150 ms.
  // (2026-10-30) the trace is stamped only after 혈 moved still_mm or turned still_deg since the last stamp (still_stop);
  // the 천장 거리 trace is stamped into the same map with the same pixel and colour as the points, its distance shown as
  // brightness (nearer ceiling = brighter). ceil_near / ceil_far are kept in saved layers but no longer drawn.
  function createTopDown(canvas, opts = {}) {
    const ctx = canvas.getContext('2d');
    const sizeOf = opts.size || (() => { const dpr = window.devicePixelRatio || 1; return [Math.round(canvas.clientWidth * dpr), Math.round(canvas.clientHeight * dpr)]; });
    simLive.start();
    const map = document.createElement('canvas'), mctx = map.getContext('2d');
    const status = {mode: 'starting', points: 0, stamps: 0, ceil: 0, still: false};
    let space = null, area = null, now_pts = new Float32Array(0), now_lines = new Float32Array(0), lastFrame = null, lastGeometry = '', lastStamp = -Infinity, lastFade = 0, stampPose = null;
    let pix = null, path = null, lastPaint = 0;   // per-pixel buffers for the 'age' / 'dissolve' styles
    let res = TOPDOWN_DEFAULTS.res_mm;
    function reset() {
      mctx.clearRect(0, 0, map.width, map.height); stampPose = null; lastStamp = -Infinity; status.stamps = status.ceil = 0;
      if (pix) { pix.t.fill(-1); pix.hits.fill(0); pix.ceil.fill(0); pix.act = []; pix.drawn = []; pix.img.data.fill(0); }
    }
    function buffers() {
      const n = map.width * map.height;
      if (pix && pix.n === n) return pix;
      const rnd = new Uint8Array(n), dir = new Uint8Array(n);
      for (let i = 0; i < n; i++) { rnd[i] = 1 + Math.floor(Math.random() * 254); dir[i] = Math.floor(Math.random() * 256); }
      // act = indices of pixels holding a trace (only those are painted), drawn = pixels written by the last paint
      pix = {n, t: new Float32Array(n).fill(-1), hits: new Uint8Array(n), ceil: new Uint8Array(n), mark: new Uint16Array(n), stampNo: 0, rnd, dir, act: [], drawn: [], img: mctx.createImageData(map.width, map.height)};
      return pix;
    }
    function paint(p, nowS) {   // buffers → map pixels
      const b = buffers(), W = map.width, H = map.height, d = b.img.data, age = p.trace_style === 'age', dissolve = p.fade_style === 'dissolve';
      const cNew = hexRgb(p.color_now), cMid = hexRgb(p.color_trace), cOld = hexRgb(p.color_old), drift = p.drift_mm / res;
      for (const o of b.drawn) d[o + 3] = 0;   // clear only what the last paint wrote
      const keep = [], drawn = [];
      for (const i of b.act) {
        const t = b.t[i];
        if (t < 0) continue;
        const ageS = nowS - t, life = p.fade_s > 0 ? ageS / p.fade_s : 0;
        if (p.fade_s > 0 && (life >= 1 || (dissolve && life > b.rnd[i] / 255))) { b.t[i] = -1; b.hits[i] = 0; b.ceil[i] = 0; continue; }
        keep.push(i);
        let a = Math.min(1, 0.3 + b.hits[i] * p.stamp);   // overlaps → brighter (both styles; 지층 = colour by time, brightness by overlaps)
        a = Math.max(a, b.ceil[i] / 255 * (age ? 1 : 1));
        if (!dissolve) a *= 1 - life;
        let c = cMid;
        if (age) {
          const u = Math.min(1, ageS / Math.max(0.1, p.age_s));
          const [x, y, w] = u < 0.5 ? [cNew, cMid, u * 2] : [cMid, cOld, (u - 0.5) * 2];
          c = [x[0] + (y[0] - x[0]) * w, x[1] + (y[1] - x[1]) * w, x[2] + (y[2] - x[2]) * w];
        }
        let j = i;
        if (dissolve && drift > 0 && life > 0.3) {   // drift outward in its own direction late in its life
          const s2 = drift * ((life - 0.3) / 0.7) ** 2, dv = TD_DIR[b.dir[i]], x = (i % W) + dv[0] * s2, y = ((i / W) | 0) + dv[1] * s2;
          if (x < 0 || y < 0 || x >= W || y >= H) continue;
          j = (y | 0) * W + (x | 0);
        }
        const o = j * 4, al = Math.round(a * 255);
        if (al <= d[o + 3]) continue;   // the brighter one wins where two land on one pixel
        if (!d[o + 3]) drawn.push(o);
        d[o] = c[0]; d[o + 1] = c[1]; d[o + 2] = c[2]; d[o + 3] = al;
      }
      b.act = keep; b.drawn = drawn;
      mctx.putImageData(b.img, 0, 0);
    }
    function setArea(a) {   // the map's extent in mm (venue bounds, or a box around the sensor in camera space)
      area = a; map.width = Math.max(1, Math.round((a.x1 - a.x0) / res)); map.height = Math.max(1, Math.round((a.y1 - a.y0) / res)); pix = null; reset();
    }
    function points(f, p, pose) {   // world (x, y) mm of the depth samples within the height band
      const out = [], s = Math.max(1, Math.round(p.step)), h = pose.heading * Math.PI / 180;
      const fx = Math.sin(h), fz = Math.cos(h), rx = Math.cos(h), rz = -Math.sin(h);
      for (let v = 0; v < f.h; v += s) for (let u = 0; u < f.w; u += s) {
        const r = f.depth[v * f.w + u];
        if (!r) continue;
        const [X, Y, Z] = slCam(u, v, r), height = SL_CAM_H + Y;
        if (height < p.z_min || height > p.z_max) continue;
        out.push(pose.x + (X * rx + Z * fx) * 1000, pose.y + (X * rz + Z * fz) * 1000);
      }
      return new Float32Array(out);
    }
    function contourLines(f, p, pose) {
      // Reuse the depth-image marching squares used by 공간선. Each endpoint is at the same measured radial distance;
      // laying the camera-space segments on the floor makes incomplete sensor-centred range arcs, not invented full rings.
      const seg = slSegments(f, {method: 'contour', interval_mm: p.contour_mm});
      const out = [], h = pose.heading * Math.PI / 180;
      const fx = Math.sin(h), fz = Math.cos(h), rx = Math.cos(h), rz = -Math.sin(h);
      const n = seg.length / 6, stride = Math.max(1, Math.ceil(n / 6000));
      for (let j = 0; j < n; j += stride) {
        const i = j * 6, z0 = SL_CAM_H + seg[i + 1], z1 = SL_CAM_H + seg[i + 4];
        if (z0 < p.z_min || z0 > p.z_max || z1 < p.z_min || z1 > p.z_max) continue;
        out.push(pose.x + (seg[i] * rx + seg[i + 2] * fx) * 1000,
                 pose.y + (seg[i] * rz + seg[i + 2] * fz) * 1000,
                 pose.x + (seg[i + 3] * rx + seg[i + 5] * fx) * 1000,
                 pose.y + (seg[i + 3] * rz + seg[i + 5] * fz) * 1000);
      }
      return new Float32Array(out);
    }
    function eachMarkPixel(p, cb) {
      if (p.mark_mode === 'contour') {
        for (let i = 0; i < now_lines.length; i += 4) {
          const [x0, y0] = toMap(now_lines[i], now_lines[i + 1]), [x1, y1] = toMap(now_lines[i + 2], now_lines[i + 3]);
          const steps = Math.min(100, Math.max(1, Math.ceil(Math.hypot(x1 - x0, y1 - y0))));
          for (let j = 0; j <= steps; j++) cb(x0 + (x1 - x0) * j / steps, y0 + (y1 - y0) * j / steps);
        }
      } else for (let i = 0; i < now_pts.length; i += 2) cb(...toMap(now_pts[i], now_pts[i + 1]));
    }
    const toMap = (x, y) => [(x - area.x0) / res, (area.y1 - y) / res];   // +y (simulator) = up on the map
    function draw(buf, layer) {
      const [cw, ch] = sizeOf();
      if (canvas.width !== cw || canvas.height !== ch) { canvas.width = cw; canvas.height = ch; }
      const f = parse(buf), p = Object.assign({}, TOPDOWN_DEFAULTS, layer && layer.params), now = performance.now();
      const sim = hub.inputs && hub.inputs.mock === true ? simLive.pose : null, mode = sim ? 'world' : 'local';
      const pose = sim || {x: 0, y: 0, heading: 0};
      if (p.res_mm !== res) { res = p.res_mm; space = null; }   // another resolution: a new (empty) map
      if (mode !== space) {
        space = mode;
        const b = simLive.bounds;
        setArea(mode === 'world' && b ? {x0: b.x0, x1: b.x1, y0: b.y0, y1: b.y1} : {x0: -6000, x1: 6000, y0: -1000, y1: 11000});
      }
      const geom = [p.mark_mode, p.contour_mm, p.step, p.z_min, p.z_max].join('|');
      const geometryChanged = geom !== lastGeometry;
      if (geometryChanged) { lastGeometry = geom; reset(); }
      if (f.depth && (f.id !== lastFrame || geometryChanged)) {
        lastFrame = f.id;
        if (p.mark_mode === 'contour') { now_lines = contourLines(f, p, pose); now_pts = new Float32Array(0); status.points = now_lines.length / 4; }
        else { now_pts = points(f, p, pose); now_lines = new Float32Array(0); status.points = now_pts.length / 2; }
      }
      const buffered = p.trace_style !== 'add' || p.fade_style !== 'uniform';
      if ((buffered ? 'buf' : 'canvas') !== path) { path = buffered ? 'buf' : 'canvas'; if (buffered) buffers(); reset(); }
      // map: fade, then add the current points every stamp_s
      if (!buffered && p.fade_s > 0 && now - lastFade > 100) {
        const dt = lastFade ? (now - lastFade) / 1000 : 0; lastFade = now;
        mctx.globalCompositeOperation = 'destination-out'; mctx.fillStyle = `rgba(0,0,0,${Math.min(1, 3 * dt / p.fade_s)})`;
        mctx.fillRect(0, 0, map.width, map.height);
      }
      // no big movement since the last stamp (still_stop): the trace is not added to (the current points stay visible)
      const turned = stampPose ? Math.abs(((pose.heading - stampPose.heading + 540) % 360) - 180) : Infinity;
      const moved = !stampPose || Math.hypot(pose.x - stampPose.x, pose.y - stampPose.y) >= p.still_mm || turned >= p.still_deg;
      status.still = mode === 'world' && p.still_stop && !moved;
      if (mode === 'world' && now - lastStamp >= p.stamp_s * 1000 && (now_pts.length || now_lines.length) && (moved || !p.still_stop)) {
        lastStamp = now; status.stamps++; stampPose = {x: pose.x, y: pose.y, heading: pose.heading};
        const up = hub.sample('sensor.up_distance'), ceilOk = p.ceil_trace && up && up.connected && typeof up.value === 'number';
        const ceilA = ceilOk ? 1 - 0.85 * Math.min(1, Math.max(0, up.value / p.ceil_max)) : 0;
        if (buffered) {
          const b = buffers(), W = map.width, H = map.height, nowS = now / 1000, no = b.stampNo = (b.stampNo + 1) % 65536 || 1;
          const put = (x, y) => { x = Math.floor(x); y = Math.floor(y); if (x < 0 || y < 0 || x >= W || y >= H) return -1; const i = y * W + x; if (b.t[i] < 0) b.act.push(i); b.t[i] = nowS; return i; };
          eachMarkPixel(p, (mx, my) => { const j = put(mx, my); if (j >= 0 && b.mark[j] !== no) { b.mark[j] = no; if (b.hits[j] < 255) b.hits[j]++; } });
          if (ceilOk) { const [mx, my] = toMap(pose.x, pose.y), j = put(mx, my); if (j >= 0) { b.ceil[j] = Math.round(ceilA * 255); status.ceil++; } }
        } else {
          mctx.globalCompositeOperation = 'lighter'; mctx.globalAlpha = p.stamp; mctx.fillStyle = p.color_trace;
          eachMarkPixel(p, (mx, my) => mctx.fillRect(Math.floor(mx), Math.floor(my), 1, 1));
          // 천장 거리 trace: the same pixel and colour at 혈's place, brightness = how near the ceiling is
          if (ceilOk) { const [mx, my] = toMap(pose.x, pose.y); mctx.globalAlpha = ceilA; mctx.fillRect(Math.floor(mx), Math.floor(my), 1, 1); status.ceil++; }
          mctx.globalAlpha = 1;
        }
      }
      if (buffered && now - lastPaint > 150) { lastPaint = now; paint(p, now / 1000); }
      // view: the whole venue map, or a window of range_m around 혈 (north up)
      ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.globalCompositeOperation = 'source-over'; ctx.globalAlpha = 1;
      ctx.fillStyle = '#000'; ctx.fillRect(0, 0, cw, ch);
      let cx, cy, k;   // screen px per map px, map point at the screen centre
      if (p.view === 'venue' || mode === 'local') {
        k = Math.min(cw / map.width, ch / map.height); cx = map.width / 2; cy = map.height / 2;
        if (mode === 'local') { k = Math.min(cw, ch) / (p.range_m * 1000 / res); [cx, cy] = toMap(0, p.range_m * 1000 * 0.45); }
      } else { k = Math.min(cw, ch) / (p.range_m * 1000 / res); [cx, cy] = toMap(pose.x, pose.y); }
      const S = (mx, my) => [cw / 2 + (mx - cx) * k, ch / 2 + (my - cy) * k];
      ctx.imageSmoothingEnabled = false;   // crisp map pixels, the same as the current points
      const [ox, oy] = S(0, 0);
      ctx.drawImage(map, ox, oy, map.width * k, map.height * k);
      // the sensor's view (fov 'shade'): a sector to the 4 m range that fades with distance, 1 m distance arcs and edges
      // that fade out — under the current points; no device marker
      if (p.fov !== 'none') {
        const [dx, dy] = S(...toMap(pose.x, pose.y)), L = 4000 / res * k, half = SL_HFOV / 2 * Math.PI / 180;
        const a0 = (pose.heading - 90) * Math.PI / 180 - half, a1 = a0 + 2 * half, lw = Math.max(1, (window.devicePixelRatio || 1));
        const g = ctx.createRadialGradient(dx, dy, 0, dx, dy, L);
        g.addColorStop(0, 'rgba(255,255,255,.16)'); g.addColorStop(0.6, 'rgba(255,255,255,.06)'); g.addColorStop(1, 'rgba(255,255,255,0)');
        ctx.fillStyle = g; ctx.beginPath(); ctx.moveTo(dx, dy); ctx.arc(dx, dy, L, a0, a1); ctx.closePath(); ctx.fill();
        ctx.lineWidth = lw;
        for (let m = 1; m <= 4; m++) {
          ctx.strokeStyle = `rgba(255,255,255,${0.22 - 0.04 * m})`; ctx.setLineDash(m === 4 ? [] : [3 * lw, 4 * lw]);
          ctx.beginPath(); ctx.arc(dx, dy, L * m / 4, a0, a1); ctx.stroke();
        }
        ctx.setLineDash([]);
        for (const a of [a0, a1]) {
          const e = ctx.createLinearGradient(dx, dy, dx + L * Math.cos(a), dy + L * Math.sin(a));
          e.addColorStop(0, 'rgba(255,255,255,.45)'); e.addColorStop(1, 'rgba(255,255,255,0)');
          ctx.strokeStyle = e; ctx.beginPath(); ctx.moveTo(dx, dy); ctx.lineTo(dx + L * Math.cos(a), dy + L * Math.sin(a)); ctx.stroke();
        }
      }
      // current points (this frame)
      // current points (this frame): one map pixel each, on the map grid — the same pixels as the trace
      // (they add their light: where they fall on the trace the pixel gets brighter)
      ctx.globalCompositeOperation = 'lighter'; ctx.fillStyle = p.color_now; const sz = Math.max(1, k);
      eachMarkPixel(p, (mx, my) => { const [sx, sy] = S(Math.floor(mx), Math.floor(my)); ctx.fillRect(sx, sy, sz, sz); });
      ctx.globalCompositeOperation = 'source-over';
      status.mode = mode;
      status.shape = p.mark_mode;
      return {f, r: {s: 1}};
    }
    function clear() {
      const [cw, ch] = sizeOf();
      if (canvas.width !== cw || canvas.height !== ch) { canvas.width = cw; canvas.height = ch; }
      ctx.fillStyle = '#000'; ctx.fillRect(0, 0, canvas.width, canvas.height);
    }
    function destroy() { reset(); }
    return {draw, clear, reset, destroy, status, kind: 'topdown'};
  }

  // ============================================================================================================
  // 깎이는 매스 (CARVED MASS, experiment, 2026-11-02): a solid block in front of the camera, in camera space (a box from
  // front_m to front_m + depth_m, as wide as the sensor's 70° view at its middle depth, so it moves with the camera;
  // each cell = one sensor direction, carved along the view axis), carved by what the ToF
  // reads. The sensor grid is cut into cells (step × step pixels); each cell keeps how deep it has been cut (m, from the
  // camera side). Every new depth frame, per cell, from the nearest measured pixel in it:
  //   press = "가까운 만큼 눌러 깎기": cut to depth_m × closeness between start_m (깎기 시작 거리: farther = no cut, so the
  //           floor / far walls leave the block whole) and the ToF near limit (= the whole depth), like pressing in;
  //   space = "보인 빈 공간만큼 깎기": cut to where the camera sees free space, Z − front_m (the space in front of a
  //           measured surface is known to be empty).
  // trace 'regrow': cuts only deepen; regrow_s > 0 fills the mass back at depth_m per regrow_s seconds (0 = cuts stay).
  // trace 'now' (남기지 않음): every frame the block is cut by the current frame only, no trace left. A cell with no
  // measurement is not cut (unknown, never invented). Drawn with Canvas 2D from an oblique view (yaw / pitch, 0 / 0 =
  // from the camera): the carved front surface (smoothed between cells) plus side walls, painter's order, one light;
  // carved parts tinted toward color_cut by how deep they are cut. These are accumulated sensor readings, not learned.
  const MASS_DEFAULTS = {method: 'press', start_m: 2.5, trace: 'regrow', front_m: 1.0, depth_m: 1.5, step: 3, regrow_s: 10, yaw: 35, pitch: 20,
    color: '#d8d0c0', color_cut: '#c0502e'};
  function createMass(canvas, opts = {}) {
    const ctx = canvas.getContext('2d');
    const sizeOf = opts.size || (() => { const dpr = window.devicePixelRatio || 1; return [Math.round(canvas.clientWidth * dpr), Math.round(canvas.clientHeight * dpr)]; });
    let cut = null, gw = 0, gh = 0, gstep = 0, lastId = null, lastAt = 0;
    const status = {cells: 0, cutCells: 0, through: 0, maxCut: 0};
    function reset() { cut = null; lastId = null; }
    function update(f, p) {
      const step = Math.max(2, Math.round(p.step)), T = Math.max(0.05, p.depth_m), F = Math.max(0.05, p.front_m);
      const w = Math.ceil(f.w / step), h = Math.ceil(f.h / step);
      if (!cut || w !== gw || h !== gh || step !== gstep) { cut = new Float32Array(w * h); gw = w; gh = h; gstep = step; }
      const now = performance.now(), dt = lastAt ? Math.min(1, (now - lastAt) / 1000) : 0;
      lastAt = now;
      const now_only = p.trace === 'now';
      if (now_only) cut.fill(0);
      else if (p.regrow_s > 0 && dt) { const k = T * dt / p.regrow_s; for (let i = 0; i < cut.length; i++) cut[i] = Math.max(0, cut[i] - k); }
      const S = p.start_m * 1000, span = Math.max(1, S - f.near);
      for (let cy = 0; cy < h; cy++) for (let cx = 0; cx < w; cx++) {
        let best = 0, bu = 0, bv = 0;   // nearest measured pixel in the cell
        for (let v = cy * step; v < Math.min(f.h, cy * step + step); v++) for (let u = cx * step; u < Math.min(f.w, cx * step + step); u++) {
          const d = f.depth[v * f.w + u];
          if (d && (!best || d < best)) { best = d; bu = u; bv = v; }
        }
        if (!best) continue;
        const target = p.method === 'space' ? slCam(bu * SL_W / f.w, bv * SL_H / f.h, best)[2] - F : T * Math.min(1, (S - best) / span);
        const i = cy * w + cx;
        cut[i] = Math.max(cut[i], Math.min(T, Math.max(0, target)));
      }
    }
    function draw(buf, layer) {
      const f = parse(buf), p = Object.assign({}, MASS_DEFAULTS, layer && layer.params);
      const [cw, ch] = sizeOf();
      if (canvas.width !== cw || canvas.height !== ch) { canvas.width = cw; canvas.height = ch; }
      ctx.fillStyle = '#000'; ctx.fillRect(0, 0, cw, ch);
      if (!f.depth) return {f, r: {s: 1}};
      if (f.id !== lastId) { update(f, p); lastId = f.id; }
      const T = Math.max(0.05, p.depth_m), F = Math.max(0.05, p.front_m), w = gw, h = gh, step = gstep;
      const solid = i => cut[i] < T - 1e-3;
      // vertex grid (cell corners): front depth = mean cut of the solid cells around it
      const VW = w + 1, VH = h + 1, vz = new Float32Array(VW * VH), vok = new Uint8Array(VW * VH);
      for (let y = 0; y < VH; y++) for (let x = 0; x < VW; x++) {
        let sum = 0, n = 0;
        for (const [cx, cy] of [[x - 1, y - 1], [x, y - 1], [x - 1, y], [x, y]]) {
          if (cx < 0 || cy < 0 || cx >= w || cy >= h) continue;
          const i = cy * w + cx;
          if (solid(i)) { sum += cut[i]; n++; }
        }
        vok[y * VW + x] = n ? 1 : 0;
        vz[y * VW + x] = F + (n ? sum / n : T);
      }
      // a vertex in camera space: the block is a box (as wide as the camera's view at its middle depth), each cell one
      // sensor direction, carved along the view axis to depth z (m)
      const zm = F + T / 2;
      const P = (x, y, z) => { const u = Math.min(f.w, x * step) * SL_W / f.w - 0.5, v = Math.min(f.h, y * step) * SL_H / f.h - 0.5;
        return [(u - (SL_W - 1) / 2) / SL_FX * zm, -(v - (SL_H - 1) / 2) / SL_FX * zm, z]; };
      // oblique view around the block's centre, perspective
      const yaw = p.yaw * Math.PI / 180, pitch = p.pitch * Math.PI / 180, cz = F + T / 2;
      const cyw = Math.cos(yaw), syw = Math.sin(yaw), cp = Math.cos(pitch), sp = Math.sin(pitch);
      const half = (SL_W / 2 / SL_FX) * zm, radius = Math.hypot(half, half * SL_H / SL_W, T / 2), dist = radius * 3.2;
      const scale = Math.min(cw, ch) * 0.62 / radius * (dist - radius) / dist;
      const view = ([x, y, z]) => {
        z -= cz;
        const x1 = x * cyw - z * syw, z1 = x * syw + z * cyw;           // yaw about the vertical axis
        const y2 = y * cp + z1 * sp, z2 = z1 * cp - y * sp;             // pitch: positive = looking down on it
        const k = scale * dist / (dist + z2);
        return [cw / 2 + x1 * k, ch / 2 - y2 * k, z2, x1, y2];
      };
      const quads = [];
      const cA = hexRgb(p.color), cB = hexRgb(p.color_cut), L = [-0.45, 0.6, -0.66];
      const add = (pts, frac, wall) => {
        const q = pts.map(view);
        const ax = q[1][3] - q[0][3], ay = q[1][4] - q[0][4], az = q[1][2] - q[0][2], bx = q[3][3] - q[0][3], by = q[3][4] - q[0][4], bz = q[3][2] - q[0][2];
        let nx = ay * bz - az * by, ny = az * bx - ax * bz, nz = ax * by - ay * bx;
        const nl = Math.hypot(nx, ny, nz) || 1;
        const lit = 0.28 + 0.72 * Math.abs((nx * L[0] + ny * L[1] + nz * L[2]) / nl);
        const t = Math.min(1, frac * 1.6), base = cA.map((v, k) => v + (cB[k] - v) * t);
        const shade = wall ? lit * 0.8 : lit;
        quads.push({q, z: (q[0][2] + q[1][2] + q[2][2] + q[3][2]) / 4, c: `rgb(${base.map(v => Math.round(v * shade)).join(',')})`});
      };
      let cutCells = 0, through = 0, maxCut = 0;
      for (let cy = 0; cy < h; cy++) for (let cx = 0; cx < w; cx++) {
        const i = cy * w + cx;
        maxCut = Math.max(maxCut, cut[i]);
        if (cut[i] > 0.01) cutCells++;
        if (!solid(i)) { through++; continue; }
        const a = cy * VW + cx, b = a + 1, c = a + VW + 1, d = a + VW;
        add([P(cx, cy, vz[a]), P(cx + 1, cy, vz[b]), P(cx + 1, cy + 1, vz[c]), P(cx, cy + 1, vz[d])], cut[i] / T, false);
        // side walls where the block ends: the grid edge or a cell cut all the way through
        const open = (nx, ny) => nx < 0 || ny < 0 || nx >= w || ny >= h || !solid(ny * w + nx);
        const wall = (x0, y0, x1, y1) => { const z0 = vz[y0 * VW + x0], z1 = vz[y1 * VW + x1];
          add([P(x0, y0, z0), P(x1, y1, z1), P(x1, y1, F + T), P(x0, y0, F + T)], 0, true); };
        if (open(cx, cy - 1)) wall(cx, cy, cx + 1, cy);
        if (open(cx + 1, cy)) wall(cx + 1, cy, cx + 1, cy + 1);
        if (open(cx, cy + 1)) wall(cx + 1, cy + 1, cx, cy + 1);
        if (open(cx - 1, cy)) wall(cx, cy + 1, cx, cy);
      }
      quads.sort((m, n) => n.z - m.z);   // far first
      for (const {q, c} of quads) {
        ctx.fillStyle = c; ctx.strokeStyle = c; ctx.lineWidth = 0.6;   // the thin stroke closes hairline gaps between quads
        ctx.beginPath(); ctx.moveTo(q[0][0], q[0][1]); ctx.lineTo(q[1][0], q[1][1]); ctx.lineTo(q[2][0], q[2][1]); ctx.lineTo(q[3][0], q[3][1]); ctx.closePath();
        ctx.fill(); ctx.stroke();
      }
      Object.assign(status, {cells: w * h, cutCells, through, maxCut: Math.round(maxCut * 1000) / 1000});
      return {f, r: {s: 1}};
    }
    function clear() {
      const [cw, ch] = sizeOf();
      if (canvas.width !== cw || canvas.height !== ch) { canvas.width = cw; canvas.height = ch; }
      ctx.fillStyle = '#000'; ctx.fillRect(0, 0, canvas.width, canvas.height);
    }
    return {draw, clear, reset, destroy: reset, status, kind: 'mass'};
  }

  const RENDERERS = {
    tof_sensor: tofRenderer('sensor', 'ToF SENSOR (깊이 흑백)', ['near', 'far', 'confidence']),
    tof_contour: tofRenderer('contour', 'ToF CONTOUR (등고선)', ['near', 'far', 'confidence', 'interval', 'line', 'slats', 'temporal']),
    tof_side: Object.assign(tofRenderer('side', 'ToF SIDE (단면 흐름)', ['near', 'far', 'confidence', 'row', 'base', 'flow', 'direction', 'vanish', 'gain', 'line', 'vertical', 'temporal']),
      {defaults: Object.assign({}, TOF_DEFAULTS, {inset: false, direction: 180, vanish: 100}), viewer: true}),   // new SIDE layers: flow only (ToF picture = linked layer)
    tof_relief: tofRenderer('relief', 'ToF RELIEF 45° (높이맵)', ['near', 'far', 'confidence', 'gain', 'interval', 'line', 'slats', 'temporal']),
    // experiment modes (not used by any built-in composition or output until someone picks them)
    tof_silhouette: artRenderer('silhouette', 'ToF SILHOUETTE (거리 범위 실루엣)', [mm('near_t', '실루엣 시작', 0, 4000), mm('far_t', '실루엣 끝', 100, 4000),
      {key: 'invert', label: '반전 (범위 밖)', type: 'bool', default: false}]),
    tof_bands: artRenderer('bands', 'ToF DEPTH BANDS (거리 띠)', [{key: 'bands', label: '띠 개수', min: 2, max: 24, step: 1, unit: '', default: 6},
      {key: 'color_near', label: '가까운 띠 색', type: 'color', default: '#ffffff'}, {key: 'color_far', label: '먼 띠 색', type: 'color', default: '#333333'},
      {key: 'edges', label: '띠 경계선', type: 'bool', default: false}]),
    tof_points: artRenderer('points', 'ToF POINTS (점군)', [{key: 'density', label: '간격 (1 = 모든 점)', min: 1, max: 8, step: 1, unit: 'px', default: 2},
      {key: 'size', label: '점 크기', min: 0.5, max: 6, step: 0.5, unit: 'px', default: 1.5},
      {key: 'size_by_distance', label: '가까울수록 크게', type: 'bool', default: true}, {key: 'alpha_by_distance', label: '가까울수록 진하게', type: 'bool', default: true}]),
    // 공간선 (experiment): ToF depth as 3D lines (WebGL), accumulated over time; animated = redrawn every display frame
    tof_spaceline: {label: '공간선(실험) · ToF 3D 선 (누적·처짐·초점)', accepts: 'frame', tofMode: 'side', art: 'spaceline', animated: true,
      params: [...tofParams(['near', 'far', 'confidence', 'temporal']),
        {key: 'method', label: '선 생성 방식', type: 'select', options: [['edge', '경계 윤곽'], ['contour', '거리별 등고선'], ['mesh', '점 연결선'], ['ir_edge', '적외선 윤곽 (밝기 경계 · 색 = 그 점의 깊이)']], default: 'edge'},
        {key: 'pose_src', label: '혈 위치 기준 (선을 공간에 고정)', type: 'select', options: [['auto', '자동 (mock = 시뮬레이터, 실제 센서 = ToF 추정)'],
          ['tof', 'ToF 추정 (윤곽으로 움직임 추측 · 조금씩 어긋남)'], ['sim', '시뮬레이터 위치 (mock만)'], ['camera', '카메라 기준 (고정 안 함)']], default: 'auto'},
        {key: 'denoise', label: '잡음 거르기 (실제 센서 · 0 = 끔)', min: 0, max: 5, step: 1, unit: 'px', default: 0},
        {key: 'edge_mm', label: '경계 기준 (깊이 차)', min: 20, max: 1000, step: 10, unit: 'mm', default: 150},
        {key: 'interval_mm', label: '등고선 간격', min: 50, max: 1000, step: 50, unit: 'mm', default: 250},
        {key: 'step', label: '점 간격', min: 1, max: 16, step: 1, unit: 'px', default: 4},
        {key: 'record_s', label: '기록 간격', min: 0.05, max: 2, step: 0.05, unit: 's', default: 0.25},
        {key: 'hold_s', label: '고정 시간', min: 0, max: 20, step: 0.5, unit: 's', default: 2},
        {key: 'sag_s', label: '처지는 시간', min: 0.5, max: 30, step: 0.5, unit: 's', default: 6},
        {key: 'fade_s', label: '사라지는 시간', min: 0.5, max: 30, step: 0.5, unit: 's', default: 4},
        {key: 'gravity_deg', label: '중력 방향', type: 'angle', default: 180},
        {key: 'gravity', label: '중력 세기 (처지는 거리)', min: 0, max: 3, step: 0.05, unit: 'm', default: 0.6},
        {key: 'clump', label: '뭉침', min: 0, max: 1, step: 0.05, unit: '', default: 0.4},
        {key: 'focus_x', label: '초점 위치 가로', min: 0, max: 100, step: 1, unit: '%', default: 50},
        {key: 'focus_y', label: '초점 위치 세로', min: 0, max: 100, step: 1, unit: '%', default: 50},
        {key: 'focus_shift', label: '초점 거리 보정', min: -3, max: 3, step: 0.05, unit: 'm', default: 0},
        {key: 'focus_range', label: '선명한 깊이 범위', min: 0.05, max: 4, step: 0.05, unit: 'm', default: 0.5},
        {key: 'blur', label: '아웃포커싱 강도', min: 0, max: 16, step: 0.5, unit: 'px', default: 6},
        {key: 'res', label: '그리기 해상도 (Pi에서 느리면 낮춤 · 0.5 = 화소 1/4)', min: 0.25, max: 1, step: 0.05, unit: '×', default: 1},
        {key: 'color_near', label: '가까운 선 색', type: 'color', default: '#ffffff'}, {key: 'color_mid', label: '중간 거리 선 색', type: 'color', default: '#adb5bf'}, {key: 'fog', label: '거리 안개 (먼 선일수록 흐림)', min: 0, max: 0.9, step: 0.05, unit: '', default: 0}, {key: 'ir_bold', label: '적외선 윤곽 강조 (거리선보다 굵게)', min: 0, max: 3, step: 1, unit: '', default: 0}, {key: 'ir_rows', label: '면 스캔선 간격 (센서 행, 0 = 끔)', min: 0, max: 20, step: 1, unit: ' 행', default: 0}, {key: 'edge_alpha', label: '윤곽 밝기 (지금 윤곽)', min: 0.1, max: 1, step: 0.05, unit: '', default: 1}, {key: 'iso_alpha', label: '거리선 밝기 (윤곽 아래 깔림)', min: 0.1, max: 1, step: 0.05, unit: '', default: 1}, {key: 'auto_color', label: '색 범위 자동 (보이는 장면의 앞 → 뒤)', type: 'bool', default: false}, {key: 'auto_mix', label: '색 범위: 장면 기준 비율 (0 = 고정 범위, 1 = 장면만)', min: 0, max: 1, step: 0.05, unit: '', default: 1}, {key: 'auto_span_min', label: '색 범위 최소 폭', min: 0.2, max: 3, step: 0.1, unit: ' m', default: 1}, {key: 'line_w', label: '선 두께', min: 1, max: 4, step: 1, unit: ' px', default: 1}, {key: 'ghost', label: '잔상 밝기 (처지기 시작하면 이만큼으로, 1 = 그대로)', min: 0.05, max: 1, step: 0.05, unit: '', default: 1}, {key: 'ir_contour', label: '적외선 윤곽 + 거리 등고선 간격 (0 = 윤곽만)', min: 0, max: 1000, step: 50, unit: 'mm', default: 0}, {key: 'color_far', label: '먼 선 색', type: 'color', default: '#5a6a80'}],
      defaults: Object.assign({}, TOF_DEFAULTS, SPACELINE_DEFAULTS),
      make(canvas, size) { return createSpaceLines(canvas, size ? {size} : {}); },
      create(off) { return {tof: createSpaceLines(off, {size: () => [off.width, off.height]})}; }},
    tof_mass: {label: '깎이는 매스(실험) · 카메라 앞 덩어리를 ToF가 깎음 (누적)', accepts: 'frame', tofMode: 'side', art: 'mass',
      params: [...tofParams(['near', 'far', 'confidence', 'temporal']),
        {key: 'method', label: '깎는 방식', type: 'select', options: [['press', '가까운 만큼 눌러 깎기'], ['space', '보인 빈 공간만큼 깎기']], default: 'press'},
        {key: 'start_m', label: '깎기 시작 거리 (눌러 깎기 · 이보다 먼 것은 안 깎음)', min: 0.5, max: 6, step: 0.1, unit: 'm', default: 2.5},
        {key: 'trace', label: '흔적', type: 'select', options: [['regrow', '남았다가 다시 차오름'], ['now', '남기지 않음 (지금 보이는 것만)']], default: 'regrow'},
        {key: 'front_m', label: '매스 앞면 (카메라에서)', min: 0.3, max: 6, step: 0.1, unit: 'm', default: 1.0},
        {key: 'depth_m', label: '매스 두께', min: 0.1, max: 5, step: 0.1, unit: 'm', default: 1.5},
        {key: 'step', label: '칸 크기', min: 2, max: 8, step: 1, unit: 'px', default: 3},
        {key: 'regrow_s', label: '다시 차오르는 시간 (흔적 남김 · 0 = 계속 남음)', min: 0, max: 600, step: 1, unit: 's', default: 10},
        {key: 'yaw', label: '보는 방향 좌우', min: -80, max: 80, step: 1, unit: '°', default: 35},
        {key: 'pitch', label: '보는 방향 위아래', min: -60, max: 60, step: 1, unit: '°', default: 20},
        {key: 'color', label: '매스 색', type: 'color', default: '#d8d0c0'}, {key: 'color_cut', label: '깎인 곳 색', type: 'color', default: '#c0502e'}],
      defaults: Object.assign({}, TOF_DEFAULTS, MASS_DEFAULTS),
      make(canvas, size) { return createMass(canvas, size ? {size} : {}); },
      create(off) { return {tof: createMass(off, {size: () => [off.width, off.height]})}; }},
    tof_topdown: {label: '평면 흔적(실험) · 위에서 본 ToF 점/같은 거리 선 누적', accepts: 'frame', tofMode: 'side', art: 'topdown', animated: true,
      params: [...tofParams(['near', 'far', 'confidence', 'temporal']),
        {key: 'view', label: '보기', type: 'select', options: [['follow', '혈 따라가기 (위가 북쪽)'], ['venue', '공간 전체']], default: 'follow'},
        {key: 'range_m', label: '보이는 범위 (따라가기)', min: 2, max: 30, step: 0.5, unit: 'm', default: 10},
        {key: 'mark_mode', label: '흔적 형태', type: 'select', options: [['points', '점 (기존)'], ['contour', '센서에서 같은 거리 잇기']], default: 'points'},
        {key: 'contour_mm', label: '거리 간격 (같은 거리 잇기)', min: 50, max: 1000, step: 50, unit: 'mm', default: 250},
        {key: 'step', label: '점 간격 (센서 화소)', min: 1, max: 12, step: 1, unit: 'px', default: 3},
        {key: 'z_min', label: '표시 높이 아래 한계', min: 0, max: 3, step: 0.05, unit: 'm', default: 0.15},
        {key: 'z_max', label: '표시 높이 위 한계', min: 0.1, max: 5, step: 0.05, unit: 'm', default: 2.6},
        {key: 'stamp_s', label: '흔적 기록 간격', min: 0.05, max: 2, step: 0.05, unit: 's', default: 0.2},
        {key: 'stamp', label: '흔적 겹침 세기 (겹칠수록 밝게)', min: 0.02, max: 1, step: 0.02, default: 0.25},
        {key: 'fade_s', label: '흔적 수명 (0 = 계속 남김)', min: 0, max: 600, step: 5, unit: 's', default: 60},
        {key: 'res_mm', label: '지도 해상도 (화소 1칸 · 흔적과 현재 점 공통)', min: 10, max: 40, step: 5, unit: 'mm', default: 10},
        {key: 'color_now', label: '현재 형태 색', type: 'color', default: '#ffffff'}, {key: 'color_trace', label: '흔적 색', type: 'color', default: '#6fa8ff'},
        {key: 'trace_style', label: '흔적 쌓는 방식', type: 'select', options: [['add', '더하기 (기존)'], ['age', '시간 색 지층 (최근 → 흔적 색 → 오래됨)']], default: 'add'},
        {key: 'age_s', label: '색이 끝까지 바뀌는 시간 (지층)', min: 2, max: 600, step: 1, unit: 's', default: 30},
        {key: 'color_old', label: '오래됨 색 (지층)', type: 'color', default: '#5a2d91'},
        {key: 'fade_style', label: '사라지는 방식', type: 'select', options: [['uniform', '균일하게 옅어짐 (기존)'], ['dissolve', '흩어지며 사라짐']], default: 'uniform'},
        {key: 'drift_mm', label: '흩어지는 거리', min: 0, max: 1000, step: 10, unit: 'mm', default: 150},
        {key: 'still_stop', label: '큰 움직임 없으면 흔적 기록 멈춤', type: 'bool', default: true},
        {key: 'still_mm', label: '움직임 기준 (이동)', min: 20, max: 2000, step: 10, unit: 'mm', default: 100},
        {key: 'still_deg', label: '움직임 기준 (회전)', min: 1, max: 90, step: 1, unit: '°', default: 5},
        {key: 'fov', label: '시야 범위 표시', type: 'select', options: [['shade', '부채꼴 · 거리 호'], ['none', '없음']], default: 'shade'},
        {key: 'ceil_trace', label: '천장 거리 trace (실험 · 지나온 경로 위, 흔적과 같은 화소 · 밝을수록 천장 가까움)', type: 'bool', default: true},
        {key: 'ceil_max', label: '천장 거리 밝기 범위 (최대)', min: 1, max: 20, step: 0.5, unit: 'm', default: 6}],
      defaults: Object.assign({}, TOF_DEFAULTS, TOPDOWN_DEFAULTS),
      make(canvas, size) { return createTopDown(canvas, size ? {size} : {}); },
      create(off) { return {tof: createTopDown(off, {size: () => [off.width, off.height]})}; }},
    tof_motion: artRenderer('motion', 'ToF MOTION (움직인 곳)', [{key: 'sensitivity', label: '감도 (변화 기준)', min: 10, max: 1000, step: 10, unit: 'mm', default: 40},
      {key: 'decay', label: '사라지는 시간', min: 0, max: 3, step: 0.1, unit: 's', default: 0.6}]),
    tof_numbers: artRenderer('numbers', 'ToF NUMBERS (깊이 숫자)', [{key: 'cell', label: '숫자 간격', min: 4, max: 30, step: 1, unit: 'px', default: 10},
      {key: 'unit', label: '단위', type: 'select', options: [['cm', 'cm'], ['mm', 'mm'], ['m', 'm']], default: 'cm'},
      {key: 'shade', label: '가까울수록 밝게', type: 'bool', default: true}, {key: 'grid', label: '칸 선', type: 'bool', default: false}]),
    maek_wave: {label: '맥 점 격자 파동(시연) · 합성 음높이/음량차 방향', accepts: 'number', sources: ['maek.mic1'],
      params: [{key: 'grid_px', label: '점 간격', min: 12, max: 40, step: 2, unit: 'px', default: 22},
        {key: 'speed', label: '파동 속도', min: 0.1, max: 0.8, step: 0.05, unit: '화면/초', default: 0.32},
        {key: 'life_s', label: '파동 지속 시간', min: 0.5, max: 4, step: 0.1, unit: '초', default: 2.3},
        {key: 'threshold', label: '음량 상승 감도', min: 0.01, max: 0.3, step: 0.01, default: 0.02},
        {key: 'color', label: '파동 색', type: 'color', default: '#ffffff'}],
      defaults: {grid_px: 22, speed: 0.32, life_s: 2.3, threshold: 0.02, color: '#ffffff'},
      create() { return {prev: 0, seq: null, lastEmit: 0, pulses: []}; },
      draw(off, layer, state) {
        const ctx = off.getContext('2d'), cw = off.width, ch = off.height, dpr = window.devicePixelRatio || 1;
        const p = Object.assign({}, this.defaults, layer.params), now = performance.now();
        const gap = Math.max(4, p.grid_px * dpr), minSide = Math.min(cw, ch);
        const samples = [1, 2, 3].map(k => hub.sample(`maek.mic${k}`));
        const ready = hub.serverOk && samples.every(s => s.connected && typeof s.value === 'number');
        const level = ready ? Math.max(...samples.map(s => s.value)) : 0;
        if (ready && samples[0].seq !== state.seq) {
          state.seq = samples[0].seq;
          const features = hub.inputs && hub.inputs.channels && hub.inputs.channels.maek_features;
          const v = features && features.connected && features.value;
          const onset = level - state.prev >= p.threshold && now - state.lastEmit >= 220;
          const sustained = level >= 0.14 && now - state.lastEmit >= 700;
          if (onset || sustained) {
            state.pulses.push({t: now, strength: Math.min(1, 0.48 + level * 1.6),
              pitch: v && typeof v.pitch_hz === 'number' ? v.pitch_hz : null,
              direction: v && typeof v.direction_deg === 'number' ? v.direction_deg * Math.PI / 180 : null});
            state.lastEmit = now;
          }
          state.prev = level;
        }
        state.pulses = state.pulses.filter(q => now - q.t < p.life_s * 1000).slice(-18);
        if (off.dataset) { off.dataset.wavePulses = String(state.pulses.length); off.dataset.waveLevel = String(level); }
        ctx.fillStyle = '#000'; ctx.fillRect(0, 0, cw, ch);
        const radius = Math.max(1, 1.5 * dpr), width = gap * 0.8, [ox, oy] = [cw / 2, ch / 2], rgb = hexRgb(p.color);
        for (let y = gap / 2; y < ch; y += gap) for (let x = gap / 2; x < cw; x += gap) {
          let light = 0.08;
          for (const q of state.pulses) {
            const age = (now - q.t) / 1000, dx = x - ox, dy = y - oy, dist = Math.hypot(dx, dy);
            const ring = age * p.speed * minSide;
            // Visual encoding only: higher synthetic pitch produces closer secondary rings. It is not a
            // physical sound wavelength drawn at scale. A rough level-derived direction lights one side.
            const spacing = gap * (q.pitch === null ? 2.3 : 3.1 - 1.5 * Math.max(0, Math.min(1, (q.pitch - 100) / 220)));
            const angle = Math.atan2(dx, -dy);
            const focus = q.direction === null ? 1 : 0.2 + 0.8 * Math.max(0, Math.cos(angle - q.direction)) ** 2;
            for (let n = 0; n < 3; n++) {
              const delta = dist - (ring - n * spacing);
              if (Math.abs(delta) > width * 2) continue;
              light = Math.max(light, Math.exp(-2 * (delta / width) ** 2) * q.strength * (1 - age / p.life_s) * focus * (1 - n * 0.16));
            }
          }
          ctx.fillStyle = `rgba(${rgb.map(Math.round).join(',')},${Math.min(1, light).toFixed(3)})`;
          ctx.fillRect(Math.round(x - radius), Math.round(y - radius), radius * 2, radius * 2);
        }
        ctx.fillStyle = `rgba(${rgb.map(Math.round).join(',')},${(0.35 + state.prev * 0.65).toFixed(3)})`;
        ctx.beginPath(); ctx.arc(ox, oy, (3 + state.prev * 4) * dpr, 0, Math.PI * 2); ctx.fill();
      }},
    // the old 맥 audio level diagnostic, for any number source: bar (value between min and max) + 10 s trace
    meter: {label: '수치 막대 + 최근 10초', accepts: 'number',
      params: [{key: 'min', label: '최솟값', type: 'number', default: 0}, {key: 'max', label: '최댓값', type: 'number', default: 1},
               {key: 'label', label: '이름표', type: 'text', default: 'level 0–1'}],
      defaults: {min: 0, max: 1, label: 'level 0–1'},
      create() { return {}; },
      draw(off, layer, src) {
        const ctx = off.getContext('2d'), cw = off.width, ch = off.height, dpr = window.devicePixelRatio || 1;
        ctx.fillStyle = '#000'; ctx.fillRect(0, 0, cw, ch);
        const now = performance.now(), HISTORY_S = 10;
        const lo = Number(layer.params.min ?? 0), hi = Number(layer.params.max ?? 1), span = hi - lo || 1;
        const norm = v => Math.min(1, Math.max(0, (v - lo) / span));
        const sample = layer.source ? hub.sample(layer.source) : {connected: false};
        const ok = hub.serverOk && sample.connected && sample.value !== null;
        const level = ok ? norm(sample.value) : 0;
        const history = layer.source ? hub.history(layer.source) : [];
        // Level bar and a 10 s trace of received values (same drawing as the old audio diagnostic).
        const m = 40 * dpr, top = ch * 0.3, barH = ch * 0.08, w = cw - 2 * m;
        ctx.strokeStyle = '#555'; ctx.lineWidth = dpr;
        ctx.strokeRect(m, top, w, barH);
        ctx.fillStyle = '#fff'; ctx.fillRect(m, top, w * level, barH);
        const gy = top + barH + 40 * dpr, gh = ch - gy - m;
        ctx.strokeRect(m, gy, w, gh);
        ctx.beginPath();
        history.forEach((p, i) => {
          const x = m + w * (1 - (now - p.t) / (HISTORY_S * 1000)), y = gy + gh * (1 - norm(p.v));
          // A gap over 0.5 s (no samples received) stays visible as a break in the line.
          i && p.t - history[i - 1].t < 500 ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
        });
        ctx.strokeStyle = '#fff'; ctx.stroke();
        ctx.fillStyle = '#888'; ctx.font = `${12 * dpr}px ui-monospace, Menlo, monospace`;
        ctx.textBaseline = 'bottom';
        ctx.fillText(String(layer.params.label ?? ''), m, top - 6 * dpr);
        ctx.fillText(`최근 ${HISTORY_S}초`, m, gy - 6 * dpr);
      }},
    // ceiling distance sensor (one distance over time): four layer types, each with the common settings below
    updist_meter: updistRenderer('meter', 'VERTICAL METER · 천장 거리 세로 막대 (거리 = 높이)', [
      {key: 'thickness', label: '두께', min: 0.05, max: 1, step: 0.05, default: 0.3},
      // 2026-11-10: the whole range as a dim track, ticks in metres, the value as text (older meters: the plain bar)
      {key: 'scale', label: '눈금 · 숫자 표시 (막대: 빈 칸도)', type: 'bool', default: true, legacy: false},
      {key: 'tick', label: '눈금 간격', min: 0.1, max: 2, step: 0.1, unit: ' m', default: 0.5},
      {key: 'style', label: '모양', type: 'select', options: [['bar', '채운 막대'], ['lines', '가는 선 쌓기 (공간선처럼)']], default: 'lines', legacy: 'bar'},
      {key: 'line_mm', label: '선 간격', min: 20, max: 500, step: 10, unit: ' mm', default: 100},
      {key: 'line_px', label: '선 간격 (화소, 0 = 위의 mm 간격)', min: 0, max: 60, step: 1, unit: ' px', default: 0},
      {key: 'side_fade', label: '안쪽으로 사라짐 (폭 대비, 0 = 끔)', min: 0, max: 1, step: 0.05, unit: '', default: 0},
      {key: 'meter_alpha', label: '미터 선 밝기', min: 0.1, max: 1, step: 0.05, unit: '', default: 1},
      {key: 'auto_span', label: '자동 범위 (최근 평균 ± 이만큼, 0 = 끔)', min: 0, max: 1.5, step: 0.05, unit: ' m', default: 0},
      {key: 'auto_s', label: '자동 범위가 따라가는 시간', min: 2, max: 120, step: 1, unit: ' s', default: 20}], {fx: true}),
    updist_pulse: updistRenderer('pulse', 'PULSE · 천장 거리 파동 (거리 = 크기, 변화 = 밝기, 최근 거리 기록 = 둘레)', [
      {key: 'size', label: '크기', min: 0.1, max: 1.5, step: 0.05, default: 0.8}, {key: 'rings', label: '반복 수', min: 1, max: 12, step: 1, default: 4},
      {key: 'sensitivity', label: '변화 감도', min: 0, max: 5, step: 0.1, default: 1}, {key: 'decay', label: '감쇠 (초)', min: 0.1, max: 3, step: 0.1, unit: ' s', default: 0.8},
      // outline from the recent distance history (time around the ring; one point sensor, not a direction measurement)
      {key: 'shape', label: '둘레 왜곡 세기 (최근 거리 변화 · 시간 순서)', min: 0, max: 1, step: 0.05, default: 0.35, legacy: 0},
      {key: 'shape_gain', label: '둘레 왜곡 민감도', min: 0.1, max: 20, step: 0.1, unit: ' /m', default: 2},
      {key: 'shape_s', label: '둘레에 놓는 시간 (한 바퀴)', min: 1, max: 30, step: 0.5, unit: ' s', default: 6}], {fx: true}),
    updist_trace: updistRenderer('trace', 'HISTORY TRACE · 천장 거리 시간 기록 (선/면)', [
      {key: 'window', label: '표시 시간', min: 5, max: 120, step: 5, unit: ' s', default: 20}, {key: 'line', label: '선 두께', min: 1, max: 8, step: 1, unit: ' px', default: 2},
      {key: 'fill', label: '아래 면 채우기', type: 'bool', default: false}, {key: 'fade', label: '잔상 (오래된 부분 흐리게)', min: 0, max: 1, step: 0.05, default: 0.6}]),
    updist_field: updistRenderer('field', 'DISTANCE FIELD · 천장 거리 흐름 띠 (거리 = 간격, 변화 = 속도)', [
      {key: 'bands', label: '띠 수', min: 2, max: 40, step: 1, default: 8}, {key: 'speed', label: '속도 감도', min: 0, max: 5, step: 0.1, default: 1},
      {key: 'angle', label: '흐르는 방향', type: 'angle', default: 180}, {key: 'sharpness', label: '선명도', min: 0, max: 1, step: 0.05, default: 0.5}]),
    // a plain colour area (for checking blend / opacity / transform and as a backdrop)
    solid: {label: '단색 면', accepts: null, params: [], defaults: {}, create() { return {}; },
      draw(off, layer) { const ctx = off.getContext('2d'); ctx.fillStyle = layer.color || '#ffffff'; ctx.fillRect(0, 0, off.width, off.height); }},
  };
  for (const id of ['tof_sensor', 'tof_silhouette', 'tof_bands', 'tof_motion']) {   // 화소 표현 (2026-11-02)
    const R = RENDERERS[id];
    R.params = [...R.params, UPSCALE_PARAM];
    R.defaults = Object.assign({}, R.defaults, {upscale: UPSCALE_PARAM.default});
  }

  // ============================================================================================================
  // COMPOSITOR — draws one composition (ordered layers) into a canvas. Used by the 작업실 preview and every output, so
  // a composition looks the same wherever it is assigned. ToF layers stream /ws?tof=<their settings> (one socket per
  // distinct settings); paced = device screens (2026-10-15 option A: aim at 15 frames/s, one request in flight, wait
  // = what is left of 1000/15 ms after this frame's request → arrival), otherwise ask for the next frame at once.
  const NUM_DEFAULTS = {opacity: 1, x: 0, y: 0, scale: 1, rotation: 0};
  function tofKey(layer) {
    // base (기준선) only places the drawing, so it is not part of the stream: changing it never reopens the stream
    const p = Object.assign({}, TOF_DEFAULTS, layer.params, {mode: RENDERERS[layer.renderer].tofMode, base: TOF_DEFAULTS.base});
    const keys = ['mode', 'near', 'far', 'confidence', 'interval', 'line', 'slats', 'vertical', 'temporal', 'row', 'gain', 'base', 'flow'];
    return JSON.stringify(Object.fromEntries(keys.map(k => [k, p[k]])));
  }
  function createComposition(canvas, opts = {}) {
    const ctx = canvas.getContext('2d');
    const TARGET_FPS = 15, TARGET_CYCLE_MS = 1000 / TARGET_FPS;
    const paced = Boolean(opts.paced);
    let comp = null, running = true;
    const layers = new Map();   // lid → {layer, inst, off, key, smooth: {}}
    // One visible full-canvas layer with no effect (every built-in composition, the old single-mode screens) is drawn
    // straight into the output canvas: browsers may rasterise an off-screen canvas slightly differently (anti-aliasing
    // of CONTOUR / SIDE lines), so this keeps those screens pixel-identical to before. Stacks go through off-screen
    // layer canvases.
    let direct = null;          // {lid, tof: createTof(canvas)} while that path is in use
    const streams = new Map();  // key → {ws, layers: Set(lid), latest, lastFrame, ...}
    const stats = {frames: 0, received: 0, bytes: 0, frameBytes: 0, rtt: [], wait: [], pause: [], decode: [], fx: [], lastFrameAt: 0, lastF: null};
    hub.start();

    function openStream(key) {
      const st = {key, layers: new Set(), latest: null, ws: null, closed: false};
      const connect = () => {
        const sock = new WebSocket(`ws://${location.host}/ws?tof=${encodeURIComponent(key)}`);
        sock.binaryType = 'arraybuffer';
        let pace = null, askedAt = 0, pinger = null;
        const ask = () => { if (sock.readyState === 1) { askedAt = performance.now(); sock.send('next'); } };
        sock.onopen = () => { ask(); if (opts.debug) pinger = setInterval(() => sock.readyState === 1 && sock.send(`ping ${performance.now()}`), 1000); };
        sock.onmessage = e => {
          if (typeof e.data === 'string') { if (e.data.startsWith('pong ')) stats.rtt.push(performance.now() - Number(e.data.split(' ')[1])); return; }
          st.latest = e.data; stats.lastFrameAt = performance.now();
          const pause = paced ? Math.max(0, TARGET_CYCLE_MS - (stats.lastFrameAt - askedAt)) : 0;
          stats.received++; stats.bytes += e.data.byteLength; stats.frameBytes = e.data.byteLength; stats.wait.push(stats.lastFrameAt - askedAt); stats.pause.push(pause);
          clearTimeout(pace);
          if (paced) pace = setTimeout(ask, pause); else ask();
        };
        sock.onclose = () => { clearTimeout(pace); clearInterval(pinger); if (!st.closed) setTimeout(() => { if (!st.closed) st.ws = connect(); }, 1000); };
        return sock;
      };
      st.ws = connect();
      return st;
    }
    function release(lid) {
      const L = layers.get(lid);
      if (!L) return;
      L.fx = null;   // a deleted layer leaves no afterimage behind
      if (L.inst && L.inst.tof && L.inst.tof.destroy) L.inst.tof.destroy();
      if (L.key && streams.has(L.key)) {
        const st = streams.get(L.key);
        st.layers.delete(lid);
        if (!st.layers.size) { st.closed = true; st.ws.close(); streams.delete(L.key); }
      }
      layers.delete(lid);
    }
    function set(next) {
      // another composition: no afterimage / motion memory carries over (layers of two compositions may share ids)
      if (!next || !comp || next.id !== comp.id) {
        for (const L of layers.values()) { L.fx = null; L.processed = null; if (L.inst && L.inst.tof && L.inst.tof.reset) L.inst.tof.reset(); }
        if (direct && direct.tof && direct.tof.reset) direct.tof.reset();
      }
      comp = next ? JSON.parse(JSON.stringify(next)) : null;
      const keep = new Set(comp ? comp.layers.map(l => l.id) : []);
      for (const lid of [...layers.keys()]) if (!keep.has(lid)) release(lid);
      for (const layer of comp ? comp.layers : []) {
        const R = RENDERERS[layer.renderer];
        let L = layers.get(layer.id);
        const key = R && R.tofMode && layer.visible ? tofKey(layer) : null;
        if (L && (L.layer.renderer !== layer.renderer || L.key !== key)) { release(layer.id); L = null; }
        if (!L) {
          const off = document.createElement('canvas');
          L = {layer, off, inst: R ? R.create(off, layer) : null, key, smooth: {}, frameSeen: null};
          layers.set(layer.id, L);
          if (key) { if (!streams.has(key)) streams.set(key, openStream(key)); streams.get(key).layers.add(layer.id); }
        }
        L.layer = layer;
      }
    }
    // a layer's number field: fixed value or an input link (min/max mapping, invert, smoothing)
    function num(L, field) {
      const p = L.layer[field];
      if (!p || !p.bind) return p && typeof p.value === 'number' ? p.value : NUM_DEFAULTS[field];
      const b = p.bind, s = hub.sample(b.source);
      let target;
      if (!s.connected || s.value === null) target = L.smooth[field] ?? b.out_min;
      else {
        let t = (s.value - b.in_min) / (b.in_max - b.in_min);
        t = Math.min(1, Math.max(0, t));
        if (b.invert) t = 1 - t;
        target = b.out_min + t * (b.out_max - b.out_min);
      }
      const prev = L.smooth[field];
      const v = prev === undefined ? target : prev + (1 - b.smoothing) * (target - prev);
      L.smooth[field] = v;
      return v;
    }
    // the shared visual experiments on one layer's canvas (ToF layers and PULSE): same order and code for every type.
    // Sizes use the layer's sensor-pixel unit (number layers: as if 240×180 filled the layer).
    function applyFx(L, layer, src, redrawn, lw, lh) {
      const e = layer.effects ? fxOf(layer) : null, still = e && fxStatic(e), fxKey = still ? JSON.stringify(e) : '';
      if (!e) { L.fx = null; L.processed = null; return src; }
      if (!L.fx) L.fx = createFx();
      if (still) {
        if (redrawn || !L.processed || L.fxKey !== fxKey) {
          const t0 = performance.now();
          L.processed = L.fx.still(L.off, e, L.unit || Math.min(lw / 240, lh / 180));
          stats.fx.push(performance.now() - t0);
          L.fxKey = fxKey;
        }
        src = L.processed;
      } else L.processed = null;
      if (e.trail.on) src = L.fx.trail(src, e, performance.now());
      else if (L.fx.trailing) L.fx.resetTrail();
      return src;
    }
    const plain = layer => layer.w === 1 && layer.h === 1 && !layer.mono && layer.blend === 'source-over' && !fxActive(layer)
      && ['opacity', 'x', 'y', 'scale', 'rotation'].every(k => layer[k] && !layer[k].bind && layer[k].value === NUM_DEFAULTS[k]);
    // settings that change the drawing without changing the stream (experiment modes' own settings)
    const artSig = (R, layer) => (R.art ? JSON.stringify(layer.params) : R.viewer ? `${layer.params.inset}|${layer.params.base}|${layer.params.direction}|${layer.params.vanish}` : '');
    function frame() {
      if (!running) return;
      requestAnimationFrame(frame);
      const dpr = window.devicePixelRatio || 1;
      const W = Math.round(canvas.clientWidth * dpr), H = Math.round(canvas.clientHeight * dpr);
      if (!W || !H) return;
      const visible = comp ? comp.layers.filter(l => l.visible && RENDERERS[l.renderer] && layers.has(l.id)) : [];
      // 화면 크기: the composition's own screen at its aspect, centred in this canvas (black outside); none = fill
      const sw = comp && comp.canvas.width, sh = comp && comp.canvas.height, fitS = sw && sh ? Math.min(W / sw, H / sh) : 0;
      const CW = fitS ? Math.max(1, Math.round(sw * fitS)) : W, CH = fitS ? Math.max(1, Math.round(sh * fitS)) : H;
      const OX = Math.floor((W - CW) / 2), OY = Math.floor((H - CH) / 2);
      if (visible.length === 1 && plain(visible[0]) && comp.canvas.background === '#000000' && CW === W && CH === H) {
        const layer = visible[0], L = layers.get(layer.id), R = RENDERERS[layer.renderer];
        if (!direct || direct.lid !== layer.id || direct.renderer !== layer.renderer) {
          if (direct && direct.tof && direct.tof.destroy) direct.tof.destroy();
          direct = {lid: layer.id, renderer: layer.renderer, tof: R.tofMode ? R.make(canvas) : null, seen: null, sig: ''};
          if (direct.tof) direct.tof.clear();
        }
        if (R.tofMode) {
          const st = streams.get(L.key), buf = st && st.latest, sig = artSig(R, layer);
          if (buf && (buf !== direct.seen || sig !== direct.sig || R.animated)) {
            const t0 = performance.now();
            const out = direct.tof.draw(buf, layer);   // sizes the canvas itself, like the old pages
            stats.decode.push(performance.now() - t0);
            if (buf !== direct.seen) { stats.lastF = out.f; stats.frames++; }
            direct.seen = buf; direct.sig = sig;
          }
        } else {
          if (canvas.width !== W || canvas.height !== H) { canvas.width = W; canvas.height = H; }
          R.draw(canvas, layer, L.inst);
        }
        return;
      }
      if (direct && direct.tof && direct.tof.destroy) direct.tof.destroy();
      direct = null;
      if (canvas.width !== W || canvas.height !== H) { canvas.width = W; canvas.height = H; }
      ctx.save();
      ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.globalAlpha = 1; ctx.globalCompositeOperation = 'source-over';
      if (CW !== W || CH !== H) { ctx.fillStyle = '#000'; ctx.fillRect(0, 0, W, H); }
      ctx.fillStyle = comp ? comp.canvas.background : '#000'; ctx.fillRect(OX, OY, CW, CH);
      ctx.restore();
      if (!comp) return;
      let drewTof = false;
      for (const layer of comp.layers) {
        const L = layers.get(layer.id), R = RENDERERS[layer.renderer];
        if (!L || !R || !layer.visible) continue;
        const lw = Math.max(1, Math.round(layer.w * CW)), lh = Math.max(1, Math.round(layer.h * CH));
        const resized = L.off.width !== lw || L.off.height !== lh;
        if (resized) { L.off.width = lw; L.off.height = lh; }
        const octx = L.off.getContext('2d');
        let src = L.off;   // what goes onto the output: the layer canvas, or its processed copy when an effect is on
        if (R.tofMode) {
          const st = streams.get(L.key);
          const buf = st && st.latest, sig = artSig(R, layer);
          let redrawn = false;
          if (buf && (buf !== L.frameSeen || resized || sig !== L.sig || R.animated)) {
            const t0 = performance.now();
            const out = L.inst.tof.draw(buf, layer);
            stats.decode.push(performance.now() - t0);
            if (buf !== L.frameSeen) { stats.lastF = out.f; drewTof = true; }
            L.frameSeen = buf; L.sig = sig; L.unit = out.r && out.r.s; redrawn = true;
            if (layer.mono) { octx.save(); octx.globalCompositeOperation = 'multiply'; octx.fillStyle = layer.color; octx.fillRect(0, 0, lw, lh); octx.restore(); }
          } else if (!buf && resized) { L.inst.tof.clear(); redrawn = true; }
          src = applyFx(L, layer, src, redrawn, lw, lh);
        } else {
          R.draw(L.off, layer, L.inst);
          if (layer.mono && layer.renderer !== 'solid') { octx.save(); octx.globalCompositeOperation = 'multiply'; octx.fillStyle = layer.color; octx.fillRect(0, 0, lw, lh); octx.restore(); }
          if (R.fx) src = applyFx(L, layer, src, true, lw, lh);   // redrawn every frame (animated number layers)
        }
        const opacity = num(L, 'opacity'), x = num(L, 'x'), y = num(L, 'y'), scale = num(L, 'scale'), rot = num(L, 'rotation');
        ctx.save();
        ctx.globalAlpha = Math.min(1, Math.max(0, opacity));
        ctx.globalCompositeOperation = layer.blend;
        if (CW !== W || CH !== H) { ctx.beginPath(); ctx.rect(OX, OY, CW, CH); ctx.clip(); }   // nothing outside the screen
        if (x === 0 && y === 0 && scale === 1 && rot === 0 && lw === W && lh === H) {
          ctx.drawImage(src, 0, 0);   // full-canvas layer: an exact copy (the old single-mode screens look the same)
        } else {
          ctx.translate(OX + x * CW + lw / 2, OY + y * CH + lh / 2);
          ctx.rotate(rot * Math.PI / 180);
          ctx.scale(scale, scale);
          ctx.drawImage(src, -lw / 2, -lh / 2);
        }
        ctx.restore();
      }
      if (drewTof) stats.frames++;
    }
    requestAnimationFrame(frame);
    return {
      set,
      stats,
      get composition() { return comp; },
      bound: (lid, field) => { const L = layers.get(lid); return L ? L.smooth[field] : undefined; },
      // a layer's renderer status (공간선: venue / camera space, batches, segments, timings)
      status(lid) { const L = layers.get(lid), t = direct && direct.lid === lid ? direct.tof : L && L.inst && L.inst.tof; return t && t.status ? Object.assign({}, t.status) : null; },
      destroy() { running = false; for (const lid of [...layers.keys()]) release(lid); },
    };
  }

  // /ws/visual: the compositions and the four output slots, server-saved (visual_store.py). onState(snapshot) on connect
  // and after every change from any page; send(op, final) applies one operation (duplicate, rename, set, assign).
  function connectVisual(onState, onEvent) {
    let ws = null, seq = 0;
    const client = Math.random().toString(36).slice(2);
    function open() {
      ws = new WebSocket(`ws://${location.host}/ws/visual`);
      ws.onmessage = e => {
        const m = JSON.parse(e.data);
        if (m.type === 'state') onState(m); else if (m.type === 'error' && onEvent) onEvent('error', m.error);
      };
      ws.onclose = () => { if (onEvent) onEvent('offline'); setTimeout(open, 1000); };
    }
    open();
    return {
      send(op, final = true) {
        if (!ws || ws.readyState !== 1) return 0;
        ws.send(JSON.stringify({type: 'op', client, seq: ++seq, final, op}));
        return seq;
      },
      client,
    };
  }

  window.RipplesRender = {MODES, parse, formatDistance, heightFrac, createTof, createDepthArt, createComposition, connectVisual, SOURCES, RENDERERS, EFFECTS,
                          fxDefaults, fxOf, fxRenderer, hub, tofKey, DIRECTIONS, rampColour,
                          _fxTest: {createFx, gpuMap}};   // checks only (GPU 색 매핑 = CPU 색 매핑)
})();
