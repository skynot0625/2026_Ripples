"""Bounded, two-voice four-channel sound design; no audio hardware side effects."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from scipy import signal

RATE = 24000
MASTER = 0.22
MAX_MASTER = 0.30
PEAK_LIMIT = 0.16
DURATIONS = {"activate": 1200, "sleep": 600}
STEREO = np.array([[.65, .15], [.15, .65], [.55, .30], [.30, .55]], np.float32)


def fade(x, seconds=.16, rate=RATE):
    x = np.asarray(x, dtype=np.float32).copy()
    n = min(len(x)//2, max(2, round(seconds*rate)))
    w = np.sin(np.linspace(0, np.pi/2, n))**2
    x[:n] *= w
    x[-n:] *= w[::-1]
    return x


def finish(x, *, rms=.105, rate=RATE, cutoff=4300):
    """No DC/infra-bass, soft edges, bounded source peak. Digital units only."""
    x = np.nan_to_num(np.asarray(x, dtype=np.float64))
    sos = signal.butter(3, [65, min(cutoff, rate*.44)], btype="bandpass", fs=rate, output="sos")
    x = signal.sosfilt(sos, x)
    power = np.sqrt(np.mean(x*x))
    if power < 1e-8:
        return np.zeros(len(x), np.float32)
    x *= min(rms/power, .48/max(np.max(np.abs(x)), 1e-8))
    return fade(x, .20, rate)


def active_bank(seed=214):
    """Original procedural sounds, not borrowed samples or speech."""
    rng = np.random.default_rng(seed)
    names = ["servo_breath", "soft_relay", "calculation", "air_valve",
             "metabolic_pulse", "telemetry", "paper_motor", "liquid_circuit",
             "rotor_rest", "memory_keys", "orbital_hum", "tiny_gears"]
    result = []
    for k, name in enumerate(names):
        length = 3.3 + (k % 4)*.75
        t = np.arange(round(RATE*length))/RATE
        noise = rng.standard_normal(len(t))
        body = .10*np.sin(2*np.pi*(105+13*k)*t + .65*np.sin(2*np.pi*.38*t))
        if k % 4 == 0:
            x = body + .18*np.sin(2*np.pi*(210*t+26*np.sin(t*1.3))) * np.sin(np.pi*t/length)**2
            x += .03*noise
        elif k % 4 == 1:
            x = body*.12
            for onset in np.cumsum(rng.uniform(.22, .75, 8)):
                u = np.maximum(0, t-onset)
                env = (t >= onset)*(1-np.exp(-u/.012))*np.exp(-u/.095)
                x += env*(.38*np.sin(2*np.pi*(410+31*k)*u) + .12*noise)
        elif k % 4 == 2:
            x = body*.5
            for j, onset in enumerate(np.arange(.25, length-.25, .53)):
                u = t-onset
                env = np.exp(-((u-.12)/.105)**2)
                x += .21*env*np.sin(2*np.pi*(310+90*((j+k)%4))*u)
        else:
            x = .12*noise*(.22+.78*np.sin(np.pi*t/length)**2)
            x += .19*np.sin(2*np.pi*(160*t+40*np.sin(t*.9))) * (1-np.cos(2*np.pi*t/length))*.5
        result.append((name, finish(x)))
    return result


def synthetic_maek(seed=451, seconds=48):
    """Explicitly synthetic three-microphone fixture, 44.1 kHz PCM source."""
    rng = np.random.default_rng(seed)
    sr = 44100
    t = np.arange(sr*seconds)/sr
    x = rng.normal(0, .0025, (len(t), 3))
    for ch in range(3):
        x[:, ch] += .006*np.sin(2*np.pi*(92+ch*11)*t)
    for j, onset in enumerate(np.arange(1.2, seconds-4, 3.7)):
        ch = j % 3
        u = t-onset
        env = np.exp(-((u-.8)/(.32+.12*(j%4)))**2)
        tone = np.sin(2*np.pi*((170+j*27)*u+13*np.sin(2*u)))
        texture = rng.standard_normal(len(t))*.3
        for c in range(3):
            x[:, c] += (.14 if c == ch else .035)*env*(tone+texture)
    return x.astype(np.float32), sr


def recording_runs(folder, *, max_seconds=1800, slab_seconds=24):
    """Join only verified contiguous packets. Never bridge sessions or missing frames.

    Each WAV has a sibling metadata.json or <stem>.json. Unknown metadata is an
    isolated file. Decode bounded slabs, yielding provenance with original frames.
    """
    import soundfile as sf
    entries = []
    for path in Path(folder).rglob("*.wav"):
        meta_path = path.with_suffix(".json")
        if not meta_path.exists():
            meta_path = path.parent/"metadata.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8-sig")) if meta_path.exists() else {}
        info = sf.info(path)
        if info.channels not in (1, 2, 3) or info.samplerate < 8000:
            continue
        if "frames" in meta and int(meta["frames"]) != info.frames:
            raise ValueError(f"frame count mismatch: {path}")
        if meta.get("sha256") and hashlib.sha256(path.read_bytes()).hexdigest() != meta["sha256"]:
            raise ValueError(f"SHA256 mismatch: {path}")
        start = meta.get("start_frame")
        key = (str(meta.get("device_id", "")), str(meta.get("session_id", "")),
               info.samplerate, info.channels)
        entries.append((key, int(start) if start is not None else -1, path, meta, info))
    entries.sort(key=lambda e: (e[0], e[1], str(e[2])))
    pending, provenance, prev_key, expected, used = [], [], None, None, 0.
    for key, start, path, meta, info in entries:
        joinable = bool(key[0] and key[1] and start >= 0)
        if pending and (not joinable or key != prev_key or start != expected):
            yield np.concatenate(pending), info_rate, provenance
            pending, provenance = [], []
        with sf.SoundFile(path) as f:
            while f.tell() < len(f) and used < max_seconds:
                filled = sum(len(p) for p in pending)
                count = min(round(slab_seconds*f.samplerate)-filled,
                            int((max_seconds-used)*f.samplerate), len(f)-f.tell())
                if count <= 0:
                    break
                offset = f.tell()
                block = f.read(count, dtype="float32", always_2d=True)
                pending.append(block)
                provenance.append({"file": path.name, "relative_file": str(path.relative_to(folder)).replace("\\", "/"),
                                   "device_id": key[0], "session_id": key[1], "file_frame": offset,
                                   "session_frame": start+offset if start >= 0 else None,
                                   "frames": len(block), "demo": meta.get("demo") is True})
                info_rate = f.samplerate
                used += len(block)/f.samplerate
                if filled+len(block) >= round(slab_seconds*f.samplerate):
                    yield np.concatenate(pending), info_rate, provenance
                    pending, provenance = [], []
        prev_key, expected = key, start+info.frames if joinable else None
        if not joinable and pending:
            yield np.concatenate(pending), info_rate, provenance
            pending, provenance = [], []
        if used >= max_seconds:
            break
    if pending:
        yield np.concatenate(pending), info_rate, provenance


def extract_sleep(folder, *, limit=12, max_seconds=1800):
    """Rank local energy contrast, onset change and spectral texture; suppress overlap.

    This is acoustic salience, not semantic classification. The selected mic is
    strongest in the segment; no phase cancellation from summing three microphones.
    """
    candidates = []
    for run_id, (raw, sr, provenance) in enumerate(recording_runs(folder, max_seconds=max_seconds)):
        if len(raw) < sr*.65:
            continue
        hop = max(1, sr//10)
        n = len(raw)//hop
        rms = np.sqrt(np.mean(raw[:n*hop].reshape(n, hop, -1)**2, axis=1))
        baseline = max(float(np.median(rms)), .001)
        for h in range(0, max(1, n-6), 5):
            a = h*hop
            b = min(len(raw), a+round(sr*3.2))
            if b-a < .65*sr:
                continue
            piece = raw[a:b]
            ch = int(np.argmax(np.mean(piece**2, axis=0)))
            mono = piece[:, ch]
            energy = float(np.sqrt(np.mean(mono**2)))
            if energy < .003 or np.mean(np.abs(mono) > .985) > .01:
                continue
            change = float(np.max(np.abs(np.diff(rms[h:min(n,h+32), ch])))) if n-h>1 else 0
            spec = np.abs(np.fft.rfft(mono[::4]))
            spec /= max(float(spec.sum()), 1e-12)
            entropy = -float(np.sum(spec*np.log(spec+1e-12)))/np.log(max(2,len(spec)))
            score = np.log1p(energy/baseline)+2*np.log1p(change/baseline)+.3*entropy
            candidates.append((score, run_id, a, b, ch, sr, mono.copy(), provenance))
        # A long input must not retain thousands of full sample copies.
        candidates = sorted(candidates, key=lambda c: c[0], reverse=True)[:max(96,limit*12)]
    selected, bank = [], []
    for score, run, a, b, ch, sr, mono, provenance in sorted(candidates, key=lambda c:c[0], reverse=True):
        if any(run == r and a < end and b > begin for r,begin,end in selected):
            continue
        selected.append((run,a,b))
        speed = (.72, .82, .90)[len(bank)%3]
        slowed = signal.resample(mono, round(len(mono)/sr/speed*RATE))
        slowed = finish(slowed, rms=.095, cutoff=2600)
        wet = np.pad(slowed, (0, round(.9*RATE)))
        for delay, gain in ((.19,.18),(.37,.11),(.61,.065)):
            off = round(delay*RATE)
            wet[off:off+len(slowed)] += slowed*gain
        wet = finish(wet, rms=.095, cutoff=2600)
        # Trim provenance to exactly the selected interval, not its whole slab.
        parts, cursor = [], 0
        for p in provenance:
            lo, hi = max(a,cursor), min(b,cursor+p["frames"])
            if lo < hi:
                delta = lo-cursor
                parts.append(dict(p, file_frame=p["file_frame"]+delta, frames=hi-lo,
                                  session_frame=None if p["session_frame"] is None else p["session_frame"]+delta))
            cursor += p["frames"]
        bank.append({"id": f"sleep_{len(bank):02d}", "samples": wet,
                     "source": {"kind": "synthetic_maek" if all(p["demo"] for p in parts) else "recording",
                                "mic_channel": ch+1, "sample_rate": sr, "parts": parts,
                                "score": round(float(score),4), "speed": speed,
                                "processing": "slow playback + 65–2600 Hz + quiet diffuse echoes"}})
        if len(bank) == limit:
            break
    return bank


def make_plan(bank, state, seed=20261121):
    """Never more than two simultaneous complete samples, including their tails."""
    if state not in DURATIONS:
        raise ValueError("unknown state")
    items = [x for x in bank if x["state"] == state]
    if not items:
        return []
    rng = np.random.default_rng(seed)
    end, cursor, events, last, counts = DURATIONS[state], 1., [], {}, [0]*4
    busy = [0.]*4
    while cursor < end-2:
        ongoing = sorted(e["start"]+e["duration"] for e in events if e["start"]+e["duration"] > cursor)
        if len(ongoing) >= 2:
            cursor = ongoing[0]+rng.uniform(.7,2.)
        available = [x for x in items if cursor-last.get(x["id"], -1000) >= 16]
        if not available:
            cursor += 2
            continue
        item = available[int(rng.integers(len(available)))]
        duration = item["duration"]
        if cursor+duration > end-1:
            break
        speakers = [i for i in range(4) if busy[i]+.6 <= cursor]
        speaker = min(speakers, key=lambda i: counts[i]+rng.uniform(0,.4))
        event = {"id": item["id"], "start": round(cursor,4), "duration": duration,
                 "speaker": speaker, "gain": round(float(rng.uniform(.68,.88)),4)}
        events.append(event)
        busy[speaker] = cursor+duration
        counts[speaker] += 1
        last[item["id"]] = cursor
        # Some paired sounds; most phrases have a little room to breathe.
        cursor += duration*rng.uniform(.55,.8) if rng.random()<.30 else duration+rng.uniform(1.3,4.8 if state=="activate" else 7.)
    return events


def mix_block(events, samples, start_frame, frames, *, master=MASTER, rate=RATE):
    out = np.zeros((frames,4), np.float32)
    for e in events:
        pos = round(e["start"]*rate)
        x = samples[e["id"]]
        lo, hi = max(start_frame,pos), min(start_frame+frames,pos+len(x))
        if lo < hi:
            out[lo-start_frame:hi-start_frame,e["speaker"]] += x[lo-pos:hi-pos]*e["gain"]
    out *= min(MAX_MASTER,max(0,float(master)))
    return np.clip(out,-PEAK_LIMIT,PEAK_LIMIT)


def load_bank(root):
    import soundfile as sf
    root = Path(root).resolve()
    manifest = json.loads((root/"manifest.json").read_text(encoding="utf-8"))
    samples = {}
    for item in manifest["bank"]:
        path = (root/item["file"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("sample path outside bank")
        x, sr = sf.read(path, dtype="float32")
        if sr != RATE or x.ndim != 1 or not np.all(np.isfinite(x)):
            raise ValueError("bank requires finite mono 24 kHz")
        if len(x)/sr != item["duration"] or np.max(np.abs(x)) > .501:
            raise ValueError("bank duration/peak mismatch")
        samples[item["id"]] = x
    return manifest, samples
