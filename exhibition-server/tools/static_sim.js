
// --- static snapshot (tools/export_static_venue3d.py): no server, the state lives in this page ------------------------
// A small stand-in for simulator_store.py (user 2026-11-07: "사람 걸어다니는 거 로컬로", 타기 · 뺏기): visitors walk the
// floor (A* on a 150 mm grid from the page's own geometry, venue3d.geometry: fixed + layout solids), stand looking at
// 심 / 혈 / 맥, and in 수집 one walks up to each device and rides it while it roams the gallery for a while, then brings
// it back to its parking box and steps off. The page's visitor (관람 시뮬) takes a person over and gets on / off / takes a
// device over with the same ops as the server. Simpler than the server: people only wait for each other and the devices
// (no dodging), devices ignore collisions (as when driven here), no ToF data.
(() => {
  const MOVE = 400, TURN = 30, BACK = 600;   // mm/s, deg/s (server MOTION top speeds), ToF behind the 혈 centre (mm)
  const G = 150, PR = 250, CLEAR = 150, WALK = [550, 800], DEV_R = 690, ROAM = [35, 55], BETWEEN = [3, 12], LOOK = [6, 14];   // a gallery stroll (user: too fast)
  const GALLERY = [-462, 90, 11149, 19851];   // devices stay in here (the gallery's walkable box)
  const rand = (a, b) => a + Math.random() * (b - a), wrap = a => ((a % 360) + 360) % 360, dAng = (a, b) => ((b - a + 540) % 360) - 180;
  const inPoly = (pl, x, y) => { let c = false; for (let i = 0, j = pl.length - 1; i < pl.length; j = i++) { const [xi, yi] = pl[i], [xj, yj] = pl[j];
    if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) c = !c; } return c; };
  const nearPoly = (pl, x, y, r) => { for (let i = 0; i < pl.length; i++) { const a = pl[i], b = pl[(i + 1) % pl.length], ex = b[0] - a[0], ey = b[1] - a[1];
    const t = Math.max(0, Math.min(1, ((x - a[0]) * ex + (y - a[1]) * ey) / (ex * ex + ey * ey || 1))); if (Math.hypot(x - a[0] - ex * t, y - a[1] - ey * t) < r) return true; } return false; };

  function connect(onState) {
    let state = null, clock = 0, last = 0;
    const input = {hyeol: {move: 0, turn: 0, strafe: 0, speed: 1}, maek: {move: 0, turn: 0, strafe: 0, speed: 1}};
    const sess = {hyeol: {next: 2}, maek: {next: 4}};   // per device: when the next visitor is called (s, clock), the session
    const tof = () => { const p = state.devices.hyeol, h = p.heading * Math.PI / 180;
      state.tof_pose = {x: p.x - Math.sin(h) * BACK, y: p.y - Math.cos(h) * BACK, heading: (p.heading + 180) % 360}; };
    const emit = () => { state.revision++; state.version++; tof(); onState(JSON.parse(JSON.stringify(state, (k, v) => k[0] === '_' ? undefined : v)), false); };

    // --- grids: where a person (with room to spare) / a device may stand; rebuilt when the page's geometry changes -------
    let grids = null, gridsOf = null;
    function buildGrids(g) {
      const b = g.bounds, nx = Math.ceil((b.x1 - b.x0) / G), ny = Math.ceil((b.y1 - b.y0) / G), walk = g.walkable;
      const sol = (zmax, r) => g.solids.filter(o => o.poly && o.poly.length > 2 && o.z0 < zmax).map(o => { const xs = o.poly.map(q => q[0]), ys = o.poly.map(q => q[1]);
        return {poly: o.poly, x0: Math.min(...xs) - r, y0: Math.min(...ys) - r, x1: Math.max(...xs) + r, y1: Math.max(...ys) + r}; });
      const ps = sol(1900, PR + CLEAR), ds = sol(2350, DEV_R);
      const hit = (list, x, y, r) => list.some(o => x > o.x0 && x < o.x1 && y > o.y0 && y < o.y1 && (inPoly(o.poly, x, y) || nearPoly(o.poly, x, y, r)));
      const person = new Uint8Array(nx * ny), dev = new Uint8Array(nx * ny);
      for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
        const x = b.x0 + (i + 0.5) * G, y = b.y0 + (j + 0.5) * G; if (!inPoly(walk, x, y)) continue;
        if (!nearPoly(walk, x, y, PR + CLEAR) && !hit(ps, x, y, PR + CLEAR)) person[j * nx + i] = 1;
        if (x > GALLERY[0] && x < GALLERY[2] && y > GALLERY[1] && y < GALLERY[3] && !nearPoly(walk, x, y, DEV_R) && !hit(ds, x, y, DEV_R)) dev[j * nx + i] = 1;
      }
      for (const a of [person, dev]) {   // only the main part (pockets behind walls out, as the server)
        const comp = new Int32Array(nx * ny).fill(-1); let best = -1, bestN = 0, c = 0;
        for (let k0 = 0; k0 < a.length; k0++) { if (!a[k0] || comp[k0] >= 0) continue; let n = 0; const st = [k0]; comp[k0] = c;
          while (st.length) { const k = st.pop(), i = k % nx, j = (k - i) / nx; n++;
            for (const [p, q] of [[i + 1, j], [i - 1, j], [i, j + 1], [i, j - 1]]) { const kk = q * nx + p; if (p >= 0 && p < nx && q >= 0 && q < ny && a[kk] && comp[kk] < 0) { comp[kk] = c; st.push(kk); } } }
          if (n > bestN) { bestN = n; best = c; } c++; }
        for (let k = 0; k < a.length; k++) if (comp[k] !== best) a[k] = 0;
      }
      const open = new Uint8Array(nx * ny), E = 3;   // standing spots: 3 cells (450 mm) clear all round, so nobody looks out of a corner
      for (let j = E; j < ny - E; j++) for (let i = E; i < nx - E; i++) { if (!person[j * nx + i]) continue; let ok = true;
        for (let dj = -E; dj <= E && ok; dj++) for (let di = -E; di <= E; di++) if (!person[(j + dj) * nx + i + di]) { ok = false; break; }
        if (ok) open[j * nx + i] = 1; }
      return {b, nx, ny, person, dev, open};
    }
    const cellOf = (x, y) => [Math.floor((x - grids.b.x0) / G), Math.floor((y - grids.b.y0) / G)];
    const okAt = (a, x, y) => { const [i, j] = cellOf(x, y); return i >= 0 && j >= 0 && i < grids.nx && j < grids.ny && !!a[j * grids.nx + i]; };
    function nearestFree(a, x, y, rmax = 25) {
      const [ci, cj] = cellOf(x, y);
      for (let r = 0; r <= rmax; r++) for (let j = cj - r; j <= cj + r; j++) for (let i = ci - r; i <= ci + r; i++) {
        if (Math.max(Math.abs(i - ci), Math.abs(j - cj)) !== r || i < 0 || j < 0 || i >= grids.nx || j >= grids.ny || !a[j * grids.nx + i]) continue;
        return [grids.b.x0 + (i + 0.5) * G, grids.b.y0 + (j + 0.5) * G];
      }
      return null;
    }
    function route(a, x0, y0, x1, y1) {   // A* (8 neighbours) over a grid; waypoints every 3rd cell, the goal last
      const {nx, ny} = grids, s = nearestFree(a, x0, y0, 6), t = nearestFree(a, x1, y1, 6); if (!s || !t) return null;
      const [si, sj] = cellOf(...s), [ti, tj] = cellOf(...t), start = sj * nx + si, goal = tj * nx + ti;
      const gs = new Float32Array(nx * ny).fill(Infinity), from = new Int32Array(nx * ny).fill(-1), heap = [[0, start]]; gs[start] = 0;
      const h = k => { const i = k % nx, j = (k - i) / nx; return Math.hypot(i - ti, j - tj); };
      const push = e => { heap.push(e); let n = heap.length - 1; while (n) { const p = (n - 1) >> 1; if (heap[p][0] <= heap[n][0]) break; [heap[p], heap[n]] = [heap[n], heap[p]]; n = p; } };
      const pop = () => { const top = heap[0], e = heap.pop(); if (heap.length) { heap[0] = e; let n = 0; for (;;) { const l = 2 * n + 1, r = l + 1; let m = n;
        if (l < heap.length && heap[l][0] < heap[m][0]) m = l; if (r < heap.length && heap[r][0] < heap[m][0]) m = r; if (m === n) break; [heap[m], heap[n]] = [heap[n], heap[m]]; n = m; } } return top; };
      let steps = 0;
      while (heap.length && steps++ < 60000) {
        const [, k] = pop(); if (k === goal) break;
        const i = k % nx, j = (k - i) / nx;
        for (let dj = -1; dj <= 1; dj++) for (let di = -1; di <= 1; di++) { if (!di && !dj) continue;
          const p = i + di, q = j + dj; if (p < 0 || q < 0 || p >= nx || q >= ny) continue; const kk = q * nx + p; if (!a[kk]) continue;
          if (di && dj && (!a[j * nx + p] || !a[q * nx + i])) continue;
          const g2 = gs[k] + (di && dj ? 1.414 : 1); if (g2 < gs[kk]) { gs[kk] = g2; from[kk] = k; push([g2 + h(kk), kk]); } }
      }
      if (from[goal] < 0 && goal !== start) return null;
      const cells = []; for (let k = goal; k >= 0 && k !== start; k = from[k]) cells.push(k); cells.reverse();
      const pts = cells.filter((_, n) => n % 3 === 2 || n === cells.length - 1).map(k => { const i = k % nx, j = (k - i) / nx; return [grids.b.x0 + (i + 0.5) * G, grids.b.y0 + (j + 0.5) * G]; });
      pts.push([x1, y1]);
      const sight = (ax, ay, bx, by) => { const L = Math.hypot(bx - ax, by - ay); for (let k = 0; k < L; k += 100) if (!okAt(a, ax + (bx - ax) * k / L, ay + (by - ay) * k / L)) return false; return true; };
      const out = []; let cx = x0, cy = y0, n = 0;   // straightened: on to the farthest point in plain sight
      while (n < pts.length) { let far = n; for (let m = pts.length - 1; m > n; m--) if (sight(cx, cy, ...pts[m])) { far = m; break; } out.push(pts[far]); [cx, cy] = pts[far]; n = far + 1; }
      return out;
    }

    // --- people ---------------------------------------------------------------------------------------------------
    const free = p => !p.held && !p.riding && !p._board;
    function lookTarget(p) {   // a work to look at (심 more often, 혈 / 맥), from an open spot at a viewing distance, not where someone stands
      const L = state.layout || {}, sim = L.sim || {x: 5700, y: 9970}, r = Math.random();
      const dv = Math.random() < 0.5 ? 'hyeol' : 'maek', q = state.devices[dv], front = (q.heading + (dv === 'maek' ? 180 : 0)) * Math.PI / 180;   // 맥's model faces back
      const [cx, cy, r0, r1, a0, spread] = r < 0.6 ? [sim.x, sim.y, 2000, 3200, 0, Math.PI] : [q.x, q.y, 1700, 2600, front, 80 * Math.PI / 180];   // a device: from its front side (not standing behind it, user)
      for (let k = 0; k < 16; k++) {
        const a = a0 + rand(-spread, spread), rad = rand(r0, r1), x = cx + Math.sin(a) * rad, y = cy + Math.cos(a) * rad;
        if (!okAt(grids.open, x, y) || state.people.some(o => o !== p && !o.away && Math.hypot(o.x - x, o.y - y) < 900)) continue;
        return {x, y, fx: cx, fy: cy};
      }
      return null;
    }
    function boardSpot(p, d) {   // behind the device first, then round it; one with a way there
      const q = state.devices[d];
      for (const dist of [1400, 1700, 1200]) for (const off of [0, 35, -35, 70, -70, 110, -110, 145, -145, 180]) {
        const h = (q.heading + 180 + off) * Math.PI / 180, x = q.x + Math.sin(h) * dist, y = q.y + Math.cos(h) * dist;
        if (!okAt(grids.person, x, y)) continue; const path = route(grids.person, p.x, p.y, x, y); if (path) return {x, y, path};
      }
      return null;
    }
    function stepOff(p, d) {   // a rider gets off by the device: a free spot behind it, else round it
      const q = state.devices[d];
      for (const off of [0, 40, -40, 80, -80, 130, -130, 180]) { const h = (q.heading + 180 + off) * Math.PI / 180, x = q.x + Math.sin(h) * 1300, y = q.y + Math.cos(h) * 1300;
        if (okAt(grids.person, x, y)) { Object.assign(p, {x, y, heading: wrap(q.heading + 180 + off), riding: null, away: false, v: 0}); p._wait = 1; return; } }
      const s = nearestFree(grids.person, q.x, q.y, 30) || [q.x, q.y]; Object.assign(p, {x: s[0], y: s[1], riding: null, away: false, v: 0});
    }
    function personTick(p, dt) {
      if (p.held || p.riding) return;
      if (p._wait > 0) { p._wait -= dt; p.v = 0;
        if (p._face) { let want = Math.atan2(p._face[0] - p.x, p._face[1] - p.y) * 180 / Math.PI;
          const vis = state.people.find(o => o.held && o !== p && Math.hypot(o.x - p.x, o.y - p.y) < 4000);   // not into the 관람 시뮬 visitor's eyes (as the server)
          if (vis) { const off = dAng(want, Math.atan2(vis.x - p.x, vis.y - p.y) * 180 / Math.PI); if (Math.abs(off) < 30) want -= Math.sign(off) * 35; }
          p.heading = wrap(p.heading + Math.max(-90 * dt, Math.min(90 * dt, dAng(p.heading, want)))); }
        return; }
      if (!p._path) {
        if (p._board) { const s = state.mode === 'collect' && boardSpot(p, p._board); if (!s) { sess[p._board].next = clock + 3; p._board = null; p._wait = 1; return; } p._path = s.path; p._face = null; }
        else { const t = lookTarget(p); const path = t && route(grids.person, p.x, p.y, t.x, t.y); if (!path) { p._wait = 1; return; } p._path = path; p._face = [t.fx, t.fy]; }
        p._speed = rand(...WALK); p._blocked = 0;
      }
      const [tx, ty] = p._path[0], dx = tx - p.x, dy = ty - p.y, dist = Math.hypot(dx, dy);
      if (dist < 120) { p._path.shift();
        if (!p._path.length) { p._path = null;
          if (p._board) { const d = p._board; p._board = null; if (state.mode === 'collect' && !state.rider[d] && !riderOf(d)) { board(p, d); return; } sess[d].next = clock + 3; }
          p._wait = p._face ? rand(...LOOK) : 0.5; }
        return; }
      const step = Math.min(dist, p._speed * dt), nx = p.x + dx / dist * step, ny = p.y + dy / dist * step;
      const busy = state.people.some(o => o !== p && !o.riding && !o.away && Math.hypot(o.x - nx, o.y - ny) < 2 * PR + 80 && (o.x - p.x) * dx + (o.y - p.y) * dy > 0)
        || ['hyeol', 'maek'].some(d => d !== p._board && Math.hypot(state.devices[d].x - nx, state.devices[d].y - ny) < DEV_R + PR + 40);   // not the one they walk up to
      if (busy) {   // someone in the way: a step aside if there is room, else wait; after a while another way
        const sx = -dy / dist, sy = dx / dist, side = (p.id.charCodeAt(p.id.length - 1) % 2 ? 1 : -1);
        for (const sg of [side, -side]) { const ax = p.x + sx * sg * step, ay = p.y + sy * sg * step;
          if (okAt(grids.person, ax, ay) && !state.people.some(o => o !== p && !o.riding && !o.away && Math.hypot(o.x - ax, o.y - ay) < 2 * PR + 60)
              && !['hyeol', 'maek'].some(d => d !== p._board && Math.hypot(state.devices[d].x - ax, state.devices[d].y - ay) < DEV_R + PR + 40)) { p.x = ax; p.y = ay; p.v = p._speed * 0.6; p._blocked += dt * 0.5; return; } }
        p.v = 0; p._blocked += dt; if (p._blocked > 2.5) { p._path = null; p._wait = rand(0.3, 1.2); } return; }
      p._blocked = 0; p.x = nx; p.y = ny; p.v = p._speed;
      p.heading = wrap(p.heading + Math.max(-240 * dt, Math.min(240 * dt, dAng(p.heading, Math.atan2(dx, dy) * 180 / Math.PI))));
    }
    const riderOf = d => state.people.find(p => p.riding === d);

    // --- sessions: a visitor rides a device round the gallery, then it goes home -----------------------------------
    function board(p, d) {
      Object.assign(p, {riding: d, away: true, x: -1e6, y: -1e6, v: 0}); p._path = null;
      state.auto[d] = true; state.rider[d] = true;
      sess[d] = {next: Infinity, phase: 'roam', until: clock + rand(...ROAM), path: null, wait: 1};
    }
    function endSession(d) {
      const p = riderOf(d); if (p && !p.held) stepOff(p, d);
      state.auto[d] = false; state.rider[d] = false; sess[d] = {next: clock + rand(...BETWEEN)};
    }
    // a device moves only where it may stand (the device grid: walls, pieces, the gallery), sliding along what it meets; it
    // stops for a person or the other device just ahead (user 2026-11-07: they went through people and walls)
    function moveDevice(d, mx, my) {
      const q = state.devices[d]; if (!mx && !my) return true;
      const L = Math.hypot(mx, my), ahead = (x, y) => (x - q.x) * mx + (y - q.y) * my > 0;
      if (state.people.some(p => !p.riding && !p.away && !p.held && ahead(p.x, p.y) && Math.hypot(p.x - q.x - mx, p.y - q.y - my) < DEV_R + PR)) return false;
      const o = state.devices[d === 'hyeol' ? 'maek' : 'hyeol']; if (ahead(o.x, o.y) && Math.hypot(o.x - q.x - mx, o.y - q.y - my) < 2 * DEV_R - 100) return false;
      if (!grids || !okAt(grids.dev, q.x, q.y)) { q.x += mx; q.y += my; return true; }   // already off the grid (put there): let it out
      if (okAt(grids.dev, q.x + mx, q.y + my)) { q.x += mx; q.y += my; return true; }
      if (okAt(grids.dev, q.x + mx, q.y)) { q.x += mx; return L < 1 || Math.abs(mx) > Math.abs(my) * 0.3; }
      if (okAt(grids.dev, q.x, q.y + my)) { q.y += my; return L < 1 || Math.abs(my) > Math.abs(mx) * 0.3; }
      return false;
    }
    function drive(d, tx, ty, dt, slow) {   // turn toward a point, go when facing it; true when there
      const q = state.devices[d], dx = tx - q.x, dy = ty - q.y, dist = Math.hypot(dx, dy); if (dist < 180) return true;
      const diff = dAng(q.heading, Math.atan2(dx, dy) * 180 / Math.PI); q.heading = wrap(q.heading + Math.max(-45 * dt, Math.min(45 * dt, diff)));
      let v = Math.abs(diff) < 20 ? MOVE * (slow ? Math.min(1, Math.max(0.3, dist / 1200)) : 1) : 0; const h = q.heading * Math.PI / 180;
      if (v && !moveDevice(d, Math.sin(h) * v * dt, Math.cos(h) * v * dt)) { v = 0; sess[d].stuck = (sess[d].stuck || 0) + dt;
        if (sess[d].stuck > 4) { sess[d].stuck = 0; sess[d].path = null; } }   // boxed in: another way
      else sess[d].stuck = 0;
      state.motion[d] = {...(state.motion[d] || {}), v, s: 0, w: diff > 0 ? 45 : -45};
      return false;
    }
    function sessionTick(d, dt) {
      const s = sess[d], i = input[d];
      if (i.move || i.turn || i.strafe) return;   // the page drives it
      if (!state.auto[d] || s.next !== Infinity) {   // no session: call a visitor when due (수집)
        if (state.mode !== 'collect' || state.rider[d] || riderOf(d) || clock < s.next || state.people.some(p => p._board === d)) return;
        const q = state.devices[d], cand = state.people.filter(p => free(p) && !p.away);
        if (!cand.length) { s.next = clock + 5; return; }
        const p = cand.reduce((a, b) => Math.hypot(a.x - q.x, a.y - q.y) < Math.hypot(b.x - q.x, b.y - q.y) ? a : b);
        p._board = d; p._path = null; p._wait = 0; s.next = clock + 60; return;   // 60 s to get there
      }
      if (s.wait > 0) { s.wait -= dt; state.motion[d] = {...(state.motion[d] || {}), v: 0, w: 0}; return; }
      const home = state.start[d];
      if (s.phase === 'roam' && clock > s.until) { s.phase = 'home'; s.path = route(grids.dev, state.devices[d].x, state.devices[d].y, home.x, home.y) || [[home.x, home.y]]; }
      if (s.phase === 'align') { const q = state.devices[d], diff = dAng(q.heading, home.heading); q.heading = wrap(q.heading + Math.max(-45 * dt, Math.min(45 * dt, diff)));
        if (Math.abs(diff) < 2) { Object.assign(q, home); endSession(d); } return; }
      if (!s.path || !s.path.length) {
        if (s.phase === 'home') { s.phase = 'align'; return; }
        const t = nearestFree(grids.dev, rand(GALLERY[0], GALLERY[2]), rand(GALLERY[1], GALLERY[3]), 20);
        s.path = t && route(grids.dev, state.devices[d].x, state.devices[d].y, t[0], t[1]); if (!s.path) { s.wait = 1; return; }
      }
      if (drive(d, ...s.path[0], dt, s.phase === 'home' && s.path.length === 1)) { s.path.shift(); if (!s.path.length && s.phase === 'roam') s.wait = rand(1, 3); }
    }

    // --- parking boxes (as venue.parking_starts): fixed in the layout (시안 2) or on their screen walls ------------------
    function parkingStarts(L) {
      const out = {...(L && L.parking || {})}, spec = (window.venue3d && window.venue3d.geometry && window.venue3d.geometry.layout || {}).spec || {};
      const walls = Object.fromEntries((L && L.small_walls || []).map(w => [w.id, w]));
      for (const [d, c] of Object.entries(spec.parking || {})) { const w = c && walls[c.wall]; if (!w || out[d]) continue;
        const a = w.heading * Math.PI / 180, ux = Math.cos(a), uy = -Math.sin(a), fx = Math.sin(a), fy = Math.cos(a);
        out[d] = {x: w.x + ux * c.u + fx * c.n, y: w.y + uy * c.u + fy * c.n, heading: wrap(w.heading + c.dh)}; }
      return out;
    }

    function tick() {
      const now = performance.now(), dt = Math.min(0.1, (now - last) / 1000); last = now; clock += dt;
      if (!state) return;
      const g = window.venue3d && window.venue3d.geometry;
      if (g && g.solids && gridsOf !== g.solids) { gridsOf = g.solids; grids = buildGrids(g);
        for (const p of state.people) if (!p.riding && !p.held && !okAt(grids.person, p.x, p.y)) { const s = nearestFree(grids.person, p.x, p.y, 40); if (s) { p.x = s[0]; p.y = s[1]; p._path = null; } } }
      for (const d of ['hyeol', 'maek']) { const i = input[d]; if (!i.move && !i.turn && !i.strafe) continue;   // the page drives
        const p = state.devices[d], k = i.speed || 1; p.heading = wrap(p.heading + TURN * k * i.turn * dt);
        const h = p.heading * Math.PI / 180, ok = moveDevice(d, (Math.sin(h) * i.move + Math.cos(h) * i.strafe) * MOVE * k * dt, (Math.cos(h) * i.move - Math.sin(h) * i.strafe) * MOVE * k * dt);
        state.motion[d] = {...(state.motion[d] || {}), v: ok ? i.move * MOVE * k : 0, s: ok ? i.strafe * MOVE * k : 0, w: i.turn * TURN * k, blocked: ok ? null : 'wall'}; }
      if (grids) { for (const p of state.people) personTick(p, dt); for (const d of ['hyeol', 'maek']) sessionTick(d, dt); }
      emit();
    }

    fetch('state.json', {cache: 'no-store'}).then(r => r.json()).then(s => {
      state = s; state.auto = {hyeol: false, maek: false}; state.rider = {hyeol: false, maek: false}; state.mode = state.mode || 'collect';
      for (const d of ['hyeol', 'maek']) if (state.start && state.start[d]) Object.assign(state.devices[d], state.start[d]);   // 혈 / 맥 start on their parking boxes (user)
      for (const p of state.people) { delete p.parts; Object.assign(p, {riding: null, held: false, away: false, v: 0});
        if (Math.abs(p.x) > 1e5) { p.x = 5700 + rand(-2500, 2500); p.y = 9970 + rand(-3500, 3500); } }   // was riding: back on the floor
      tof(); onState(JSON.parse(JSON.stringify(state)), true); last = performance.now(); setInterval(tick, 50);
    });
    return {
      client: 'static', seq: 0, get connected() { return !!state; },
      send(op) {
        if (!state) return 0;
        if (op.op === 'layout') {
          state.layout = op.reset ? JSON.parse(JSON.stringify(state.default_layout)) : op.set; state.layout_revision++;
          const st = parkingStarts(state.layout), preset = op.set && 'parking' in op.set;
          for (const [d, h] of Object.entries(st)) { const o = state.start[d], q = state.devices[d];
            if (preset || (o && Math.hypot(q.x - o.x, q.y - o.y) < 150 && !state.rider[d])) { if (state.rider[d] && !preset) continue; if (riderOf(d) && preset) endSession(d); Object.assign(q, h); }
            state.start[d] = h; }
          emit(); }
        else if (op.op === 'pose') { Object.assign(state.devices[op.device], op.set); emit(); }
        else if (op.op === 'mode') { state.mode = op.mode; for (const p of state.people) p._board = null;
          for (const d of ['hyeol', 'maek']) { if (op.mode === 'learn' || op.mode === 'replay') Object.assign(state.devices[d], state.start[d]);
            if (riderOf(d) || state.auto[d]) endSession(d); sess[d].next = op.mode === 'collect' ? clock : Infinity; }
          emit(); }
        else if (op.op === 'auto') { if (!op.on) endSession(op.device); emit(); }
        else if (op.op === 'attach' && state.hyeol) { state.hyeol.attached = !!op.attached; state.hyeol.label = op.attached ? '사용 중' : '아이들링'; emit(); }
        else if (op.op === 'drive') input[op.device] = {move: op.move || 0, turn: op.turn || 0, strafe: op.strafe || 0, speed: op.speed || 1};
        else if (op.op === 'person') { const p = state.people.find(q => q.id === op.id); if (p) {
          if (op.held !== undefined) { p.held = !!op.held; p._path = null; if (p._board) { sess[p._board].next = clock + 3; p._board = null; } }
          if (op.set) Object.assign(p, op.set); } }
        else if (op.op === 'board') { const d = op.device, other = riderOf(d);
          if (other && other.id !== op.id) { if (!op.take) return ++this.seq; stepOff(other, d); }   // 뺏기
          state.auto[d] = false; state.rider[d] = true; sess[d] = {next: Infinity};
          const p = state.people.find(q => q.id === op.id); if (p) Object.assign(p, {riding: d, away: true, x: -1e6, y: -1e6});
          emit(); }
        else if (op.op === 'unboard') { const d = op.device, h = state.start[d];
          if (Math.hypot(state.devices[d].x - h.x, state.devices[d].y - h.y) <= 1300) { Object.assign(state.devices[d], h); const p = riderOf(d); if (p) stepOff(p, d); else state.rider[d] = false;
            state.rider[d] = false; state.auto[d] = false; sess[d] = {next: clock + rand(...BETWEEN)}; emit(); } }
        return ++this.seq;
      },
    };
  }
  window.RipplesSim.connect = connect;
})();
