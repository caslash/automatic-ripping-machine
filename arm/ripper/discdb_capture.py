"""Capture what TheDiscDb needs to catalogue a disc, while ARM has it mounted.

Called from arm/ripper/identify.py after show_override (so the per-job MINLENGTH/MAXLENGTH are
final) and before the unmount. Writes /home/arm/rip/discdb/captures/<job_id>_<LABEL>/:

  inventory.json   every file on the disc with its size (the ODM file inventory)
  structure.tar.gz the small structure files (MPLS/CLPI/IFO/BUP, index/MovieObject, AACS, META) laid
                   out as on the disc; with the inventory, `thediscdb-scan` can build the .odm.json
                   later from this copy, with no disc in the drive
  makemkv.txt      `makemkvcon -r info --minlength=0` - the full scan log TheDiscDb asks for (ARM's own
                   info call uses MINLENGTH, which hides short titles)
  capture.json     label, job, lengths, the thediscdb-content-hash, and whether TheDiscDb knows it

The content hash is the ODM recipe (optical-disc-manifest tests/Get-ContentHash.ps1): MD5 over the
sizes of BDMV/STREAM/*.m2ts (DVD: VIDEO_TS/*.VOB|IFO|BUP) sorted by bare file name, each an 8-byte
little-endian integer. A disc TheDiscDb already has (public /api/dischash lookup) skips the MakeMKV
scan, so the common case costs a few seconds.

Never raises: a failure here is logged and the rip carries on. Off with ARM_DISCDB_CAPTURE=0.
"""
import hashlib
import io
import json
import logging
import os
import re
import struct
import subprocess
import tarfile
import time
import urllib.error
import urllib.request

ROOT = os.environ.get("ARM_DISCDB_DIR", "/home/arm/rip/discdb/captures")
LOOKUP = os.environ.get("ARM_DISCDB_LOOKUP", "https://thediscdb.com/api/dischash/")
MAKEMKV_TIMEOUT = int(os.environ.get("ARM_DISCDB_MAKEMKV_TIMEOUT", "900"))

_STRUCT_EXT = {".mpls", ".clpi", ".bdmv", ".ifo", ".bup", ".xml", ".inf", ".cer", ".tbl", ".bin"}
_STRUCT_MAX = 16 * 2**20      # per file; anything bigger is listed in the inventory only


def _safe(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s or "NOLABEL").strip("_") or "NOLABEL"


def inventory(mount):
    """[(relative path with '/', size)] for every file, sorted."""
    out = []
    for dirpath, _dirs, files in os.walk(mount):
        for f in files:
            full = os.path.join(dirpath, f)
            try:
                out.append((os.path.relpath(full, mount).replace(os.sep, "/"), os.path.getsize(full)))
            except OSError as e:
                logging.warning(f"discdb_capture: cannot stat {full}: {e}")
    return sorted(out)


def content_hash(files, fmt):
    """thediscdb-content-hash from (path, size) pairs; None if nothing hashable."""
    if fmt == "dvd":
        sel = [(p, n) for p, n in files
               if p.upper().startswith("VIDEO_TS/") and os.path.splitext(p)[1].upper() in (".VOB", ".IFO", ".BUP")]
    else:
        sel = [(p, n) for p, n in files
               if p.upper().startswith("BDMV/STREAM/") and not p.upper().startswith("BDMV/BACKUP/")
               and p.lower().endswith(".m2ts")]
    if not sel:
        return None
    # Ordinal (byte-value) sort on the bare file name, as the reference implementation does.
    sel.sort(key=lambda x: os.path.basename(x[0]).encode("utf-8"))
    buf = b"".join(struct.pack("<q", n) for _p, n in sel)
    return hashlib.md5(buf).hexdigest().upper()


