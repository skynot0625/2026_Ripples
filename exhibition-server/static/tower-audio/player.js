/* Four-output composition, explicitly stereo audition in the browser. No autoplay. */
const TowerAudioRules = {
  validate(manifest) {
    if (manifest.schema !== 'ripples.tower-audio.v1' || manifest.max_voices !== 2) throw Error('지원하지 않는 소리 폼');
    const bank = new Map(manifest.bank.map(x => [x.id, x]));
    for (const [state, duration] of [['activate', 1200], ['sleep', 600]]) {
      const marks = [], voiceMarks=[], channels = [[], [], [], []];
      for (const e of manifest.plans[state]) {
        const b = bank.get(e.id);
        if (!b || b.state !== state || !Number.isInteger(e.speaker) || e.speaker < 0 || e.speaker > 3 ||
            !Number.isFinite(e.start) || e.start < 0 || e.start+e.duration > duration ||
            e.duration !== b.duration || !Number.isFinite(e.gain) || e.gain < 0 || e.gain > .9 ||
            !/^bank\/[a-z0-9_]+\.wav$/.test(b.file)) throw Error('잘못된 소리 이벤트');
        marks.push([e.start, 1], [e.start+e.duration, -1]);
        if(b.source.category==='speech_candidate') voiceMarks.push([e.start,1],[e.start+e.duration,-1]);
        channels[e.speaker].push([e.start,e.start+e.duration]);
      }
      let n=0;
      for (const [, delta] of marks.sort((a,b)=>a[0]-b[0]||a[1]-b[1])) {
        n += delta; if (n > 2) throw Error('동시 소리 제한 초과');
      }
      for (const c of channels) {
        c.sort((a,b)=>a[0]-b[0]);
        for(let i=1;i<c.length;i++) if(c[i][0]<c[i-1][1]) throw Error('출력별 소리 중첩');
      }
      n=0;
      for(const [,delta] of voiceMarks.sort((a,b)=>a[0]-b[0]||a[1]-b[1])) {
        n+=delta;if(n>1) throw Error('목소리 동시 재생 제한 초과');
      }
    }
    return true;
  }
};
if (typeof module !== 'undefined' && module.exports) module.exports=TowerAudioRules;
else (() => {
  'use strict';
  const root = new URL('.', document.currentScript.src);
  const $ = id => document.getElementById(id);
  const matrix = [[.65,.15],[.15,.65],[.55,.30],[.30,.55]];
  let manifest, buffers, context, master, running=false, loading=false, generation=0;
  let state=null, mode=null, epoch=0, offset=0, scheduled=new Set(), voices=[], waitUntil=0;
  let paused=false, uiError='';
  const clock = s => `${Math.floor(s/60)}:${String(Math.floor(s%60)).padStart(2,'0')}`;
  const current = () => window.RipplesExample?.audioState() || {state:'off', mode:'off', paused:false};
  function stopVoices() {
    if (!context) return;
    const now=context.currentTime;
    for (const v of voices) {
      v.gain.gain.cancelScheduledValues(now);
      v.gain.gain.setTargetAtTime(0,now,.07);
      try { v.source.stop(now+.35); } catch (_) { /* already ended */ }
    }
    voices=[]; scheduled.clear(); waitUntil=now+.4;
  }
  function begin(s, seek=0) {
    stopVoices(); state=s.state; mode=s.mode;
    offset=Math.max(0,Math.min(state==='sleep'?599:1199,seek));
    epoch=waitUntil;
  }
  function stop() {
    generation++; loading=false; running=false;
    stopVoices(); state=null; uiError='';
    if(context && context.state==='running') {
      const old=context;
      setTimeout(()=>{if(!running && context===old) old.suspend();},450);
    }
    $('tower-audio-start').disabled=false;
    $('tower-audio-start').textContent='소리 시작';
    updateUI();
  }
  async function start() {
    if(loading || running) return;
    loading=true; uiError=''; const token=++generation;
    $('tower-audio-start').disabled=true;
    $('tower-audio-status').textContent='소리 샘플 불러오는 중…';
    try {
      if(!context) {
        context=new AudioContext();
        master=context.createGain(); master.gain.value=0;
        const limiter=context.createWaveShaper(), curve=new Float32Array(8193);
        for(let i=0;i<curve.length;i++) curve[i]=Math.max(-.16,Math.min(.16,2*i/(curve.length-1)-1));
        limiter.curve=curve;
        master.connect(limiter); limiter.connect(context.destination);
      }
      await context.resume();
      if(!manifest) {
        const response=await fetch(new URL('manifest.json',root),{cache:'no-cache'});
        if(!response.ok) throw Error(`manifest HTTP ${response.status}`);
        const m=await response.json(); TowerAudioRules.validate(m);
        const entries=await Promise.all(m.bank.map(async b=>{
          const r=await fetch(new URL(b.file,root),{cache:'no-cache'}); if(!r.ok) throw Error(`sample HTTP ${r.status}`);
          const bytes=await r.arrayBuffer();
          if(b.sha256 && window.crypto?.subtle) {
            const hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),x=>x.toString(16).padStart(2,'0')).join('');
            if(hash!==b.sha256) throw Error('소리 파일 SHA256 불일치');
          }
          const buffer=await context.decodeAudioData(bytes);
          if(buffer.numberOfChannels!==1 || Math.abs(buffer.duration-b.duration)>.002) throw Error('샘플 길이 오류');
          return [b.id,buffer];
        }));
        manifest=m; buffers=new Map(entries); showSources();
      }
      if(token!==generation) return;
      loading=false; running=true; paused=false;
      const s=current(); begin(s,s.mode==='clock'?s.offset:0);
      master.gain.setTargetAtTime(Math.min(.30,Number($('tower-audio-volume').value)/100),context.currentTime,.2);
      $('tower-audio-start').disabled=false;
      $('tower-audio-start').textContent='재생 중';
    } catch(e) {
      if(token===generation) {
        loading=false; running=false; uiError=String(e.message||e);
        $('tower-audio-start').disabled=false;
      }
    }
    updateUI();
  }
  function play(e,index,t) {
    const source=context.createBufferSource(), gain=context.createGain(), merger=context.createChannelMerger(2);
    const left=context.createGain(),right=context.createGain();
    source.buffer=buffers.get(e.id); gain.gain.value=e.gain;
    left.gain.value=matrix[e.speaker][0];right.gain.value=matrix[e.speaker][1];
    source.connect(gain);gain.connect(left);gain.connect(right);
    left.connect(merger,0,0);right.connect(merger,0,1);merger.connect(master);
    const skip=Math.max(0,t-e.start), when=context.currentTime+Math.max(0,e.start-t);
    if(skip>0) {gain.gain.setValueAtTime(0,when);gain.gain.linearRampToValueAtTime(e.gain,when+.15);}
    const voice={source,gain,event:e}; voices.push(voice); scheduled.add(index);
    source.onended=()=>{
      voices=voices.filter(v=>v!==voice);
      for(const n of [source,gain,left,right,merger]) n.disconnect();
    };
    source.start(when,skip);
  }
  function tick() {
    if(!running || !context || !manifest) {updateUI();return;}
    const s=current();
    if(s.state==='off') {
      if(state!=='off') {stopVoices();state='off';}
      updateUI();return;
    }
    if(s.paused!==paused) {
      paused=s.paused;
      if(paused) context.suspend(); else context.resume();
    }
    if(paused) {updateUI();return;}
    if(s.state!==state || s.mode!==mode) begin(s,s.mode==='clock'?s.offset:0);
    let t=offset+context.currentTime-epoch;
    if(s.mode==='clock' && context.currentTime>epoch && Math.abs(t-s.offset)>1) {begin(s,s.offset);t=offset+context.currentTime-epoch;}
    const duration=state==='sleep'?600:1200;
    if(t>=duration) {begin(s,0); t=0;}
    if(context.currentTime>=waitUntil) {
      manifest.plans[state].forEach((e,i)=>{
        if(!scheduled.has(i) && e.start<=t+.20 && e.start+e.duration>t+.02) play(e,i,t);
      });
    }
    updateUI(Math.max(0,t));
  }
  function updateUI(t=running&&context ? Math.max(0,offset+context.currentTime-epoch) : 0) {
    const label=state==='sleep'?'Sleep · 맥 원음 변형':'Activate · 작업·대사 소리';
    $('tower-audio-status').textContent=uiError?`소리 오류: ${uiError}`:loading?'소리 샘플 불러오는 중…':
      !running?'소리 꺼짐 · 시작 버튼으로 미리듣기':state==='off'?'LED 예시 꺼짐 · 소리 대기':
      `${label} · ${clock(t)} / ${state==='sleep'?'10:00':'20:00'}${paused?' · 일시정지':''}`;
    $('tower-audio-provenance').textContent=manifest?.sleep_source==='empty'?'Sleep 소리 풀 비어 있음 · 원음 수신 대기':
      manifest?.sleep_source==='mixed_recordings'?'Sleep: 제공된 녹음과 합성 예시가 섞인 풀 · 아래에서 샘플별 출처 확인':
      manifest?.sleep_source==='supplied_recordings'?
      'Sleep: 제공된 녹음에서 추출한 구간 · 실제 파일 여부는 원본 메타데이터 확인':
      'Sleep 원음은 합성 맥 3채널 예시입니다. 실제 현장 녹음이 아닙니다.';
    const seek=$('tower-audio-seek');seek.disabled=!running||mode==='clock'||state==='off';
    seek.max=state==='sleep'?599:1199; if(document.activeElement!==seek) seek.value=t;
    for(let i=0;i<4;i++) {
      const v=voices.find(v=>v.event.speaker===i && v.event.start<=t && v.event.start+v.event.duration>t);
      const el=$(`tower-audio-ch${i}`);el.textContent=`OUT ${i+1} · ${v?v.event.id+(v.event.category==='speech_candidate'?' · 목소리 후보':''):'쉼'}`;
      el.style.color=v?'#9de0cf':'';
    }
  }
  function showSources(m=manifest) {
    const holder=$('tower-audio-sources');holder.replaceChildren();
    for(const b of m.bank) {
      const s=b.source, p=s.parts?.[0], origin=s.origin||p?.origin, line=document.createElement('p');
      line.className='note';
      const type=s.kind==='original_procedural'?'직접 합성한 기계·작업음':origin?.type==='system_tts'?
        `합성 음성 · ${origin.voice}`:s.kind==='synthetic_maek'?'합성 환경음 예시':'맥에서 수집한 녹음';
      line.textContent=`${b.id} — ${type} · ${b.duration.toFixed(1)}초`+
        (p?` · ${p.relative_file} / MIC ${s.mic_channel} / ${(p.file_frame/s.sample_rate).toFixed(2)}초부터`:'')+
        (s.category==='speech_candidate'?' · 음높이 유지, 0.96배 속도, 부드러운 잔향':'')+
        ` · SHA256 ${b.sha256?.slice(0,12)||'미기록'}`;
      if(origin?.text) line.textContent+=' · 원본 파일의 합성 문안: '+origin.text.join(' / ');
      holder.appendChild(line);
    }
    holder.dataset.loaded='true';
  }
  $('tower-audio-source-panel').addEventListener('toggle',async e=>{
    if(!e.target.open || $('tower-audio-sources').dataset.loaded) return;
    try {
      const r=await fetch(new URL('manifest.json',root),{cache:'no-cache'});
      if(!r.ok) throw Error(`HTTP ${r.status}`);
      const m=await r.json();TowerAudioRules.validate(m);showSources(m);
    } catch(e) {$('tower-audio-sources').textContent='출처를 불러오지 못했습니다: '+e.message;}
  });
  $('tower-audio-start').addEventListener('click',start);
  $('tower-audio-stop').addEventListener('click',stop);
  $('tower-audio-volume').addEventListener('input',()=>{
    const value=Math.max(0,Math.min(30,Number($('tower-audio-volume').value)));
    $('tower-audio-volume-label').textContent=`디지털 음량 ${value}% · 상한 30%`;
    if(master) master.gain.setTargetAtTime(value/100,context.currentTime,.15);
  });
  $('tower-audio-seek').addEventListener('change',()=>{if(running&&mode!=='clock') begin(current(),Number($('tower-audio-seek').value));});
  window.addEventListener('pagehide',()=>{stop();if(context)context.close();});
  setInterval(tick,100);
  updateUI();
})();
