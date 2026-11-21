/* Final visible LEDs -> continuous pitches and independent, data-timed onsets. */
const LedSoundRules={
  features(colors,xyz,weights) {
    const sum=Array.from({length:4},()=>({w:0,e:0,h:0,r:0,c:0}));
    for(let i=0;i<weights.length;i++) {
      const w=weights[i];if(w<=0)continue;
      const q=(xyz[i*3]>=.5?1:0)+(xyz[i*3+1]>=.5?2:0),s=sum[q];
      const rgb=[0,1,2].map(c=>Math.max(0,Math.min(1,colors[i*3+c]/96)));
      const b=(rgb[0]+rgb[1]+rgb[2])/3;
      s.w+=w;s.e+=b*w;s.h+=b*w*xyz[i*3+2];s.r+=rgb[0]*w;s.c+=(rgb[0]+rgb[1]+rgb[2])*w;
    }
    return sum.map(s=>({level:Math.sqrt(s.e/Math.max(s.w,1e-9)),height:s.h/Math.max(s.e,1e-9),warmth:s.r/Math.max(s.c,1e-9)}));
  },
  planner() {return {previous:[0,0,0,0],charge:[0,0,0,0],until:[0,0,0,0],last:null};},
  update(p,regions,now) {
    const dt=p.last===null?.1:Math.min(.3,Math.max(0,now-p.last));p.last=now;
    const notes=[];
    regions.forEach((r,i)=>{
      const [level,height,warmth]=['level','height','warmth'].map(k=>Math.max(0,Math.min(1,r[k])));
      const rise=level-p.previous[i];p.previous[i]=level;
      p.charge[i]=level>=.025?Math.min(.30,p.charge[i]+level*dt):0;
      if(level<.025||now<p.until[i]||!(rise>=.018||p.charge[i]>=.22))return;
      const duration=1.05+1.65*height;
      notes.push({speaker:i,start:now,duration,frequency:120*2**(2*height+.6*warmth),gain:.10+.14*level,warmth});
      p.until[i]=now+duration+.15;p.charge[i]=0;
    });
    return notes;
  },
  wave(note,t) {
    if(t<0||t>=note.duration)return 0;
    const env=Math.sin(Math.PI*t/note.duration)**2,phase=2*Math.PI*note.frequency*t;
    return note.gain/1.12*env*(Math.sin(phase)+.12*Math.sin(phase*(1.31+.43*note.warmth)));
  }
};
if(typeof module!=='undefined'&&module.exports)module.exports=LedSoundRules;
else {
  let sequence=0;
  window.LedSonification={
    features(P) {
      if(!P.soundXYZ) {
        const xs=[],ys=[];
        for(let i=0;i<P.count;i++)if(P.ripplesWeight[i]>0){xs.push(P.loc[i*4]);ys.push(-P.loc[i*4+2]);}
        const x0=Math.min(...xs),y0=Math.min(...ys),dx=Math.max(...xs)-x0,dy=Math.max(...ys)-y0;
        P.soundXYZ=new Float64Array(P.count*3);
        for(let i=0;i<P.count;i++)P.soundXYZ.set([(P.loc[i*4]-x0)/Math.max(dx,1e-9),(-P.loc[i*4+2]-y0)/Math.max(dy,1e-9),P.ripplesHeight[i]],i*3);
      }
      return {version:String(++sequence),timestamp:Date.now()/1000,
        regions:LedSoundRules.features(Float64Array.from(P.col,x=>x*255),P.soundXYZ,P.ripplesWeight)};
    },
    Player:class {
      constructor(context,output,matrix) {
        this.context=context;this.output=output;this.matrix=matrix;
        this.planner=LedSoundRules.planner();this.voices=[];this.version=null;this.waiting=true;
      }
      stop() {
        const now=this.context.currentTime;
        for(const v of this.voices){v.gain.gain.setTargetAtTime(0,now,.04);try{v.source.stop(now+.2);}catch(_){}}
        this.voices=[];this.planner=LedSoundRules.planner();this.version=null;this.waiting=true;
      }
      update(frame) {
        if(!frame||frame.state!=='activate'||Date.now()/1000-frame.timestamp>1.5){this.stop();return;}
        this.waiting=false;
        if(frame.version===this.version)return;
        this.version=frame.version;
        for(const note of LedSoundRules.update(this.planner,frame.regions,this.context.currentTime)) {
          const rate=this.context.sampleRate,buffer=this.context.createBuffer(2,Math.ceil(note.duration*rate),rate);
          for(let side=0;side<2;side++) {
            const a=buffer.getChannelData(side);
            for(let j=0;j<a.length;j++)a[j]=LedSoundRules.wave(note,j/rate)*this.matrix[note.speaker][side];
          }
          const source=this.context.createBufferSource(),gain=this.context.createGain();
          source.buffer=buffer;source.connect(gain);gain.connect(this.output);
          const voice={source,gain,note};this.voices.push(voice);
          source.onended=()=>{source.disconnect();gain.disconnect();this.voices=this.voices.filter(v=>v!==voice);};
          source.start();
        }
      }
    }
  };
}
