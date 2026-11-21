import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('speaker_control',ROOT/'exhibition-server/speaker_control.py')
control=importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)


def handler(body=None,peer='127.0.0.1',host='127.0.0.1:8000',origin='http://127.0.0.1:8000',token=''):
    data=json.dumps(body).encode() if body is not None else b''
    result=[]
    return SimpleNamespace(client_address=(peer,1234),server=SimpleNamespace(server_port=8000),
        headers={'Host':host,'Origin':origin,'Content-Type':'application/json','Content-Length':str(len(data)),
                 'X-Speaker-Token':token},rfile=io.BytesIO(data),page_dir=ROOT/'exhibition-server',
        _json=lambda value,status=200:result.append((status,value)),result=result)


@pytest.mark.parametrize('peer,host,origin',[
    ('192.168.0.5','127.0.0.1:8000','http://127.0.0.1:8000'),
    ('127.0.0.1','evil.example:8000','http://evil.example:8000'),
    ('127.0.0.1','127.0.0.1:8000','http://evil.example'),
    ('127.0.0.1','127.0.0.1:8001','http://127.0.0.1:8001'),
])
def test_reject_remote_origin_and_rebinding(peer,host,origin):
    h=handler({'action':'start','speaker':1},peer,host,origin)
    control.handle(h,True)
    assert h.result[0][0]==403


def test_valid_request_requires_token_and_strict_body(monkeypatch):
    instance=control.SpeakerControl(ROOT/'exhibition-server')
    monkeypatch.setattr(control,'_controllers',{str(ROOT/'exhibition-server'):instance})
    h=handler({'action':'start','speaker':1})
    control.handle(h,True)
    assert h.result[0][0]==403
    for body in ({'action':'start','speaker':True},{'action':'start','speaker':5},
                 {'action':'start','speaker':1,'device':'HDMI'},{'action':'shell','command':'bad'},[]):
        h=handler(body,token=instance.token)
        control.handle(h,True)
        assert h.result[0][0]==400
    h=handler({'action':'stop'},token=instance.token)
    control.handle(h,True)
    assert h.result[0][0]==200 and h.result[0][1]['state']=='stopped'


def test_single_owned_process_fixed_args_and_stop(monkeypatch):
    instance=control.SpeakerControl(ROOT/'exhibition-server')
    calls=[]
    class Process:
        returncode=0
        terminated=False
        def __init__(self,args,**kwargs): calls.append((args,kwargs,self))
        def terminate(self): self.terminated=True
        def wait(self,timeout): return 0
        def communicate(self,timeout=None): return b'test completed',None
    monkeypatch.setattr(control.subprocess,'Popen',Process)
    monkeypatch.setattr(control.threading,'Thread',lambda **kw:SimpleNamespace(start=lambda:None))
    assert instance.start(3)['state']=='running'
    with pytest.raises(RuntimeError): instance.start(4)
    args,kw,proc=calls[0]
    assert args[-2:]==['--test-speaker','3']
    assert '--allow-demo' not in args and not kw.get('shell')
    instance.stop()
    assert proc.terminated and instance.snapshot()['state']=='stopped'
    instance._watch(proc)
    assert instance.snapshot()['state']=='stopped'
    instance.start(2)
    instance._watch(calls[-1][2])
    assert instance.snapshot()['state']=='done'
    assert instance.snapshot()['physical_verified'] is False


def test_failed_stream_never_reports_success(monkeypatch):
    instance=control.SpeakerControl(ROOT/'exhibition-server')
    proc=SimpleNamespace(returncode=1,communicate=lambda timeout:(b'Error opening output device',None))
    instance.process=proc
    instance._watch(proc)
    assert instance.snapshot()['state']=='error'
    assert 'Error opening' in instance.snapshot()['message']


def test_timeout_kills_only_test_child():
    instance=control.SpeakerControl(ROOT/'exhibition-server')
    class Process:
        killed=False
        returncode=-1
        def communicate(self,timeout=None):
            if timeout is not None:
                raise control.subprocess.TimeoutExpired('speaker-test',timeout)
            return b'',None
        def kill(self): self.killed=True
    proc=Process()
    instance.process=proc
    instance._watch(proc)
    assert proc.killed
    assert instance.snapshot()['state']=='error'
    assert '15초' in instance.snapshot()['message']
