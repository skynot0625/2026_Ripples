// Shared simulator state for test pages (/simulator and the later AI visualisation test pages).
//
//   <script src="/simulator.js"></script>
//   const sim = RipplesSim.connect((state, fresh) => { … state.devices.hyeol.x … });
//   sim.send({op: 'drive', device: 'hyeol', move: 1, turn: 0, strafe: 0});   // held buttons (repeat every state.drive_repeat_s)
//
// state (from the server, simulator_store.py; the latest one after every change, from any browser):
//   venue:  the venue fixed for this run {id, name, status, bounds, ceiling_known, …}; "testroom" = procedural test room
//   devices.hyeol / devices.maek: {x, y, heading}   2D pose on the venue plan, mm and degrees (0 = facing +y, 90 = +x)
//   motion.hyeol / motion.maek:  {v forward, s sideways (mm/s), w (°/s), move, strafe, turn, moving,
//                                 blocked (id of what stopped it or null), sliding (id of the surface it slides along)}
//   motion_config: the fixed speeds / acceleration (read only; changed in simulator_store.MOTION)
//   fixed:  mounting that is not adjustable (혈: ToF 1.5 m facing forward, 20 m sensor 2.0 m straight up)
//   bounds: where the devices may stand; room: only for the test room
//   saved, origin, seq: save state and who made the last change
// Read-only pages can also poll GET /api/simulator. The 혈 ToF mock (server) and the 20 m distance mock follow
// devices.hyeol in the venue geometry; nothing consumes devices.maek yet besides these pages.
(() => {
  function connect(onState, onStatus) {
    const client = Math.random().toString(36).slice(2, 10);
    let ws = null, seq = 0;
    function open() {
      const socket = new WebSocket(`ws://${location.host}/ws/simulator`);
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
  // Hold-to-move buttons ([data-drive] = fwd | back | left | right | sleft | sright). While any is held the page sends
  // {"op": "drive", device, move, turn, strafe} and repeats it every repeatS() seconds; releasing sends the new input
  // (all 0 = released). The motion itself (speeds, inertia, collision, sliding) is computed only on the server
  // (simulator_store.py), so every page that uses this behaves the same. Used by /simulator and /venue3d.
  function bindDrivePad({sim, buttons, device, repeatS}) {
    const held = new Map();  // pointerId (or 'key') -> drive key
    let repeat = null;
    function input() {
      const k = new Set(held.values());
      return {move: (k.has('fwd') ? 1 : 0) - (k.has('back') ? 1 : 0), turn: (k.has('right') ? 1 : 0) - (k.has('left') ? 1 : 0),
              strafe: (k.has('sright') ? 1 : 0) - (k.has('sleft') ? 1 : 0)};
    }
    const op = () => { const i = input(); return {op: 'drive', device: device(), move: i.move, turn: i.turn, strafe: i.strafe}; };
    function sendDrive() {
      const o = op();
      sim.send(o, false);
      clearInterval(repeat); repeat = null;
      if (o.move || o.turn || o.strafe) repeat = setInterval(() => sim.send(op(), false), 1000 * repeatS());
    }
    function syncHeld() {
      const k = new Set(held.values());
      for (const b of buttons) b.classList.toggle('held', k.has(b.dataset.drive));
    }
    function release(id) { if (held.has(id)) { held.delete(id); syncHeld(); sendDrive(); } }
    function releaseAll() { if (held.size || repeat) { held.clear(); syncHeld(); sendDrive(); } }
    for (const b of buttons) {
      b.addEventListener('pointerdown', e => {
        e.preventDefault();
        e.stopPropagation();
        try { b.setPointerCapture(e.pointerId); } catch (_) {}
        held.set(e.pointerId, b.dataset.drive); syncHeld(); sendDrive();
      });
      for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) b.addEventListener(type, e => release(e.pointerId));
      // keyboard: Space / Enter held on a focused button works the same way
      b.addEventListener('keydown', e => { if ((e.key === ' ' || e.key === 'Enter') && !e.repeat) { e.preventDefault(); held.set('key', b.dataset.drive); syncHeld(); sendDrive(); } });
      b.addEventListener('keyup', e => { if (e.key === ' ' || e.key === 'Enter') release('key'); });
      b.addEventListener('blur', () => release('key'));
    }
    return {input, releaseAll};
  }

  // Marks the drive button whose direction was stopped by a collision (state.motion[device]).
  function markBlocked(buttons, motion) {
    const blocked = Boolean(motion && motion.blocked);
    for (const b of buttons) {
      const k = b.dataset.drive;
      b.classList.toggle('blocked', blocked && ((k === 'fwd' && motion.move > 0) || (k === 'back' && motion.move < 0) ||
                                                (k === 'sright' && motion.strafe > 0) || (k === 'sleft' && motion.strafe < 0)));
    }
  }
  window.RipplesSim = {connect, bindDrivePad, markBlocked};
})();
