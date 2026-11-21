"""Explicit-device four-output player; follows the same exhibition wall clock."""
import argparse
import json
from pathlib import Path
import time

import numpy as np
from scipy.signal import resample_poly

from .schedule import ExhibitionSchedule, StateController
from .tower_audio import RATE, DURATIONS, MAX_MASTER, load_bank, make_plan, mix_block


def validate_output(device, mapping):
    if len(mapping)!=4 or any(type(x) is not int or x<0 for x in mapping) or len(set(mapping))!=4:
        raise ValueError("channel_map must contain four distinct zero-based output channels")
    if device.get("max_output_channels",0)<=max(mapping):
        raise ValueError("Selected device cannot supply all four independent outputs")


def output_channels(device, mapping, requested=None):
    """Keep the full surround stream when routing only front/back speakers."""
    validate_output(device, mapping)
    channels = max(mapping) + 1 if requested is None else requested
    if type(channels) is not int or not max(mapping) < channels <= device["max_output_channels"]:
        raise ValueError("stream_channels must include mapped channels and fit the selected device")
    return channels


def main():
    import sounddevice as sd
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",type=Path,default=Path("configs/audio.m30.json"))
    parser.add_argument("--bank",type=Path,default=Path("exhibition-server/static/tower-audio"))
    parser.add_argument("--device",help="PortAudio device ID or unique device name; never inferred")
    parser.add_argument("--list-devices",action="store_true")
    parser.add_argument("--check-output",action="store_true",help="open a one-second silent callback stream, without loading or playing the bank")
    parser.add_argument("--allow-demo",action="store_true",help="explicitly permit synthetic Sleep fixture on hardware")
    parser.add_argument("--state",choices=["activate","sleep"],help="manual audition; otherwise exhibition wall clock")
    args=parser.parse_args()
    if args.list_devices:
        print(sd.query_devices())
        return
    cfg=json.loads(args.config.read_text(encoding="utf-8-sig"))
    device=args.device if args.device is not None else cfg.get("device")
    if device is None:
        parser.error("No output configured. List devices and explicitly assign a verified 4+ output interface.")
    if isinstance(device,str) and device.isdigit():
        device=int(device)
    mapping=cfg["channel_map"]
    channels=output_channels(sd.query_devices(device,"output"),mapping,cfg.get("stream_channels"))
    output_rate=int(cfg.get("sample_rate",48000))
    if output_rate not in (24000,44100,48000):
        parser.error("output sample_rate must be 24000, 44100 or 48000")
    sd.check_output_settings(device=device,channels=channels,dtype="float32",samplerate=output_rate)
    if args.check_output:
        statuses=[]
        callbacks=0
        def silence(outdata,frames,timing,status):
            nonlocal callbacks
            outdata.fill(0)
            callbacks+=1
            if status:
                statuses.append(str(status))
        with sd.OutputStream(device=device,samplerate=output_rate,channels=channels,dtype="float32",
                             blocksize=1024,latency="high",callback=silence) as stream:
            sd.sleep(1000)
            if not stream.active or not callbacks or statuses:
                raise RuntimeError(f"Silent stream failed: callbacks={callbacks}, statuses={statuses}")
        print(f"Silent stream OK: {output_rate} Hz, {channels} channels, map {mapping}. Physical jack routing is not verified.")
        return
    manifest,samples=load_bank(args.bank)
    if any(b["source"]["kind"]=="synthetic_maek" for b in manifest["bank"]) and not args.allow_demo:
        parser.error("Sleep bank is a synthetic example; use real recordings or explicitly pass --allow-demo")
    master=float(cfg.get("master",.22))
    if not 0<=master<=MAX_MASTER:
        parser.error("master must be between 0 and 0.30")
    schedule=ExhibitionSchedule(manifest["schedule"])
    controller=StateController(schedule,mode=cfg.get("state_mode","scheduled"),
        state_file=Path(cfg["state_file"]) if cfg.get("state_file") else None)
    job=None
    stopping=None
    underruns=0
    callback_job=None
    next_frame=0
    played=0

    def callback(outdata,frames,timing,status):
        nonlocal underruns, callback_job, next_frame, played
        outdata.fill(0)
        if status:
            underruns+=1
        current=job
        if current is None:
            return
        events,bank_samples,base,epoch,state=current
        if current is not callback_job:
            callback_job=current
            next_frame=base+round((time.monotonic()-epoch)*output_rate)
            played=0
        frame=next_frame
        next_frame+=frames
        elapsed=played/output_rate
        played+=frames
        x=mix_block(events,bank_samples,frame,frames,master=master,rate=output_rate)
        t=(frame+np.arange(frames))/output_rate
        envelope=np.minimum(1,np.maximum(0,(DURATIONS[state]-t)/.7))
        envelope*=min(1,max(0,elapsed/.7))
        if stopping is not None:
            envelope*=max(0,1-(time.monotonic()-stopping)/.5)
        outdata[:,mapping]=x*envelope[:,None]

    stream=sd.OutputStream(device=device,samplerate=output_rate,channels=channels,dtype="float32",
                           blocksize=1024,latency="high",callback=callback)
    key=None
    manual_epoch=time.monotonic()
    with stream:
        try:
            while True:
                current=controller.at()
                state=args.state or current["state"]
                if args.state or current["mode"]=="manual":
                    phase=(time.monotonic()-manual_epoch)%DURATIONS[state]
                    newkey=(state,int((time.monotonic()-manual_epoch)//DURATIONS[state]))
                else:
                    phase=current["phase_seconds"]-(schedule.active if state=="sleep" else 0)
                    newkey=(current["anchor"],current["cycle_index"],state)
                if newkey!=key:
                    # No overlapping old/new layers: finish old fade at the wall-clock boundary.
                    seed=int(time.time()//1800)+(1 if state=="sleep" else 0)
                    # A prepared bank is re-read at state boundaries, outside the callback.
                    # Replace completed bank directories between sessions, never partial files.
                    fresh,fresh_samples=load_bank(args.bank)
                    if any(b["source"]["kind"]=="synthetic_maek" for b in fresh["bank"]) and not args.allow_demo:
                        raise ValueError("replacement bank contains unapproved synthetic Sleep")
                    manifest=fresh
                    samples={name:resample_poly(x,output_rate,RATE).astype(np.float32) if output_rate!=RATE else x
                             for name,x in fresh_samples.items()}
                    job=(make_plan(manifest["bank"],state,seed),samples,round(phase*output_rate),time.monotonic(),state)
                    key=newkey
                    print(f"{state}: {phase:.1f}s; outputs {mapping}; master {master:.2f}",flush=True)
                elif current["mode"]=="scheduled" and not args.state:
                    # Correct wall-clock adjustments without accumulating callback timing drift.
                    predicted=job[2]/output_rate+time.monotonic()-job[3]
                    if abs(predicted-phase)>1:
                        job=(job[0],job[1],round(phase*output_rate),time.monotonic(),state)
                time.sleep(.1)
        except KeyboardInterrupt:
            stopping=time.monotonic()
            time.sleep(.55)
    print(f"Audio stopped. Callback status events: {underruns}")


if __name__=="__main__":
    main()
