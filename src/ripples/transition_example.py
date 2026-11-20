"""Compare expanding and planar upward transitions on the same recording; no USB."""
import gzip
import json
import numpy as np
from PIL import Image, ImageDraw
from ripples.paths import ROOT
from ripples.hardware.transport import VALID_MASK
from ripples.visualization import RipplesVisual
from ripples.transition import StateTransition, PEAK, MEAN


def main():
    folder=ROOT/"exhibition-server/static"
    clips={s: np.frombuffer(gzip.decompress((folder/f"ripples-{s}.rgb.gz").read_bytes()),np.uint8).reshape(680,8460,3) for s in ("activate","sleep")}
    v=RipplesVisual()
    xyz=v.waves.xyz[VALID_MASK]; xyz=(xyz-xyz.min(axis=0))/np.ptp(xyz,axis=0)
    xy=np.column_stack((24+xyz[:,0]*280+xyz[:,1]*48,415-xyz[:,2]*285-xyz[:,1]*28)).astype(int)
    order=np.argsort(xyz[:,1]); weights=v.transition.weights
    transitions=[StateTransition(xyz[:,2],weights,d,v.transition.radius) for d in ("up","up_flat")]
    images=[]; peaks=[]; means=[]
    for frame in range(180):
        t=frame/10; state="sleep" if 2<=t<10 else "activate"
        im=Image.new("RGB",(760,480),(6,9,16)); draw=ImageDraw.Draw(im)
        draw.text((18,12),"STATE TRANSITION / SAME SENSORS + 8,460 NEURONS / 10 FPS",fill=(225,232,244))
        draw.text((18,32),"Broad lower center -> flare above halfway | same RGB budgets: 96 / 18",fill=(155,171,192))
        for panel,transition in enumerate(transitions):
            rgb=transition.render(clips[state][180+frame],state,.1)
            peaks.append(int(rgb.max())); mean=float(np.average(rgb.mean(axis=-1),weights=weights)); means.append(mean)
            offset=380*panel
            draw.text((offset+20,65),"NEW: WIDE START + UPPER FLARE" if panel==0 else "BEFORE: FLAT BOTTOM -> TOP",fill=(245,215,155))
            draw.text((offset+20,85),f"{state.upper()} | {t:04.1f}s | mean {mean:.1f}/255",fill=(190,205,222))
            for i in order:
                x,y=xy[i]; draw.point((int(x)+offset,int(y)),fill=(17,23,31))
            for i in order[rgb[order].max(axis=1)>0]:
                x,y=xy[i]; color=tuple((255*(rgb[i]/255.)**.7).astype(int))
                draw.ellipse((int(x)+offset-1,int(y)-1,int(x)+offset+1,int(y)+1),fill=color)
        draw.text((18,445),"2s Activate -> Sleep | 10s Sleep -> Activate | display gain for preview only",fill=(165,185,205))
        images.append(im)
    out=ROOT/"docs/media/state_transition_wide_start.gif"
    frames=[im.convert("P",palette=Image.Palette.ADAPTIVE,colors=128) for im in images]
    frames[0].save(out,save_all=True,append_images=frames[1:],duration=100,loop=0,optimize=False)
    images[24].save(out.with_suffix(".png"))
    report={"example":True,"duration_s":18,"fps":10,"switches_s":[2,10],"directions":["up","up_flat"],"style":"wide_start_mid_flare","peak_limit":PEAK,"mean_limit":MEAN,"observed_max_channel":max(peaks),"observed_max_weighted_mean":max(means),"physical_leds":10260,"hardware_transmission":False,"current_measured":False}
    out.with_suffix(".json").write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(report),flush=True)

if __name__=="__main__": main()
