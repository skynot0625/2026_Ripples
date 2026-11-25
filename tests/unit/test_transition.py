import json
from pathlib import Path
import shutil
import subprocess

import numpy as np
import pytest

from ripples.transition import StateTransition, composite, limit_rgb, DURATION, PEAK, MEAN, pulse_field, bezier_ease_in, base_frame


def test_direction_and_both_state_changes():
    h = np.array([0., .5, 1.])
    dark = np.zeros((3,3))
    up = composite(dark, dark, h, .2, "up_flat")
    down = composite(dark, dark, h, .2, "down")
    assert up[0].max() > 0 and not up[-1].any()
    np.testing.assert_allclose(up, down[::-1])
    t = StateTransition(h)
    assert not t.render(dark, "activate", .1).any()  # no startup flash
    t.render(dark, "sleep", .1)
    assert t.render(dark, "sleep", .1).any()
    for _ in range(60): t.render(dark, "sleep", .1)
    assert not t.previous.any()  # repeated same state does not retrigger
    t.render(dark, "activate", .1)
    assert t.render(dark, "activate", .1).any()


def test_limits_include_base_and_interrupted_transitions():
    h = np.linspace(0,1,100)
    weights = np.linspace(1,2,100)
    t = StateTransition(h, weights)
    for i in range(130):
        state = "activate" if (i//7)%2 else "sleep"
        out=t.render(np.full((100,3),255.),state,.125)
        assert out.max() <= PEAK
        assert np.average(out.mean(axis=-1),weights=weights) <= MEAN+1e-8
    target=np.full((100,3),20.)
    np.testing.assert_allclose(composite(target,target,h,DURATION,"down",weights),limit_rgb(target*PEAK/255.,weights))


def test_browser_python_envelope_parity():
    if not shutil.which("node"): pytest.skip("node unavailable")
    root=Path(__file__).resolve().parents[2]
    js=root/"exhibition-server/static/ripples-example.js"
    old=np.arange(15).reshape(5,3)*2.
    target=old*8
    heights=np.linspace(0,1,5); weights=np.arange(1,6); radius=np.array([0,.3,.8,.5,1.])
    for direction in ("up","down","up_flat"):
        for age in (0,.2,1.4,2.8,5):
            code=f"const t=require({json.dumps(str(js))}); console.log(JSON.stringify(Array.from(t.composite({old.ravel().tolist()},{target.ravel().tolist()},{heights.tolist()},{age},{json.dumps(direction)},{weights.tolist()},{radius.tolist()}))));"
            out=json.loads(subprocess.check_output(["node","-e",code],text=True))
            np.testing.assert_allclose(np.array(out).reshape(5,3),composite(old,target,heights,age,direction,weights,radius),atol=1e-10)


def test_physical_resampling_weights_and_budget():
    from ripples.visualization import RipplesVisual
    from ripples.hardware.rgb565 import InstallationEncoder
    from ripples.hardware.transport import VALID_MASK
    v=RipplesVisual(); encoder=InstallationEncoder()
    rgb=np.random.default_rng(44).integers(0,256,(*VALID_MASK.shape,3),dtype=np.uint8)
    physical=encoder.prepare(rgb)[encoder.mask]
    assert v.transition.weights.sum() == 10260
    np.testing.assert_allclose(np.average(rgb[VALID_MASK].mean(axis=-1),weights=v.transition.weights),physical.mean())
    rgb[VALID_MASK]=v.transition.render(rgb[VALID_MASK],"activate",.1)
    assert encoder.prepare(rgb)[encoder.mask].mean() <= MEAN


def test_bloom_starts_central_widens_and_strengthens_upward():
    h=np.array([0.,0.,.5,.5,1.,1.]); r=np.array([0.,.8,0.,.8,0.,.8])
    peaks=pulse_field(h,1.4*bezier_ease_in(h,True)+.2*r+.2,"up",r)
    assert peaks[0] > 0 and peaks[1] == 0
    assert peaks[2] > peaks[0] and peaks[4] > peaks[2]
    assert peaks[5] > 0 and peaks[3] == 0
    early=pulse_field(h,.2,"up",r)
    assert early[0] > 0 and not early[1:].any()
    # Equal height steps take less time near the top: upward acceleration.
    arrival=1.4*bezier_ease_in(np.array([0.,.25,.5,.75,1.]),True)
    assert np.all(np.diff(np.diff(arrival)) < 0)
    assert not pulse_field(h,5.,"up",r).any()


def test_broad_start_keeps_width_until_midpoint_then_flares():
    h=np.array([0.,.25,.5,.7,1.])
    r=np.full_like(h,.45)
    peak=pulse_field(h,1.4*bezier_ease_in(h,True)+.2*r+.2,"up",r)
    assert peak[0] > .1  # wide lower region is visible, not only the innermost strip
    r[:]=.82
    outside=pulse_field(h,1.4*bezier_ease_in(h,True)+.2*r+.2,"up",r)
    assert not outside[:3].any()
    assert outside[3] > 0 and outside[4] > outside[3]


def test_bezier_slow_start_accelerates_and_inverse_matches():
    times=np.linspace(0,1,101)
    progress=bezier_ease_in(times)
    assert progress[0]==0 and progress[-1]==1
    assert np.all(np.diff(progress)>0)
    assert progress[25]<.1 and .30<progress[50]<.33
    assert np.all(np.diff(np.diff(progress))>0)
    np.testing.assert_allclose(bezier_ease_in(progress,True)[1:-1],times[1:-1],atol=.0003)


@pytest.mark.parametrize('first,second',[('activate','sleep'),('sleep','activate')])
def test_real_renderer_transition_crosses_both_layer_gaps(first,second):
    from ripples.visualization import RipplesVisual
    from ripples.hardware.transport import VALID_MASK
    v=RipplesVisual(first)
    raw=np.zeros((*VALID_MASK.shape,3),np.uint8)
    assert not v.render(raw,0,dt=.125).any()
    v.set_state(second)
    frames=np.array([v.render(raw,0,dt=.125)[VALID_MASK] for _ in range(44)])
    h,r=v.transition.height,v.transition.radius
    # Both separators used to suppress the pulse by 90%, making it look broken.
    for lo,hi in ((.34,.38),(.70,.74)):
        region=(h>lo)&(h<hi)&(r<.45)
        assert region.any()
        assert frames[:,region].max()>15
    lower=(h<.2)&(r<.45);upper=(h>.8)&(r<.45)
    assert frames[:,lower].mean(axis=(1,2)).argmax()<frames[:,upper].mean(axis=(1,2)).argmax()
    assert not frames[-1].any()
    assert frames.max()<=PEAK
    assert np.all(np.average(frames.mean(axis=-1),axis=1,weights=v.transition.weights)<=MEAN)


def test_base_mask_preserves_steady_frame_and_browser_parity():
    h=np.array([.1,.36,.72,.95]);mask=np.array([1.,.1,.1,1.]);weights=np.array([1.,2.,3.,4.])
    rgb=np.random.default_rng(19).integers(0,256,(4,3))
    expected=(limit_rgb(rgb*PEAK/255.,weights).astype(np.uint8)*mask[:,None]).astype(np.uint8)
    np.testing.assert_array_equal(base_frame(rgb,weights,mask),expected)
    if not shutil.which('node'):pytest.skip('node unavailable')
    js=Path(__file__).resolve().parents[2]/'exhibition-server/static/ripples-example.js'
    old=expected.astype(float);radius=np.zeros(4)
    for age in (0.,.7,1.4,5.):
        code=f"const t=require({json.dumps(str(js))});console.log(JSON.stringify(Array.from(t.composite({old.ravel().tolist()},{rgb.ravel().tolist()},{h.tolist()},{age},'up',{weights.tolist()},{radius.tolist()},{mask.tolist()}))));"
        out=np.array(json.loads(subprocess.check_output(['node','-e',code],text=True))).reshape(4,3)
        np.testing.assert_allclose(out,composite(old,rgb,h,age,'up',weights,radius,mask),atol=1e-10)
