"""Displayed LED values -> independent tones. No random notes or chord quantization."""
import json
import queue
import threading
import time
from pathlib import Path

import numpy as np

SCHEMA='ripples.led-sound.v1'


def extract_features(rgb, xyz, weights=None):
    """RGB is the final limited 0..255 frame; XYZ uses X/Y floor and Z height."""
    rgb=np.clip(np.asarray(rgb,dtype=float)/96,0,1)
    xyz=np.asarray(xyz,dtype=float)
    xyz=(xyz-xyz.min(axis=0))/np.maximum(np.ptp(xyz,axis=0),1e-9)
    w=np.ones(len(rgb)) if weights is None else np.asarray(weights,dtype=float)
    sectors=(xyz[:,0]>=.5).astype(int)+2*(xyz[:,1]>=.5)
    brightness=rgb.mean(axis=1)
    result=[]
    for sector in range(4):
        mask=sectors==sector
        total=float(w[mask].sum())
        energy=float((brightness[mask]*w[mask]).sum())
        colors=(rgb[mask]*w[mask,None]).sum(axis=0)
        result.append({'level':float(np.sqrt(energy/max(total,1e-9))),
            'height':float((brightness[mask]*w[mask]*xyz[mask,2]).sum()/max(energy,1e-9)),
            'warmth':float(colors[0]/max(float(colors.sum()),1e-9))})
    return result


class NotePlanner:
    def __init__(self):
        self.previous=np.zeros(4)
        self.charge=np.zeros(4)
        self.until=np.zeros(4)
        self.last=None

    def update(self, regions, now):
        dt=.1 if self.last is None else min(.3,max(0,now-self.last))
        self.last=now
        notes=[]
        for i,r in enumerate(regions):
            level,height,warmth=[float(np.clip(r[k],0,1)) for k in ('level','height','warmth')]
            rise=level-self.previous[i]
            self.previous[i]=level
            self.charge[i]=min(.30,self.charge[i]+level*dt) if level>=.025 else 0
            if level<.025 or now<self.until[i] or not (rise>=.018 or self.charge[i]>=.22):
                continue
            duration=1.05+1.65*height
            note={'speaker':i,'start':now,'duration':duration,
                  'frequency':120*2**(2*height+.6*warmth),
                  'gain':.10+.14*level,'warmth':warmth}
            notes.append(note)
            self.until[i]=now+duration+.15
            self.charge[i]=0
        return notes


def note_wave(note, local_time):
    t=np.asarray(local_time)
    u=np.clip(t/note['duration'],0,1)
    env=np.sin(np.pi*u)**2*((t>=0)&(t<note['duration']))
    # A soft inharmonic companion follows the measured color, not a fixed chord.
    ratio=1.31+.43*note['warmth']
    phase=2*np.pi*note['frequency']*t
    return (note['gain']/1.12*env*(np.sin(phase)+.12*np.sin(phase*ratio))).astype(np.float32)


class DataSynth:
    """All mutable synth state belongs to the callback/render thread."""
    def __init__(self,rate):
        self.rate=rate
        self.clock=0.
        self.reset()

    def reset(self):
        self.planner=NotePlanner()
        self.notes=[]
        self.version=None
        self.last_seen=-100.

    def render(self, frame, frames):
        if frame is not None and frame.get('state')=='activate':
            self.last_seen=self.clock
            if frame['version']!=self.version:
                self.notes.extend(self.planner.update(frame['regions'],self.clock))
                self.version=frame['version']
        out=np.zeros((frames,4),np.float32)
        t=self.clock+np.arange(frames)/self.rate
        for note in self.notes:
            out[:,note['speaker']]+=note_wave(note,t-note['start'])
        # Missing/stale input fades; never falls back to an invented sample plan.
        if frame is None or frame.get('state')!='activate':
            out*=np.clip(1-(t-self.last_seen)/.18,0,1)[:,None]
        self.clock+=frames/self.rate
        self.notes=[n for n in self.notes if n['start']+n['duration']>self.clock]
        if self.clock-self.last_seen>.3:
            self.reset()
        return out


def read_frame(path):
    try:
        data=json.loads(Path(path).read_text(encoding='utf-8'))
        if data['schema']!=SCHEMA or data['state'] not in ('activate','sleep'):
            return None
        age=time.time()-data['timestamp']
        if not 0<=age<=1.5 or len(data['regions'])!=4:
            return None
        if type(data['version']) is not str or len(data['version'])>100:
            return None
        for region in data['regions']:
            if any(type(region[k]) not in (int,float) or not np.isfinite(region[k]) or not 0<=region[k]<=1
                   for k in ('level','height','warmth')):
                return None
        return data
    except (OSError,ValueError,KeyError,TypeError):
        return None


class FeaturePublisher:
    """One latest JSON snapshot, atomically replaced off the LED thread; no archive."""
    def __init__(self, visual, path):
        from .hardware.transport import VALID_MASK
        self.mask=VALID_MASK
        self.xyz=visual.waves.xyz[VALID_MASK]
        self.weights=visual.transition.weights
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.queue=queue.Queue(maxsize=1)
        self.closed=threading.Event()
        self.session=str(time.time_ns())
        self.sequence=0
        self.worker=threading.Thread(target=self._write,daemon=True)
        self.worker.start()

    def publish(self, frame, state):
        self.sequence+=1
        data={'schema':SCHEMA,'timestamp':time.time(),'version':f'{self.session}:{self.sequence}',
              'state':state,'source':'final_led_frame',
              'regions':extract_features(frame[self.mask],self.xyz,self.weights)}
        try:self.queue.get_nowait()
        except queue.Empty:pass
        try:self.queue.put_nowait(data)
        except queue.Full:pass

    def _write(self):
        tmp=self.path.with_name(self.path.name+'.'+self.session+'.tmp')
        while not self.closed.is_set():
            try:data=self.queue.get(timeout=.2)
            except queue.Empty:continue
            try:
                tmp.write_text(json.dumps(data,separators=(',',':'),allow_nan=False),encoding='utf-8')
                tmp.replace(self.path)
            except OSError:pass  # next latest frame retries; reader detects staleness
        tmp.unlink(missing_ok=True)

    def close(self):
        self.closed.set()
        self.worker.join(timeout=1)
