"""Read supplied Ripples recordings; synthetic positioning is explicit, not measured.

No code from the incoming archive is executed. Distances are metres, audio power
is mean square full-scale PCM (not calibrated SPL), and timestamps are seconds.
"""
import hashlib
import json
from pathlib import Path
import struct
import wave
import zipfile

import numpy as np

FEATURES = tuple([f"depth_{i}" for i in range(9)] +
                 [f"coverage_{i}" for i in range(9)] +
                 ["ceiling_0", "ceiling_1"] +
                 [f"power_{i}" for i in range(3)] +
                 [f"centroid_{i}" for i in range(3)] +
                 ["band_low", "band_mid", "band_high"] +
                 [f"mic_level_{i}" for i in range(3)])
DEFAULT_ANCHORS = np.array([[-5., -5.], [5., -5.], [5., 5.], [-5., 5.]])


def estimate_position(rssi_dbm, anchors=DEFAULT_ANCHORS, reference_dbm=-59., exponent=4.):
    """P_mW=10**(RSSI/10); P/P_1m=d**(-exponent). Assumes same-height anchors."""
    rssi = np.asarray(rssi_dbm, dtype=float)
    anchors = np.asarray(anchors, dtype=float)
    ref = np.broadcast_to(np.asarray(reference_dbm, dtype=float), rssi.shape)
    if (rssi.shape != (len(anchors),) or anchors.ndim != 2 or anchors.shape[1] != 2
            or not np.isfinite(anchors).all() or not np.isfinite(ref).all()
            or not np.isfinite(exponent) or exponent <= 0):
        raise ValueError("invalid beacon calibration")
    good = np.isfinite(rssi) & (rssi < 0) & (rssi > -127)
    a, values, refs = anchors[good], rssi[good], ref[good]
    if len(a) < 3 or np.linalg.matrix_rank(a - a.mean(axis=0)) < 2:
        return None
    distances = 10. ** ((refs-values)/(10.*exponent))
    from scipy.optimize import least_squares
    result = least_squares(lambda xy: (np.linalg.norm(a-xy, axis=1)-distances),
                           a.mean(axis=0), loss="soft_l1", f_scale=.5)
    return result.x


def circular_beacons(t, period=59., phase=0., noise_db=.45):
    """Demo only: four virtual corner beacons in a 10 x 10 m room, radius 3.2 m."""
    angle = 2*np.pi*t/period + phase
    truth = 3.2*np.array([np.cos(angle), np.sin(angle)])
    distances = np.linalg.norm(DEFAULT_ANCHORS-truth, axis=1)
    # Reproducible, smooth RF perturbation. This is not site calibration.
    noise = noise_db*np.sin(.7*t + np.arange(4)*1.7)
    rssi = -59. - 40.*np.log10(np.maximum(distances, .1)) + noise
    return rssi, estimate_position(rssi), truth


def packets(stream):
    def exact(n):
        b = stream.read(n)
        if len(b) != n:
            raise ValueError("truncated Hyeol packet")
        return b
    while True:
        prefix = stream.read(8)
        if not prefix:
            return
        if len(prefix) != 8:
            raise ValueError("truncated packet prefix")
        nh, nb = struct.unpack("<II", prefix)
        if not 0 < nh <= 1 << 20 or nb > 16 << 20:
            raise ValueError("invalid packet size")
        h = json.loads(exact(nh))
        if not isinstance(h, dict):
            raise ValueError("packet header must be an object")
        yield h, exact(nb)


