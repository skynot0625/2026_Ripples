import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

import numpy as np
import pytest

sf=pytest.importorskip("soundfile")
from ripples.tower_audio import (RATE, PEAK_LIMIT, DURATIONS, active_bank, recording_runs,
                                 extract_sleep, load_bank, make_plan, mix_block)
from ripples.audio_play import validate_output, output_channels

ROOT=Path(__file__).resolve().parents[2]
BANK=ROOT/"exhibition-server/static/tower-audio"


def packet(folder,name,start,*,session="s",demo=True,seconds=1,silent=False):
    sr=8000
    t=np.arange(sr*seconds)/sr
    x=np.zeros_like(t) if silent else .10*np.sin(2*np.pi*240*t)*np.exp(-((t-.5)/.22)**2)
    sf.write(folder/f"{name}.wav",np.column_stack([x*.2,x,x*.3]),sr,subtype="PCM_16")
    (folder/f"{name}.json").write_text(json.dumps({"device_id":"m","session_id":session,
         "start_frame":start,"frames":len(x),"demo":demo}),encoding="utf-8")


def test_packet_order_gap_session_and_budget(tmp_path):
    packet(tmp_path,"z_first",0)
    packet(tmp_path,"a_second",8000)
    packet(tmp_path,"c_gap",24000)
    packet(tmp_path,"b_new",0,session="t")
    runs=list(recording_runs(tmp_path))
    assert [len(x) for x,_,_ in runs]==[16000,8000,8000]
    assert runs[0][2][0]["relative_file"]=="z_first.wav"
    assert sum(len(x)/sr for x,sr,_ in recording_runs(tmp_path,max_seconds=1.5))==1.5


def test_silent_rejected_salient_channel_and_source(tmp_path):
    packet(tmp_path,"quiet",0,silent=True)
    assert extract_sleep(tmp_path)==[]
    packet(tmp_path,"event",8000)
    bank=extract_sleep(tmp_path)
    assert bank
    source=bank[0]["source"]
    assert source["mic_channel"]==2
    assert source["kind"]=="synthetic_maek"
    assert all(p["session_frame"] is not None for p in source["parts"])
    assert abs(bank[0]["samples"][0])<1e-7
    assert abs(bank[0]["samples"][-1])<1e-7
    assert np.max(np.abs(bank[0]["samples"]))<=.481


def test_corrupt_metadata_fails_before_selection(tmp_path):
    packet(tmp_path,"x",0)
    path=tmp_path/"x.json"
    meta=json.loads(path.read_text());meta["frames"]+=1
    path.write_text(json.dumps(meta))
    with pytest.raises(ValueError,match="frame count"):
        list(recording_runs(tmp_path))


@pytest.mark.parametrize("state",DURATIONS)
def test_random_plans_bound_concurrency_and_use_all_outputs(state):
    manifest,_=load_bank(BANK)
    for seed in range(20):
        plan=make_plan(manifest["bank"],state,seed)
        assert plan==make_plan(manifest["bank"],state,seed)
        assert {e["speaker"] for e in plan}=={0,1,2,3}
        marks=sorted([(e["start"],1) for e in plan]+[(e["start"]+e["duration"],-1) for e in plan])
        n=0
        for _,delta in marks:
            n+=delta
            assert 0<=n<=2
        last_end=[0]*4
        last_id={}
        for e in plan:
            assert e["start"]>=last_end[e["speaker"]]
            assert e["start"]-last_id.get(e["id"],-1000)>=15.999
            last_end[e["speaker"]]=e["start"]+e["duration"]
            last_id[e["id"]]=e["start"]
        assert max(last_end)<DURATIONS[state]


def test_mixer_exact_routing_offsets_silence_and_caps():
    samples={"x":np.ones(100,np.float32)}
    events=[{"id":"x","start":.5,"speaker":2,"gain":1}]
    x=mix_block(events,samples,45,20,rate=100,master=1)
    assert np.all(x[:5]==0)
    assert np.all(x[:,[0,1,3]]==0)
    assert np.allclose(x[5:,2],PEAK_LIMIT)
    assert not np.any(mix_block(events,samples,150,20,rate=100))


