#!/usr/bin/env python3
"""Capture a disc for TheDiscDb without ripping it - for discs ripped before captures existed.

    docker exec -u root arm-rippers python3 /opt/arm/scripts/local/discdb_rescan.py /dev/srN [minlength]

PAUSE ARM FIRST (see reference_new_discs_arrive_dirty in memory: move 51-docker-arm.rules aside and
reload udev) or ARM starts a rip of the disc you insert. Writes the same capture ARM writes during
identify, under job id manual-<timestamp>; rip-pipeline's discdb pass labels it from an EXPLICIT
mapping (sequential mappings need the rip, so those come out unlabelled). minlength must be the one
the disc was ripped with: the show's ripMinSeconds, else ARM's MINLENGTH (600).
Root is needed only to mount; the capture files are handed to arm (1001) afterwards.
"""
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import types

sys.path.insert(0, "/opt/arm")
from arm.ripper import discdb_capture  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
if len(sys.argv) < 2:
    sys.exit(__doc__)
devpath = sys.argv[1]
minlength = sys.argv[2] if len(sys.argv) > 2 else "600"

label = subprocess.run(["blkid", "-o", "value", "-s", "LABEL", devpath],
                       capture_output=True, text=True).stdout.strip()
if not label:
    sys.exit(f"{devpath}: no disc, or no volume label")
mnt = tempfile.mkdtemp(prefix="discdb-")
subprocess.run(["mount", "-o", "ro", devpath, mnt], check=True)
try:
    fmt = "bluray" if os.path.isdir(os.path.join(mnt, "BDMV")) else \
        "dvd" if os.path.isdir(os.path.join(mnt, "VIDEO_TS")) else None
    if not fmt:
        sys.exit(f"{label}: neither BDMV nor VIDEO_TS - not a video disc")
    job = types.SimpleNamespace(job_id="manual-" + time.strftime("%Y%m%d%H%M%S"))
    d = discdb_capture.capture(job, mnt, devpath, fmt, label, (minlength, ""))
finally:
    subprocess.run(["umount", mnt])
    os.rmdir(mnt)
subprocess.run(["chown", "-R", "1001:1001", d])
meta = json.load(open(os.path.join(d, "capture.json")))
state = "ALREADY IN TheDiscDb" if meta["in_thediscdb"] else "not in TheDiscDb - the pipeline will package it"
print(f"{label}: {d}\n  hash {meta['content_hash']} - {state}")
