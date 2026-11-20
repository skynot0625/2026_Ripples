// Mapped output, shared by the mapping editor preview (/mapping) and the blind outputs (display.html: /display/blind?space=a|b&clean).
// One booth = one master canvas. Each blind surface shows a part of it (crop), warped onto its 4 corners in the output.
// Data comes from the server (mapping_store.py) over /ws/mapping; this file only draws.
(() => {
  const MASTER_LONG = 1600;  // master canvas long side in px for the internal test pattern

  // --- geometry ---------------------------------------------------------------------------------------------------
  // Homography from the unit square (0,0),(1,0),(1,1),(0,1) to quad q (Heckbert's closed form).
  function squareToQuad(q) {
    const [[x0, y0], [x1, y1], [x2, y2], [x3, y3]] = q;
    const dx1 = x1 - x2, dx2 = x3 - x2, dx3 = x0 - x1 + x2 - x3;
    const dy1 = y1 - y2, dy2 = y3 - y2, dy3 = y0 - y1 + y2 - y3;
    let g = 0, h = 0;
    if (Math.abs(dx3) > 1e-9 || Math.abs(dy3) > 1e-9) {
      const det = dx1 * dy2 - dx2 * dy1;
      g = (dx3 * dy2 - dx2 * dy3) / det;
      h = (dx1 * dy3 - dx3 * dy1) / det;
    }
    return {a: x1 - x0 + g * x1, b: x3 - x0 + h * x3, c: x0, d: y1 - y0 + g * y1, e: y3 - y0 + h * y3, f: y0, g, h};
  }
  // CSS matrix3d (column-major) mapping a w×h element onto quad q (container px).
  function matrix3d(q, w, h) {
    const m = squareToQuad(q), sx = 1 / w, sy = 1 / h;
    return `matrix3d(${m.a * sx},${m.d * sx},0,${m.g * sx},${m.b * sy},${m.e * sy},0,${m.h * sy},0,0,1,0,${m.c},${m.f},0,1)`;
  }
  // Only convex quads in TL, TR, BR, BL order (clockwise on screen) are accepted, so a surface cannot fold over itself.
  function convex(q) {
    let sign = 0;
    for (let i = 0; i < 4; i++) {
      const [ax, ay] = q[i], [bx, by] = q[(i + 1) % 4], [cx, cy] = q[(i + 2) % 4];
      const z = (bx - ax) * (cy - by) - (by - ay) * (cx - bx);
      if (Math.abs(z) < 1e-9) return false;
      if (sign && Math.sign(z) !== sign) return false;
      sign = Math.sign(z);
    }
    return sign > 0;
  }
  function insideQuad(q, x, y) {
    let inside = false;
    for (let i = 0, j = 3; i < 4; j = i++) {
      const [xi, yi] = q[i], [xj, yj] = q[j];
      if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
  }

  // --- gap modes ----------------------------------------------------------------------------------------------------
  // "include": each blind shows its stored rect, so the real gaps between blinds stay empty parts of the master canvas.
  // "ignore": the empty space between blinds is removed and the master canvas is spread over the blinds only, so the
  // picture runs on directly from one blind to the next. Both read the same stored rects.
  function compressor(intervals) {
    const sorted = intervals.map(i => i.slice()).sort((p, q) => p[0] - q[0]);
    const merged = [];
    for (const [a, b] of sorted) {
      const last = merged[merged.length - 1];
      if (last && a <= last[1]) last[1] = Math.max(last[1], b); else merged.push([a, b]);
    }
    const total = merged.reduce((t, [a, b]) => t + (b - a), 0);
    return v => {
      let acc = 0;
      for (const [a, b] of merged) { if (v <= a) break; acc += Math.min(v, b) - a; }
      return total ? acc / total : 0;
    };
  }
  function crops(booth) {
    const rects = booth.surfaces.map(s => s.rect);
    if (booth.gap_mode !== 'ignore') return rects.map(r => ({...r}));
    const cx = compressor(rects.map(r => [r.x, r.x + r.w])), cy = compressor(rects.map(r => [r.y, r.y + r.h]));
    return rects.map(r => { const x = cx(r.x), y = cy(r.y); return {x, y, w: cx(r.x + r.w) - x, h: cy(r.y + r.h) - y}; });
  }

  // --- measured size (2026-10-15 night) ---------------------------------------------------------------------------
  // real_w / real_h: the blind's measured width and height (same unit, only the ratio counts). Both > 0 = the content
  // plane of that blind has that ratio: its crop (master canvas px) is narrowed, centred, to width / height = real
  // ratio, and the corners warp that plane as before. Otherwise (none, one, 0 or negative) the crop is used as it is.
  function realAspect(s) {
    const w = Number(s.real_w), h = Number(s.real_h);
    return w > 0 && h > 0 && isFinite(w) && isFinite(h) ? w / h : null;
  }
  function plane(c, s, MW, MH) {
    let x = c.x * MW, y = c.y * MH, w = c.w * MW, h = c.h * MH;
    const A = realAspect(s);
    if (A) { if (w / h > A) { const nw = h * A; x += (w - nw) / 2; w = nw; } else { const nh = w / A; y += (h - nh) / 2; h = nh; } }
    return {x, y, w, h};
  }
  // Uniformly zoom the single master artwork around its centre before all blind crops are taken. Width and height use
  // the same scale, so the artwork's aspect ratio never changes and continuity between blinds is preserved. The
  // destination plane and four mapping corners do not move; only the outer part of the source artwork is cropped.
  function sourcePlane(p, MW, MH, zoom) {
    const z = Math.max(1, Math.min(2, Number(zoom) || 1));
    return {x: MW / 2 + (p.x - MW / 2) / z, y: MH / 2 + (p.y - MH / 2) / z, w: p.w / z, h: p.h / z};
  }

  // --- connection test pattern ------------------------------------------------------------------------------------
  // Continuous grid with percent labels, diagonals and a circle across the whole master canvas, plus a sweep line
  // that moves across all blinds, so breaks and continuity between blinds are visible. Not an artwork.
  function drawPattern(ctx, W, H, booth, frame, roleName) {
    ctx.fillStyle = '#000'; ctx.fillRect(0, 0, W, H);
    ctx.lineWidth = Math.max(1, W / 800);
    ctx.strokeStyle = '#444'; ctx.fillStyle = '#888';
    ctx.font = `${Math.round(H / 30)}px ui-monospace, Menlo, monospace`; ctx.textBaseline = 'top'; ctx.textAlign = 'left';
    ctx.beginPath();
    for (let i = 1; i < 20; i++) { const x = W * i / 20; ctx.moveTo(x, 0); ctx.lineTo(x, H); }
    for (let i = 1; i < 10; i++) { const y = H * i / 10; ctx.moveTo(0, y); ctx.lineTo(W, y); }
    ctx.stroke();
    for (let i = 0; i < 20; i += 2) ctx.fillText(`${i * 5}%`, W * i / 20 + 4, 4);
    ctx.strokeStyle = '#fff'; ctx.lineWidth = Math.max(2, W / 400);
    ctx.strokeRect(1, 1, W - 2, H - 2);
    ctx.beginPath(); ctx.moveTo(0, 0); ctx.lineTo(W, H); ctx.moveTo(W, 0); ctx.lineTo(0, H); ctx.stroke();
    ctx.beginPath(); ctx.arc(W / 2, H / 2, H * 0.42, 0, Math.PI * 2); ctx.stroke();
    ctx.fillStyle = '#fff'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.font = `600 ${Math.round(H / 8)}px ui-monospace, Menlo, monospace`;
    ctx.fillText(`${booth.name} · MASTER`, W / 2, H / 2);
    ctx.font = `${Math.round(H / 22)}px ui-monospace, Menlo, monospace`;
    ctx.fillText(`${booth.gap_mode === 'ignore' ? '간격 무시' : '간격 포함'} · 비율 ${booth.aspect}${roleName ? ` · ${roleName}` : ''}`, W / 2, H / 2 + H / 9);
    const sx = (frame * W / 400) % W;
    ctx.fillRect(sx, H * 0.8, Math.max(3, W / 300), H * 0.12);
  }

  // --- renderer -----------------------------------------------------------------------------------------------------
  // container: element whose rect is the output viewport (corners are 0–1 of it).
  // source: canvas drawn by the page (its diagnostic visualization) or null; used when booth.content === 'source'.
  // forceSource: always map the source canvas, whatever the booth's "출력 내용" (the simulator's 맵핑 확인 view only)
  function createRenderer({container, source = null, labels = true, forcePattern = false, forceSource = false}) {
    let booth = null, frame = 0, raf = 0, roleName = '';
    let patternOverride = !!forcePattern, sourceOverride = !!forceSource;
    const pattern = document.createElement('canvas'), pctx = pattern.getContext('2d');
    const layer = document.createElement('div');
    layer.className = 'mapout-layer';
    layer.style.cssText = 'position:absolute;inset:0;overflow:hidden;pointer-events:none';
    container.append(layer);
    const els = new Map();  // surface id -> {div, canvas, ctx}

    function masterSize(aspect) {
      return aspect >= 1 ? [MASTER_LONG, Math.max(1, Math.round(MASTER_LONG / aspect))] : [Math.max(1, Math.round(MASTER_LONG * aspect)), MASTER_LONG];
    }
    function useSource() { return source && booth && (booth.content === 'source' || sourceOverride) && !patternOverride; }
    function sizeSource() {
      if (!source || !booth) return;
      // The page keeps drawing into its own canvas; it is sized to the booth's master canvas and hidden.
      const [w, h] = masterSize(booth.aspect), dpr = window.devicePixelRatio || 1;
      source.style.cssText = `position:fixed;left:0;top:0;right:auto;bottom:auto;inset:auto;width:${w / dpr}px;height:${h / dpr}px;visibility:hidden;pointer-events:none`;
    }
    function setBooth(next) {
      const aspectChanged = !booth || booth.aspect !== next.aspect;
      booth = next;
      if (aspectChanged) sizeSource();
      const ids = new Set(booth.surfaces.map(s => s.id));
      for (const [id, el] of els) if (!ids.has(id)) { el.div.remove(); els.delete(id); }
      for (const s of booth.surfaces) if (!els.has(s.id)) {
        const div = document.createElement('div'), canvas = document.createElement('canvas');
        div.className = 'mapout-surface'; div.dataset.surface = s.id;
        div.style.cssText = 'position:absolute;left:0;top:0;transform-origin:0 0';
        canvas.style.cssText = 'display:block;width:100%;height:100%';
        div.append(canvas); layer.append(div);
        els.set(s.id, {div, canvas, ctx: canvas.getContext('2d')});
      }
      // keep DOM order = list order (later surfaces on top)
      for (const s of booth.surfaces) layer.append(els.get(s.id).div);
    }
    function render() {
      raf = requestAnimationFrame(render);
      if (!booth) return;
      frame++;
      let master, MW, MH;
      const sourceOn = useSource();
      if (sourceOn) { master = source; MW = source.width; MH = source.height; }
      else {
        [MW, MH] = masterSize(booth.aspect);
        if (pattern.width !== MW || pattern.height !== MH) { pattern.width = MW; pattern.height = MH; }
        drawPattern(pctx, MW, MH, booth, frame, roleName);
        master = pattern;
      }
      const r = container.getBoundingClientRect(), cr = crops(booth);
      booth.surfaces.forEach((s, i) => {
        const el = els.get(s.id), c = plane(cr[i], s, MW, MH);   // destination plane: crop, or its real-ratio centre
        const src = sourceOn ? sourcePlane(c, MW, MH, booth.source_zoom) : c;
        const sw = Math.max(1, Math.round(c.w)), sh = Math.max(1, Math.round(c.h));
        if (el.canvas.width !== sw || el.canvas.height !== sh) { el.canvas.width = sw; el.canvas.height = sh; el.div.style.width = sw + 'px'; el.div.style.height = sh + 'px'; }
        el.ctx.drawImage(master, src.x, src.y, src.w, src.h, 0, 0, sw, sh);
        if (labels && !sourceOn) {
          el.ctx.fillStyle = '#fff'; el.ctx.textAlign = 'left'; el.ctx.textBaseline = 'bottom';
          el.ctx.font = `600 ${Math.max(12, Math.round(sh / 14))}px ui-monospace, Menlo, monospace`;
          el.ctx.fillText(s.name, 8, sh - 8);
        }
        el.div.style.transform = matrix3d(s.corners.map(([x, y]) => [x * r.width, y * r.height]), sw, sh);
      });
    }
    render();
    return {
      setBooth,
      setRole(name) { roleName = name || ''; },
      setPreviewMode(mode) { patternOverride = mode !== 'source'; sourceOverride = mode === 'source'; },
      destroy() { cancelAnimationFrame(raf); layer.remove(); },
      get booth() { return booth; },
    };
  }

  // --- server connection --------------------------------------------------------------------------------------------
  // onState(msg) gets every server state; returns {send(op, final), client, connected}.
  function connect(onState, onStatus) {
    const client = Math.random().toString(36).slice(2, 10);
    let ws = null, seq = 0;
    function open() {
      const socket = new WebSocket(`ws://${location.host}/ws/mapping`);
      let fresh = true;
      socket.onopen = () => { ws = socket; };
      socket.onmessage = e => {
        const msg = JSON.parse(e.data);
        if (msg.type === 'state') { onState(msg, fresh); fresh = false; }
        else if (msg.type === 'error' && onStatus) onStatus('error', msg.error);
      };
      socket.onclose = () => { if (ws === socket) ws = null; if (onStatus) onStatus('offline'); setTimeout(open, 1000); };
    }
    open();
    return {
      client,
      get seq() { return seq; },
      get connected() { return !!ws && ws.readyState === 1; },
      send(op, final) {
        seq++;
        if (!ws || ws.readyState !== 1) return 0;
        ws.send(JSON.stringify({type: 'op', client, seq, final: !!final, op}));
        return seq;
      },
    };
  }

  // --- exhibition outputs (display.html) -----------------------------------------------------------------------
  // A space's projector window (/display/blind?space=a|b&clean, also the old /output/<role>?space=…&clean) maps its own
  // canvas (source, the blind preset drawn by tofrender.js) onto booth <space>'s blinds. Which role's blind preset it
  // draws follows the server's assignment: onRole(role) is called on connect and after every 혈 ↔ 맥 swap, and the page
  // switches its preset in the same window (before 2026-10-15 the window moved to another address instead).
  function startSpaceOutput({space, source, onRole, forceSource = false}) {
    const container = document.createElement('div');
    container.id = 'mapout';
    container.style.cssText = 'position:fixed;inset:0;background:#000;overflow:hidden;z-index:5';
    document.body.append(container);
    const renderer = createRenderer({container, source, labels: true, forceSource});
    let role = null;
    connect(msg => {
      const next = msg.assignment && msg.assignment[space];
      if (next && next !== role) { role = next; onRole(role); }
      renderer.setRole(msg.roles && msg.roles[role]);
      renderer.setBooth(msg.booths[space]);
    });
    return renderer;
  }

  window.RipplesMap = {createRenderer, connect, crops, realAspect, plane, sourcePlane,
    matrix3d, convex, insideQuad, squareToQuad, startSpaceOutput};
})();
