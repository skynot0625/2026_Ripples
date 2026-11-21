/* Explicit split: browser stereo audition versus loopback server hardware output. */
(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  const real=[...document.querySelectorAll('[data-speaker-real]')];
  const previews=[...document.querySelectorAll('[data-speaker-preview]')];
  const liveScript=new URL(document.currentScript.src).pathname.startsWith('/static/tower-audio/');
  const local=liveScript && ['127.0.0.1','localhost','[::1]'].includes(location.hostname);
  let token='', busy=false, pending=false, configured=false, polling=false;
  let context=null, source=null, generation=0;
  function buttons() {
    real.forEach(b=>b.disabled=!configured||busy||pending);
    previews.forEach(b=>b.disabled=busy||pending);
    $('speaker-real-stop').disabled=!configured;
    $('tower-audio-start').disabled=busy||pending;
    $('tower-cue-compare').disabled=busy||pending;
  }
  function apply(state) {
    token=state.token; configured=state.configured; busy=state.state==='running';
    $('speaker-real-status').textContent=configured?state.message:'실제 출력 사용 불가 · '+state.reason;
    if(busy) {stopPreview(); window.TowerAudio?.stop();}
    real.forEach(b=>b.setAttribute('aria-pressed',String(busy&&Number(b.dataset.speakerReal)===state.speaker)));
    buttons();
  }
  async function request(body) {
    const response=await fetch('/api/speaker-test',body?{
      method:'POST',headers:{'Content-Type':'application/json','X-Speaker-Token':token},
      body:JSON.stringify(body),signal:AbortSignal.timeout(5000)
    }:{cache:'no-store',signal:AbortSignal.timeout(5000)});
    const data=await response.json();
    if(!response.ok) throw Error(data.error||`HTTP ${response.status}`);
    return data;
  }
  async function poll() {
    if(!local||pending||polling) return;
    polling=true;
    try {const state=await request();if(!pending) apply(state);}
    catch(e) {
      configured=false;
      $('speaker-real-status').textContent='실제 출력 서버 연결 안 됨 · '+e.message;
      // If a started test loses its connection, keep previews blocked until status returns.
      buttons();
    } finally {polling=false;}
  }
  function stopPreview() {
    generation++;
    if(source) {try {source.stop();} catch(_) {} source.disconnect(); source=null;}
    if(context) {context.close();context=null;}
    $('speaker-preview-status').textContent='미리듣기 꺼짐 · 실제 출력과 별도';
  }
  async function preview(number) {
    if(busy||pending) return;
    stopPreview();
    const run=++generation;
    await window.TowerAudio?.silence();
    if(run!==generation) return;
    try {
      context=new AudioContext();
      await context.resume();
      if(run!==generation) return;
      const rate=context.sampleRate, length=Math.round((number*.48+.30)*rate);
      const buffer=context.createBuffer(2,length,rate);
      const out=buffer.getChannelData((number-1)%2), n=Math.round(.24*rate);
      for(let ping=0;ping<number;ping++) {
        const start=Math.round(ping*.48*rate);
        for(let j=0;j<n;j++) out[start+j]=.02*Math.sin(Math.PI*j/(n-1))**2*Math.sin(2*Math.PI*660*j/rate);
      }
      source=context.createBufferSource();source.buffer=buffer;source.connect(context.destination);
      source.onended=()=>{if(run===generation) stopPreview();};
      $('speaker-preview-status').textContent=`미리듣기 ${number} · 브라우저 ${(number%2)?'왼쪽':'오른쪽'} · 실제 스피커 검사가 아님`;
      source.start();
    } catch(e) {stopPreview();$('speaker-preview-status').textContent='미리듣기 오류: '+e.message;}
  }
  async function action(body) {
    if(pending) return;
    pending=true;buttons();stopPreview();
    try {
      await window.TowerAudio?.silence();
      // Fresh token/status also detects another tab's active test.
      const state=await request();apply(state);
      if(body.action==='start'&&state.state==='running') throw Error('다른 스피커 시험이 진행 중입니다');
      apply(await request(body));
    } catch(e) {$('speaker-real-status').textContent='실제 출력 요청 오류: '+e.message;}
    finally {pending=false;buttons();}
  }
  real.forEach(b=>b.addEventListener('click',()=>action({action:'start',speaker:Number(b.dataset.speakerReal)})));
  previews.forEach(b=>b.addEventListener('click',()=>preview(Number(b.dataset.speakerPreview))));
  $('speaker-real-stop').addEventListener('click',()=>action({action:'stop'}));
  $('speaker-preview-stop').addEventListener('click',stopPreview);
  window.SpeakerPanel={stopPreview,hardwareBusy:()=>busy||pending};
  window.addEventListener('pagehide',stopPreview);
  if(local) {poll();setInterval(poll,700);}
  else {$('speaker-real-status').textContent='실제 출력은 심탑 PC의 localhost 화면에서만 사용합니다. 내려받은 웹판·다른 PC는 미리듣기만 가능합니다.';buttons();}
})();
