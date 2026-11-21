import json
import sys
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from ripples.transition_audio import make_cue, cue_block, CUE_PEAK, DURATION

ROOT=Path(__file__).resolve().parents[2]


def test_lan_sha256_fallback_keeps_asset_integrity():
    if not shutil.which('node'): pytest.skip('node unavailable')
    code="""
const crypto=require('node:crypto'),fs=require('node:fs');
const t=require('./exhibition-server/static/ripples-example.js');
for(const n of [0,3,55,56,63,64,65,1000,1000000]) {
 const b=Buffer.alloc(n,97);
 if(t.sha256Fallback(b)!==crypto.createHash('sha256').update(b).digest('hex')) throw Error('hash '+n);
}
for(const f of ['exhibition-server/static/ripples-sleep.rgb.gz','exhibition-server/static/tower-audio/cues/to_sleep.wav']) {
 const b=fs.readFileSync(f);
 if(t.sha256Fallback(b)!==crypto.createHash('sha256').update(b).digest('hex')) throw Error('hash asset');
}
"""
    subprocess.run(['node','-e',code],cwd=ROOT,check=True)


@pytest.mark.parametrize('state',['activate','sleep'])
def test_cue_follows_five_second_light_swell_and_fades(state):
    rate=24000
    x=make_cue(state,rate)
    assert x.shape==(rate*5,4) and np.isfinite(x).all()
    assert np.max(np.abs(x))<=CUE_PEAK+1e-7
    assert not np.any(x[[0,-1]])
    energy=np.sqrt(np.mean(x.reshape(50,2400,4)**2,axis=(1,2)))
    assert 1<np.argmax(energy)/10<2.5
    assert energy[-1]<energy.max()*.01 and energy[0]<energy.max()*.1
    assert np.max(np.abs(np.diff(x,axis=0)))<.02
    assert np.array_equal(x,make_cue(state,rate))


def test_cue_block_slices_silence_and_master():
    x=make_cue('sleep',1000)
    assert not cue_block(x,5000,100,.22).any()
    assert not cue_block(x,0,100,0).any()
    sliced=cue_block(x,-10,100,.1)
    assert not sliced[:10].any()
    assert np.allclose(sliced[10:],x[:90]*.1)
    assert not np.allclose(make_cue('sleep'),make_cue('activate'))


@pytest.mark.parametrize('initial,target',[('activate','sleep'),('sleep','activate'),('activate','activate')])
def test_player_cues_only_real_state_changes_and_routes_four_outputs(monkeypatch,tmp_path,initial,target):
    from ripples import audio_play
    observed={}
    blocks=[]
    class Stream:
        def __init__(self,**kwargs): observed.update(kwargs)
        def __enter__(self): return self
        def __exit__(self,*args): pass
    fake=SimpleNamespace(query_devices=lambda *args:{'max_output_channels':8},
        check_output_settings=lambda **kwargs:None,OutputStream=Stream)
    monkeypatch.setitem(sys.modules,'sounddevice',fake)
    cfg=tmp_path/'audio.json'
    cfg.write_text(json.dumps({'device':1,'channel_map':[0,1,4,5], 'stream_channels':8,'master':.10}))
    monkeypatch.setattr(sys,'argv',['audio','--config',str(cfg),'--allow-demo'])
    states=iter([initial,target])
    class Controller:
        def __init__(self,*args,**kwargs): pass
        def at(self): return {'state':next(states),'mode':'manual'}
    monkeypatch.setattr(audio_play,'StateController',Controller)
    monkeypatch.setattr(audio_play,'make_plan',lambda *args:[])
    monkeypatch.chdir(ROOT)
    calls=0
    def sleep(seconds):
        nonlocal calls
        if seconds!=.1: return
        chunk=[]
        for _ in range(250):
            out=np.empty((1024,8),np.float32)
            observed['callback'](out,1024,None,False)
            chunk.append(out)
        blocks.append(np.concatenate(chunk))
        calls+=1
        if calls==2: raise KeyboardInterrupt
    monkeypatch.setattr(audio_play.time,'sleep',sleep)
    audio_play.main()
    assert not blocks[0].any()  # launching midway through a state is not a transition
    assert not blocks[1][:,[2,3,6,7]].any()
    if initial==target:
        assert not blocks[1].any()
    else:
        assert np.max(np.abs(blocks[1]))>.005
        assert np.max(np.abs(blocks[1]))<.015
        assert not blocks[1][-1000:].any()
