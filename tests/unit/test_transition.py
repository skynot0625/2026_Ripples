import json
from pathlib import Path
import shutil
import subprocess

import numpy as np
import pytest

from ripples.transition import StateTransition, composite, limit_rgb, DURATION, PEAK, MEAN


def test_direction_and_both_state_changes():
    h = np.array([0., .5, 1.])
    dark = np.zeros((3,3))
    up = composite(dark, dark, h, .2, "up")
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
    heights=np.linspace(0,1,5); weights=np.arange(1,6)
    for direction in ("up","down"):
        for age in (0,.2,1.4,2.8,5):
            code=f"const t=require({json.dumps(str(js))}); console.log(JSON.stringify(Array.from(t.composite({old.ravel().tolist()},{target.ravel().tolist()},{heights.tolist()},{age},{json.dumps(direction)},{weights.tolist()}))));"
            out=json.loads(subprocess.check_output(["node","-e",code],text=True))
            np.testing.assert_allclose(np.array(out).reshape(5,3),composite(old,target,heights,age,direction,weights),atol=1e-10)


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
