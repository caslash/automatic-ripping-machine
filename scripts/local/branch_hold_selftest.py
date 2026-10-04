#!/usr/bin/env python3
"""Exercise branch_hold end to end against a disc in a drive, without ARM and without ripping.

    docker exec -u arm arm-rippers python3 /opt/arm/scripts/local/branch_hold_selftest.py <arm_job_id> /dev/srN [minlength]

Borrows the Track rows of an earlier ARM job for the SAME disc (they carry the sizes the
playlist mapping is checked against), writes a real hold under job9<arm_job_id padded>,
and blocks until a pick arrives from the riptrack page -- so it tests the push, the page
and the decision file too. Nothing is ripped and ARM's database is only read.
"""
import logging, sqlite3, sys, types

sys.path.insert(0, "/opt/arm")
from arm.ripper import branch_hold

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

src_job, devpath = int(sys.argv[1]), sys.argv[2]
minlength = int(sys.argv[3]) if len(sys.argv) > 3 else 600

con = sqlite3.connect("file:/home/arm/db/arm.db?mode=ro", uri=True)
rows = con.execute("SELECT track_number, filesize, length FROM track WHERE job_id=?", (src_job,)).fetchall()
label = con.execute("SELECT label FROM job WHERE job_id=?", (src_job,)).fetchone()[0]
con.close()
if not rows:
    sys.exit(f"job {src_job} has no tracks")

live = branch_hold.disc_label(devpath)
if live != label:
    sys.exit(f"{devpath} holds {live!r}, but job {src_job} was {label!r}; insert the same disc")

top = max(r[2] for r in rows)
tied = branch_hold.tied_titles(rows, top)
print("tied titles:", tied)
if len(tied) < 2 or len({r[1] for r in tied}) == 1:
    sys.exit("no size-differing tie on this job; branch_hold would not hold it")

job = types.SimpleNamespace(job_id=900000 + src_job, label=label, title=f"SELFTEST {label}", year=None,
                            devpath=devpath, mountpoint=f"/mnt{devpath}",
                            config=types.SimpleNamespace(MINLENGTH=minlength))
picked = branch_hold.hold_and_wait(job, tied, selftest=True)
print("PICKED track_number:", picked)
