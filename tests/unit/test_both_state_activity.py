import numpy as np
import pytest
from ripples.visualization import RipplesVisual
from ripples.hardware.transport import VALID_MASK

@pytest.mark.parametrize("state", ["activate", "sleep"])
def test_firing_in_both_states_is_white(state):
    v=RipplesVisual(state,output_limits=False)
    raw=np.zeros((*VALID_MASK.shape,3),np.uint8)
    assert not v.render(raw,0,dt=.1).any()
    raw[VALID_MASK]=[0,160,255]
    result=v.render(raw,8460,dt=.1)[VALID_MASK]
    h=v.transition.height
    assert result.any()
    np.testing.assert_array_equal(result[:,0],result[:,1])
    np.testing.assert_array_equal(result[:,1],result[:,2])
    after=v.render(raw*0,0,dt=.1)[VALID_MASK]
    assert after.max()<result.max() and after.any()
