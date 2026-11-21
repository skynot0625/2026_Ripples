"""Bounded Maek WAV intake, offline speech selection and sound-space publishing."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import threading
import time

import soundfile as sf

from .sound_space import SoundSpace, CapacityError
from .tower_audio import recording_runs


def accept_wav(space,data,metadata):
    if len(data)>space.policy['max_upload_bytes']:
        raise ValueError('WAV upload exceeds limit')
    if not isinstance(metadata,dict):
        raise ValueError('metadata must be an object')
    for key in ('device_id','session_id','chunk_id'):
        if not isinstance(metadata.get(key),str) or not 1<=len(metadata[key])<=160:
            raise ValueError('invalid '+key)
    for key in ('start_frame','frames'):
        if type(metadata.get(key)) is not int or metadata[key]<(1 if key=='frames' else 0):
            raise ValueError('invalid '+key)
    digest=hashlib.sha256(data).hexdigest()
    if metadata.get('sha256',digest)!=digest:
        raise ValueError('WAV SHA256 mismatch')
    with sf.SoundFile(io.BytesIO(data)) as stream:
        if stream.format!='WAV' or stream.subtype!='PCM_16' or stream.samplerate!=44100 or stream.channels!=3:
            raise ValueError('requires WAV PCM16, 44100 Hz, three microphones')
        if len(stream)!=metadata['frames'] or not 1<=len(stream)<=44100:
            raise ValueError('requires <=1 second and matching frames metadata')
        x=stream.read(dtype='float32',always_2d=True)
    ident,added=space.add('raw',x,44100,dict(metadata,input_sha256=digest))
    return {'stored':True,'chunk_id':metadata['chunk_id'],'sha256':digest,'new':added,'storage_id':ident}


def handler_for(space,health):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)
        def respond(self,code,obj):
            data=json.dumps(obj,ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            if self.path!='/status':
                self.respond(404,{'error':'not found'});return
            self.respond(200,dict(space.status(),worker=health.copy()))
        def do_POST(self):
            if self.path!='/audio':
                self.respond(404,{'error':'not found'});return
            try:
                if self.headers.get('Content-Type','').split(';')[0].lower()!='audio/wav':
                    raise ValueError('Content-Type must be audio/wav')
                n=int(self.headers.get('Content-Length','0'))
                if not 0<n<=space.policy['max_upload_bytes']:
                    self.respond(413,{'error':'bounded Content-Length required'});return
                header=self.headers.get('X-Audio-Metadata','')
                if len(header)>8192:
                    raise ValueError('metadata header too large')
                metadata=json.loads(header)
                if self.headers.get('Idempotency-Key')!=metadata.get('chunk_id'):
                    raise ValueError('Idempotency-Key must equal chunk_id')
                data=self.rfile.read(n)
                if len(data)!=n: raise ValueError('incomplete body')
                self.respond(200,accept_wav(space,data,metadata))
            except CapacityError as exc:
                self.respond(507,{'stored':False,'error':str(exc)})
            except OSError as exc:
                self.respond(507,{'stored':False,'error':str(exc)})
            except (ValueError,RuntimeError,TypeError,AttributeError) as exc:
                self.respond(400,{'stored':False,'error':str(exc)})
        def log_message(self,*args):
            pass
    return Handler


def serve(space,bind,port):
    stop=threading.Event()
    health={'error':None,'last_processed_at':None}
    server=ThreadingHTTPServer((bind,port),handler_for(space,health))
    def work():
        while not stop.is_set():
            try:
                space.maintain()
                space.process()
                space.export()
                health.update(error=None,last_processed_at=time.time())
            except Exception as exc:
                health['error']=str(exc)
            stop.wait(5)
    thread=threading.Thread(target=work,daemon=True)
    thread.start()
    print(f'WAV intake: http://{bind}:{port}/audio; status: /status',flush=True)
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set();server.server_close();thread.join(timeout=30)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['init','status','ingest','process','export','serve'])
    p.add_argument('--root',type=Path,default=Path('runtime/sound-space'))
    p.add_argument('--policy',type=Path,default=Path('configs/sound-space.m30.json'))
    p.add_argument('--recordings',type=Path)
    p.add_argument('--max-source-seconds',type=float,default=1800)
    p.add_argument('--bind',default='127.0.0.1')
    p.add_argument('--port',type=int,default=9503)
    args=p.parse_args()
    if not args.max_source_seconds>0 or not 1024<=args.port<=65535:
        p.error('invalid duration or port')
    space=SoundSpace(args.root,json.loads(args.policy.read_text(encoding='utf-8-sig')))
    if args.command=='ingest':
        if not args.recordings: p.error('--recordings is required')
        for x,sr,parts in recording_runs(args.recordings,max_seconds=args.max_source_seconds):
            first=parts[0]
            space.add('raw',x,sr,{'device_id':first['device_id'],'session_id':first['session_id'],
                'start_frame':first['session_frame'],'demo':all(v['demo'] for v in parts),'original_parts':parts,
                'origin':first.get('origin')})
        print('new selected clips:',space.process(flush=True))
        print('published:',space.export())
    elif args.command=='process':
        print('new selected clips:',space.process(flush=True))
        print('published:',space.export())
    elif args.command=='export':
        print('published:',space.export())
    elif args.command=='serve':
        serve(space,args.bind,args.port)
    else:
        space.maintain()
    print(json.dumps(space.status(),indent=2))


if __name__=='__main__':
    main()
