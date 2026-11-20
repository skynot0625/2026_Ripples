"""Installer check: python -m maek_audio.check SETTINGS.ini [--pcm RAW] [--out STEREO.wav]

Without --pcm it only validates the settings file. With --pcm it runs a raw
recording (interleaved, in the configured input format) through the processor
and reports levels, clipping, and motor command ranges. Nothing is sent anywhere.
"""

import argparse
import sys
import wave

import numpy as np

from .settings import SettingsError, describe, load_settings


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m maek_audio.check")
    parser.add_argument("settings", nargs="?", help="INI path (default: $MAEK_AUDIO_CONFIG or ./maek_audio.ini)")
    parser.add_argument("--pcm", help="raw 3-channel recording in the configured input format")
    parser.add_argument("--out", help="write processed stereo to this WAV (speaker format, int formats only)")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.settings)
    except SettingsError as error:
        print(error, file=sys.stderr)
        return 2
    print("settings: %s" % settings.source_path)
    print(describe(settings))
    if not args.pcm:
        return 0

    with open(args.pcm, "rb") as handle:
        raw = handle.read()
    frame_bytes = settings.input.frame_bytes
    usable = len(raw) - len(raw) % frame_bytes
    if usable != len(raw):
        print("warning: dropped %d trailing bytes (not a whole frame)" % (len(raw) - usable))
    raw = raw[:usable]
    processor = settings.make_processor()
    chunker = settings.make_chunker() if settings.input.sample_format != "float32le" else None
    rate = settings.processor.sample_rate_hz
    block_bytes = settings.input.frames_per_block * frame_bytes
    stereo_parts, motors, rms, clipped, chunks = [], [], [], 0, 0
    for offset in range(0, len(raw), block_bytes):
        piece = raw[offset:offset + block_bytes]
        result = processor.process(settings.decode_input(piece), sample_rate_hz=rate)
        stereo_parts.append(result.stereo_pcm)
        motors.append(settings.motor_commands(result))
        rms.append((result.rms_left, result.rms_center, result.rms_right))
        clipped += result.input_clipped
        if chunker is not None:
            chunks += len(chunker.append_bytes(settings.wav_source_bytes(piece)))
    frames = len(raw) // frame_bytes
    rms = np.asarray(rms)
    motors = np.asarray(motors, dtype=float)
    print("\nrecording: %d frames (%.2f s), %d blocks" % (frames, frames / rate, len(motors)))
    for index, name in enumerate(("left", "center", "right")):
        print("  band RMS %-6s  mean %.4f  max %.4f" % (name, rms[:, index].mean(), rms[:, index].max()))
    print("  blocks with input clipping: %d" % clipped)
    print("  motor commands  left %s..%s  right %s..%s" % (
        motors[:, 0].min(), motors[:, 0].max(), motors[:, 1].min(), motors[:, 1].max()))
    if chunker is not None:
        print("  complete 1-second source WAV chunks: %d" % chunks)
    if args.out:
        fmt = settings.speaker.sample_format
        width = {"s16le": 2, "s24le": 3, "s32le": 4}.get(fmt)
        if width is None:
            print("--out skipped: WAV writer here supports integer speaker formats only", file=sys.stderr)
            return 1
        with wave.open(args.out, "wb") as writer:
            writer.setnchannels(2)
            writer.setsampwidth(width)
            writer.setframerate(rate)
            writer.writeframes(settings.encode_speaker(np.concatenate(stereo_parts)))
        print("  stereo written: %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
