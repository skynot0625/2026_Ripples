/* Same envelope/budgets as ripples.transition, tested against Python. */
const RipplesTransition = {
  smooth(x) { x = Math.max(0, Math.min(1, x)); return x*x*(3-2*x); },
  limit(rgb, weights) {
    const out = Float64Array.from(rgb, x => Math.max(0, Math.min(96, x)));
    let sum = 0, weight = 0;
    for (let i=0; i<out.length/3; i++) {
      const w = weights ? weights[i] : 1; weight += w;
      sum += w*(out[i*3]+out[i*3+1]+out[i*3+2])/3;
    }
    const scale = sum > 18*weight ? 18*weight/sum : 1;
    if (scale < 1) for (let i=0; i<out.length; i++) out[i] *= scale;
    return out;
  },
  base(rgb, weights) { return this.limit(Float64Array.from(rgb, x => x*96/255), weights); },
  pulse(h, r, age, direction) {
    if (direction === 'up') {
      const local=age-(1.4*Math.sqrt(h)+.20*r), width=.12+1.02*Math.pow(h,.8);
      const aperture=this.smooth((width-r)/.16), gain=.12+.88*Math.pow(h,1.4);
      return aperture*gain*this.smooth(local/.2)*Math.exp(-Math.max(local-.2,0)/.95)*(1-this.smooth((local-2.2)/1.2));
    }
    const local=age-1.4*(direction === 'up_flat' ? h : 1-h);
    return this.smooth(local/.2)*Math.exp(-Math.max(local-.2,0)/.95)*(1-this.smooth((local-2.4)/1.2));
  },
  composite(old, target, height, age, direction, weights, radius) {
    const out = this.base(target, weights);
    if (age >= 5) return out;
    const mix = this.smooth(age/2.6), tint = [96,84,64];
    for (let i=0; i<height.length; i++) {
      const pulse = this.pulse(height[i],radius ? radius[i] : 0,age,direction);
      for (let c=0; c<3; c++) {
        const k=i*3+c;
        out[k]=(old[k]*(1-mix)+out[k]*mix)*(1-.45*pulse)+tint[c]*pulse;
      }
    }
    return this.limit(out, weights);
  }
};
if (typeof module !== 'undefined' && module.exports) module.exports = RipplesTransition;
else /* Local example playback: no sensor POST, no USB, no simulator layout writes. */
(() => {
  'use strict';
  const root = '/static/', $ = id => document.getElementById(id);
  let direction = 'up', transitionStart = -Infinity, origin = null, shownState = null, reset = 0;
  let mode = 'activate', meta = null, clips = null, elapsed = 0, last = null, paused = false, error = '', uiAt = 0;
  const buttons = [...document.querySelectorAll('[data-ripples-example]')];
  const comparisons = [...document.querySelectorAll('[data-ripples-compare]')];
  const duration = () => mode === 'compare' ? 18 : (meta ? meta.duration_s : 68);
  function phase() {
    if (mode === 'compare') return elapsed >= 2 && elapsed < 10 ? 'sleep' : 'activate';
    if (mode !== 'clock') return mode;
    const kst = Date.now() / 1000 + 9 * 3600;
    return kst % 1800 < 1200 ? 'activate' : 'sleep';
  }
  function update(now = performance.now()) {
    if (last !== null && !paused && mode !== 'off') elapsed += Math.max(0, now - last) / 1000;
    last = now;
    if (meta && elapsed >= duration()) { elapsed %= duration(); reset++; }
    if (now - uiAt < 200) return;
    uiAt = now;
    comparisons.forEach(b => b.setAttribute('aria-pressed', String(mode === 'compare' && direction === b.dataset.ripplesCompare)));
    buttons.forEach(b => b.setAttribute('aria-pressed', String(b.dataset.ripplesExample === mode)));
    $('ripples-example-pause').setAttribute('aria-pressed', String(paused));
    $('ripples-example-pause').textContent = paused ? '예시 계속' : '예시 일시정지';
    const state = phase();
    $('ripples-example-status').textContent = error ? '예시 오류: ' + error : mode === 'off' ? '예시 꺼짐 · 기존 시뮬레이션 표시' :
      !clips ? '심탑 예시 불러오는 중…' :
      `${state === 'activate' ? 'Activate · 현재 감각' : 'Sleep · 공간 기억 + 발화'} · ${elapsed.toFixed(1)} / ${duration()}초 · ${direction === 'up' ? '중앙→위 · 확산 상승' : direction === 'up_flat' ? '기존 수평 상승' : '위→아래'}${mode === 'compare' ? ' · 2초 A→S / 10초 S→A' : ''}${mode === 'clock' ? ' · 한국 시각 20분/10분' : ''}`;
    $('ripples-example-time').max = duration()-.1;
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
      const frame = Math.min(meta.frames - 1, Math.floor((elapsed + (mode === 'compare' ? 18 : 0)) * meta.fps)), state = phase();
      if (P.ripplesReset !== reset) { shownState = null; origin = null; P.ripplesReset = reset; P.ripplesFrame = null; }
      if (shownState !== null && shownState !== state) {
        origin = Float64Array.from(P.col, x => x*255); transitionStart = elapsed;
      }
      shownState = state;
      let age = origin ? Math.max(0, elapsed-transitionStart) : 5;
      const key = state + ':' + frame + ':' + Math.floor(age*10) + ':' + direction;
      if (P.ripplesFrame === key && P.on) return true;
      if (!P.ripplesOffsets) {
        if (!P.address || P.address.length !== P.count) throw Error('심탑 스트립 주소 없음');
        P.ripplesOffsets = new Int32Array(P.count).fill(-1);
        P.ripplesHeight = new Float64Array(P.count);
        P.ripplesWeight = new Float64Array(P.count);
        P.ripplesRadius = new Float64Array(P.count);
        P.address.forEach(([name, j, n], i) => {
          const s = meta.strips[name];
          if (!s) throw Error('알 수 없는 스트립: ' + name);
          if (j > 0) {
            P.ripplesOffsets[i] = (s.offset + Math.round((j - 1) / Math.max(1, n - 2) * (s.count - 1))) * 3;
            P.ripplesWeight[i] = s.physical_visible_count/(n-1);
          }
          P.ripplesHeight[i] = P.loc[i*4+1];
        });
        const h = Array.from(P.ripplesHeight).filter((_,i) => P.ripplesWeight[i]>0);
        const lo=Math.min(...h), span=Math.max(...h)-lo;
        P.ripplesHeight = P.ripplesHeight.map(x => Math.max(0,Math.min(1,(x-lo)/span)));
        const visible=Array.from(P.ripplesWeight,(_,i)=>i).filter(i=>P.ripplesWeight[i]>0);
        const xs=visible.map(i=>P.loc[i*4]), ys=visible.map(i=>P.loc[i*4+2]);
        const cx=(Math.min(...xs)+Math.max(...xs))/2, cy=(Math.min(...ys)+Math.max(...ys))/2;
        const rs=visible.map(i=>Math.hypot(P.loc[i*4]-cx,P.loc[i*4+2]-cy));
        const rmin=Math.min(...rs), rspan=Math.max(...rs)-rmin;
        visible.forEach((i,j)=>{ P.ripplesRadius[i]=(rs[j]-rmin)/Math.max(rspan,1e-9); });
      }
      const rgb = clips[state], base = frame * meta.neurons * 3, target = new Float64Array(P.count*3);
      for (let i = 0; i < P.count; i++) {
        const offset = P.ripplesOffsets[i];
        for (let c = 0; c < 3; c++) target[i * 3 + c] = offset < 0 ? 0 : rgb[base + offset + c];
      }
      if (mode === 'compare') {
        const start = elapsed >= 10 ? 10 : elapsed >= 2 ? 2 : null;
        age = start === null ? 5 : elapsed-start;
        if (start !== null && age < 5) {
          const before=clips[start === 2 ? 'activate' : 'sleep'];
          const from=new Float64Array(P.count*3), at=(180+start*10-1)*meta.neurons*3;
          for (let i=0; i<P.count; i++) for (let c=0; c<3; c++) {
            const o=P.ripplesOffsets[i]; from[i*3+c]=o<0 ? 0 : before[at+o+c];
          }
          origin=RipplesTransition.base(from,P.ripplesWeight);
        } else origin=null;
      }
      const out = origin ? RipplesTransition.composite(origin, target, P.ripplesHeight, age, direction, P.ripplesWeight, P.ripplesRadius) : RipplesTransition.base(target, P.ripplesWeight);
      for (let i=0; i<P.count; i++) for (let c=0; c<3; c++) P.col[i*3+c] = P.ripplesWeight[i] ? Math.floor(out[i*3+c])/255 : 0;
      P.on = true; P.ripplesFrame = key;
      return true;
    }
  };
  buttons.forEach(b => b.addEventListener('click', () => {
    if (mode === 'off') reset++;
    mode = b.dataset.ripplesExample; last = performance.now(); changed();
  }));
  comparisons.forEach(b => b.addEventListener('click', () => {
    direction=b.dataset.ripplesCompare; mode='compare'; elapsed=0; reset++; paused=false;
    last=performance.now(); changed();
  }));
  $('ripples-example-pause').addEventListener('click', () => { paused = !paused; last = performance.now(); changed(); });
  $('ripples-example-time').addEventListener('input', e => { elapsed = Number(e.target.value); reset++; last = performance.now(); changed(); });
  async function load() {
    try {
      const response = await fetch(root + 'ripples-example.json', {cache: 'no-cache'});
      if (!response.ok) throw Error('예시 메타데이터 HTTP ' + response.status);
      meta = await response.json();
      if (meta.schema !== 'ripples.venue-example/1' || !meta.example || meta.neurons !== 8460 ||
          meta.frames !== 680 || meta.fps !== 10 || meta.channels !== 3 || meta.duration_s !== 68 || meta.sleep_style !== 'dream-v2')
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