def depth_features(h, payload, rotate=180, confidence=60):
    w, height = int(h["width"]), int(h["height"])
    if not 3 <= w <= 2048 or not 3 <= height <= 2048 or len(payload) != w*height*4:
        raise ValueError("bad ToF dimensions/payload")
    if h.get("depth_dtype") != "<u2" or h.get("conf_dtype") != "<u2":
        raise ValueError("unsupported ToF dtype")
    if rotate not in (0, 90, 180, 270):
        raise ValueError("unsupported camera rotation")
    data = np.frombuffer(payload, "<u2").reshape(2, height, w)
    depth, conf = [np.rot90(a, -(rotate//90)) for a in data]
    valid = (conf >= confidence) & (depth > 0) & (depth <= 4000)
    return depth_grid_features(depth, valid)


def depth_grid_features(depth, valid):
    """Reduce an already oriented depth image on the tower; no temporal fill."""
    features = {}
    for row, ys in enumerate(np.array_split(np.arange(depth.shape[0]), 3)):
        for col, xs in enumerate(np.array_split(np.arange(depth.shape[1]), 3)):
            index = np.ix_(ys, xs)
            mask, patch = valid[index], depth[index]
            i = row*3+col
            features[f"coverage_{i}"] = float(mask.mean())
            if mask.any():
                features[f"depth_{i}"] = float(np.median(patch[mask]))/1000.
    return features


def wav_features(path):
    with wave.open(str(path), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (3, 2, 44100, "NONE"):
            raise ValueError(f"expected 44.1 kHz / 16-bit / 3-channel PCM: {path}")
        n = wav.getnframes()
        if not 0 < n <= 44100*10:
            raise ValueError("WAV must contain 0..10 seconds of samples")
        raw = wav.readframes(n)
        if len(raw) != n*6:
            raise ValueError("truncated WAV")
    return pcm_features(raw), n/44100.


def pcm_features(raw):
    """Three interleaved signed PCM16 LE channels at 44.1 kHz, at most 10 s."""
    if not 12 <= len(raw) <= 44100*10*6 or len(raw) % 6:
        raise ValueError("invalid PCM size")
    n = len(raw)//6
    pcm = np.frombuffer(raw, "<i2").reshape(-1, 3).astype(np.float64)/32768.
    pcm -= pcm.mean(axis=0)  # microphone DC is not acoustic energy
    power = np.mean(pcm**2, axis=0)
    spectrum = np.abs(np.fft.rfft(pcm*np.hanning(n)[:, None], axis=0))**2
    freq = np.fft.rfftfreq(n, 1/44100.)
    total = spectrum.sum(axis=0)
    features = {f"power_{i}": float(power[i]) for i in range(3)}
    for i in range(3):
        if total[i] > 1e-12:
            features[f"centroid_{i}"] = float(np.dot(freq, spectrum[:, i])/total[i])
    # One instrument, three frequency bands. Average microphone POWERS, not
    # waveforms: opposite microphone phases must not cancel the displayed sound.
    # Hann spectra provide proportions; measured time-domain power sets energy.
    for name, mask in (("low", freq < 400),
                       ("mid", (freq >= 400) & (freq < 2000)),
                       ("high", freq >= 2000)):
        fraction = np.divide(spectrum[mask].sum(axis=0), total,
                             out=np.zeros(3), where=total > 1e-12)
        features[f"band_{name}"] = float(np.mean(power*fraction))
    return features


def _read_hyeol(stream):
    events, counts, ids = [], {}, set()
    origin = None
    rotate = 180
    for h, b in packets(stream):
        kind = h.get("type")
        counts[kind] = counts.get(kind, 0)+1
        if kind == "hello":
            rotate = int(h.get("rotate", 180))
            continue
        if kind not in ("tof", "lidar"):
            continue
        stamp = int(h["t_mono_ns"])/1e9
        if origin is None:
            origin = stamp
        if kind == "tof":
            features = depth_features(h, b, rotate)
        else:
            sensor = int(h["id"])
            ids.add(sensor)
            # Never clone the single recorded ceiling channel into a second one.
            if sensor not in (0, 1) or not h.get("valid", False) or h.get("status") == 255:
                continue
            distance = float(h["distance_mm"])/1000.
            if not 0 < distance < 60:
                continue
            features = {f"ceiling_{sensor}": distance}
        events.append({"t": stamp-origin, "source": "hyeol", "features": features})
    return sorted(events, key=lambda e: e["t"]), {"packets": counts, "ceiling_ids": sorted(ids)}


def load_recordings(folder):
    """Stream the supplied ZIP, plus optional WAV+metadata pairs. No raw copies."""
    folder = Path(folder)
    archives = sorted(folder.glob("ripples-hyeol-send-sample*.zip"))
    direct = sorted(folder.glob("stream.bin"))
    events, report = [], {"position_mode": "SYNTHETIC_RSSI_CIRCLE", "wav_files": 0}
    digest = hashlib.sha256()
    cursor, seen_recordings = 0., set()
    for path in archives or direct:
        file_hash = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024*1024), b""):
                file_hash.update(chunk)
        identity = file_hash.hexdigest()
        if identity in seen_recordings:
            continue
        seen_recordings.add(identity)
        digest.update(identity.encode())
        if path.suffix == ".zip":
            with zipfile.ZipFile(path) as z:
                names = [n for n in z.namelist() if n.endswith("/sample/stream.bin")]
                if len(names) != 1:
                    raise ValueError("archive must contain one sample/stream.bin")
                with z.open(names[0]) as stream:
                    recording, details = _read_hyeol(stream)
        else:
            with path.open("rb") as stream:
                recording, details = _read_hyeol(stream)
        for event in recording:
            event.update(recording_key="hyeol:"+identity+":v1-circle60", sequence=-1,
                         position_t=event["t"])
            event["t"] += cursor
        if recording:
            cursor = recording[-1]["t"]+.125
        events.extend(recording)
        report.setdefault("hyeol_files", []).append(str(path))
        report["ceiling_ids"] = sorted(set(report.get("ceiling_ids", [])) | set(details["ceiling_ids"]))
        for kind, count in details["packets"].items():
            total = report.setdefault("packets", {})
            total[kind] = total.get(kind, 0)+count
    # Uploaded chunks are ordered within device/session; duplicated chunk IDs are ignored.
    chunks, seen = [], {}
    for path in folder.rglob("*.wav"):
        meta_path = path.with_name("metadata.json")
        if not meta_path.exists():
            raise ValueError(f"WAV metadata.json missing: {path}")
        meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
        blob_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if meta.get("sha256") != blob_hash:
            raise ValueError(f"WAV SHA256 mismatch: {path}")
        key = str(meta["chunk_id"])
        if key in seen:
            if seen[key] != blob_hash:
                raise ValueError("chunk_id collision")
            continue
        seen[key] = blob_hash
        chunks.append((str(meta["device_id"]), str(meta["session_id"]),
                       int(meta["start_frame"]), key, path, meta, blob_hash))
    offsets, cursor = {}, 0.
    for device, session, start, key, path, meta, blob_hash in sorted(chunks):
        if start < 0:
            raise ValueError("negative audio start_frame")
        features, duration = wav_features(path)
        if int(meta["frames"]) != round(duration*44100):
            raise ValueError("audio metadata frame count mismatch")
        identity = device, session
        if identity not in offsets:
            offsets[identity] = cursor-start/44100.
        t = offsets[identity]+start/44100.
        cursor = max(cursor, t+duration+1.)
        events.append({"t": t, "position_t": start/44100., "source": "maek", "features": features,
                       "recording_key": "maek:"+device+":"+session+":v1-circle60",
                       "sequence": start, "frames": int(meta["frames"]),
                       "audio_demo": bool(meta.get("demo", False))})
        digest.update((key+blob_hash).encode())
    report["wav_files"] = len(chunks)
    if not events:
        raise ValueError("no valid stream.bin or WAV recordings found")
    events.sort(key=lambda e: e["t"])
    duration = max(1., events[-1]["t"]+.125)
    errors = []
    for e in events:
        rssi, xy, truth = circular_beacons(e.get("position_t", e["t"]), 60., 0. if e["source"] == "hyeol" else np.pi)
        e.update(xy=xy.tolist(), rssi_dbm=rssi.tolist())
        errors.append(float(np.linalg.norm(xy-truth)))
    report.update(duration_s=duration, events=len(events),
                  position_rmse_m=float(np.sqrt(np.mean(np.square(errors)))),
                  missing_channels=(["ceiling_0"] if 0 not in report.get("ceiling_ids", []) else []) +
                  (["MIC1", "MIC2", "MIC3"] if not chunks else []))
    # Changing feature extraction/virtual calibration creates a distinct demo dataset.
    dataset_id = hashlib.sha256((digest.hexdigest()+"ripples-v1-circle10m").encode()).hexdigest()
    return events, dataset_id, report
