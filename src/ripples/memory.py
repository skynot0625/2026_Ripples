"""Bounded spatial sufficient statistics; raw depth/audio is never copied here.

Each cell/channel first averages measurements within one second. Welford moments
then average those second-bin means, so a 50 Hz sensor does not get five times
the weight of a 10 Hz sensor. Missing features never become zeros.
"""
from collections import defaultdict
import json
from pathlib import Path
import shutil
import sqlite3
import time

import numpy as np
from ripples.sensors import FEATURES

GRID = 24
LIMIT_MIB = 256


def cell_xy(xy):
    xy = np.asarray(xy, float)
    if xy.shape != (2,) or not np.isfinite(xy).all() or np.any(xy < -5) or np.any(xy > 5):
        return None
    return tuple(int(v) for v in np.minimum(((xy+5)/10*GRID).astype(int), GRID-1))


class SpatialMemory:
    def __init__(self, path, *, namespace="demo", retain_days=30):
        if namespace not in ("demo", "measured", "estimated") or retain_days < 1:
            raise ValueError("invalid memory settings")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA journal_size_limit=8388608")
        page = self.db.execute("PRAGMA page_size").fetchone()[0]
        self.db.execute(f"PRAGMA max_page_count={LIMIT_MIB*1024**2//page}")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS moments (
            day INTEGER, source TEXT, x INTEGER, y INTEGER, feature TEXT,
            n INTEGER NOT NULL, mean REAL NOT NULL, m2 REAL NOT NULL,
            PRIMARY KEY(day,source,x,y,feature));
          CREATE TABLE IF NOT EXISTS imports (key TEXT PRIMARY KEY, imported REAL NOT NULL, sequence INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS trajectory (
            source TEXT NOT NULL, second INTEGER NOT NULL, x REAL NOT NULL, y REAL NOT NULL,
            PRIMARY KEY(source,second)) WITHOUT ROWID;
        """)
        if "end_sequence" not in [r[1] for r in self.db.execute("PRAGMA table_info(imports)")]:
            self.db.execute("ALTER TABLE imports ADD COLUMN end_sequence INTEGER NOT NULL DEFAULT -1")
        spec = json.dumps([1, namespace, GRID, [-5, 5], FEATURES])
        old = self.db.execute("SELECT value FROM settings WHERE key='schema'").fetchone()
        if old and old[0] != spec:
            previous = json.loads(old[0])
            current = json.loads(spec)
            # Adding frequency bands does not reinterpret or erase previous moments.
            if previous[:4] != current[:4] or not set(previous[4]).issubset(FEATURES):
                self.db.close()
                raise ValueError("memory calibration/namespace mismatch; use a separate database")
            with self.db:
                self.db.execute("UPDATE settings SET value=? WHERE key='schema'", (spec,))
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('schema',?)", (spec,))
        self.retain_days = retain_days
        self.namespace = namespace

    def close(self):
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.db.close()

    def import_recording(self, key, events, *, epoch=None, sequence=-1, end_sequence=-1):
        """Atomic import, including replay deduplication. Events may contain real epoch t.

        File recordings use a stable content ID. Continuous receivers should submit
        completed recording batches with stable IDs. Partial transactions roll back.
        """
        previous = self.db.execute("SELECT sequence,end_sequence FROM imports WHERE key=?", (key,)).fetchone()
        if previous and (sequence < 0 or sequence <= previous[0]):
            return False
        if previous and previous[1] >= 0 and sequence != previous[1]:
            raise ValueError(f"audio session gap/overlap: expected start_frame {previous[1]}, got {sequence}")
        if shutil.disk_usage(self.path.parent).free < 512*1024**2:
            raise RuntimeError("less than 512 MiB free; spatial import stopped without deleting data")
        if not previous and self.db.execute("SELECT count(*) FROM imports").fetchone()[0] >= 10000:
            raise RuntimeError("10,000 recording limit reached; consolidate batches before importing more")
        epoch = time.time() if epoch is None else epoch
        buckets = defaultdict(list)
        positions = defaultdict(list)
        for event in events:
            cell = cell_xy(event["xy"])
            if cell is None:
                continue
            source = event["source"]
            if source not in ("hyeol", "maek"):
                raise ValueError("unknown sensor source")
            second = int(epoch+event["t"])
            position = event.get('_track_xy', event['xy'])
            if cell_xy(position) is None:
                raise ValueError('invalid trajectory position')
            positions[(source,second)].append(position)
            for feature, value in event["features"].items():
                if feature not in FEATURES or not np.isfinite(value):
                    raise ValueError("unknown or nonfinite feature")
                buckets[(second, source, *cell, feature)].append(float(value))
        now_day = int(time.time()//86400)
        try:
            with self.db:
                self.db.executemany('INSERT OR IGNORE INTO trajectory VALUES (?,?,?,?)',
                    [(source,second,*np.mean(xy,axis=0).tolist()) for (source,second),xy in positions.items()])
                for (second, source, x, y, feature), values in buckets.items():
                    value = float(np.mean(values))
                    # day=-1 is lifetime memory, never pruned with the daily layer.
                    for day in (-1, second//86400):
                        identity = day, source, x, y, feature
                        old = self.db.execute("SELECT n,mean,m2 FROM moments WHERE day=? AND source=? AND x=? AND y=? AND feature=?", identity).fetchone()
                        if old:
                            n, mean, m2 = old
                            new_mean = mean+(value-mean)/(n+1)
                            self.db.execute("UPDATE moments SET n=?,mean=?,m2=? WHERE day=? AND source=? AND x=? AND y=? AND feature=?",
                                            (n+1, new_mean, m2+(value-mean)*(value-new_mean), *identity))
                        else:
                            self.db.execute("INSERT INTO moments VALUES (?,?,?,?,?,?,?,?)", (*identity, 1, value, 0.))
                self.db.execute("DELETE FROM moments WHERE day >= 0 AND day < ?", (now_day-self.retain_days+1,))
                self.db.execute("INSERT INTO imports VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET imported=excluded.imported, sequence=excluded.sequence, end_sequence=excluded.end_sequence", (key, time.time(), sequence, end_sequence))
        except sqlite3.DatabaseError as exc:
            raise RuntimeError("spatial import rolled back (database full or write failure)") from exc
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return True

    def snapshot(self):
        return self._snapshot(self.db)

    def history(self, max_points=9600):
        """Full history remains on disk; only this display response is decimated.

        Fourth column is a continuity segment ID, computed BEFORE decimation.
        Never connect across missing acquisition intervals after time compression.
        """
        if not 4 <= max_points <= 9600:
            raise ValueError('history display budget must be 4..9600')
        count, low, high = self.db.execute('SELECT count(*),min(second),max(second) FROM trajectory').fetchone()
        tracks = {'hyeol':[], 'maek':[]}
        stride = max(1, int(np.ceil(((high or 0)-(low or 0)+1)/(max_points-2))))
        rows = self.db.execute('''WITH previous AS (
          SELECT *,lag(second) OVER(PARTITION BY source ORDER BY second) AS prev FROM trajectory
        ), segmented AS (
          SELECT *,sum(CASE WHEN prev IS NULL OR second-prev>3 THEN 1 ELSE 0 END)
            OVER(PARTITION BY source ORDER BY second) AS segment FROM previous
        ), sampled AS (
          SELECT *,row_number() OVER(PARTITION BY source,cast((second-?)/? AS INTEGER) ORDER BY second DESC) AS rn,
            min(second) OVER(PARTITION BY source) AS first FROM segmented
        ) SELECT source,second,x,y,segment FROM sampled WHERE rn=1 OR second=first ORDER BY source,second''',(low or 0,stride))
        for role, second, x, y, segment in rows:
            tracks[role].append([second,x,y,segment])
        return {'schema':'ripples.sleep-replay/1','example':self.namespace=='demo',
                'replay_s':480,'all_s':120,'tracks':tracks,'snapshot':self.snapshot(),
                'storage':{'points':count,'first_second':low,'last_second':high,
                           'duration_s':0 if low is None else high-low,
                           'display_sample_s':stride,'full_history_retained':True,
                           'database_bytes':self.path.stat().st_size,'limit_MiB':LIMIT_MIB}}

    @classmethod
    def read_snapshot(cls, path):
        """Existing preview statistics only; never create/migrate/update a DB."""
        db = sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True)
        try:
            return cls._snapshot(db)
        finally:
            db.close()

    @staticmethod
    def _snapshot(db):
        groups = {}
        for source, x, y, feature, n, mean, m2 in db.execute(
                "SELECT source,x,y,feature,n,mean,m2 FROM moments WHERE day=-1"):
            entry = groups.setdefault((source, x, y), {"source": source,
                "xy": [((x+.5)/GRID)*10-5, ((y+.5)/GRID)*10-5], "features": {}, "counts": {}, "std": {}})
            entry["features"][feature] = mean
            entry["counts"][feature] = n
            # Sample std of the observed one-second cell means, not raw ADC noise.
            entry["std"][feature] = float(np.sqrt(max(0., m2)/(n-1))) if n > 1 else 0.
        return list(groups.values())

    def report(self):
        n = self.db.execute("SELECT count(*) FROM moments").fetchone()[0]
        return {"namespace": self.namespace, "grid": [GRID, GRID],
                "daily_retention_days": self.retain_days, "lifetime_memory_retained": True,
                "rows": n, "database_bytes": self.path.stat().st_size,
                "database_limit_MiB": LIMIT_MIB, "import_limit": 10000,
                "free_disk_GiB": shutil.disk_usage(self.path.parent).free/2**30,
                "raw_wav_GB_per_day_if_retained": 44100*3*2*86400/1e9,
                "raw_tof_GB_per_day_at_10Hz_if_retained": 240*180*4*10*86400/1e9}
