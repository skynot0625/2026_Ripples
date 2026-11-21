"""Explicit-device four-output player; follows the same exhibition wall clock."""
import argparse
import json
from pathlib import Path
import time

import numpy as np
from scipy.signal import resample_poly

from .schedule import ExhibitionSchedule, StateController
from .tower_audio import RATE, DURATIONS, MAX_MASTER, load_bank, make_plan, mix_block
from .transition_audio import make_cue, cue_block, DURATION as CUE_DURATION
from .sonification import DataSynth, read_frame
from .paths import RUNTIME_DIR


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
    checks=parser.add_mutually_exclusive_group()
    checks.add_argument("--check-output",action="store_true",help="open a one-second silent callback stream, without loading or playing the bank")
    checks.add_argument("--test-speakers",action="store_true",help="quiet identification pings on OUT 1 to 4, once, with two-second gaps")
    checks.add_argument("--test-speaker",type=int,choices=[1,2,3,4],help="quiet identification pings on just this logical speaker")
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
    if args.test_speakers or args.test_speaker is not None:
        from .speaker_test import play_speaker_test
        labels=cfg.get("speaker_labels",["OUT 1","OUT 2","OUT 3","OUT 4"])
        if len(labels)!=4:
            parser.error("speaker_labels must contain four labels")
        play_speaker_test(sd,device,output_rate,channels,mapping,labels,args.test_speaker)
        return
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
    master=float(cfg.get("master",.26))
    if not 0<=master<=MAX_MASTER:
        parser.error("master must be between 0 and 0.30")
    cues={state:make_cue(state,output_rate) for state in DURATIONS}
    cue_enabled=cfg.get('transition_cues',True)
    led_mode=cfg.get('active_source','bank')=='led'
    led_path=Path(cfg.get('led_frame_file') or RUNTIME_DIR/'led-sonification.json')
    data_synth=DataSynth(output_rate)
    led_frame=None
    last_data_report=-100.
    schedule=ExhibitionSchedule(manifest["schedule"])
    controller=StateController(schedule,mode=cfg.get("state_mode","scheduled"),
        state_file=Path(cfg["state_file"]) if cfg.get("state_file") else None)
    job=None
    stopping=None
    underruns=0
    callback_job=None
    next_frame=0
    played=0
    release=None
    release_pos=0

    def callback(outdata,frames,timing,status):
        nonlocal underruns, callback_job, next_frame, played, release, release_pos
        outdata.fill(0)
        if status:
            underruns+=1
        current=job
        if current is None:
            return
        events,bank_samples,base,epoch,state,with_cue=current
        if current is not callback_job:
            if callback_job is not None and callback_job[4]!=state:
                release=(callback_job,next_frame)
                release_pos=0
            callback_job=current
            next_frame=base+round((time.monotonic()-epoch)*output_rate)
            played=0
        frame=next_frame
        next_frame+=frames
        elapsed=played/output_rate
        played+=frames
        if led_mode and state=='activate':
            available=led_frame if not with_cue or frame/output_rate>=CUE_DURATION else None
            x=data_synth.render(available,frames)*master
        else:
            if release is None or release[0][4]!='activate':
                data_synth.reset()
            x=mix_block(events,bank_samples,frame,frames,master=master,rate=output_rate)
        if with_cue:
            x+=cue_block(cues[state],frame,frames,master)
        if release is not None:
            old,old_frame=release
            count=min(frames,round(.12*output_rate)-release_pos)
            tail=(data_synth.render(None,count)*master if led_mode and old[4]=='activate' else
                  mix_block(old[0],old[1],old_frame+release_pos,count,master=master,rate=output_rate))
            if old[5]:
                tail+=cue_block(cues[old[4]],old_frame+release_pos,count,master)
            fade=1-(release_pos+np.arange(count))/round(.12*output_rate)
            # Finish old voices before the cue; the cue continues at the matching light age.
            x[:count]=tail*fade[:,None]
            release_pos+=count
            if release_pos>=round(.12*output_rate):
                release=None
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
                if led_mode:
                    led_frame=read_frame(led_path)
                    if time.monotonic()-last_data_report>=5:
                        print('LED sound: '+('fresh displayed values' if led_frame and led_frame['state']==state else 'waiting for matching LED frame'),flush=True)
                        last_data_report=time.monotonic()
                changed=job is not None and state!=job[4]
                if changed and current['mode']=='manual' and not args.state:
                    manual_epoch=time.monotonic()
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
                    with_cue=bool(cue_enabled and changed and phase<CUE_DURATION)
                    plan=make_plan(manifest["bank"],state,seed)
                    # The cue occupies the opening five seconds by itself; no clipped sentences.
                    if with_cue:
                        plan=[event for event in plan if event['start']>=CUE_DURATION]
                    job=(plan,samples,round(phase*output_rate),time.monotonic(),state,with_cue)
                    key=newkey
                    print(f"{state}: {phase:.1f}s; outputs {mapping}; master {master:.2f}",flush=True)
                elif current["mode"]=="scheduled" and not args.state:
                    # Correct wall-clock adjustments without accumulating callback timing drift.
                    predicted=job[2]/output_rate+time.monotonic()-job[3]
                    if abs(predicted-phase)>1:
                        job=(job[0],job[1],round(phase*output_rate),time.monotonic(),state,job[5])
                time.sleep(.1)
        except KeyboardInterrupt:
            stopping=time.monotonic()
            time.sleep(.55)
    print(f"Audio stopped. Callback status events: {underruns}")


if __name__=="__main__":
    main()
