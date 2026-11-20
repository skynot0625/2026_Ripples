import importlib.util
import io
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import wave

import numpy as np
from maek_audio.settings import settings_from_text
from maek_runtime.engine import Engine
from maek_runtime.capture import Capture
from maek_runtime.playback import detect_device
from maek_runtime.protocol import Packet, Decoder, encode

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = '''[input]
sample_rate_hz=44100
sample_format=s16le
channel_order=left,center,right
[speaker]
sample_format=s16le
[tuning]
output_gain=0.1
processed_mix=0
'''

class RuntimeTests(unittest.TestCase):
    def test_wire_mapping_and_center_exclusion(self):
        engine = Engine(settings_from_text(SETTINGS), [1,1,1])
        pcm = struct.pack('<hhh', 1000, 20000, -2000) * 128
        data, result, gap = engine.process(Packet(0,0,pcm))
        out = np.frombuffer(data, dtype='<i2').reshape(-1,2)
        np.testing.assert_array_equal(out, np.tile([100,-200], (128,1)))
        self.assertFalse(gap)

    def test_channel_mute(self):
        engine = Engine(settings_from_text(SETTINGS), [0,1,1])
        data, _, _ = engine.process(Packet(0,0,struct.pack('<hhh', 20000,0,1000)*128))
        self.assertTrue(np.all(np.frombuffer(data,dtype='<i2').reshape(-1,2)[:,0] == 0))

    def test_gap_generation_and_wrap(self):
        engine = Engine(settings_from_text(SETTINGS), [1,1,1])
        pcm = bytes(768)
        engine.process(Packet(0xffffffff,0,pcm))
        self.assertFalse(engine.process(Packet(0,0,pcm))[2])
        self.assertTrue(engine.process(Packet(2,0,pcm))[2])
        self.assertTrue(engine.process(Packet(3,0,pcm), generation=1)[2])
        self.assertEqual(engine.reset_count, 2)

    def test_queue_bounded_latest_retained(self):
        capture = Capture(threading.Event(), capacity=2)
        for i in range(5):
            capture.offer(Packet(i,0,bytes(768)))
        self.assertEqual(capture.dropped,3)
        self.assertEqual([capture.queue.get()[2].sequence for _ in range(2)], [3,4])

    def test_detect_max_card_not_hdmi(self):
        text = ' 0 [vc4hdmi       ]: vc4-hdmi - vc4-hdmi\n 1 [MAX98357A     ]: simple-card - MAX98357A\n'
        self.assertEqual(detect_device(text), 'plughw:CARD=MAX98357A,DEV=0')
        with self.assertRaises(RuntimeError):
            detect_device(' 0 [HDMI ]: hdmi')

    def test_usb_crc_and_fragmentation(self):
        valid = encode(12,bytes(768))
        bad = bytearray(valid)
        bad[70] ^= 1
        decoder = Decoder()
        self.assertEqual(decoder.feed(bad+valid[:25]), [])
        self.assertEqual(decoder.feed(valid[25:])[0].sequence,12)
        self.assertEqual(decoder.bad_packets,1)

    def test_boot_rewrite_and_conflicts(self):
        spec = importlib.util.spec_from_file_location('enable_i2s', ROOT/'tools/enable_i2s.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        text = module.rewrite('[all]\ndtparam=audio=on\ndtoverlay=max98357a\n')
        self.assertIn('dtoverlay=max98357a,no-sdmode',text)
        self.assertIn('# maek previous: dtparam=audio=on',text)
        with self.assertRaises(ValueError):
            module.rewrite('dtoverlay=hifiberry-dac')
        with self.assertRaises(ValueError):
            module.rewrite('include usercfg.txt')

    def test_demo_entrypoint_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'out.wav'
            cmd=[sys.executable,str(ROOT/'run.py'),'--demo','--seconds','0.15','--output-wav',str(path)]
            result=subprocess.run(cmd,cwd=tmp,capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            with wave.open(str(path)) as wav:
                self.assertEqual((wav.getnchannels(),wav.getsampwidth(),wav.getframerate()), (2,2,44100))
                self.assertGreater(wav.getnframes(),0)
                values=np.frombuffer(wav.readframes(wav.getnframes()),dtype='<i2')
                self.assertGreater(np.max(np.abs(values)),0)
            self.assertNotEqual(subprocess.run(cmd,cwd=tmp,capture_output=True,timeout=10).returncode,0)

    def test_demo_through_aplay_process_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            fake = folder / 'aplay'
            destination = folder / 'captured.raw'
            fake.write_text("#!/usr/bin/env python3\nimport os,sys,signal\n"
                            "signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))\n"
                            "assert sys.argv[sys.argv.index('-r')+1] == '44100'\n"
                            "assert sys.argv[sys.argv.index('-c')+1] == '2'\n"
                            "with open(os.environ['MAEK_TEST_PCM_PATH'], 'wb', buffering=0) as f:\n"
                            " while True:\n"
                            "  data = os.read(0, 512)\n"
                            "  if not data: break\n"
                            "  f.write(data)\n")
            fake.chmod(0o755)
            env = dict(os.environ, PATH=str(folder)+os.pathsep+os.environ['PATH'],
                       MAEK_TEST_PCM_PATH=str(destination))
            result = subprocess.run([sys.executable,str(ROOT/'run.py'),'--demo','--seconds','0.5',
                                     '--device','test-output'],env=env,capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            values = np.frombuffer(destination.read_bytes(), dtype='<i2')
            self.assertGreater(values.size,0)
            self.assertEqual(values.size % 2,0)
            self.assertGreater(np.abs(values).max(),0)

    def test_output_test_has_separate_channels(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'tones.wav'
            result=subprocess.run([sys.executable,str(ROOT/'run.py'),'--test-output','--seconds','2',
                '--output-wav',str(path)],capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            with wave.open(str(path)) as wav:
                data=np.frombuffer(wav.readframes(wav.getnframes()),dtype='<i2').reshape(-1,2)
            self.assertTrue(np.all(data[:44100,1]==0))
            self.assertTrue(np.all(data[44100:,0]==0))
            self.assertGreater(np.abs(data[:44100,0]).max(),0)
            self.assertGreater(np.abs(data[44100:,1]).max(),0)

if __name__ == '__main__':
    unittest.main()
