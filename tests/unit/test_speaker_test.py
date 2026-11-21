from types import SimpleNamespace

import numpy as np
import pytest

from ripples.speaker_test import TEST_PEAK, identification_tone, play_speaker_test


@pytest.mark.parametrize("number",[1,2,3,4])
def test_tones_are_quiet_faded_and_countable(number):
    rate=48000
    x=identification_tone(number,rate)
    assert x.dtype==np.float32
    assert np.max(np.abs(x))<=TEST_PEAK
    assert x[0]==x[-1]==0
    for i in range(number):
        pulse=x[round(i*.48*rate):round((i*.48+.24)*rate)]
        gap=x[round((i*.48+.24)*rate):round((i+1)*.48*rate)]
        assert np.max(np.abs(pulse))>.019
        assert not np.any(gap)


@pytest.mark.parametrize("selected",[None,1,2,3,4])
def test_callback_routes_one_speaker_and_keeps_other_six_channels_silent(selected):
    captured=[]
    pauses=[]
    class Stop(Exception):
        pass
    class Abort(Exception):
        pass
    class Stream:
        def __init__(self,**kwargs):
            self.kwargs=kwargs
        def __enter__(self):
            blocks=[]
            for _ in range(200):
                out=np.full((1024,8),np.nan,np.float32)
                try:
                    self.kwargs['callback'](out,1024,None,False)
                except Stop:
                    blocks.append(out.copy())
                    break
                blocks.append(out.copy())
            else:
                pytest.fail('test did not terminate')
            captured.append(np.concatenate(blocks))
            self.kwargs['finished_callback']()
            return self
        def __exit__(self,*args):
            pass
    sd=SimpleNamespace(OutputStream=Stream,CallbackStop=Stop,CallbackAbort=Abort,sleep=pauses.append)
    mapping=[0,1,4,5]
    play_speaker_test(sd,'test',48000,8,mapping,['A','B','C','D'],selected)
    numbers=[selected] if selected else [1,2,3,4]
    assert len(captured)==len(numbers)
    assert pauses==[2000]*(len(numbers)-1)
    for number,x in zip(numbers,captured):
        target=mapping[number-1]
        assert np.all(np.isfinite(x))
        assert not np.any(np.delete(x,target,axis=1))
        assert 0<np.max(np.abs(x[:,target]))<=TEST_PEAK


def test_device_error_stops_before_next_speaker():
    class Abort(Exception):
        pass
    opened=[]
    class Stream:
        def __init__(self,**kwargs):
            self.kwargs=kwargs
            opened.append(self)
        def __enter__(self):
            out=np.ones((1024,8),np.float32)
            with pytest.raises(Abort):
                self.kwargs['callback'](out,1024,None,'underflow')
            assert not np.any(out)
            self.kwargs['finished_callback']()
            return self
        def __exit__(self,*args):
            pass
    sd=SimpleNamespace(OutputStream=Stream,CallbackAbort=Abort)
    with pytest.raises(RuntimeError,match='underflow'):
        play_speaker_test(sd,'test',48000,8,[0,1,4,5],['A','B','C','D'])
    assert len(opened)==1
