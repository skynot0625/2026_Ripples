import numpy as np
import pytest

from ripples.visualization import RipplesVisual, frequency_rate
from ripples.hardware.transport import VALID_MASK
from ripples.sensors import pcm_features
from test_position_simulation import run


def test_camera_sound_ceiling_have_separate_ordered_heights():
    ranges=[]
    for source, features in [('hyeol', {'depth_4':4.,'coverage_4':1.}),
                             ('maek', {'mic_level_1':1.}), ('hyeol', {'ceiling_0':60.})]:
        v=np.zeros((32,32,24,3),np.float32)
        RipplesVisual.stamp(v,dict(source=source,xy=[0,0],features=features))
        z=np.where(v.any(axis=(0,1,3)))[0]
        ranges.append((z.min(),z.max()))
    assert ranges[0][1]<ranges[1][0] and ranges[1][1]<ranges[2][0]


@pytest.mark.parametrize('state',['activate','sleep'])
def test_sensor_gap_survives_interpolation_and_dream_spread(state):
    visual=RipplesVisual(state,output_limits=False)
    visual.trail.fill(255);visual.memory.fill(255);visual.memory_spread.fill(90)
    raw=np.zeros((*VALID_MASK.shape,3),np.uint8)
    out=visual.render(raw,0,dt=.1)[VALID_MASK]
    h=visual.transition.height
    assert not out[(h>=.32)&(h<=.40)].any()
    assert out[h<.28].any() and out[(h>.43)&(h<.70)].any()
    result=run(f"""
    const s=new S([[0,10],[0,10]]);s.trail.fill(255);s.mean.fill(255);s.spread.fill(90);
    const h=[.2,.34,.38,.55],P={{count:4,ripplesWeight:[1,1,1,1],ripplesHeight:h,ripplesGrid:h.map(z=>[16,16,z*23])}};
    console.log(JSON.stringify(Array.from(s.render(P,'{state}'))));
    """)
    rgb=np.array(result).reshape(4,3)
    assert not rgb[1:3].any() and rgb[0].any() and rgb[3].any()


def test_frequency_field_matches_browser_and_rate_is_slow():
    e={'source':'maek','xy':[2,1],'features':{'centroid_0':400,'centroid_1':800}}
    field=np.zeros((32,32,2))
    RipplesVisual.stamp_frequency(field,e)
    result=run("const a=new Float64Array(2048);S.stampFrequency(a,{source:'maek',uv:[.7,.6],features:{centroid_0:400,centroid_1:800}});console.log(JSON.stringify(Array.from(a)));")
    np.testing.assert_allclose(np.asarray(result).reshape(field.shape),field,atol=1e-9)
    np.testing.assert_allclose(frequency_rate([0,200,4000,22050]),[.15,.15,.65,.65])
    empty=np.zeros_like(field)
    RipplesVisual.stamp_frequency(empty,dict(e,features={'mic_level_0':.9}))
    assert not empty.any()


def test_sleep_mean_frequency_modulates_only_sound():
    raw=np.zeros((*VALID_MASK.shape,3),np.uint8)
    visual=RipplesVisual('sleep',dream_strength=0,output_limits=False)
    # The physical tower has an empty central shaft; use an occupied strip XY.
    idx=np.argmin(abs(visual.transition.height-.55))
    xy=(visual.coords[:2,idx]/31*10-5).tolist()
    events=[{'source':'maek','xy':xy,'features':{'mic_level_1':.8,'centroid_1':800}},
            {'source':'hyeol','xy':xy,'features':{'depth_4':2.,'coverage_4':1.,'ceiling_0':2.2}}]
    visual.set_memory(events)
    samples=np.array([visual.render(raw,0,dt=.1)[VALID_MASK] for _ in range(40)])
    h=visual.transition.height
    assert np.ptp(samples[:,(h>=.3625)&(h<.7375)],axis=0).max()>5
    assert not np.ptp(samples[:,(h<.3625)|(h>=.7375)],axis=0).any()
    np.testing.assert_array_equal(samples[:,:,0],samples[:,:,2])
    assert samples.max()<=255


def test_pcm_spectral_centroid_tracks_known_tones():
    t=np.arange(4410)/44100
    pcm=np.stack([.3*np.sin(2*np.pi*f*t) for f in (400,800,1600)],axis=1)
    f=pcm_features((pcm*32767).astype('<i2').tobytes())
    np.testing.assert_allclose([f[f'centroid_{i}'] for i in range(3)],[400,800,1600],atol=1)
