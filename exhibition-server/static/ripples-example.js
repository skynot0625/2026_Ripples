/* Local example playback: no sensor POST, no USB, no simulator layout writes. */
(() => {
  'use strict';
  const root = '/static/', $ = id => document.getElementById(id);
  let mode = 'activate', meta = null, clips = null, elapsed = 0, last = null, paused = false, error = '', uiAt = 0;
  const buttons = [...document.querySelectorAll('[data-ripples-example]')];
  function phase() {
    if (mode !== 'clock') return mode;
    const kst = Date.now() / 1000 + 9 * 3600;
    return kst % 1800 < 1200 ? 'activate' : 'sleep';
  }
  function update(now = performance.now()) {
    if (last !== null && !paused && mode !== 'off') elapsed += Math.max(0, now - last) / 1000;
    last = now;
    if (meta) elapsed %= meta.duration_s;
    if (now - uiAt < 200) return;
    uiAt = now;
    buttons.forEach(b => b.setAttribute('aria-pressed', String(b.dataset.ripplesExample === mode)));
    $('ripples-example-pause').setAttribute('aria-pressed', String(paused));
    $('ripples-example-pause').textContent = paused ? '예시 계속' : '예시 일시정지';
    const state = phase();
    $('ripples-example-status').textContent = error ? '예시 오류: ' + error : mode === 'off' ? '예시 꺼짐 · 기존 시뮬레이션 표시' :
      !clips ? '심탑 예시 불러오는 중…' :
      `${state === 'activate' ? 'Activate · 현재 감각' : 'Sleep · 공간 기억 + 발화'} · ${elapsed.toFixed(1)} / ${meta.duration_s}초${mode === 'clock' ? ' · 한국 시각 20분/10분' : ''}`;
    $('ripples-example-time').value = elapsed;
  }
  function changed() { uiAt = -Infinity; update(); if (api.onUpdate) api.onUpdate(); }
  const api = window.RipplesExample = {
    onUpdate: null,
    enabled: () => mode !== 'off',
    apply(P, now) {
      update(now);
      if (mode === 'off') return null;
      if (!clips) { P.col.fill(0); P.on = true; return true; }
      const frame = Math.min(meta.frames - 1, Math.floor(elapsed * meta.fps)), state = phase();
      const key = state + ':' + frame;
      if (P.ripplesFrame === key && P.on) return true;
      if (!P.ripplesOffsets) {
        if (!P.address || P.address.length !== P.count) throw Error('심탑 스트립 주소 없음');
        P.ripplesOffsets = new Int32Array(P.count).fill(-1);
        P.address.forEach(([name, j, n], i) => {
          const s = meta.strips[name];
          if (!s) throw Error('알 수 없는 스트립: ' + name);
          if (j > 0) P.ripplesOffsets[i] = (s.offset + Math.round((j - 1) / Math.max(1, n - 2) * (s.count - 1))) * 3;
        });
      }
      const rgb = clips[state], base = frame * meta.neurons * 3;
      for (let i = 0; i < P.count; i++) {
        const offset = P.ripplesOffsets[i];
        for (let c = 0; c < 3; c++) P.col[i * 3 + c] = offset < 0 ? 0 : rgb[base + offset + c] / 255;
      }
      P.on = true; P.ripplesFrame = key;
      return true;
    }
  };
  buttons.forEach(b => b.addEventListener('click', () => {
    mode = b.dataset.ripplesExample; last = performance.now(); changed();
  }));
  $('ripples-example-pause').addEventListener('click', () => { paused = !paused; last = performance.now(); changed(); });
  $('ripples-example-time').addEventListener('input', e => { elapsed = Number(e.target.value); last = performance.now(); changed(); });
  async function load() {
    try {
      const response = await fetch(root + 'ripples-example.json', {cache: 'no-cache'});
      if (!response.ok) throw Error('예시 메타데이터 HTTP ' + response.status);
      meta = await response.json();
      if (meta.schema !== 'ripples.venue-example/1' || !meta.example || meta.neurons !== 8460 ||
          meta.frames !== 680 || meta.fps !== 10 || meta.channels !== 3 || meta.duration_s !== 68)
        throw Error('예시 형식 불일치');
      const entries = await Promise.all(['activate', 'sleep'].map(async state => {
        const f = meta.files[state];
        if (f.name !== `ripples-${state}.rgb.gz`) throw Error('예시 파일 이름 불일치');
        const r = await fetch(root + f.name, {cache: 'no-cache'});
        if (!r.ok) throw Error('예시 프레임 HTTP ' + r.status);
        const compressed = await r.arrayBuffer();
        if (compressed.byteLength !== f.bytes) throw Error('예시 파일 크기 불일치');
        const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256', compressed))].map(x => x.toString(16).padStart(2, '0')).join('');
        if (hash !== f.sha256) throw Error('예시 파일 해시 불일치');
        const raw = new Uint8Array(await new Response(new Blob([compressed]).stream().pipeThrough(new DecompressionStream('gzip'))).arrayBuffer());
        if (raw.byteLength !== meta.frames * meta.neurons * 3) throw Error('예시 프레임 길이 불일치');
        return [state, raw];
      }));
      clips = Object.fromEntries(entries); elapsed = 0; last = performance.now();
      $('ripples-example-time').disabled = false;
      changed();
    } catch (e) { error = e.message; changed(); }
  }
  changed(); load();
})();
