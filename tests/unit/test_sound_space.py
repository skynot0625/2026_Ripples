import hashlib
import io
import json
from pathlib import Path
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import numpy as np
import pytest

sf=pytest.importorskip('soundfile')
pytest.importorskip('webrtcvad')
from ripples.sound_space import SoundSpace, CapacityError
from ripples.sound_space_cli import accept_wav, handler_for
from ripples.tower_audio import RATE, load_bank, make_plan
from ripples.voice_memory import voice_effect, speech_regions

ROOT=Path(__file__).resolve().parents[2]


def tone(seed=1,seconds=1):
    return np.random.default_rng(seed).normal(0,.03,int(RATE*seconds)).astype(np.float32)


def space(tmp_path,**policy):
    return SoundSpace(tmp_path/'memory',dict(min_free_bytes=0,**policy))


def test_refuses_unowned_directory_and_path_escape(tmp_path):
    p=tmp_path/'existing';p.mkdir();(p/'mine.txt').write_text('keep')
    with pytest.raises(ValueError,match='unmarked'): SoundSpace(p)
    s=space(tmp_path)
    with pytest.raises(ValueError,match='Unowned'): s.path('raw','../../mine')
    assert (p/'mine.txt').read_text()=='keep'


def test_raw_byte_count_age_and_duplicate_limits(tmp_path):
    s=space(tmp_path,raw_max_bytes=65000,raw_max_items=2)
    first,new=s.add('raw',tone(1),RATE,{'session_id':'s','start_frame':0})
    assert new
    assert s.add('raw',tone(1),RATE,{'session_id':'s','start_frame':0})==(first,False)
    for i in range(2,8): s.add('raw',tone(i),RATE,{'start_frame':i*RATE})
    assert s.status()['raw']['bytes']<=65000
    assert s.status()['raw']['items']<=2
    assert s.status()['unprocessed_evicted']>0
    with s.db() as db: db.execute("UPDATE sounds SET added=?",(time.time()-4000,))
    s.maintain()
    assert s.status()['raw']['items']==0
    assert not list((s.root/'raw').glob('*.flac'))


def test_unknown_files_and_disk_reserve_are_not_bypassed(tmp_path):
    s=space(tmp_path,raw_max_bytes=1000)
    sentinel=s.root/'raw'/'external-note.txt';sentinel.write_bytes(b'x'*990)
    with pytest.raises(CapacityError): s.add('raw',np.zeros(24000),RATE,{})
    assert sentinel.exists()
    s.policy['raw_max_bytes']=1000000
    s.add('raw',tone(),RATE,{})
    s.policy['min_free_bytes']=10**18
    with pytest.raises(CapacityError): s.add('raw',tone(2),RATE,{})
    # Evicted files and committed catalog must agree even when the admission fails.
    assert s.status()['raw']['items']==0
    assert sentinel.exists()


def test_pinned_input_is_not_deleted_under_pressure(tmp_path):
    s=space(tmp_path,raw_max_items=1)
    ident,_=s.add('raw',tone(),RATE,{})
    with s.db() as db: db.execute('UPDATE sounds SET pin=?',(time.time()+60,))
    with pytest.raises(CapacityError): s.add('raw',tone(2),RATE,{})
    assert s.path('raw',ident).exists()


def test_wav_ack_and_retry_contract_over_http(tmp_path):
    s=space(tmp_path)
    buf=io.BytesIO();sf.write(buf,np.zeros((44100,3)),44100,format='WAV',subtype='PCM_16')
    data=buf.getvalue()
    meta={'device_id':'test-maek','session_id':'session','chunk_id':'one','start_frame':0,'frames':44100,'demo':True}
    http=ThreadingHTTPServer(('127.0.0.1',0),handler_for(s,{}))
    thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
    try:
        url=f'http://127.0.0.1:{http.server_port}/audio'
        req=urllib.request.Request(url,data=data,headers={'Content-Type':'audio/wav','X-Audio-Metadata':json.dumps(meta),'Idempotency-Key':'one'})
        with urllib.request.urlopen(req) as r: first=json.load(r)
        with urllib.request.urlopen(req) as r: again=json.load(r)
        assert first['stored'] and first['new'] and not again['new']
        assert first['sha256']==hashlib.sha256(data).hexdigest()
        assert s.status()['raw']['items']==1
        with pytest.raises(ValueError,match='SHA256'): accept_wav(s,data,dict(meta,sha256='bad'))
        with pytest.raises(ValueError,match='identity'): accept_wav(s,data,dict(meta,start_frame=44100))
    finally:
        http.shutdown();http.server_close();thread.join()


