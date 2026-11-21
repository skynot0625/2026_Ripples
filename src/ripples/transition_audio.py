"""Original five-second tonal cues following the existing upward light envelope."""
import hashlib
import json
from pathlib import Path

import numpy as np

from .transition import DURATION, pulse_field, smooth, BEZIER, bezier_ease_in
from .tower_audio import RATE, MASTER, STEREO

CUE_PEAK = .14  # before master gain; no recorded samples, hiss or infra-bass


def make_cue(state, rate=RATE):
    if state not in ('activate', 'sleep'):
        raise ValueError('unknown cue destination')
    t = np.arange(round(DURATION * rate)) / rate
    # Sample the same light field on a compact time/height/radius grid.
    control_t = np.linspace(0, DURATION, 501)
    h, r = np.meshgrid(np.linspace(0, 1, 16), np.linspace(0, 1, 8))
    energy = np.array([pulse_field(h, age, 'up', r).mean() for age in control_t])
    energy /= max(float(energy.max()), 1e-9)
    envelope = np.interp(t, control_t, energy)
    envelope *= smooth(t/.18) * (1-smooth((t-3.5)/1.5))
    rise = bezier_ease_in(t/1.6)
    f0 = (128 + 36*rise - 18*smooth((t-1.8)/2.8) if state == 'sleep'
          else 154 + 78*rise)
    phase = 2*np.pi*np.cumsum(f0)/rate
    body = np.sin(phase) + .26*np.sin(2*phase+.3*np.sin(2*np.pi*.23*t))
    body += (.10 if state == 'sleep' else .17)*np.sin(3*phase)
    # A single coherent cue opens gently across four speakers; never a spinning effect.
    spread = smooth((t-.55)/1.1)
    out = np.empty((len(t), 4), np.float32)
    for ch in range(4):
        shimmer = .07*np.sin(phase*1.005 + ch*.6)
        out[:, ch] = envelope*(body+shimmer)*(.72+.28*spread)
    out *= CUE_PEAK/max(float(np.max(np.abs(out))), 1e-9)
    out[0] = out[-1] = 0
    return out


def cue_block(cue, frame, frames, master):
    out = np.zeros((frames, 4), np.float32)
    lo, hi = max(0, frame), min(len(cue), frame+frames)
    if hi > lo:
        out[lo-frame:hi-frame] = cue[lo:hi]*min(.30, max(0, master))
    return out


def write_examples(root, output):
    import soundfile as sf
    root, output = Path(root), Path(output)
    (root/'cues').mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    manifest = {'schema':'ripples.transition-audio.v1', 'duration':DURATION,
                'sample_rate':RATE, 'source':'Original procedural synthesis; no external recordings',
                'generator':'src/ripples/transition_audio.py::make_cue',
                'motion':'ripples.transition.pulse_field / up', 'bezier':list(BEZIER), 'cues':{}}
    examples=[]
    for state in ('sleep', 'activate'):
        cue=make_cue(state)
        path=root/'cues'/f'to_{state}.wav'
        sf.write(path, cue, RATE, subtype='PCM_16')
        decoded,_=sf.read(path,dtype='float32')
        stereo=np.clip(decoded@STEREO*MASTER,-.16,.16)
        sf.write(output/f'to_{state}_preview.wav',stereo,RATE,subtype='PCM_16')
        examples.extend([stereo,np.zeros((RATE,2),np.float32)])
        manifest['cues'][state]={'file':f'cues/to_{state}.wav',
            'sha256':hashlib.sha256(path.read_bytes()).hexdigest(), 'channels':4,
            'peak_before_master':float(np.max(np.abs(decoded))),
            'preview_peak':float(np.max(np.abs(stereo)))}
    sf.write(output/'sleep_then_activate.wav',np.concatenate(examples),RATE,subtype='PCM_16')
    (root/'cues.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')


if __name__ == '__main__':
    write_examples('exhibition-server/static/tower-audio','data_led_example/tower_audio/transitions')
