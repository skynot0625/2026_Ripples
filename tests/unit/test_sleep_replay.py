import json
import numpy as np
import pytest

from ripples.sleep_replay import SleepReplay, example_sleep_data
from ripples.visualization import RipplesVisual
from ripples.hardware.transport import VALID_MASK
from test_position_simulation import run


TRACKS={'hyeol':[[t,t/60-2,0] for t in range(241)],
        'maek':[[t,0,2-t/60] for t in range(241)]}


@pytest.mark.parametrize('seconds',[0,120,240,479,480,599.9,600,1080])
def test_timeline_masks_and_browser_parity(seconds):
    xy=np.array([[-2,0],[0,0],[2,0],[0,2],[0,-2],[4,4]])
    r=SleepReplay(TRACKS)
    actual=r.mask(xy,seconds)
    browser=run(f"const r=new T.SleepReplay({json.dumps(TRACKS)});console.log(JSON.stringify(Array.from(r.mask({xy.tolist()},{seconds}))));")
    np.testing.assert_allclose(actual.ravel(),browser,atol=1e-12)
    assert r.stage(seconds)==('track' if seconds%600<480 else 'all')
    if seconds%600>=480:
        assert np.all(actual==1)
    else:
        assert actual[-1].max()<1
    np.testing.assert_array_equal(actual[:,0],actual[:,1])


def test_chronological_order_separate_instruments_and_gaps():
    r=SleepReplay(TRACKS)
    np.testing.assert_allclose(r.heads('hyeol',0)[0][0],[-2,0])
    np.testing.assert_allclose(r.heads('hyeol',240)[0][0],[0,0])
    np.testing.assert_allclose(r.heads('maek',0)[0][0],[0,2])
    assert not SleepReplay({'hyeol':[[0,0,0],[100,5,5]]}).heads('hyeol',240)
    with pytest.raises(ValueError):SleepReplay({'hyeol':[[1,0,0],[0,0,0]]})


def test_only_sensor_layer_is_selected_criticality_is_preserved():
    raw=np.zeros((*VALID_MASK.shape,3),np.uint8);raw[VALID_MASK]=120
    a,b=RipplesVisual('sleep',output_limits=False),RipplesVisual('sleep',output_limits=False)
    a.memory.fill(255)
    np.testing.assert_array_equal(a.render(raw,dt=.1,sensor_mask=np.zeros((8460,3))),b.render(raw,dt=.1))


def test_history_export_is_bounded_and_not_raw_audio():
    events=[{'t':i/10,'source':'maek','xy':[0,0]} for i in range(13000)]
    doc=example_sleep_data(events,[])
    assert len(doc['tracks']['maek'])==1200
    assert doc['tracks']['maek'][0][0]==100
    assert set(doc['tracks'])=={'hyeol','maek'}


def test_browser_freezes_track_on_sleep_entry_while_receiving_and_bounds_history():
    result=run("""
    const s=new S([[0,10],[0,10]]);
    for(let i=0;i<1300;i++){s.time=i;s.observe({source:'maek',uv:[.2,.3],features:{mic_level_0:.5}});}
    const P={count:1,ripplesWeight:[1],ripplesHeight:[.55],ripplesGrid:[[6,9,.55*23]]};
    s.render(P,'sleep',0);const first=s.sleepReplay.tracks.maek[0][0];
    s.time=1400;s.observe({source:'maek',uv:[.8,.3],features:{mic_level_0:.8}});s.render(P,'sleep',10);
    console.log(JSON.stringify({count:s.trackHistory.maek.length,first,held:s.sleepReplay.tracks.maek.at(-1)[0],live:s.trackHistory.maek.at(-1)[0]}));
    """)
    assert result=={'count':1200,'first':100,'held':1299,'live':1400}