def _is_structure(path, size):
    up = path.upper()
    if size > _STRUCT_MAX or "/STREAM/" in up or up.endswith((".M2TS", ".SSIF", ".VOB")):
        return False
    return (up.startswith(("AACS/", "CERTIFICATE/")) or "/META/" in up
            or os.path.splitext(path)[1].lower() in _STRUCT_EXT)


def _write_structure(mount, files, dest):
    with tarfile.open(dest, "w:gz") as tar:
        for p, n in files:
            if _is_structure(p, n):
                try:
                    tar.add(os.path.join(mount, p), arcname=p, recursive=False)
                except OSError as e:
                    logging.warning(f"discdb_capture: cannot read {p}: {e}")


def lookup(chash):
    """True/False when TheDiscDb answers, None when it cannot be asked."""
    try:
        with urllib.request.urlopen(LOOKUP + chash, timeout=15) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        return False if e.code == 404 else None
    except Exception:  # noqa: BLE001 - offline is not an error worth stopping for
        return None


def _makemkv(devpath, dest):
    t0 = time.time()
    with open(dest + ".part", "wb") as fh:
        rc = subprocess.run(["makemkvcon", "-r", "--noscan", "--cache=1", "info", f"dev:{devpath}", "--minlength=0"],
                            stdout=fh, stderr=subprocess.STDOUT, timeout=MAKEMKV_TIMEOUT).returncode
    os.replace(dest + ".part", dest)
    return rc, time.time() - t0


def capture(job, mount, devpath, fmt, label, lengths):
    """Write the capture for one disc. Returns the capture directory."""
    d = os.path.join(ROOT, f"{job.job_id}_{_safe(label)}")
    os.makedirs(d, exist_ok=True)
    files = inventory(mount)
    with open(os.path.join(d, "inventory.json"), "w") as fh:
        json.dump({"format": fmt, "files": [{"path": p, "sizeBytes": n} for p, n in files]}, fh, indent=1)
    _write_structure(mount, files, os.path.join(d, "structure.tar.gz"))
    chash = content_hash(files, fmt)
    known = lookup(chash) if chash else None
    meta = {"job_id": job.job_id, "label": label, "devpath": devpath, "format": fmt,
            "title": getattr(job, "title", None), "year": getattr(job, "year", None),
            "video_type": getattr(job, "video_type", None), "imdb_id": getattr(job, "imdb_id", None),
            "minlength": lengths[0], "maxlength": lengths[1],
            "content_hash": chash, "in_thediscdb": known, "captured": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "files": len(files)}
    if known:
        logging.info(f"discdb_capture: {label} ({chash}) is already in TheDiscDb - no MakeMKV scan needed")
    else:
        try:
            rc, secs = _makemkv(devpath, os.path.join(d, "makemkv.txt"))
            meta["makemkv_rc"], meta["makemkv_seconds"] = rc, round(secs)
            logging.info(f"discdb_capture: full MakeMKV scan of {label} took {secs:.0f}s (rc={rc})")
        except subprocess.TimeoutExpired:
            meta["makemkv_rc"] = "timeout"
            logging.warning(f"discdb_capture: MakeMKV scan of {label} timed out after {MAKEMKV_TIMEOUT}s")
    with open(os.path.join(d, "capture.json"), "w") as fh:
        json.dump(meta, fh, indent=1)
    return d


def apply(job):
    if os.environ.get("ARM_DISCDB_CAPTURE", "1") == "0":
        return
    try:
        mount = getattr(job, "mountpoint", None)
        if job.disctype not in ("dvd", "bluray") or not mount or not os.path.isdir(mount):
            return
        cfg = getattr(job, "config", None)
        lengths = (str(getattr(cfg, "MINLENGTH", "")), str(getattr(cfg, "MAXLENGTH", "")))
        d = capture(job, mount, job.devpath, job.disctype, job.label or "", lengths)
        logging.info(f"discdb_capture: wrote {d}")
    except Exception as e:  # noqa: BLE001 - never stop a rip over this
        logging.warning(f"discdb_capture: skipped ({type(e).__name__}: {e})")
