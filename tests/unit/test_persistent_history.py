import json
import numpy as np
import pytest
from ripples.memory import SpatialMemory
from ripples.sleep_replay import SleepReplay
from ripples.visualization import layer_separation,RipplesVisual
from ripples.hardware.transport import VALID_MASK
from test_position_simulation import run


def test_restart_dedup_full_period_and_gap_segments(tmp_path):
    path=tmp_path/'history.sqlite3'
    events=[dict(source='hyeol',xy=[float(i%5),0],features={'ceiling_0':2},t=i) for i in [0,1,2,3600,3601]]
    db=SpatialMemory(path)
    assert db.import_recording('recording',events,epoch=100000)
    db.close();db=SpatialMemory(path)
    assert not db.import_recording('recording',events,epoch=100000)
    data=db.history(max_points=4)
    assert data['storage']['points']==5
    assert data['tracks']['hyeol'][0][0]==100000 and data['tracks']['hyeol'][-1][0]==103601
    assert data['tracks']['hyeol'][0][3]!=data['tracks']['hyeol'][-1][3]
    assert not SleepReplay(data['tracks']).heads('hyeol',240)
    assert db.snapshot()[0]['counts']['ceiling_0']>=1
    db.close()


def test_40_hours_retained_on_disk_but_replay_response_bounded(tmp_path):
    db=SpatialMemory(tmp_path/'forty-hours.sqlite3')
    # Same on-disk one-Hz position representation, without manufacturing sensor features.
    with db.db:
        db.db.executemany('INSERT INTO trajectory VALUES (?,?,?,?)',
            ((role,1700000000+t,float(np.sin(t/50)),0.) for role in ('hyeol','maek') for t in range(40*3600)))
    db.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    data=db.history()
    assert data['storage']['points']==288000
    assert data['storage']['duration_s']==143999
    assert all(2<len(rows)<=9600 for rows in data['tracks'].values())
    assert data['storage']['database_bytes']<64*1024**2
    replay=SleepReplay(data['tracks'])
    assert replay.heads('hyeol',240)  # Decimation must not look like a sensor outage.
    rows=data['tracks']['hyeol'][:3]
    expected=SleepReplay({'hyeol':rows}).mask([[0,0]],240).ravel()
    browser=run(f"console.log(JSON.stringify(Array.from(new T.SleepReplay({json.dumps({'hyeol':rows})}).mask([[0,0]],240))));")
    np.testing.assert_allclose(browser,expected,atol=1e-12)
    db.close()


def test_failed_import_cannot_leave_a_path_without_its_statistics(tmp_path):
    db=SpatialMemory(tmp_path/'history.sqlite3')
    with pytest.raises(ValueError):
        db.import_recording('bad',[dict(source='maek',xy=[0,0],t=0,features={'invented':1})],epoch=0)
    assert db.history()['storage']['points']==0
    db.close()


def test_two_visible_separators_and_python_browser_parity():
    h=np.linspace(0,1,101)
    gain=layer_separation(h)
    browser=run(f"console.log(JSON.stringify({h.tolist()}.map(x=>T.layerSeparation(x))));")
    np.testing.assert_allclose(browser,gain,atol=1e-12)
    assert layer_separation([.36,.72]).max()<.11
    np.testing.assert_array_equal(layer_separation([.2,.55,.85]),1)
    visual=RipplesVisual('sleep',output_limits=False)
    visual.memory.fill(255);visual.memory_spread.fill(90)
    out=visual.render(np.zeros((*VALID_MASK.shape,3),np.uint8),dt=.1)[VALID_MASK]
    h=visual.transition.height
    assert not out[((h>=.32)&(h<=.40))|((h>=.68)&(h<=.76))].any()
