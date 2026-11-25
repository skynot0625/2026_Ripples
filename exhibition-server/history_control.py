"""Persist ONLY explicitly synthetic venue3d observations; never mix with live sensor DBs."""
import json
import math
from pathlib import Path
import sys
import threading
import time
from urllib.parse import urlsplit

LOCK = threading.RLock()


def project_dir(page_dir):
    settings = Path(page_dir)/'run/speaker-test.json'
    return Path(json.loads(settings.read_text(encoding='utf-8-sig'))['project_dir']) if settings.exists() else Path(page_dir).parent


def request(page_dir, events=None):
    root = project_dir(page_dir)
    src = str(root/'src')
    if src not in sys.path:
        sys.path.insert(0,src)
    from ripples.memory import SpatialMemory
    with LOCK:
        db = SpatialMemory(root/'runtime/venue3d-history.sqlite3', namespace='demo')
        try:
            if events is None:
                return db.history()
            if not isinstance(events,list) or len(events)>32:
                raise ValueError('at most 32 normalized example observations per request')
            groups={'hyeol':{},'maek':{}}
            for event in events:
                if not isinstance(event,dict) or set(event)!={'source','xy','features','t'}:
                    raise ValueError('invalid observation fields')
                role=event['source'];stamp=event['t']
                if role not in groups or type(stamp) not in (int,float) or not math.isfinite(stamp) or not 1577836800<=stamp<=time.time()+10:
                    raise ValueError('invalid source or timestamp')
                if not isinstance(event['features'],dict) or len(event['features'])>32:
                    raise ValueError('invalid features')
                groups[role][int(stamp)]=dict(event,t=int(stamp))
            accepted=0
            for role,rows in groups.items():
                key='venue3d:'+role
                old=db.db.execute('SELECT sequence FROM imports WHERE key=?',(key,)).fetchone()
                batch=[e for t,e in sorted(rows.items()) if not old or t>old[0]]
                if batch and db.import_recording(key,batch,epoch=0,sequence=batch[-1]['t']):
                    accepted+=len(batch)
            count=db.db.execute('SELECT count(*) FROM trajectory').fetchone()[0]
            return {'ok':True,'example':True,'accepted':accepted,'points':count}
        finally:
            db.close()


def handle(handler, mutation=False):
    try:
        if mutation:
            host=urlsplit('http://'+handler.headers.get('Host',''))
            origin=urlsplit(handler.headers.get('Origin',''))
            if origin.scheme!='http' or origin.netloc!=host.netloc:
                handler._json({'error':'same-origin request required'},403);return
            length=int(handler.headers.get('Content-Length','0'))
            if not 0<length<=65536:
                raise ValueError('invalid body size')
            body=json.loads(handler.rfile.read(length))
            if set(body)!={'example','events'} or body['example'] is not True:
                raise ValueError('explicit example flag required')
            data=request(handler.page_dir,body['events'])
        else:
            data=request(handler.page_dir)
        handler._json(data)
    except (ValueError,TypeError,KeyError) as exc:
        handler._json({'error':str(exc)},400)
    except Exception as exc:
        handler._json({'error':f'history not saved: {type(exc).__name__}: {exc}'},503)
