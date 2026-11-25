import base64,gzip,json,os,time,threading,queue
from contextlib import nullcontext
from pathlib import Path
import numpy as np
import soundfile as sf
import sounddevice as sd
from serial.tools import list_ports
from ripples.hardware.transport import LEDUSB,VALID_MASK
from ripples.visualization import RipplesVisual,layer_separation
from ripples.transition import limit_rgb
from ripples.spikes import send_frames
from ripples.sleep_replay import SleepReplay,three_phase_preview
from ripples.memory import SpatialMemory
root=Path(__file__).resolve().parents[1]
(root/'runtime').mkdir(parents=True,exist_ok=True)
stop=root/'runtime/exhibition-example.stop';stop.unlink(missing_ok=True)
(root/'runtime/exhibition-example.pid').write_text(str(os.getpid()))
assets=root/'exhibition-server/static'
meta=json.loads((assets/'ripples-example.json').read_text())
clips={state:np.frombuffer(gzip.decompress((assets/f'ripples-{state}.rgb.gz').read_bytes()),np.uint8).reshape(-1,8460,3) for state in ('activate','sleep')}
sleep_data=meta['sleep_replay']
history_path=root/'runtime/m30-example-history.sqlite3'
if history_path.exists():
 history_db=SpatialMemory(history_path,namespace='demo')
 try:sleep_data=history_db.history()
 finally:history_db.close()
print('DURABLE EXAMPLE HISTORY',sleep_data.get('storage',{}),flush=True)
assert sleep_data['schema']=='ripples.sleep-replay/1'
sleep_replay=SleepReplay(sleep_data['tracks'])
sleep_visual=RipplesVisual('sleep',output_limits=False)
sleep_visual.set_memory(sleep_data['snapshot'])
sleep_xy=sleep_visual.coords[:2].T/31*10-5
activity_frames=np.frombuffer(gzip.decompress(base64.b64decode(meta['activity_clip']['data'])),np.uint8).reshape(-1,8460,3)
if not os.environ.get('RIPPLES_MP3_PATH'):
 raise RuntimeError('Set RIPPLES_MP3_PATH to the locally licensed 30-minute stereo MP3')
source=Path(os.environ['RIPPLES_MP3_PATH'])
info=sf.info(source)
assert info.samplerate==48000 and info.channels==2 and info.frames==1800*48000
q=queue.Queue(64);done=threading.Event();played=0;errors=[]
def produce():
 global played
 try:
  with sf.SoundFile(source) as f:
   played=int((time.time()%1800)*48000)
   f.seek(played)
   print("WALL CLOCK SEEK",round(played/48000,2),flush=True)
   while not done.is_set() and not stop.exists():
    x=f.read(1024,dtype='float32',always_2d=True)
    if not len(x):f.seek(0);continue
    while not done.is_set():
     try:q.put(x,timeout=.1);break
     except queue.Full:pass
 except Exception as e:errors.append(str(e));done.set()
def callback(out,frames,timing,status):
 global played
 out.fill(0)
 if done.is_set() or stop.exists():raise sd.CallbackStop
 try:x=q.get_nowait()
 except queue.Empty:return
 t=(played+np.arange(len(x)))/48000;phase=t%1800
 gain=1.0*np.minimum(1,phase/.5)*np.minimum(1,(1800-phase)/.5)
 out[:len(x)]=np.tile(np.clip(x*gain[:,None],-1.,1.),(1,2))
 played+=len(x)
 if status:print('AUDIO STATUS',status,flush=True)
visual=RipplesVisual();last=time.monotonic();previous=None
h=visual.transition.height
# White sensor layers, firing and transition in both display states.
final_tints=np.ones((len(h),3))
ports=[p for p in list_ports.comports() if p.serial_number=='20:6E:F1:32:89:50']
if len(ports)!=1:raise RuntimeError('Known LED board not uniquely detected')
audio_available=any(d["max_output_channels"]>=4 and "Realtek" in d["name"] for d in sd.query_devices())
print("AUDIO AVAILABLE",audio_available,flush=True)
def frames():
 global last,previous,played
 while not done.is_set() and not stop.exists():
  if not audio_available:played=int((time.time()%1800)*48000)
  phase=(played/48000)%1800
  state='activate' if phase<1200 else 'sleep'
  try:
   override=(root/'runtime/exhibition-display-mode.txt').read_text().strip()
   if override in ('activate','sleep'):state=override
  except OSError:pass
  local=phase if state=='activate' else (phase-1200)%600
  activity_local=local;preview_label='NORMAL'
  try:
   preview_config=json.loads((root/'runtime/three-phase-preview.json').read_text())
   preview=three_phase_preview(time.time()-float(preview_config['started_at']))
   if preview is not None:state,local,activity_local,preview_label=preview
  except (OSError,ValueError,KeyError,TypeError):pass
  rgb=clips[state][int(local*10)%len(clips[state])]
  now=time.monotonic();dt=max(.001,now-last);last=now
  if state=='sleep':
   raw=np.zeros((*VALID_MASK.shape,3),np.uint8)
   raw[VALID_MASK]=activity_frames[int(activity_local*10)%len(activity_frames)]
   # Activity frames already contain the established 1.5 s release envelope.
   sleep_visual.activity.fill(0)
   rgb=sleep_visual.render(raw,dt=dt,sensor_mask=sleep_replay.mask(sleep_xy,local))[VALID_MASK]
  frame=np.zeros((*VALID_MASK.shape,3),np.uint8)
  shown=visual.transition.render(rgb,state,dt)
  frame[VALID_MASK]=(shown.max(axis=1)[:,None]*final_tints*layer_separation(h)[:,None]).astype(np.uint8)
  frame[VALID_MASK]=limit_rgb(frame[VALID_MASK],visual.transition.weights).astype(np.uint8)
  stage=sleep_replay.stage(local) if state=='sleep' else '-'
  if previous!=(state,stage,preview_label):
   print('STATE',state,'SLEEP PHASE',stage,'SLEEP SECOND',round(local,2),'AUDIO POSITION',round(phase,2),'PREVIEW',preview_label,'DEMO / recorded sensors and reservoir',flush=True);previous=(state,stage,preview_label)
  yield frame
try:
 with LEDUSB(ports[0].device,version='rgb565') as board:
  print('LED BLACK ACK OK; wall-clock 10:30 anchored 20/10 loop; AUDIO GAIN 100%, QUAD ORDER UNCHANGED',flush=True)
  threading.Thread(target=produce,daemon=True).start()
  while q.qsize()<8 and not done.is_set():time.sleep(.01)
  if errors:raise RuntimeError(errors)
  with (sd.OutputStream(device='스피커(Realtek(R) Audio), Windows WASAPI',samplerate=48000,channels=4,dtype='float32',blocksize=1024,latency='high',callback=callback,finished_callback=done.set) if audio_available else nullcontext()):
   send_frames(board,frames(),10)
finally:
 done.set()
 print('SESSION STOPPED',errors,flush=True)
