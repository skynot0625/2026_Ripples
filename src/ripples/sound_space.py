"""Owned, bounded sound memory. Only files created in this marked store are pruned."""
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
import uuid

import numpy as np
import soundfile as sf

from .tower_audio import RATE, MASTER, MAX_MASTER, PEAK_LIMIT, STEREO, active_bank, extract_sleep, make_plan, procedural_source

DEFAULTS={"raw_max_bytes":512*1024**2,"raw_max_age_seconds":3600,"raw_max_items":2048,
          "clips_max_bytes":256*1024**2,"clips_max_age_seconds":30*86400,"clips_max_items":512,
          "exports_max_bytes":64*1024**2,"export_clip_limit":32,"min_free_bytes":2*1024**3,
          "max_upload_bytes":1024**2}
MARKER={"format":"ripples.sound-space.v1"}


class CapacityError(RuntimeError):
    pass


class SoundSpace:
    def __init__(self,root,policy=None):
        self.root=Path(root).resolve()
        self.policy=dict(DEFAULTS,**(policy or {}))
        for k in DEFAULTS:
            if type(self.policy[k]) is not int or self.policy[k]<(0 if k=='min_free_bytes' else 1):
                raise ValueError('invalid storage limit: '+k)
        if self.policy['export_clip_limit']>32:
            raise ValueError('export_clip_limit must be <=32')
        self.root.mkdir(parents=True,exist_ok=True)
        marker=self.root/'OWNER.json'
        if not marker.exists():
            if any(self.root.iterdir()):
                raise ValueError('Refusing to adopt a non-empty, unmarked directory')
            marker.write_text(json.dumps(MARKER),encoding='utf-8')
        if json.loads(marker.read_text())!=MARKER:
            raise ValueError('Unrecognized sound-space owner')
        for name in ('raw','clips','exports'):
            path=self.root/name
            if path.resolve()!=path:
                raise ValueError('Store subdirectory must not redirect outside its root')
            path.mkdir(exist_ok=True)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS sounds(id TEXT PRIMARY KEY, kind TEXT, added REAL, seen REAL, bytes INTEGER, meta TEXT, processed INTEGER DEFAULT 0, pin REAL DEFAULT 0)')
            db.execute('CREATE TABLE IF NOT EXISTS stats(key TEXT PRIMARY KEY, value INTEGER)')

    @contextmanager
    def db(self):
        with sqlite3.connect(self.root/'catalog.sqlite3',timeout=15) as db:
            db.row_factory=sqlite3.Row
            db.execute('PRAGMA journal_size_limit=1048576')
            db.execute('PRAGMA max_page_count=8192')
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
            except CapacityError:
                db.commit()
                raise

    def path(self,kind,ident,suffix='.flac'):
        if kind not in ('raw','clips') or not re.fullmatch('[a-f0-9]{64}',ident):
            raise ValueError('Unowned file reference')
        original=self.root/kind/(ident+suffix)
        path=original.resolve()
        if path!=original:
            raise ValueError('Storage file must not be a link')
        if path.parent!=(self.root/kind).resolve() or not path.is_relative_to(self.root):
            raise ValueError('Storage path escape')
        return path

    def _remove(self,db,row):
        for suffix in ('.flac','.json'):
            self.path(row['kind'],row['id'],suffix).unlink(missing_ok=True)
        db.execute('DELETE FROM sounds WHERE id=?',(row['id'],))
        db.execute("INSERT INTO stats VALUES('evicted',1) ON CONFLICT(key) DO UPDATE SET value=value+1")
        if row['kind']=='raw' and not row['processed']:
            db.execute("INSERT INTO stats VALUES('unprocessed_evicted',1) ON CONFLICT(key) DO UPDATE SET value=value+1")

    def _prune(self,db,kind,incoming=0):
        now=time.time()
        age='added' if kind=='raw' else 'seen'
        for row in db.execute(f'SELECT * FROM sounds WHERE kind=? AND pin<? AND {age}<?',(kind,now,now-self.policy[kind+'_max_age_seconds'])).fetchall():
            self._remove(db,row)
        while True:
            # Count actual files too, including crash leftovers and unknown files.
            # Unknown files consume capacity but are never selected for eviction.
            files=[p for p in (self.root/kind).iterdir() if p.is_file()]
            count=sum(p.suffix=='.flac' for p in files)
            total=sum(p.stat().st_size for p in files)
            headroom=shutil.disk_usage(self.root).free-incoming>=self.policy['min_free_bytes']
            if total+incoming<=self.policy[kind+'_max_bytes'] and count+(1 if incoming else 0)<=self.policy[kind+'_max_items'] and headroom:
                return
            row=db.execute(f'SELECT * FROM sounds WHERE kind=? AND pin<? ORDER BY {age},id LIMIT 1',(kind,now)).fetchone()
            if row is None:
                raise CapacityError('No space within storage/free-disk limits')
            self._remove(db,row)

    def maintain(self):
        with self.db() as db:
            for row in db.execute('SELECT * FROM sounds').fetchall():
                if not self.path(row['kind'],row['id']).exists() or not self.path(row['kind'],row['id'],'.json').exists():
                    self._remove(db,row)
            for kind in ('raw','clips'):
                self._prune(db,kind)
            # Recover only our generated hex-named orphan/staging files after a crash.
            known={r[0] for r in db.execute('SELECT id FROM sounds')}
            for kind in ('raw','clips'):
                for p in (self.root/kind).iterdir():
                    if re.fullmatch('[a-f0-9]{64}\\.(flac|json|tmp)',p.name) and p.stem not in known:
                        if time.time()-p.stat().st_mtime>300:
                            self.path(kind,p.stem,p.suffix).unlink()
            pointer=self.root/'CURRENT.json'
            current=json.loads(pointer.read_text())['generation'] if pointer.exists() else None
            if current is not None and not re.fullmatch('[a-f0-9]{32}',current):
                raise ValueError('invalid current generation')
            for p in (self.root/'exports').iterdir():
                if p.is_dir() and re.fullmatch('[a-f0-9]{32}',p.name) and p.name!=current and time.time()-p.stat().st_mtime>300:
                    if p.resolve().parent!=(self.root/'exports').resolve(): raise ValueError('Export path escape')
                    shutil.rmtree(p)

    def add(self,kind,samples,sr,metadata):
        samples=np.asarray(samples,np.float32)
        if kind not in ('raw','clips') or (kind=='clips' and (samples.ndim!=1 or sr!=RATE)):
            raise ValueError('clips must be mono 24 kHz')
        if samples.ndim not in (1,2) or (samples.ndim==2 and samples.shape[1] not in (1,2,3)):
            raise ValueError('expected mono or up to 3 microphone channels')
        if sr not in (8000,16000,24000,32000,44100,48000) or not 0<len(samples)<=30*sr or not np.all(np.isfinite(samples)):
            raise ValueError('finite audio chunks must be <=30 seconds at a supported rate')
        buf=io.BytesIO()
        sf.write(buf,samples,sr,format='FLAC',subtype='PCM_16')
        data=buf.getvalue()
        identity={k:metadata.get(k) for k in ('device_id','session_id','start_frame')}
        ident=hashlib.sha256(data+(json.dumps(identity,sort_keys=True).encode() if kind=='raw' else b'')).hexdigest()
        meta=dict(metadata,frames=len(samples),sample_rate=sr,sha256=hashlib.sha256(data).hexdigest())
        packed=json.dumps(meta,ensure_ascii=False).encode('utf-8')
        size=len(data)+len(packed)
        if size>self.policy[kind+'_max_bytes']:
            raise CapacityError('Single item exceeds its storage budget')
        with self.db() as db:
            old=db.execute('SELECT * FROM sounds WHERE id=?',(ident,)).fetchone()
            if old:
                db.execute('UPDATE sounds SET seen=? WHERE id=?',(time.time(),ident))
                return ident,False
            if kind=='raw' and metadata.get('chunk_id'):
                for row in db.execute("SELECT meta FROM sounds WHERE kind='raw'"):
                    previous=json.loads(row['meta'])
                    if all(previous.get(k)==metadata.get(k) for k in ('device_id','session_id','chunk_id')):
                        raise ValueError('chunk identity reused with different content or position')
            if kind=='clips':
                # Spectral near-duplicate rejection, limited to the same sound category.
                fp=meta.get('fingerprint')
                for row in db.execute("SELECT meta FROM sounds WHERE kind='clips'"):
                    oldmeta=json.loads(row['meta'])
                    if fp and oldmeta.get('category')==meta.get('category') and oldmeta.get('fingerprint'):
                        similar=float(np.dot(fp,oldmeta['fingerprint']))>.997
                        if similar and abs(oldmeta['frames']/oldmeta['sample_rate']-len(samples)/sr)<.3:
                            return None,False
            self._prune(db,kind,size)
            audio=self.path(kind,ident)
            temp=self.path(kind,ident,'.tmp')
            temp.write_bytes(data)
            os.replace(temp,audio)
            self.path(kind,ident,'.json').write_bytes(packed)
            db.execute('INSERT INTO sounds(id,kind,added,seen,bytes,meta) VALUES(?,?,?,?,?,?)',
                       (ident,kind,time.time(),time.time(),size,packed.decode()))
        return ident,True

    def process(self,*,flush=False):
        with self.db() as db:
            rows=db.execute("SELECT * FROM sounds WHERE kind='raw' AND processed=0 AND pin<? ORDER BY added LIMIT 120",(time.time(),)).fetchall()
            duration=sum(json.loads(r['meta'])['frames']/json.loads(r['meta'])['sample_rate'] for r in rows)
            if not rows or (not flush and duration<24 and time.time()-rows[-1]['added']<4):
                return 0
            for row in rows:
                db.execute('UPDATE sounds SET pin=? WHERE id=?',(time.time()+300,row['id']))
        created=0
        try:
            paths=[self.path('raw',r['id']) for r in rows]
            bank=extract_sleep(self.root/'raw',paths=paths,max_seconds=3600,limit=24)
            for item in bank:
                x=item['samples']
                spectrum=np.abs(np.fft.rfft(x[::4]))
                fp=np.array([float(np.log1p(np.mean(a))) for a in np.array_split(spectrum,24)])
                fp/=max(float(np.linalg.norm(fp)),1e-12)
                meta=dict(item['source'],fingerprint=fp.tolist(),demo=item['source']['kind']=='synthetic_maek')
                _,added=self.add('clips',x,RATE,meta)
                created+=int(added)
            with self.db() as db:
                for row in rows:
                    db.execute('UPDATE sounds SET processed=1,pin=0 WHERE id=?',(row['id'],))
        finally:
            with self.db() as db:
                for row in rows:
                    db.execute('UPDATE sounds SET pin=0 WHERE id=?',(row['id'],))
        self.maintain()
        return created

    def export(self):
        """Publish a complete generation, then atomically swap CURRENT.json."""
        with self.db() as db:
            rows=db.execute("SELECT * FROM sounds WHERE kind='clips' ORDER BY seen DESC").fetchall()
            # Interleave voice/environment so either category can survive a busy day.
            voice=[r for r in rows if json.loads(r['meta']).get('category')=='speech_candidate']
            ambient=[r for r in rows if r not in voice]
            chosen=[]
            for i in range(max(len(voice),len(ambient))):
                if i<len(voice): chosen.append(voice[i])
                if i<len(ambient): chosen.append(ambient[i])
            chosen=chosen[:self.policy['export_clip_limit']]
            selection=hashlib.sha256(('dream-work-v3-no-speech:'+','.join(r['id'] for r in chosen)).encode()).hexdigest()
            pointer=self.root/'CURRENT.json'
            if pointer.exists():
                old_pointer=json.loads(pointer.read_text())
                if not re.fullmatch('[a-f0-9]{32}',old_pointer['generation']):
                    raise ValueError('invalid current generation')
                if old_pointer.get('selection')==selection and (self.root/'exports'/old_pointer['generation']/'manifest.json').is_file():
                    return self.root/'exports'/old_pointer['generation']
            bank=[]
            audio=[]
            for name,x in active_bank():
                audio.append((name,x,procedural_source(name),'activate'))
            for row in chosen:
                x,sr=sf.read(self.path('clips',row['id']),dtype='float32')
                audio.append(('memory_'+row['id'][:16],x,json.loads(row['meta']),'sleep'))
            expected=sum(len(x)*2+44 for _,x,_,_ in audio)+512*1024
            exports=self.root/'exports'
            pointer=self.root/'CURRENT.json'
            current=json.loads(pointer.read_text())['generation'] if pointer.exists() else None
            dirs=[p for p in exports.iterdir() if p.is_dir() and re.fullmatch('[a-f0-9]{32}',p.name)]
            def size(p): return sum(f.stat().st_size for f in p.rglob('*') if f.is_file())
            for p in sorted(dirs,key=lambda p:p.stat().st_mtime):
                if p.name!=current:
                    # Verified generated directory under the exact owned exports root.
                    if p.resolve().parent!=exports.resolve(): raise ValueError('Export path escape')
                    shutil.rmtree(p)
            occupied=sum(size(p) for p in exports.iterdir() if p.is_dir())
            if expected+occupied>self.policy['exports_max_bytes'] or shutil.disk_usage(self.root).free-expected<self.policy['min_free_bytes']:
                raise CapacityError('Export budget/free-disk reserve would be exceeded')
            generation=uuid.uuid4().hex
            dest=exports/generation
            (dest/'bank').mkdir(parents=True)
            for name,x,source,state in audio:
                file='bank/'+name+'.wav'
                sf.write(dest/file,x,RATE,subtype='PCM_16')
                bank.append({'id':name,'file':file,'duration':len(x)/RATE,'source':source,'state':state,
                             'sha256':hashlib.sha256((dest/file).read_bytes()).hexdigest()})
            manifest={'schema':'ripples.tower-audio.v1','sample_rate':RATE,'channels':4,'max_voices':2,
                'master':MASTER,'max_master':MAX_MASTER,'peak_limit':PEAK_LIMIT,'stereo_matrix':STEREO.tolist(),
                'sleep_source':('empty' if not chosen else 'synthetic_demo' if all(b['source']['kind']=='synthetic_maek' for b in bank if b['state']=='sleep') else
                                'mixed_recordings' if any(b['source']['kind']=='synthetic_maek' for b in bank if b['state']=='sleep') else 'supplied_recordings'),
                'schedule':{'timezone':'Asia/Seoul','anchor':'11:00','activate_minutes':20,'sleep_minutes':10},
                'bank':bank,'plans':{s:make_plan(bank,s,int(time.time()//1800)) for s in ('activate','sleep')}}
            (dest/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False),encoding='utf-8')
            tmp=self.root/'CURRENT.tmp'
            tmp.write_text(json.dumps({'generation':generation,'selection':selection}),encoding='utf-8')
            os.replace(tmp,pointer)
            return dest

    def status(self):
        with self.db() as db:
            result={k:dict(zip(('items','bytes'),db.execute('SELECT count(*),coalesce(sum(bytes),0) FROM sounds WHERE kind=?',(k,)).fetchone())) for k in ('raw','clips')}
            result['pending_raw']=db.execute("SELECT count(*) FROM sounds WHERE kind='raw' AND processed=0").fetchone()[0]
            result['evicted']=dict(db.execute('SELECT key,value FROM stats')).get('evicted',0)
            result['unprocessed_evicted']=dict(db.execute('SELECT key,value FROM stats')).get('unprocessed_evicted',0)
        result['exports_bytes']=sum(p.stat().st_size for p in (self.root/'exports').rglob('*') if p.is_file())
        result['free_bytes']=shutil.disk_usage(self.root).free
        result['policy']=self.policy
        return result
