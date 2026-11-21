"""Build original demo banks, exact 20/10 minute plans and portable audio files."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

import numpy as np
import soundfile as sf

from .tower_audio import (RATE, MASTER, MAX_MASTER, PEAK_LIMIT, STEREO, DURATIONS,
                          active_bank, synthetic_maek, extract_sleep, make_plan, mix_block)


def render(root, manifest, samples, output):
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    output.mkdir(parents=True, exist_ok=True)
    report = {}
    for state, duration in DURATIONS.items():
        plan = manifest["plans"][state]
        flac = output/f"{state}_{duration//60}min_4ch.flac"
        mp3 = output/f"{state}_{duration//60}min_stereo.mp3"
        cmd = [ffmpeg,"-hide_banner","-loglevel","error","-y","-f","f32le","-ar",str(RATE),
               "-ac","2","-i","pipe:0","-c:a","libmp3lame","-b:a","96k",str(mp3)]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        peak = np.zeros(4)
        squares = np.zeros(4)
        with sf.SoundFile(flac,"w",samplerate=RATE,channels=4,format="FLAC",subtype="PCM_16") as f:
            for frame in range(0,duration*RATE,RATE):
                block = mix_block(plan,samples,frame,RATE)
                f.write(block)
                peak = np.maximum(peak,np.max(np.abs(block),axis=0))
                squares += np.sum(block.astype(float)**2,axis=0)
                proc.stdin.write(np.clip(block@STEREO,-PEAK_LIMIT,PEAK_LIMIT).astype("<f4").tobytes())
        proc.stdin.close()
        if proc.wait():
            raise RuntimeError("MP3 encoding failed")
        preview = output/f"{state}_90s_preview.mp3"
        subprocess.run([ffmpeg,"-hide_banner","-loglevel","error","-y","-i",str(mp3),
                        "-t","90","-af","afade=t=out:st=87:d=3","-c:a","libmp3lame","-b:a","128k",str(preview)],check=True)
        marks = sorted([(e["start"],1) for e in plan]+[(e["start"]+e["duration"],-1) for e in plan])
        voices=maximum=0
        for _, delta in marks:
            voices+=delta
            maximum=max(maximum,voices)
        report[state]={"duration_seconds":duration,"sample_rate":RATE,"channels":4,
                       "events":len(plan),"max_simultaneous_voices":maximum,
                       "speaker_events":[sum(e["speaker"]==c for e in plan) for c in range(4)],
                       "peak_per_channel":peak.tolist(),"rms_per_channel":np.sqrt(squares/(duration*RATE)).tolist(),
                       "flac_bytes":flac.stat().st_size,"mp3_bytes":mp3.stat().st_size}
        print(state,report[state],flush=True)
    (output/"validation.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    fig,axes=plt.subplots(2,1,figsize=(12,5.2),layout="constrained")
    for ax,state in zip(axes,DURATIONS):
        for e in manifest["plans"][state]:
            ax.broken_barh([(e["start"]/60,e["duration"]/60)],(e["speaker"]-.25,.5),
                           facecolors="#53b6b0" if state=="activate" else "#b6a0dc")
        ax.set(xlim=(0,DURATIONS[state]/60),yticks=range(4),yticklabels=["OUT 1","OUT 2","OUT 3","OUT 4"],
               xlabel="Minutes",title=f"{state.upper()} — maximum 2 voices across 4 outputs")
        ax.grid(axis="x",alpha=.15)
    fig.savefig(output/"speaker_timeline.png",dpi=150)
    plt.close(fig)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bank",type=Path,default=Path("exhibition-server/static/tower-audio"))
    parser.add_argument("--output",type=Path,default=Path("data_led_example/tower_audio"))
    parser.add_argument("--recordings",type=Path,help="Real Maek WAV packets + metadata; omit for explicitly synthetic demo")
    parser.add_argument("--max-source-seconds",type=float,default=1800)
    parser.add_argument("--bank-only",action="store_true")
    args=parser.parse_args()
    if args.max_source_seconds<=0:
        parser.error("source duration must be positive")
    args.bank.mkdir(parents=True,exist_ok=True)
    (args.bank/"bank").mkdir(exist_ok=True)
    args.output.mkdir(parents=True,exist_ok=True)
    source=args.recordings
    if source is None:
        source=args.output/"synthetic_source"
        source.mkdir(exist_ok=True)
        raw,sr=synthetic_maek()
        path=source/"audio.wav"
        sf.write(path,raw,sr,subtype="PCM_16")
        (source/"metadata.json").write_text(json.dumps({"device_id":"SYNTHETIC-MAEK","session_id":"tower-audio-demo-v1",
            "chunk_id":"synthetic-48s","start_frame":0,"frames":len(raw),"demo":True,
            "sha256":hashlib.sha256(path.read_bytes()).hexdigest()},indent=2),encoding="utf-8")
    bank,samples=[],{}
    designs=[{"id":name,"samples":x,"state":"activate","source":{"kind":"original_procedural"}} for name,x in active_bank()]
    designs += [dict(x,state="sleep") for x in extract_sleep(source,max_seconds=args.max_source_seconds)]
    if not any(x["state"]=="sleep" for x in designs):
        raise ValueError("No usable recording segment; RMS-only/silent input cannot supply Sleep audio")
    for item in designs:
        x=item.pop("samples")
        file=f"bank/{item['id']}.wav"
        sf.write(args.bank/file,x,RATE,subtype="PCM_16")
        decoded,_=sf.read(args.bank/file,dtype="float32")
        samples[item["id"]]=decoded
        bank.append(dict(item,file=file,duration=len(x)/RATE))
    manifest={"schema":"ripples.tower-audio.v1","sample_rate":RATE,"channels":4,"max_voices":2,
              "master":MASTER,"max_master":MAX_MASTER,"peak_limit":PEAK_LIMIT,"stereo_matrix":STEREO.tolist(),
              "sleep_source":"synthetic_demo" if args.recordings is None else "supplied_recordings",
              "schedule":{"timezone":"Asia/Seoul","anchor":"11:00","activate_minutes":20,"sleep_minutes":10},
              "bank":bank,"plans":{s:make_plan(bank,s,20261121+i) for i,s in enumerate(DURATIONS)}}
    (args.bank/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    if not args.bank_only:
        render(args.bank,manifest,samples,args.output)
    print(f"Built {len(bank)} sounds: {args.bank}",flush=True)


if __name__=="__main__":
    main()