def test_voice_vad_and_pitch_preserving_tail():
    x,sr=sf.read(ROOT/'data_led_example/tower_audio/synthetic_source/voice.wav',dtype='float32')
    regions=speech_regions(x,sr)
    assert len(regions)==3
    assert speech_regions(np.zeros(sr*2),sr)==[]
    t=np.arange(RATE*2)/RATE
    y=voice_effect(.10*np.sin(2*np.pi*220*t),RATE)
    assert len(y)>RATE*4
    f=np.fft.rfftfreq(RATE,1/RATE)
    peak=f[np.argmax(np.abs(np.fft.rfft(y[RATE//2:RATE//2+RATE])))]
    assert abs(peak-220)<2
    assert np.max(np.abs(y))<=.481


def test_voice_scheduling_never_layers_sentences():
    manifest,_=load_bank(ROOT/'exhibition-server/static/tower-audio')
    voice_ids={b['id'] for b in manifest['bank'] if b['source'].get('category')=='speech_candidate'}
    assert voice_ids
    for seed in range(25):
        plan=make_plan(manifest['bank'],'sleep',seed,include_speech=True)
        v=[e for e in plan if e['id'] in voice_ids]
        assert v
        assert all(b['start']>=a['start']+a['duration']+5.999 for a,b in zip(v,v[1:]))


def test_atomic_export_load_and_expired_pool_becomes_silent_sleep(tmp_path):
    s=space(tmp_path,clips_max_items=2)
    meta={'kind':'synthetic_maek','category':'environment','score':1}
    s.add('clips',tone(),RATE,meta)
    first=s.export()
    m,x=load_bank(s.root)
    assert len([b for b in m['bank'] if b['state']=='sleep'])==1
    assert s.export()==first
    s.add('clips',tone(2),RATE,meta)
    second=s.export();assert second!=first
    assert s.status()['exports_bytes']<=s.policy['exports_max_bytes']
    with s.db() as db: db.execute("UPDATE sounds SET seen=0 WHERE kind='clips'")
    s.maintain();s.export()
    m,_=load_bank(s.root)
    assert m['plans']['sleep']==[] and m['sleep_source']=='empty'


def test_managed_processing_keeps_voice_provenance_and_acknowledges_inputs(tmp_path):
    s=space(tmp_path)
    path=ROOT/'data_led_example/tower_audio/synthetic_source/voice.wav'
    x,sr=sf.read(path,dtype='float32')
    meta=json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
    s.add('raw',x,sr,meta)
    assert s.process(flush=True)>=3
    assert s.status()['pending_raw']==0
    s.export()
    manifest,_=load_bank(s.root)
    voices=[b for b in manifest['bank'] if b['source'].get('category')=='speech_candidate']
    assert voices and all(b['source']['parts'][0]['origin']['voice']=='Microsoft Heami Desktop' for b in voices)
    assert all(b['source']['kind']=='synthetic_maek' for b in voices)
    assert s.process(flush=True)==0


def test_real_source_report_does_not_claim_synthetic_voice(tmp_path):
    from ripples.audio_build import write_sources
    manifest={'bank':[{'id':'real','file':'bank/real.wav','state':'sleep','source':{
        'kind':'recording','sample_rate':RATE,'mic_channel':2,'processing':'quiet reverb',
        'parts':[{'relative_file':'field.wav','file_frame':RATE,'frames':RATE,'origin':None}]}}]}
    write_sources(manifest,tmp_path)
    report=(tmp_path/'SOURCES.md').read_text(encoding='utf-8')
    assert '제공된 녹음에서 선택한 소리' in report
    assert '## 목소리 예시의 원본' not in report
    assert '1.00–2.00s' in report
