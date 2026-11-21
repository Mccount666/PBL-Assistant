#!/usr/bin/env python3
"""每日备份：SQLite（在线 .backup）+ reports/media 打包，保留 14 天。"""
import sqlite3
import tarfile
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
BK = APP / "backups"
BK.mkdir(exist_ok=True)
ts = time.strftime("%Y%m%d_%H%M%S")

db = APP / "data" / "voice.sqlite"
if db.exists():
    dst = BK / f"voice_{ts}.sqlite"
    src = sqlite3.connect(str(db))
    dst_conn = sqlite3.connect(str(dst))
    with dst_conn:
        src.backup(dst_conn)
    dst_conn.close()
    src.close()

targets = [p for p in (APP / "reports", APP / "data" / "media")
           if p.exists() and any(p.iterdir())]
if targets:
    with tarfile.open(BK / f"files_{ts}.tar.gz", "w:gz") as t:
        for p in targets:
            t.add(p, arcname=p.name)

cutoff = time.time() - 14 * 86400
for f in BK.iterdir():
    if f.is_file() and f.stat().st_mtime < cutoff:
        f.unlink()
print("backup done", ts)
