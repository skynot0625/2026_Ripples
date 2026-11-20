import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from ripples.visualization import RipplesVisual

JS=Path(__file__).resolve().parents[2]/"exhibition-server/static/ripples-example.js"


def run(code):
    if not shutil.which("node"): pytest.skip("node unavailable")
    return json.loads(subprocess.check_output(["node","-e",f"const T=require({json.dumps(str(JS))}), S=T.PositionSimulation;"+code],text=True))


def test_scene_coordinates_not_synthetic_path_and_outside_rejected():
    result=run("""
    const s=new S([[-.462,11.149],[.09,19.851]]);
    const lower=s.position({x:-462,y:90}),upper=s.position({x:11149,y:19851}),outside=s.position({x:-1000,y:3000});
    s.setPositions({hyeol:{x:3000,y:4000},maek:{x:8000,y:15000}},'m30',100);s.tick(100,false);
    const before=JSON.parse(JSON.stringify(s.latest));
    s.setPositions({hyeol:{x:9000,y:7000},maek:{x:8000,y:15000}},'m30',200);s.tick(200,false);
    console.log(JSON.stringify({lower,upper,outside,before,after:s.latest}));
    """)
    assert result['lower']==[0,0] and result['upper']==[1,1]
    assert result['outside'] is None
    assert result['after']['hyeol']['world']==[9,7]
    assert result['before']['maek']['uv']==result['after']['maek']['uv']
    assert result['before']['hyeol']['uv']!=result['after']['hyeol']['uv']


@pytest.mark.parametrize('source,features',[
 ('hyeol',{'depth_0':1.2,'coverage_0':.8,'depth_1':2.1,'coverage_1':.9,'ceiling_0':2.3}),
 ('maek',{'mic_level_0':.3,'mic_level_1':.6,'mic_level_2':.9}),
])
def test_feature_mapping_and_spatial_smoothing_match_python(source,features):
    event={'source':source,'uv':[.7,.6],'features':features}
    result=run(f"const v=new Float32Array(32*32*24*3);S.stamp(v,{json.dumps(event)});console.log(JSON.stringify(Array.from(S.blur(v))));")
    volume=np.zeros((32,32,24,3),np.float32)
    RipplesVisual.stamp(volume,{'source':source,'xy':[2,1],'features':features})
    np.testing.assert_allclose(np.asarray(result).reshape(volume.shape),RipplesVisual._smooth(volume),atol=5e-5)


def test_memory_bin_means_std_disconnect_and_clear():
    result=run("""
    const s=new S([[0,10],[0,10]]),e=v=>({source:'hyeol',uv:[.2,.3],features:{ceiling_0:v}});
    s.observe(e(1));s.observe(e(3));s.flush();s.observe(e(4));s.flush();
    const cell=Array.from(s.cells.values())[0];s.rebuildMemory();
    const spread=Math.max(...s.spread),copy=JSON.parse(JSON.stringify(cell));
    s.setPositions({hyeol:{x:2000,y:3000}},'m30',0);s.offline();s.tick(4000,false);
    const stale=Object.keys(s.latest).length,bucket=s.bucket.size;
    s.clear();console.log(JSON.stringify({cell:copy,spread,stale,bucket,size:s.cells.size,mean:s.mean.some(x=>x)}));
    """)
    assert result['cell']['features']['ceiling_0']==3
    assert result['cell']['count']==2 and result['cell']['m2']['ceiling_0']==2
    assert result['spread']>0
    assert result['stale']==0 and result['bucket']==0
    assert result['size']==0 and not result['mean']


def test_pausing_stops_observation_and_implicit_storage_is_bounded():
    result=run("""
    const s=new S([[0,10],[0,10]]);
    s.setPositions({maek:{x:5000,y:5000}},'m30',0);s.tick(100,false);
    const before=s.time;s.tick(200,true);
    for(const source of ['hyeol','maek'])for(let x=0;x<24;x++)for(let y=0;y<24;y++) {
      s.observe({source,uv:[(x+.5)/24,(y+.5)/24],features:{ceiling_0:2}});
    }
    s.flush();console.log(JSON.stringify({before,after:s.time,size:s.cells.size}));
    """)
    assert result['before']==result['after']
    assert result['size']==1152


def test_led_light_centroid_follows_maek_and_trail_fades_on_disconnect():
    result=run("""
    const s=new S([[0,10],[0,10]]),P={count:1024,loc:[],ripplesWeight:[],ripplesHeight:[]};
    for(let x=0;x<32;x++)for(let y=0;y<32;y++){P.loc.push(x,0,-y,0);P.ripplesWeight.push(1);P.ripplesHeight.push(.2);}
    const centroid=rgb=>{let total=0,sum=0;for(let i=0;i<P.count;i++){const v=rgb[i*3]+rgb[i*3+1]+rgb[i*3+2];total+=v;sum+=Math.floor(i/32)*v;}return sum/total;};
    let now=0;
    for(let i=0;i<10;i++){now+=100;s.setPositions({maek:{x:2000,y:3000}},'m30',now);s.tick(now,false);}
    const left=centroid(s.render(P,'activate'));
    for(let i=0;i<30;i++){now+=100;s.setPositions({maek:{x:8000,y:3000}},'m30',now);s.tick(now,false);}
    const right=centroid(s.render(P,'activate'));
    s.offline();for(let i=0;i<70;i++){now+=100;s.tick(now,false);}
    console.log(JSON.stringify({left,right,dark:!s.render(P,'activate').some(x=>x)}));
    """)
    assert result['left'] < 10 and result['right'] > 20
    assert result['dark']


def test_stationary_pose_remains_valid_without_change_messages():
    result=run("""
    const s=new S([[0,10],[0,10]]);
    s.setPositions({maek:{x:2000,y:3000}},'m30',0);s.tick(5000,false);
    console.log(JSON.stringify({xy:s.latest.maek.world,bucket:s.bucket.size}));
    """)
    assert result['xy']==[2,3] and result['bucket']==1
