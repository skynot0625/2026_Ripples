/* Synthetic sensor values at actual simulator XY. Bounded, tab-local memory only. */
class RipplesPositionSimulation {
  constructor(bounds) {
    this.bounds=bounds; this.time=0; this.poses={}; this.receivedAt=-Infinity; this.online=false;
    this.trail=new Float32Array(32*32*24*3); this.mean=new Float32Array(this.trail.length);
    this.spread=new Float32Array(this.trail.length); this.cells=new Map(); this.bucket=new Map();
    this.second=0; this.memoryDirty=false; this.latest={}; this.revision=0; this.memoryRevision=0;
    this.rng=20261120; this.noise=new Float32Array(8*8*6); this.lastNow=null;
    for(let i=0;i<this.noise.length;i++) this.noise[i]=this.normal();
  }
  uniform() { this.rng=(Math.imul(1664525,this.rng)+1013904223)>>>0; return (this.rng+.5)/4294967296; }
  normal() { return Math.sqrt(-2*Math.log(this.uniform()))*Math.cos(2*Math.PI*this.uniform()); }
  setPositions(devices,venue,now) {
    this.poses=venue==='m30' ? devices || {} : {}; this.receivedAt=now; this.online=true;
  }
  offline() { this.poses={}; this.receivedAt=-Infinity; this.online=false; }
  position(p) {
    if(!p || !Number.isFinite(p.x) || !Number.isFinite(p.y)) return null;
    const uv=[(p.x/1000-this.bounds[0][0])/(this.bounds[0][1]-this.bounds[0][0]),
              (p.y/1000-this.bounds[1][0])/(this.bounds[1][1]-this.bounds[1][0])];
    return uv.every(v=>v>=0 && v<=1) ? uv : null;
  }
  randomValue(seed,t) {
    const n=Math.floor(t), hash=k=>{let x=Math.imul(k+seed,374761393);x=Math.imul(x^(x>>>13),1274126177);return ((x^(x>>>16))>>>0)/4294967296;};
    const u=t-n, a=u*u*(3-2*u); return hash(n)*(1-a)+hash(n+1)*a;
  }
  features(role,t) {
    const f={};
    if(role==='hyeol') {
      for(let i=0;i<9;i++){f['depth_'+i]=.3+3.4*this.randomValue(100+i,t/.9); f['coverage_'+i]=.72+.28*this.randomValue(200+i,t/1.7);}
      f.ceiling_0=1.2+2.5*this.randomValue(300,t/2.2);
    } else for(let i=0;i<3;i++) f['mic_level_'+i]=.08+.82*this.randomValue(400+i,t/.7);
    return f;
  }
  static smooth(x){ x=Math.max(0,Math.min(1,x));return x*x*(3-2*x); }
  static stamp(v,e) {
    const xy=e.uv.map(x=>x*31), f=e.features, clamp=x=>Math.max(0,Math.min(1,x));
    const dot=(dx,dy,z,color,strength)=>{
      const x=Math.round(xy[0]+dx), y=Math.round(xy[1]+dy), k=Math.round(clamp(z)*23);
      if(x<0||x>31||y<0||y>31) return;
      const at=((x*32+y)*24+k)*3;
      for(let c=0;c<3;c++)v[at+c]=Math.max(v[at+c],color[c]*strength);
    };
    const line=(a,b,color,level)=>{
      const steps=Math.max(2,Math.floor(Math.max(Math.abs(a[0]-b[0]),Math.abs(a[1]-b[1]),Math.abs(a[2]-b[2])*23)*2)+1);
      for(let i=0;i<steps;i++){const u=i/(steps-1);dot(a[0]*(1-u)+b[0]*u,a[1]*(1-u)+b[1]*u,a[2]*(1-u)+b[2]*u,color,level);}
    };
    if(e.source==='hyeol') {
      const points={};
      for(let i=0;i<9;i++) {
        const cov=clamp(f['coverage_'+i]||0),d=Math.max(0,Math.min(4,f['depth_'+i]||0));
        if(!cov||!d)continue;
        const p=[(i%3-1)*1.7,(1-Math.floor(i/3))*1.7,.12+.46*Math.log1p(d/.35)/Math.log1p(4/.35)];
        const level=Math.sqrt(cov)*(.4+.6*(1-d/4));points[i]=[p,level];dot(...p,[25,220,255],level);
      }
      for(const [key,[a,level]] of Object.entries(points)) {
        const i=Number(key); for(const j of [i%3<2?i+1:-1,i+3]) if(points[j]) line(a,points[j][0],[25,220,255],.48*Math.min(level,points[j][1]));
      }
      if(f.ceiling_0>0){const z=.72+.22*clamp(f.ceiling_0/4);line([0,0,.64],[0,0,z],[165,95,255],.28);
        line([-1.7,0,z],[1.7,0,z],[195,125,255],.95);line([0,-1.7,z],[0,1.7,z],[195,125,255],.95);}
    } else {
      const colors=[[255,155,55],[255,220,145],[255,125,100]];
      for(let i=0;i<3;i++){const level=clamp(f['mic_level_'+i]||0);if(level){const x=(i-1)*1.7,z=.08+.38*level;
        line([x,0,.06],[x,0,z],colors[i],.65*Math.sqrt(level));dot(x,0,z,colors[i],Math.sqrt(level));}}
    }
  }
  static blur(input) {
    let src=input; const dims=[32,32,24], strides=[32*24*3,24*3,3];
    for(let axis=0;axis<3;axis++) {
      const sigma=axis===2?.8:.9, radius=Math.floor(4*sigma+.5), kernel=[];let sum=0;
      for(let k=-radius;k<=radius;k++){const q=Math.exp(-.5*(k/sigma)**2);kernel.push(q);sum+=q;}
      for(let i=0;i<kernel.length;i++)kernel[i]/=sum;
      const out=new Float32Array(input.length), stride=strides[axis],n=dims[axis];
      for(let at=0;at<src.length;at++) {
        const coord=Math.floor(at/stride)%n;let value=0;
        for(let k=-radius;k<=radius;k++){let q=coord+k;if(q<0)q=-q-1;if(q>=n)q=2*n-q-1;value+=src[at+(q-coord)*stride]*kernel[k+radius];}
        out[at]=value;
      }
      src=out;
    }
    for(let i=0;i<src.length;i++)src[i]=Math.min(255,src[i]*6.5);
    return src;
  }
  static sample(v,x,y,z,c=0,dims=[32,32,24],channels=3) {
    x=Math.max(0,Math.min(dims[0]-1,x));y=Math.max(0,Math.min(dims[1]-1,y));z=Math.max(0,Math.min(dims[2]-1,z));
    const ix=Math.floor(x),iy=Math.floor(y),iz=Math.floor(z);let value=0;
    for(let a=0;a<2;a++)for(let b=0;b<2;b++)for(let d=0;d<2;d++) {
      const w=(a?x-ix:1-x+ix)*(b?y-iy:1-y+iy)*(d?z-iz:1-z+iz);
      value+=w*v[((Math.min(ix+a,dims[0]-1)*dims[1]+Math.min(iy+b,dims[1]-1))*dims[2]+Math.min(iz+d,dims[2]-1))*channels+c];
    }
    return value;
  }
  observe(e) {
    const x=Math.min(23,Math.floor(e.uv[0]*24)),y=Math.min(23,Math.floor(e.uv[1]*24)),key=e.source+':'+x+':'+y;
    let b=this.bucket.get(key);if(!b){b={source:e.source,uv:[(x+.5)/24,(y+.5)/24],sum:{},n:0};this.bucket.set(key,b);}
    b.n++;for(const [k,v] of Object.entries(e.features))b.sum[k]=(b.sum[k]||0)+v;
  }
  flush() {
    for(const [key,b] of this.bucket) {
      let cell=this.cells.get(key);if(!cell){cell={source:b.source,uv:b.uv,features:{},m2:{},count:0};this.cells.set(key,cell);}
      cell.count++;
      for(const [k,total] of Object.entries(b.sum)) {const value=total/b.n,delta=value-(cell.features[k]||0);cell.features[k]=(cell.features[k]||0)+delta/cell.count;cell.m2[k]=(cell.m2[k]||0)+delta*(value-cell.features[k]);}
    }
    if(this.bucket.size)this.memoryDirty=true;
    this.bucket.clear();
  }
  rebuildMemory() {
    const mean=new Float32Array(this.trail.length),hi=new Float32Array(mean.length),lo=new Float32Array(mean.length);
    for(const cell of this.cells.values()) {
      RipplesPositionSimulation.stamp(mean,cell);
      const high={},low={};
      for(const [k,v] of Object.entries(cell.features)) {
        const sd=cell.count>1?Math.sqrt(Math.max(0,cell.m2[k]/(cell.count-1))):0;
        const cap=k.startsWith('coverage_')?1:Infinity;high[k]=Math.min(cap,v+sd);low[k]=Math.max(0,v-sd);
      }
      RipplesPositionSimulation.stamp(hi,{...cell,features:high});RipplesPositionSimulation.stamp(lo,{...cell,features:low});
    }
    this.mean=RipplesPositionSimulation.blur(mean);const upper=RipplesPositionSimulation.blur(hi),lower=RipplesPositionSimulation.blur(lo);
    for(let i=0;i<this.spread.length;i++)this.spread[i]=Math.min(90,Math.max(Math.abs(upper[i]-this.mean[i]),Math.abs(lower[i]-this.mean[i])));
    this.memoryDirty=false;this.memoryRevision++;
  }
  tick(now,paused) {
    if(this.lastNow===null)this.lastNow=now-100;
    if(paused){this.lastNow=now;return false;}
    if(now-this.lastNow<99)return false;
    const dt=Math.min(.25,(now-this.lastNow)/1000);this.lastNow=now;this.time+=dt;this.revision++;
    const second=Math.floor(this.time);if(second!==this.second){this.flush();this.second=second;}
    const fade=Math.exp(-dt/.9);for(let i=0;i<this.trail.length;i++){this.trail[i]*=fade;if(this.trail[i]<.5)this.trail[i]=0;}
    this.latest={};
    if(this.online) for(const source of ['hyeol','maek']) {
      const p=this.poses[source],uv=this.position(p);
      if(!uv){this.latest[source]={status:p?'오른쪽 공간 밖':'위치 없음'};continue;}
      const e={source,uv,features:this.features(source,this.time),world:[p.x/1000,p.y/1000]};
      this.latest[source]=e;RipplesPositionSimulation.stamp(this.trail,e);this.observe(e);
    }
    const alpha=Math.exp(-dt/3.2),beta=Math.sqrt(1-alpha*alpha);
    for(let i=0;i<this.noise.length;i++)this.noise[i]=alpha*this.noise[i]+beta*this.normal();
    return true;
  }
  clear() {this.cells.clear();this.bucket.clear();this.mean.fill(0);this.spread.fill(0);this.memoryDirty=false;this.memoryRevision++;this.revision++;}
  render(P,state) {
    if(state==='sleep'&&this.memoryDirty)this.rebuildMemory();
    const volume=state==='activate'?RipplesPositionSimulation.blur(this.trail):this.mean;
    if(!P.ripplesGrid) {
      const visible=Array.from(P.ripplesWeight,(_,i)=>i).filter(i=>P.ripplesWeight[i]>0);
      const xs=visible.map(i=>P.loc[i*4]),ys=visible.map(i=>-P.loc[i*4+2]);
      const x0=Math.min(...xs),y0=Math.min(...ys),dx=Math.max(...xs)-x0,dy=Math.max(...ys)-y0;
      P.ripplesGrid=Array.from({length:P.count},(_,i)=>[(P.loc[i*4]-x0)/dx*31,(-P.loc[i*4+2]-y0)/dy*31,P.ripplesHeight[i]*23]);
    }
    const rgb=new Float64Array(P.count*3);
    for(let i=0;i<P.count;i++) {
      if(!P.ripplesWeight[i])continue;
      const [x,y,z]=P.ripplesGrid[i];
      const noise=state==='sleep'?Math.tanh(.8*RipplesPositionSimulation.sample(this.noise,x/31*7,y/31*7,z/23*5,0,[8,8,6],1)):0;
      for(let c=0;c<3;c++) {
        let v=RipplesPositionSimulation.sample(volume,x,y,z,c);
        if(state==='sleep')v=.72*v+.45*noise*RipplesPositionSimulation.sample(this.spread,x,y,z,c);
        rgb[i*3+c]=.85*Math.max(0,Math.min(255,v));
      }
    }
    return rgb;
  }
}

