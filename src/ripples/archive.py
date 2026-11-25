"""Headless live sensor archival. No LED or audio device is opened."""
import json
import os
import time
from pathlib import Path

from ripples.paths import ROOT,RUNTIME_DIR
from ripples.realtime import LiveMemory,LiveSensors,validate_config,position_enabled,memory_namespace


def main():
    import msvcrt
    RUNTIME_DIR.mkdir(exist_ok=True)
    lock=(RUNTIME_DIR/'history-live.lock').open('a+b')
    if lock.tell()==0:lock.write(b'0');lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:
        return  # Another archiver already owns this stream.
    (RUNTIME_DIR/'history-live.pid').write_text(str(os.getpid()))
    stop=RUNTIME_DIR/'history-live.stop'
    config=validate_config(json.loads((ROOT/'configs/live.m30.json').read_text(encoding='utf-8-sig')))
    config['input_modes']=['monitor','levels']
    if not position_enabled(config):
        raise RuntimeError('Position mapping not enabled; do not invent a path')
    path=RUNTIME_DIR/('history-live-'+memory_namespace(config)+'.sqlite3')
    while not stop.exists():
        memory=sensors=None
        try:
            memory=LiveMemory(path,config);sensors=LiveSensors(config,memory)
            sensors.start_center('http://127.0.0.1:8000')
            while not stop.exists():
                if memory.error:raise RuntimeError(memory.error)
                status=sensors.report()
                status.update(database=str(path),example=False,namespace=memory_namespace(config),
                              updated=time.time(),history=memory.history.get('storage',{}) if memory.history else {})
                tmp=RUNTIME_DIR/'history-live-status.tmp'
                tmp.write_text(json.dumps(status,ensure_ascii=False,indent=2),encoding='utf-8')
                tmp.replace(RUNTIME_DIR/'history-live-status.json')
                # No renderer consumes live events in this archival-only process.
                sensors.drain()
                for _ in range(10):
                    if stop.exists():break
                    time.sleep(.5)
        except Exception as exc:
            (RUNTIME_DIR/'history-live-status.json').write_text(json.dumps({'error':str(exc),'updated':time.time()}),encoding='utf-8')
            print('History archive error:',exc,flush=True)
        finally:
            if sensors:sensors.close()
            if memory:memory.close()
        for _ in range(20):
            if stop.exists():break
            time.sleep(.5)
    lock.close()


if __name__=='__main__':
    main()