def test_no_silent_stereo_fallback():
    with pytest.raises(ValueError,match="four independent"):
        validate_output({"max_output_channels":2},[0,1,2,3])
    with pytest.raises(ValueError,match="distinct"):
        validate_output({"max_output_channels":8},[0,1,0,1])
    validate_output({"max_output_channels":8},[4,5,6,7])


def test_surround_stream_preserves_front_and_back_mapping():
    device={"max_output_channels":8}
    assert output_channels(device,[0,1,4,5],8)==8
    for count in (4,5,9,True):
        with pytest.raises(ValueError,match="stream_channels"):
            output_channels(device,[0,1,4,5],count)


def test_rendered_files_are_exact_length_and_four_channels():
    report=json.loads((ROOT/"data_led_example/tower_audio/validation.json").read_text())
    for state,duration in DURATIONS.items():
        info=sf.info(ROOT/f"data_led_example/tower_audio/{state}_{duration//60}min_4ch.flac")
        assert (info.samplerate,info.channels,info.frames)==(RATE,4,RATE*duration)
        assert max(report[state]["peak_per_channel"])<PEAK_LIMIT
        assert report[state]["max_simultaneous_voices"]==2


def test_browser_rejects_overlapping_or_wrong_channel_plans():
    if not shutil.which("node"):
        pytest.skip("node unavailable")
    code="""
const fs=require('fs'),rules=require('./exhibition-server/static/tower-audio/player.js');
const m=JSON.parse(fs.readFileSync('./exhibition-server/static/tower-audio/manifest.json'));
rules.validate(m);
const e=m.plans.activate[0];m.plans.activate.push({...e},{...e});
try {rules.validate(m);process.exit(2);} catch(e) {if(!e.message.includes('제한')) throw e;}
"""
    subprocess.run(["node","-e",code],cwd=ROOT,check=True)


def test_static_asset_traversal_and_allowed_types():
    spec=importlib.util.spec_from_file_location("tower_audio_assets",ROOT/"exhibition-server/audio_assets.py")
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    base=ROOT/"exhibition-server"
    assert module.asset_path(base,"/static/tower-audio/manifest.json").is_file()
    for path in ("../ripples-example.json","%2e%2e/ripples-example.json","../../server.py","missing.wav"):
        assert module.asset_path(base,"/static/tower-audio/"+path) is None


def test_player_resamples_and_routes_without_opening_real_hardware(monkeypatch,tmp_path):
    import sys
    from types import SimpleNamespace
    from ripples import audio_play
    observed={}
    class Stream:
        def __init__(self,**kwargs):
            observed.update(kwargs)
        def __enter__(self):
            return self
        def __exit__(self,*args):
            observed["closed"]=True
    fake=SimpleNamespace(query_devices=lambda *args:{"max_output_channels":8},
        check_output_settings=lambda **kwargs:observed.update(checked=kwargs),OutputStream=Stream)
    monkeypatch.setitem(sys.modules,"sounddevice",fake)
    cfg=tmp_path/"config.json"
    cfg.write_text(json.dumps({"device":3,"channel_map":[0,1,4,5],"stream_channels":8,"sample_rate":48000,"master":.22}))
    monkeypatch.setattr(sys,"argv",["audio","--config",str(cfg),"--bank",str(BANK),"--allow-demo"])
    class Controller:
        def __init__(self,*args,**kwargs):
            pass
        def at(self):
            return {"state":"activate","mode":"scheduled","phase_seconds":2.,"anchor":"2026-11-21","cycle_index":0}
    monkeypatch.setattr(audio_play,"StateController",Controller)
    blocks=[]
    def sleep(seconds):
        if seconds==.1:
            for _ in range(5):
                out=np.empty((1024,8),np.float32)
                observed["callback"](out,1024,None,False)
                blocks.append(out)
            raise KeyboardInterrupt
    monkeypatch.setattr(audio_play.time,"sleep",sleep)
    audio_play.main()
    assert observed["samplerate"]==48000 and observed["channels"]==8
    assert observed["closed"]
    x=np.concatenate(blocks)
    assert not np.any(x[:,[2,3,6,7]])
    assert np.max(np.abs(x[:,[0,1,4,5]]))>0
    assert np.max(np.abs(x))<=PEAK_LIMIT
