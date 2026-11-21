"""Reproduce a 60-second audition from the checked-in visible LED example."""
import gzip
import hashlib
import json
from pathlib import Path

import numpy as np
import soundfile as sf

from ripples.hardware.transport import VALID_MASK
from ripples.sonification import NotePlanner, extract_features, note_wave
from ripples.transition import limit_rgb
from ripples.visualization import RipplesVisual

ROOT=Path(__file__).resolve().parents[1]


def main():
    source=ROOT/'exhibition-server/static/ripples-activate.rgb.gz'
    meta=json.loads((source.parent/'ripples-example.json').read_text())
    raw=np.frombuffer(gzip.decompress(source.read_bytes()),np.uint8).reshape(-1,meta['neurons'],3)
    visual=RipplesVisual()
    xyz=visual.waves.xyz[VALID_MASK]; weights=visual.transition.weights
    planner=NotePlanner(); notes=[]; features=[]
    for i,rgb in enumerate(raw[:600]):
        displayed=np.floor(limit_rgb(rgb.astype(float)*96/255,weights))
        regions=extract_features(displayed,xyz,weights)
        features.append(dict(time=i/10,regions=regions))
        notes.extend(planner.update(regions,i/10))
    rate=24000; duration=60; out=np.zeros((rate*duration,4),np.float32)
    for note in notes:
        first=round(note['start']*rate);count=min(round(note['duration']*rate),len(out)-first)
        out[first:first+count,note['speaker']]+=note_wave(note,np.arange(count)/rate)*.26
    out[-rate:]*=np.linspace(1,0,rate)[:,None]
    matrix=np.array([[.65,.15],[.15,.65],[.55,.30],[.30,.55]])
    stereo=out@matrix
    folder=ROOT/'data_led_example/tower_audio/led_sonification';folder.mkdir(exist_ok=True)
    sf.write(folder/'led_60s_stereo.wav',stereo,rate,subtype='PCM_16')
    sf.write(folder/'led_60s_4ch.flac',out,rate,subtype='PCM_24')
    starts={n['start'] for n in notes}
    report=dict(source=str(source.relative_to(ROOT)),source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        provenance='Recorded synthetic sensor LED example; not a live venue recording. Final limited Active RGB, first 60 seconds.',
        schema='ripples.led-sound.example.v1',sample_rate=rate,duration=duration,master=.26,
        stereo_peak=float(np.max(np.abs(stereo))),peak_per_channel=np.max(np.abs(out),axis=0).tolist(),
        notes=len(notes),simultaneous_start_groups=sum(sum(n['start']==t for n in notes)>1 for t in starts),
        single_start_groups=sum(sum(n['start']==t for n in notes)==1 for t in starts),
        source_geometry='src/ripples/waves.py + physical pixel weights from mapping_plus10.csv',
        reproduction='python scripts/render_led_sound.py',events=notes,features=features)
    (folder/'provenance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('events','features')},indent=2))


if __name__=='__main__':main()
