#!/usr/bin/env python3
"""One-entry-point Teensy USB -> Maek DSP -> dual MAX98357A playback."""
import argparse
import json
import logging
import math
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT/'config/audio.ini')
    parser.add_argument('--device-config', type=Path, default=ROOT/'config/device.json')
    parser.add_argument('--port')
    parser.add_argument('--device', help='ALSA device override, e.g. plughw:CARD=MAX98357A,DEV=0')
    parser.add_argument('--list-devices', action='store_true')
    parser.add_argument('--check', action='store_true', help='Check config and devices without playback')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--demo', action='store_true', help='Synthetic three-mic input')
    modes.add_argument('--test-output', action='store_true', help='Quiet left/right tones, no USB needed')
    parser.add_argument('--seconds', type=float)
    parser.add_argument('--output-wav', type=Path, help='Write stereo WAV instead of speaker playback')
    parser.add_argument('--control-port', type=int, help='Remote settings page port (0 = off; default device.json control_port or 8090)')
    parser.add_argument('--mute-mic', type=int, action='append', choices=[1,2,3], default=[])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.seconds is not None and (not math.isfinite(args.seconds) or args.seconds <= 0):
        parser.error('--seconds must be positive and finite')
    if args.output_wav and args.seconds is None and not args.test_output:
        parser.error('--output-wav requires --seconds (bounded file size)')
    try:
        if args.list_devices:
            return subprocess.run(['aplay', '-l']).returncode
        import numpy as np
        from maek_audio import load_settings
        from maek_runtime.engine import Engine
        from maek_runtime.capture import Capture
        from maek_runtime.playback import detect_device, AlsaSink, WavSink
        settings = load_settings(args.config)
        device_config = json.loads(args.device_config.read_text())
        gains = list(device_config['mic_gains'])
        for mic in args.mute_mic:
            gains[mic-1] = 0
        control = None
        control_port = args.control_port if args.control_port is not None else int(device_config.get('control_port', 8090))
        if control_port and not (args.output_wav or args.test_output or args.check or args.list_devices):
            from maek_runtime.control import LiveControl, serve
            control = LiveControl(settings, gains, ROOT/'config/live.json')
            settings, gains = control.settings()
            for mic in args.mute_mic:
                gains[mic-1] = 0
        engine = Engine(settings, gains)
        capacity = device_config['max_queue_packets']
        age_limit = float(device_config['max_packet_age_seconds'])
        timeout = float(device_config['usb_timeout_seconds'])
        if type(capacity) is not int or not 2 <= capacity <= 128:
            raise ValueError('max_queue_packets must be integer 2..128')
        if not math.isfinite(age_limit) or not 0.02 <= age_limit <= 1:
            raise ValueError('max_packet_age_seconds must be 0.02..1')
        if not math.isfinite(timeout) or not 1 <= timeout <= 30:
            raise ValueError('usb_timeout_seconds must be 1..30')
        device = args.device or device_config['alsa_device']
        if device == 'auto' and not args.output_wav:
            device = detect_device()
        port = args.port or device_config['serial_port']
        if args.check:
            print('Config OK: 44100Hz, 3ch s16le -> 2ch s16le')
            print('Input channel order:', settings.processor.channel_order)
            print('ALSA:', device)
            import serial.tools.list_ports
            print('USB:', [(p.device, p.description) for p in serial.tools.list_ports.comports()])
            print('Device enumeration only; actual capture/playback is not verified.')
            return 0
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        sink = WavSink(args.output_wav) if args.output_wav else AlsaSink(device, stop, int(device_config.get('alsa_buffer_ms', 40)),
                                                                   int(device_config.get('alsa_period_ms', 10)))
        capture = None
        try:
            if args.test_output:
                frames = int((args.seconds or 4) * 44100)
                for offset in range(0, frames, 441):
                    if stop.is_set():
                        break
                    size = min(441, frames-offset)
                    t = (np.arange(size) + offset)/44100
                    out = np.zeros((size,2), dtype=np.float32)
                    # 0..1s left, 1..2s right, repeating. Gentle edge envelope.
                    channel = int(offset/44100) % 2
                    phase = t % 1
                    envelope = np.minimum(np.minimum(phase/0.02, (1-phase)/0.02), 1)
                    out[:,channel] = 0.015 * envelope * np.sin(2*np.pi*440*t)
                    sink.write(settings.encode_speaker(out))
                return 0
            capture = Capture(stop, port, capacity, timeout, args.demo)
            capture.start()
            logging.info('Started: %s -> %s; Ctrl+C to stop', 'demo' if args.demo else port,
                         str(args.output_wav) if args.output_wav else device)
            logging.info('Gain %.3f; mic gains %s; no AEC, no motor control, no server upload',
                         settings.processor.output_gain, gains)
            from maek_runtime.smooth import Declicker, DriftResampler, DriftController
            declick = Declicker()
            resampler = DriftResampler()
            drift = DriftController(target_packets=float(device_config.get('target_queue_packets', 6)))
            paced = not args.output_wav  # WAV output is not clocked; no drift to correct
            ratio = 1.0

            def emit(blocks):
                for block in blocks:
                    if paced:
                        block = resampler.process(block, ratio)
                    sink.write(settings.encode_speaker(block))

            motors = None
            motor_next = 0.0
            motor_pct = (0.0, 0.0)
            if device_config.get('motors', True) and not args.output_wav:
                from maek_runtime.motors import PwmMotors, duty
                try:
                    motors = PwmMotors(int(device_config.get('motor_pwm_hz', 20000)),
                                       bool(device_config.get('motor_swap', False))).open()
                except (OSError, RuntimeError) as exc:
                    logging.warning('Motors disabled: %s', exc)
                    motors = None
            sim = None
            sim_url = device_config.get('sim_url')
            if sim_url and not args.output_wav:
                from maek_runtime.simlink import SimSender
                sim = SimSender(sim_url, float(device_config.get('sim_hz', 10)),
                                settings.server.device_id or 'mic',
                                *device_config.get('sim_level_db', [-70, -20]))
                sim.start()
                logging.info('Sending live values to 심: %s', sim_url)
            control_version = None
            if control:
                control_version = control.version
                serve(control, control_port)
            started = last_log = time.monotonic()
            stale = gaps = underruns = 0
            silent = True
            rate_origin = None  # (received time, sequence, generation) for measured input rate
            pending = []  # a packet taken from the queue that did not fit the last batch
            max_batch = int(device_config.get('max_batch_packets', 8))
            while not stop.is_set():
                now = time.monotonic()
                if args.seconds and now - started >= args.seconds:
                    break
                try:
                    received, generation, packet = (pending.pop(0) if pending
                                                    else capture.queue.get(timeout=0.02))
                except queue.Empty:
                    if motors:
                        motors.set(0, 0)
                    if not silent:
                        underruns += 1
                        emit(declick.fade_out())
                        engine.reset()
                    silent = True
                    if paced:
                        sink.write(bytes(441 * 4))
                    continue
                if time.monotonic() - received > age_limit:
                    stale += 1
                    emit(declick.fade_out())
                    engine.reset()
                    continue
                silent = False
                if paced:
                    ratio = drift.update(capture.queue.qsize() + len(pending))
                # Drain what is already waiting (up to max_batch) and process consecutive
                # packets together: per-call overhead dominates on a Pi 3B.
                batch = [packet]
                while len(batch) < max_batch:
                    try:
                        nxt_received, nxt_generation, nxt = capture.queue.get_nowait()
                    except queue.Empty:
                        break
                    if nxt_generation == generation and ((nxt.sequence - batch[-1].sequence) & 0xffffffff) == 1:
                        batch.append(nxt)
                    else:
                        pending.append((nxt_received, nxt_generation, nxt))
                        break
                if control and control.version != control_version:
                    control_version = control.version
                    emit(declick.fade_out())
                    live_settings, live_gains = control.settings()
                    engine.apply(live_settings, live_gains)
                data, result, gap = engine.process_many(batch, generation)
                packet = batch[-1]
                if gap:
                    gaps += 1
                    if gaps == 1:
                        logging.warning('Input discontinuity; faded out/in and DSP reset '
                                        '(further gaps are counted in the status line)')
                emit(declick.push(result.stereo_pcm, gap))
                if sim:
                    sim.offer(result)
                if motors and now >= motor_next:
                    motor_next = now + 0.02  # 50 Hz is plenty for vibration
                    low, high = control.motor() if control else (25.0, 80.0)
                    motor_pct = (duty(result.haptic_left, low, high), duty(result.haptic_right, low, high))
                    motors.set(*motor_pct)
                if rate_origin is None or rate_origin[2] != generation or gap:
                    rate_origin = (received, packet.sequence, generation)
                if now - last_log >= 2:
                    span = received - rate_origin[0]
                    measured = (((packet.sequence - rate_origin[1]) & 0xffffffff) * 128 / span
                                if span >= 1 else float('nan'))
                    logging.info('RMS L/C/R %.4f %.4f %.4f | in_rate=%.1fHz ratio=%.5f queue=%d | '
                                 'gaps=%d underrun=%d queue_drop=%d stale=%d crc_bad=%d reconnect=%d',
                                 result.rms_left, result.rms_center, result.rms_right,
                                 measured, ratio, capture.queue.qsize(), gaps, underruns,
                                 capture.dropped, stale, capture.bad_packets, capture.reconnects)
                    last_log = now
                    if control:
                        control.status = {
                            'rms': '%.4f %.4f %.4f' % (result.rms_left, result.rms_center, result.rms_right),
                            'in_rate': '%.1fHz' % measured, 'ratio': '%.5f' % ratio,
                            'queue': capture.queue.qsize(), 'gaps': gaps, 'underrun': underruns,
                            'queue_drop': capture.dropped, 'crc_bad': capture.bad_packets,
                            'reconnect': capture.reconnects,
                            'motors': ('%.0f%% %.0f%%' % motor_pct) if motors else 'off',
                            'sim': ('sent %d, failed %d%s' % (sim.sent, sim.failed,
                                    (' (' + sim.last_error + ')') if sim.last_error else '')) if sim else 'off'}
            emit(declick.fade_out())
            if capture.error:
                raise RuntimeError('Capture failed: ' + str(capture.error))
        finally:
            stop.set()
            if 'motors' in locals() and motors:
                motors.close()
            if 'sim' in locals() and sim:
                sim.stop()
            if capture:
                capture.join(timeout=2)
            sink.close()
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        logging.error('%s', exc)
        if isinstance(exc, ImportError):
            logging.error('Install dependencies: bash setup_pi.sh; run with /usr/bin/python3 run.py')
        return 1

if __name__ == '__main__':
    sys.exit(main())
