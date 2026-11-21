"""Build original demo banks, exact 20/10 minute plans and portable audio files."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

import numpy as np
import soundfile as sf

from .tower_audio import (RATE, MASTER, MAX_MASTER, PEAK_LIMIT, STEREO, DURATIONS,
                          active_bank, synthetic_maek, extract_sleep, make_plan, mix_block, procedural_source)


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


def write_sources(manifest, output):
    has_real=any(b['source']['kind']=='recording' for b in manifest['bank'])
    has_tts=any((p.get('origin') or {}).get('type')=='system_tts' for b in manifest['bank'] for p in b['source'].get('parts',[]))
    lines=['# 음원 출처 명세','',
        '이 예시는 외부 음원 사이트의 녹음 샘플을 사용하지 않았습니다. Active는 코드 합성, Sleep 환경음도 코드 합성 원음입니다. 목소리는 **Windows 기본 한국어 TTS**로 만든 예시로 실제 관객 녹음·특정인의 복제 음성이 아닙니다.',
        '', '효과를 적용한 WAV의 전체 SHA256과 원본 구간별 SHA256·프레임은 [manifest.json](../../exhibition-server/static/tower-audio/manifest.json)에 기록합니다. 아래 발췌 시간은 원본 파일의 초 단위입니다.', '',
        '| 샘플 | 출처 | 원본 / 구간 | 처리 |', '|---|---|---|---|']
    for b in manifest['bank']:
        s=b['source'];parts=s.get('parts',[]);origin=s.get('origin') or (parts[0].get('origin') if parts else {}) or {}
        typ=('직접 코드 합성' if s['kind']=='original_procedural' else
             'Microsoft Heami Desktop / Windows SAPI 합성 음성' if origin.get('type')=='system_tts' else
             '합성 맥 환경음' if s['kind']=='synthetic_maek' else '제공된 녹음')
        location=('active_bank · seed 214 · dream-work-v3' if not parts else '<br>'.join(
            f"{p['relative_file']} · MIC {s['mic_channel']} · {p['file_frame']/s['sample_rate']:.2f}–{(p['file_frame']+p['frames'])/s['sample_rate']:.2f}s" for p in parts))
        effect=('음높이 유지 · 0.96배 속도 · 작은 2.4초 잔향' if s.get('category')=='speech_candidate' else
                s.get('processing',''))
        if s.get('category')=='speech_candidate': effect='현재 재생 제외 · 보관한 이전 음성 예시; '+effect
        lines.append(f"| [{b['id']}](../../exhibition-server/static/tower-audio/{b['file']}) | {typ} | {location} | {effect} |")
    lines += ['', '## 목소리 예시의 원본', '',
        '[voice.wav](synthetic_source/voice.wav) / [생성 정보와 문안](synthetic_source/voice.json). Microsoft Windows의 설치된 `Microsoft Heami Desktop` 음성, System.Speech/SAPI, Rate −1을 사용했습니다. 이 프로젝트 예시를 위해 작성한 문안이며 현장에서 들은 발화가 아닙니다.', '',
        '> 잠시 걸음을 멈추고, 이 공간의 소리를 들어보세요.\n> 지나간 작은 소리들이, 천천히 기억 속에 머뭅니다.\n> 우리는 같은 공간에서, 서로 다른 소리를 듣고 있습니다.', '',
        '[원음 → 효과 비교](voice_dry_then_dream.wav)는 첫 문장의 마른 소리, 1초 여백, 같은 소리의 Sleep 효과 순서입니다. 비교용으로 두 버전의 음량을 낮춰 맞췄습니다. 원본 TTS 소스 파일 자체는 출력용 음량을 적용하기 전입니다.', '',
        '## 최종 재생 파일', '',
        '20분/10분 파일과 90초 발췌는 위 샘플을 manifest의 `plans`대로 섞은 것입니다. 4채널 FLAC은 독립 출력, MP3는 스테레오 합본입니다. 원음·발췌·효과·최종 믹스를 구분하여 추적할 수 있습니다.', '',
        '## 코드와 도구의 출처', '',
        '- 작업음과 환경음 생성: 이 저장소의 `src/ripples/tower_audio.py`.',
        '- 목소리 구간 검출: [WebRTC VAD Python 구현](https://github.com/daanzu/py-webrtcvad-wheels), `webrtcvad-wheels 2.0.14.post1`. 음원 제공자가 아니라 검출 도구입니다. 문장 내용·화자 신원을 인식하지 않습니다.',
        '- 자연스러운 속도 조절: [FFmpeg atempo](https://ffmpeg.org/ffmpeg-filters.html#atempo). 음정 이동·역재생·음성 복제는 적용하지 않습니다.',
        '- 실제 맥 원음이 들어오면 `recording` 출처로 별도 기록합니다. 원본의 장치·세션·마이크·프레임·해시를 유지하며 자동 자막이나 추정 화자 이름을 만들지 않습니다.', '',
        '합성기·라이브러리·Windows 음성의 출처 표시는 해당 도구 자체를 이 프로젝트가 제작했다는 뜻이 아닙니다.']
    if has_real:
        lines[2]='Active는 코드 합성입니다. Sleep에는 제공된 녹음에서 선택한 소리가 포함됩니다. 각 샘플의 실제 녹음·합성 구분과 원본 파일·발췌 구간·효과는 아래 표 및 manifest를 따릅니다.'
    if not has_tts:
        del lines[lines.index('## 목소리 예시의 원본'):lines.index('## 최종 재생 파일')]
    lines[2]+=' 현재 Active/Sleep 재생 계획에서는 말소리 후보를 제외했습니다. 아래 음성 항목은 출처를 보존한 이전 예시입니다.'
    (output/'SOURCES.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    with (output/'SOURCES.md').open('a',encoding='utf-8') as f:
        f.write('\n## 상태 전환 큐\n\n`src/ripples/transition_audio.py::make_cue`가 직접 합성합니다. 외부 음원·녹음·TTS를 사용하지 않습니다. 기존 LED `pulse_field`의 5초 상승·감쇠 곡선과 부드러운 배음으로 만들며, [큐 파일과 SHA256](../../exhibition-server/static/tower-audio/cues.json)에 출처를 기록합니다. [두 방향 미리듣기](transitions/sleep_then_activate.wav)는 Sleep 진입, 1초 여백, Activate 진입 순서입니다.\n')


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
            "sha256":hashlib.sha256(path.read_bytes()).hexdigest(),
            "origin":{"type":"procedural_synthesis","generator":"src/ripples/tower_audio.py::synthetic_maek",
                      "seed":451,"external_audio_samples":False,"speech_content":False}},indent=2),encoding="utf-8")
    bank,samples=[],{}
    designs=[{"id":name,"samples":x,"state":"activate","source":procedural_source(name)} for name,x in active_bank()]
    designs += [dict(x,state="sleep") for x in extract_sleep(source,max_seconds=args.max_source_seconds)]
    if not any(x["state"]=="sleep" for x in designs):
        raise ValueError("No usable recording segment; RMS-only/silent input cannot supply Sleep audio")
    for item in designs:
        x=item.pop("samples")
        file=f"bank/{item['id']}.wav"
        sf.write(args.bank/file,x,RATE,subtype="PCM_16")
        decoded,_=sf.read(args.bank/file,dtype="float32")
        samples[item["id"]]=decoded
        bank.append(dict(item,file=file,duration=len(x)/RATE,sha256=hashlib.sha256((args.bank/file).read_bytes()).hexdigest()))
    manifest={"schema":"ripples.tower-audio.v1","sample_rate":RATE,"channels":4,"max_voices":2,
              "master":MASTER,"max_master":MAX_MASTER,"peak_limit":PEAK_LIMIT,"stereo_matrix":STEREO.tolist(),
              "active_profile":"dream-work-v3","speech_playback":False,
              "sleep_source":"synthetic_demo" if all(b['source']['kind']=='synthetic_maek' for b in bank if b['state']=='sleep') else
                             "mixed_recordings" if any(b['source']['kind']=='synthetic_maek' for b in bank if b['state']=='sleep') else "supplied_recordings",
              "schedule":{"timezone":"Asia/Seoul","anchor":"11:00","activate_minutes":20,"sleep_minutes":10},
              "bank":bank,"plans":{s:make_plan(bank,s,20261121+i) for i,s in enumerate(DURATIONS)}}
    (args.bank/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    write_sources(manifest,args.output)
    from .transition_audio import write_examples
    write_examples(args.bank,args.output/'transitions')
    if not args.bank_only:
        render(args.bank,manifest,samples,args.output)
    print(f"Built {len(bank)} sounds: {args.bank}",flush=True)


if __name__=="__main__":
    main()
