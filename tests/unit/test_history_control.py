import importlib.util
import json
import time
from pathlib import Path
import pytest

path=Path(__file__).resolve().parents[2]/'exhibition-server/history_control.py'
spec=importlib.util.spec_from_file_location('history_control',path)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


def test_preview_api_reopens_deduplicates_and_keeps_actual_inputs_separate(tmp_path):
    page=tmp_path/'server';(page/'run').mkdir(parents=True)
    (page/'run/speaker-test.json').write_text(json.dumps({'project_dir':str(tmp_path)}))
    second=int(time.time())-2
    event={'source':'maek','xy':[1,2],'features':{'mic_level_0':.5},'t':second}
    assert module.request(page,[event])['accepted']==1
    assert module.request(page,[event])['accepted']==0
    data=module.request(page)
    assert data['example'] is True and data['storage']['points']==1
    assert data['tracks']['maek'][0][1:3]==[1,2]
    assert data['snapshot'][0]['counts']['mic_level_0']==1
    assert (tmp_path/'runtime/venue3d-history.sqlite3').is_file()
    assert not (tmp_path/'runtime/history-live-estimated.sqlite3').exists()
    with pytest.raises(ValueError):module.request(page,[dict(event,source='unknown')])
