import json
from pathlib import Path
import subprocess
import time

import numpy as np
import pytest

from ripples.sonification import DataSynth, FeaturePublisher, NotePlanner, SCHEMA, extract_features, note_wave, read_frame

ROOT=Path(__file__).resolve().parents[2]


def regions(level=.4,height=.5,warmth=.3):
    return [dict(level=level,height=height,warmth=warmth) for _ in range(4)]


def test_silence_synchronous_and_staggered_onsets():
    p=NotePlanner()
    assert p.update(regions(0),0)==[]
    notes=p.update(regions(),.1)
    assert [n['speaker'] for n in notes]==[0,1,2,3]
    assert len(set(n['start'] for n in notes))==1
    assert p.update(regions(),.2)==[]
    p=NotePlanner(); r=regions(0); starts=[]
    for i in range(4):
        r[i]['level']=.4
        starts+=p.update(r,i*.3)
    assert [n['start'] for n in starts]==[0,.3,.6,pytest.approx(.9)]
    assert len(set(n['speaker'] for n in starts))==4


def test_python_browser_rules_match_display_features_and_wave():
    xyz=np.array([[x,y,z] for x in [0,1] for y in [0,1] for z in [0,1]],float)
    rgb=np.arange(24).reshape(8,3)*3
    weights=np.arange(1,9)
    r=extract_features(rgb,xyz,weights)
    notes=NotePlanner().update(r,0)
    js="const r=require('./exhibition-server/static/tower-audio/led-sonification.js');const d=JSON.parse(process.argv[1]);const f=r.features(d.rgb,d.xyz,d.w);const n=r.update(r.planner(),f,0);console.log(JSON.stringify({f,n,w:d.t.map(t=>r.wave(n[0],t))}));"
    times=[-.1,0,.123,.6,1.,2.9]
    result=json.loads(subprocess.check_output(['node','-e',js,json.dumps(dict(rgb=rgb.ravel().tolist(),xyz=xyz.ravel().tolist(),w=weights.tolist(),t=times))],cwd=ROOT))
    for py,browser in zip(r,result['f']):
        assert py==pytest.approx(browser)
    for py,browser in zip(notes,result['n']):
        assert py==pytest.approx(browser)
    assert note_wave(notes[0],times)==pytest.approx(result['w'],abs=1e-7)
    assert all(x['level']==0 for x in extract_features(rgb*0,xyz))
    high=NotePlanner().update(regions(height=.8),0)[0]
    low=NotePlanner().update(regions(height=.2),0)[0]
    assert high['frequency']>low['frequency']


def test_four_voice_limit_peak_and_stale_fade():
    synth=DataSynth(24000); peak=0
    for i in range(100):
        frame=dict(state='activate',version=str(i),regions=regions(1))
        x=synth.render(frame,2400)
        peak=max(peak,float(np.max(np.abs(x))))
        assert len(synth.notes)<=4
    assert .1<peak<=.24
    x=synth.render(None,12000)
    assert not x[4800:].any()
    assert not synth.render(dict(state='sleep'),2400).any()


def test_snapshot_validation_and_atomic_publisher(tmp_path):
    from ripples.visualization import RipplesVisual
    from ripples.hardware.transport import VALID_MASK
    path=tmp_path/'latest.json'
    assert read_frame(path) is None
    pub=FeaturePublisher(RipplesVisual(),path)
    try:
        pub.publish(np.full((*VALID_MASK.shape,3),10,np.uint8),'activate')
        deadline=time.monotonic()+2
        while not path.exists() and time.monotonic()<deadline: time.sleep(.01)
        data=read_frame(path)
        assert data['schema']==SCHEMA and len(data['regions'])==4
        data['timestamp']-=2;path.write_text(json.dumps(data))
        assert read_frame(path) is None
        data['timestamp']=time.time();data['regions'][0]['level']=float('nan')
        path.write_text(json.dumps(data));assert read_frame(path) is None
    finally:pub.close()