/* Same envelope/budgets as ripples.transition, tested against Python. */
const RipplesBezierTable=(()=>{
  const table=new Float64Array(1025);
  for(let i=1;i<1024;i++) {
    let lo=0,hi=1;
    for(let j=0;j<24;j++) {
      const u=(lo+hi)/2,x=3*.42*(1-u)**2*u+3*(1-u)*u*u+u**3;
      if(x<i/1024)lo=u;else hi=u;
    }
    const u=(lo+hi)/2;
    table[i]=3*(1-u)*u*u+u**3;
  }
  table[1024]=1;return table;
})();
const RipplesTransition = {
  bezier(x,inverse=false) {
    x=Math.max(0,Math.min(1,x));
    if(inverse) {
      const u=.5+Math.sin(Math.asin(2*x-1)/3);
      return 3*.42*(1-u)**2*u+3*(1-u)*u*u+u**3;
    }
    x*=1024;
    const i=Math.min(1023,Math.floor(x)),f=x-i,table=RipplesBezierTable;
    return table[i]+(table[i+1]-table[i])*f;
  },
  async sha256(data) {
    if(globalThis.crypto?.subtle) return [...new Uint8Array(await crypto.subtle.digest('SHA-256',data))].map(x=>x.toString(16).padStart(2,'0')).join('');
    return this.sha256Fallback(data);
  },
  // HTTP LAN pages do not expose WebCrypto. Keep integrity checking with SHA-256.
  sha256Fallback(data) {
    const bytes=data instanceof Uint8Array?data:new Uint8Array(data);
    const n=bytes.length,padded=new Uint8Array(Math.ceil((n+9)/64)*64);
    padded.set(bytes);padded[n]=128;
    const view=new DataView(padded.buffer);
    view.setUint32(padded.length-8,Math.floor(n/0x20000000));view.setUint32(padded.length-4,(n*8)>>>0);
    const k=[0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
      0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
      0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
      0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
      0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
      0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
      0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
      0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2];
    const h=new Uint32Array([0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19]);
    const w=new Uint32Array(64),rr=(x,n)=>(x>>>n)|(x<<(32-n));
    for(let block=0;block<padded.length;block+=64) {
      for(let i=0;i<16;i++)w[i]=view.getUint32(block+i*4);
      for(let i=16;i<64;i++)w[i]=(w[i-16]+(rr(w[i-15],7)^rr(w[i-15],18)^(w[i-15]>>>3))+w[i-7]+(rr(w[i-2],17)^rr(w[i-2],19)^(w[i-2]>>>10)))>>>0;
      let [a,b,c,d,e,f,g,q]=h;
      for(let i=0;i<64;i++) {
        const t1=(q+(rr(e,6)^rr(e,11)^rr(e,25))+((e&f)^(~e&g))+k[i]+w[i])>>>0;
        const t2=((rr(a,2)^rr(a,13)^rr(a,22))+((a&b)^(a&c)^(b&c)))>>>0;
        q=g;g=f;f=e;e=(d+t1)>>>0;d=c;c=b;b=a;a=(t1+t2)>>>0;
      }
      [a,b,c,d,e,f,g,q].forEach((x,i)=>h[i]=(h[i]+x)>>>0);
    }
    return [...h].map(x=>x.toString(16).padStart(8,'0')).join('');
  },
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
      const local=age-(1.4*this.bezier(h,true)+.20*r), startWidth=.12+1.02*Math.pow(.5,.8);
      const width=startWidth+(1.14-startWidth)*this.smooth((h-.5)/.35);
      const aperture=this.smooth((width-r)/.16), gain=.12+.88*Math.pow(h,1.4);
      return aperture*gain*this.bezier(local/.2)*Math.exp(-Math.max(local-.2,0)/.95)*(1-this.smooth((local-2.2)/1.2));
    }
    const local=age-1.4*this.bezier(direction === 'up_flat' ? h : 1-h,true);
    return this.bezier(local/.2)*Math.exp(-Math.max(local-.2,0)/.95)*(1-this.smooth((local-2.4)/1.2));
  },
  composite(old, target, height, age, direction, weights, radius) {
    const out = this.base(target, weights);
    if (age >= 5) return out;
    const mix = this.bezier(age/2.6), tint = [96,84,64];
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
RipplesTransition.PositionSimulation = RipplesPositionSimulation;
if (typeof module !== 'undefined' && module.exports) module.exports = RipplesTransition;
else /* Local example playback: no sensor POST, no USB, no simulator layout writes. */
(() => {
  'use strict';
  const root = '/static/', $ = id => document.getElementById(id);
  let input = 'position', positionEngine = null, pendingSimulation = null, activityClip = null;
  let soundFrame=null;
  let direction = 'up', transitionStart = -Infinity, origin = null, shownState = null, reset = 0;
  let mode = 'activate', meta = null, clips = null, elapsed = 0, last = null, paused = false, error = '', uiAt = 0;
  const buttons = [...document.querySelectorAll('[data-ripples-example]')];
  const comparisons = [...document.querySelectorAll('[data-ripples-compare]')];
  const sources = [...document.querySelectorAll('[data-ripples-source]')];
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
    sources.forEach(b => b.setAttribute('aria-pressed', String(input === b.dataset.ripplesSource)));
    comparisons.forEach(b => b.setAttribute('aria-pressed', String(mode === 'compare' && direction === b.dataset.ripplesCompare)));
    buttons.forEach(b => b.setAttribute('aria-pressed', String(b.dataset.ripplesExample === mode)));
    $('ripples-example-pause').setAttribute('aria-pressed', String(paused));
    $('ripples-example-pause').textContent = paused ? '예시 계속' : '예시 일시정지';
    const state = phase();
    $('ripples-example-status').textContent = error ? '예시 오류: ' + error : mode === 'off' ? '예시 꺼짐 · 기존 시뮬레이션 표시' :
      !clips ? '심탑 예시 불러오는 중…' :
      `${state === 'activate' ? 'Activate · 현재 감각' : 'Sleep · 공간 기억 + 발화'} · ${input === 'position' ? '3D 위치 연동' : elapsed.toFixed(1)+' / '+duration()+'초'} · ${direction === 'up' ? '중앙→위 · 확산 상승' : direction === 'up_flat' ? '기존 수평 상승' : '위→아래'}${mode === 'compare' ? ' · 2초 A→S / 10초 S→A' : ''}${mode === 'clock' ? ' · 한국 시각 20분/10분' : ''}`;
    const live=input==='position';
    $('ripples-example-time').disabled=live||!clips;
    $('ripples-source-note').textContent=live ? '위치: 현재 3D 혈·맥 / 감각값: 합성 · 이 탭에만 임시 기억 · 새로고침 시 초기화' : '기록된 합성 경로 · 3D 기구 조종과 독립';
    $('ripples-position-status').textContent=live && positionEngine ? ['hyeol','maek'].map(role=>{
      const e=positionEngine.latest[role],name=role==='hyeol'?'혈':'맥';
      return name+': '+(!e?'위치 수신 대기':e.status||`X ${e.world[0].toFixed(2)}, Y ${e.world[1].toFixed(2)}m → LED ${(e.uv[0]*100).toFixed(0)}%, ${(e.uv[1]*100).toFixed(0)}%`);
    }).join(' / ')+` · 임시 기억 ${positionEngine.cells.size}셀` : '';
    $('ripples-example-time').max = duration()-.1;
    $('ripples-example-time').value = elapsed;
  }
  function changed() { uiAt = -Infinity; update(); if (api.onUpdate) api.onUpdate(); }
  const api = window.RipplesExample = {
    onUpdate: null,
    audioFrame:()=>mode==='off'?null:soundFrame,
    audioState: () => {
      const state=phase(), p=(Date.now()/1000+9*3600)%1800;
      return {state, mode, paused, revision:reset,
        transitionAge:shownState===state&&origin?Math.max(0,elapsed-transitionStart):null,
        offset:mode==='clock'?p-(state==='sleep'?1200:0):null};
    },
    compareTransitions() {
      input='recorded';direction='up';mode='compare';elapsed=0;reset++;paused=false;
      last=performance.now();changed();
    },
    enabled: () => mode !== 'off',
    setSimulation(msg) {
      pendingSimulation={devices:msg.devices,venue:msg.venue&&msg.venue.id,now:performance.now()};
      if(positionEngine)positionEngine.setPositions(pendingSimulation.devices,pendingSimulation.venue,pendingSimulation.now);
    },
    offline() { pendingSimulation=null;if(positionEngine)positionEngine.offline(); },
    apply(P, now) {
      update(now);
      if (mode === 'off') return null;
      if (!clips) { P.col.fill(0); P.on = true; return true; }
      if(input === 'position' && positionEngine)positionEngine.tick(now,paused);
      const frame = input==='position' ? Math.floor(positionEngine.time*10)%meta.frames : Math.min(meta.frames - 1, Math.floor((elapsed + (mode === 'compare' ? 18 : 0)) * meta.fps)), state = phase();
      if (P.ripplesReset !== reset) { shownState = null; origin = null; P.ripplesReset = reset; P.ripplesFrame = null; }
      if (shownState !== null && shownState !== state) {
        origin = Float64Array.from(P.col, x => x*255); transitionStart = elapsed;
      }
      shownState = state;
      let age = origin ? Math.max(0, elapsed-transitionStart) : 5;
      const key = input + ':' + (positionEngine ? positionEngine.revision : 0) + ':' + state + ':' + frame + ':' + Math.floor(age*10) + ':' + direction;
      if (P.ripplesFrame === key && P.on) {if(soundFrame)soundFrame.timestamp=Date.now()/1000;return true;}
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
      const rgb = clips[state], base = frame * meta.neurons * 3;
      let target = new Float64Array(P.count*3);
      for (let i = 0; i < P.count; i++) {
        const offset = P.ripplesOffsets[i];
        for (let c = 0; c < 3; c++) target[i * 3 + c] = offset < 0 ? 0 : rgb[base + offset + c];
      }
      if(input === 'position' && positionEngine) {
        target=positionEngine.render(P,state);
        if(state==='sleep') for(let i=0;i<P.count;i++) {
          const offset=P.ripplesOffsets[i]; if(offset<0)continue;
          for(let c=0;c<3;c++){const k=i*3+c;target[k]+=(255-target[k])*activityClip[base+offset+c]/255;}
        }
      }
      if (mode === 'compare' && input === 'recorded') {
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
      if(window.LedSonification)soundFrame={...window.LedSonification.features(P),state};
      P.on = true; P.ripplesFrame = key;
      return true;
    }
  };
  buttons.forEach(b => b.addEventListener('click', () => {
    if (mode === 'off') reset++;
    mode = b.dataset.ripplesExample; last = performance.now(); changed();
  }));
  comparisons.forEach(b => b.addEventListener('click', () => {
    input='recorded'; direction=b.dataset.ripplesCompare; mode='compare'; elapsed=0; reset++; paused=false;
    last=performance.now(); changed();
  }));
  sources.forEach(b=>b.addEventListener('click',()=>{
    input=b.dataset.ripplesSource;reset++; if(mode==='compare'||mode==='off')mode='activate';
    last=performance.now();if(positionEngine)positionEngine.lastNow=last;changed();
  }));
  $('ripples-memory-clear').addEventListener('click',()=>{if(positionEngine)positionEngine.clear();reset++;changed();});
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
        const hash = await RipplesTransition.sha256(compressed);
        if (hash !== f.sha256) throw Error('예시 파일 해시 불일치');
        const raw = new Uint8Array(await new Response(new Blob([compressed]).stream().pipeThrough(new DecompressionStream('gzip'))).arrayBuffer());
        if (raw.byteLength !== meta.frames * meta.neurons * 3) throw Error('예시 프레임 길이 불일치');
        return [state, raw];
      }));
      const a=meta.activity_clip;
      if(!a||a.encoding!=='gzip+base64'||a.frames!==680||a.neurons!==8460||a.channels!==3||a.fps!==10)throw Error('위치연동 발화 기록 형식 불일치');
      const packed=Uint8Array.from(atob(a.data),c=>c.charCodeAt(0));
      const digest=await RipplesTransition.sha256(packed);
      if(digest!==a.sha256)throw Error('위치연동 발화 기록 해시 불일치');
      activityClip=new Uint8Array(await new Response(new Blob([packed]).stream().pipeThrough(new DecompressionStream('gzip'))).arrayBuffer());
      if(activityClip.byteLength!==680*8460*3)throw Error('위치연동 발화 기록 길이 불일치');
      positionEngine=new RipplesPositionSimulation(meta.led_bounds_m);
      if(pendingSimulation)positionEngine.setPositions(pendingSimulation.devices,pendingSimulation.venue,pendingSimulation.now);
      clips = Object.fromEntries(entries); elapsed = 0; last = performance.now();
      $('ripples-example-time').disabled = false;
      changed();
    } catch (e) { error = e.message; changed(); }
  }
  changed(); load();
})();
