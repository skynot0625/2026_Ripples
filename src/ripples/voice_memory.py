"""Offline speech candidates and a restrained, pitch-preserving reverberant voice."""
import subprocess

import numpy as np
from scipy import signal

from .tower_audio import RATE, fade, finish


def speech_regions(raw, sr):
    """VAD candidates, not transcription, speaker identity or sentence recognition.

    Preserve 250 ms context; join pauses <= 550 ms. Split very long utterances at
    a quiet point near 12 s and explicitly mark fragments. Each region names the
    microphone used by VAD so the played signal is the detected signal.
    """
    import webrtcvad
    raw=np.asarray(raw)
    if raw.ndim==1:
        raw=raw[:,None]
    regions=[]
    for ch in range(raw.shape[1]):
        mono=signal.resample_poly(raw[:,ch],16000,sr)
        pcm=(np.clip(mono,-1,1)*32767).astype('<i2')
        vad=webrtcvad.Vad(2)
        hop=320
        flags=np.array([vad.is_speech(pcm[i:i+hop].tobytes(),16000)
                        for i in range(0,len(pcm)-hop+1,hop)],bool)
        idx=np.flatnonzero(flags)
        if not len(idx):
            continue
        groups=np.split(idx,np.where(np.diff(idx)>28)[0]+1)
        for group in groups:
            if len(group)<15 or (group[-1]-group[0]+1)*.02<.65:
                continue
            a=max(0,round((group[0]*.02-.25)*sr))
            end=min(len(raw),round(((group[-1]+1)*.02+.35)*sr))
            while end-a>.65*sr:
                b=end
                fragment=False
                if b-a>14*sr:
                    # A low-energy cut is less jarring than a fixed-time cut.
                    step=max(1,sr//20)
                    cuts=range(a+10*sr,min(end,a+14*sr),step)
                    b=min(cuts,key=lambda q:float(np.mean(raw[q:q+step,ch]**2)))
                    fragment=True
                energy=float(np.sqrt(np.mean(raw[a:b,ch]**2)))
                if energy>=.003 and np.mean(np.abs(raw[a:b,ch])>.985)<.01:
                    coverage=float(np.mean(flags[max(0,round(a/sr/.02)):min(len(flags),round(b/sr/.02))]))
                    regions.append({"start":a,"end":b,"channel":ch,"score":2.+coverage+np.log1p(energy/.01),
                                    "boundary_cut":bool(fragment or a==0 or b==len(raw))})
                a=b
    # Same utterance on multiple microphones appears only once.
    selected=[]
    for r in sorted(regions,key=lambda r:r['score'],reverse=True):
        if not any(r['start']<s['end'] and r['end']>s['start'] for s in selected):
            selected.append(r)
    return sorted(selected,key=lambda r:r['start'])


def voice_effect(mono, sr, *, tempo=.96):
    """Dry speech leads a dark diffuse tail; no reverse, pitch shift or voice clone."""
    import imageio_ffmpeg
    if not .92<=tempo<=1:
        raise ValueError('voice tempo must stay between 0.92 and 1.0')
    dry=signal.resample_poly(np.asarray(mono,np.float32),RATE,sr).astype('<f4')
    result=subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-hide_banner','-loglevel','error',
        '-f','f32le','-ar',str(RATE),'-ac','1','-i','pipe:0','-af',f'atempo={tempo}',
        '-f','f32le','-ar',str(RATE),'-ac','1','pipe:1'],input=dry.tobytes(),capture_output=True,check=True)
    dry=np.frombuffer(result.stdout,dtype='<f4').copy()
    dry=finish(dry,rms=.085,cutoff=5200)
    tail=2.4
    t=np.arange(round(tail*RATE))/RATE
    rng=np.random.default_rng(801)
    impulse=rng.standard_normal(len(t))*np.exp(-3.45*t/.95)
    impulse[t<.065]=0
    impulse=signal.sosfilt(signal.butter(2,2600,fs=RATE,output='sos'),impulse)
    impulse/=max(np.linalg.norm(impulse),1e-8)
    reverb=signal.fftconvolve(dry,impulse).astype(np.float32)
    # Bound the wet energy relative to dry; long tails count as an occupied voice.
    reverb*=min(1,float(np.linalg.norm(dry))/max(float(np.linalg.norm(reverb)),1e-8))
    out=np.pad(dry,(0,len(reverb)-len(dry)))+.22*reverb
    out=finish(out,rms=.075,cutoff=4800)
    return fade(out,.12)
