#!/usr/bin/env python3.11
"""Read-only snapshot of the persistent-history counters + one memo's title.

Used to prove a Core Data write actually emitted a history transaction (which is what
the CloudKit mirroring delegate exports) rather than only changing the row.
"""
import os
import shutil
import sqlite3
import sys
import tempfile

REC = ("/Users/alex/Library/Group Containers/"
       "group.com.apple.VoiceMemos.shared/Recordings/CloudRecordings.db")

uid = sys.argv[1] if len(sys.argv) > 1 else None
tmp = tempfile.mkdtemp()
for suffix in ("", "-wal", "-shm"):
    try:
        shutil.copy2(REC + suffix, os.path.join(tmp, "d.db" + suffix))
    except OSError:
        pass
con = sqlite3.connect(os.path.join(tmp, "d.db"))
one = lambda q, a=(): con.execute(q, a).fetchone()

print("ATRANSACTION:", one("select count(*) from ATRANSACTION")[0],
      " ACHANGE:", one("select count(*) from ACHANGE")[0])
row = one("select ZAUTHORTS, ZTIMESTAMP from ATRANSACTION order by ZTIMESTAMP desc limit 1")
print("latest transaction: authorTS=%s ts=%s" % row)
if uid:
    r = one("select ZENCRYPTEDTITLE from ZCLOUDRECORDING where ZUNIQUEID=?", (uid,))
    print("title:", r[0] if r else "(no row)")
