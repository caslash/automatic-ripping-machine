#!/usr/bin/env python3
"""Hold a movie disc whose longest titles tie on length but not on size.

Disney (and others) ship one film as several playlists that share almost all of their
video and swap in short language-specific segments: on-screen text, production credits.
They have identical length and chapters, so ARM's length-first main-feature pick cannot
tell them apart and falls through to track_number -- Incredibles 2 UHD ripped its Spanish
copy that way. Same length AND same size is a harmless duplicate playlist (9 of 10 discs
so far); only a size difference means branching.

Rather than guess, drop a hold for riptrack (credit frames + a pick key), wait as long as
it takes for a human pick, and rip exactly that title.

Lives in the repo (arm/ripper/branch_hold.py); formerly deployed on the bind mount so it survived container
rebuilds. The hook that calls it lives in /opt/arm (image) and does NOT survive -- re-run
apply_branch_hold_patch.py after every rebuild, after apply_mainfeature_fix.py.
"""
import fcntl, glob, json, logging, os, re, secrets, shutil, signal, struct, subprocess, tempfile, time

HOLDS_DIR = os.environ.get("ARM_BRANCH_HOLDS_DIR", "/home/arm/media/riptrack/holds")
LENGTH_EPSILON = 2            # seconds; same epsilon the mover uses for duplicate playlists
POLL_SECONDS = 15
DISC_MISSES = 3               # consecutive 'gone' polls before deciding the disc is gone
LETTERS = "ABCDEFGH"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
INFO_TIMEOUT = 20 * 60
GRAB_TIMEOUT = 60 * 60

# Which language sections to grab frames from. A bluray: input cannot seek -- -ss before
# -i returns an empty output -- so every second of window costs a second of disc read,
# on top of ~4 minutes just to open a UHD. Production credits and title cards sit near
# the start, so take the earliest few differing sections and stop.
EARLY_SECTION_LIMIT = 600     # only sections starting in the first 10 minutes...
WINDOW_BUDGET = 90            # ...until this many seconds of window are collected
MAX_WINDOWS = 3
MAX_WINDOW_SECONDS = 60
FRAMES_PER_COPY = 3
MIN_VISIBLE_DIFF = 2.0        # mean |delta| on a 64x36 gray thumbnail, 0-255
THUMB_W, THUMB_H = 64, 36


# --- entry point from makemkv.py ---------------------------------------------------------
def resolve(job, top):
    """Return the Track to rip. `top` is what the length-first sort picked."""
    from arm.models import Track
    from arm.ui import db

    rows = Track.query.filter_by(job_id=job.job_id).all()
    db.session.commit()       # end the read transaction before a wait that can last days
    tied = tied_titles([(t.track_number, t.filesize, t.length) for t in rows], top.length)
    if len(tied) < 2:
        return top
    if len({size for _, size, _ in tied}) == 1:
        logging.info(f"branch hold: {len(tied)} titles tie at {top.length}s and are byte-identical: "
                     f"duplicate playlists, no hold")
        return top

    sizes = ", ".join(f"#{n}={size}" for n, size, _ in tied)
    logging.warning(f"branch hold: {len(tied)} titles tie at {top.length}s but differ in size ({sizes}): "
                    f"language branches, holding for a human pick")

    def set_status(value):
        job.status = value
        db.session.commit()

    number = hold_and_wait(job, tied, set_status=set_status)
    return next(t for t in rows if str(t.track_number) == str(number))


def tied_titles(rows, top_length):
    """[(track_number, filesize, length)] within LENGTH_EPSILON of the longest, by number."""
    tied = [r for r in rows if r[2] is not None and r[2] >= top_length - LENGTH_EPSILON]
    return sorted(tied, key=lambda r: int(r[0]))


# --- the hold ------------------------------------------------------------------------------
def hold_and_wait(job, tied, set_status=None, selftest=False):
    """Write the hold, build the credit sheet, block until riptrack writes a decision.
    Returns the chosen track_number; raises on abandon or if the disc is removed."""
    RipperException = _ripper_exception()
    hid = f"job{job.job_id}"
    hdir = os.path.join(HOLDS_DIR, hid)
    shutil.rmtree(hdir, ignore_errors=True)
    os.makedirs(hdir)

    branches = [{"letter": LETTERS[i], "track_number": str(n), "size": int(size or 0),
                 "length": int(length or 0), "mpls": None}
                for i, (n, size, length) in enumerate(tied[:len(LETTERS)])]
    hold = {"job_id": job.job_id, "label": job.label, "title": job.title, "year": getattr(job, "year", None),
            "devpath": job.devpath, "created": time.time(), "key": secrets.token_urlsafe(18),
            "branches": branches, "sheet": "pending", "sheet_detail": None, "selftest": selftest}
    if set_status:
        set_status("waiting")                      # JobState.VIDEO_WAITING
    _write(hdir, "hold.json", hold)                # visible at once; riptrack pushes after the sheet

    try:
        mapping, height = map_playlists(job, branches)
        for b in branches:
            b["mpls"] = mapping[b["track_number"]]
        _write(hdir, "hold.json", hold)
        windows = language_windows(mountpoint(job), [b["mpls"] for b in branches])
        note = grab_sheet(job, branches, windows, height, os.path.join(hdir, "sheet.jpg"))
        hold["sheet"] = "ready"
        hold["sheet_detail"] = note or (f"frames from {len(windows)} language section(s) "
                                        f"between {windows[0][0]:.0f}s and {windows[-1][1]:.0f}s")
    except Exception as e:
        logging.exception("branch hold: could not build the credit sheet")
        hold["sheet"] = "failed"
        hold["sheet_detail"] = str(e)[:300]
    _write(hdir, "hold.json", hold)
    logging.info(f"branch hold {hid}: waiting for a pick (sheet {hold['sheet']}: {hold['sheet_detail']})")

    misses, last_note = 0, time.time()
    while True:
        decision = _read(hdir, "decision.json")
        if decision:
            break
        if not disc_gone(job.devpath, job.label):
            misses = 0
        else:
            misses += 1
            if misses >= DISC_MISSES:
                _write(hdir, "done.json", {"result": "disc_removed", "at": time.time()})
                raise RipperException("Branch hold: the disc left the drive before a copy was picked")
        if time.time() - last_note >= 3600:
            logging.info(f"branch hold {hid}: still waiting, "
                         f"{(time.time() - hold['created']) / 3600:.1f} h so far")
            last_note = time.time()
        time.sleep(POLL_SECONDS)

    choice = decision.get("choice")
    if choice == "abandon":
        _write(hdir, "done.json", {"result": "abandoned", "at": time.time()})
        raise RipperException("Branch hold abandoned from the pick page")
    branch = next((b for b in branches if b["letter"] == choice), None)
    if branch is None:
        _write(hdir, "done.json", {"result": "failed", "at": time.time(), "detail": f"unknown choice {choice!r}"})
        raise RipperException(f"Branch hold: unknown choice {choice!r}")

    _write(hdir, "done.json", {"result": "selftest_picked" if selftest else "ripping", "choice": choice,
                               "track_number": branch["track_number"], "mpls": branch["mpls"],
                               "at": time.time()})
    if set_status:
        set_status("ripping")                      # JobState.VIDEO_RIPPING
    logging.info(f"branch hold {hid}: picked {choice} = title {branch['track_number']} ({branch['mpls']})")
    return branch["track_number"]


# --- which playlist is which title ---------------------------------------------------------
_TINFO = re.compile(r'^TINFO:(\d+),(\d+),\d+,"(.*)"$')
_SINFO = re.compile(r'^SINFO:(\d+),(\d+),(\d+),\d+,"(.*)"$')


def map_playlists(job, branches):
    """{track_number: 'NNNNN.mpls'} plus the video height, from a fresh info pass.

    ARM's Track rows do not keep the source playlist. The numbering depends on the
    arguments (a run without --minlength numbered this disc differently), so use exactly
    the ones rip_mainfeature will use, and refuse to map if any size disagrees with ARM's
    own info pass.

    --noscan: without it makemkvcon scans every drive first, which freezes a rip in
    progress on another drive (see the --noscan commit)."""
    cmd = ["makemkvcon", "--robot", "--messages=-stdout", "--noscan", "info", "--cache=1",
           f"dev:{job.devpath}", f"--minlength={job.config.MINLENGTH}"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=INFO_TIMEOUT).stdout
    titles, height = {}, 0
    for line in out.splitlines():
        m = _TINFO.match(line)
        if m:
            titles.setdefault(int(m.group(1)), {})[int(m.group(2))] = m.group(3)
            continue
        m = _SINFO.match(line)
        if m and m.group(3) == "19":
            dims = re.match(r"^(\d+)x(\d+)$", m.group(4))
            if dims:
                height = max(height, int(dims.group(2)))
    mapping = {}
    for b in branches:
        t = titles.get(int(b["track_number"])) or {}
        size = t.get(11)
        if not t.get(16) or size is None or int(size) != b["size"]:
            raise RuntimeError(f"MakeMKV title {b['track_number']} does not match ARM's info pass "
                               f"(size {size} vs {b['size']}); refusing to guess its playlist")
        mapping[b["track_number"]] = t[16]
    return mapping, height


# --- where the copies differ -------------------------------------------------------------
def mountpoint(job):
    mp = getattr(job, "mountpoint", None) or f"/mnt{job.devpath}"
    if not os.path.isdir(os.path.join(mp, "BDMV", "PLAYLIST")):
        subprocess.run(["mount", job.devpath], capture_output=True, timeout=120)
    if not os.path.isdir(os.path.join(mp, "BDMV", "PLAYLIST")):
        raise RuntimeError(f"disc is not mounted at {mp}")
    return mp


def playitems(path):
    """[(clip, in_seconds, out_seconds)] from an MPLS file (these are not encrypted)."""
    with open(path, "rb") as f:
        d = f.read()
    playlist = struct.unpack(">I", d[8:12])[0]
    count = struct.unpack(">H", d[playlist + 6:playlist + 8])[0]
    p, items = playlist + 10, []
    for _ in range(count):
        length = struct.unpack(">H", d[p:p + 2])[0]
        clip = d[p + 2:p + 7].decode("ascii", "replace")
        t_in, t_out = struct.unpack(">II", d[p + 14:p + 22])
        items.append((clip, t_in / 45000, t_out / 45000))
        p += 2 + length
    return items


def language_windows(mp, mpls_names):
    """[(start, end)] seconds of the earliest sections where the copies use different clips."""
    lists = [playitems(os.path.join(mp, "BDMV", "PLAYLIST", name)) for name in mpls_names]
    if len({len(l) for l in lists}) != 1:
        raise RuntimeError("the playlists have different item counts; not a simple language branch")
    sections, t = [], 0.0
    for row in zip(*lists):
        dur = row[0][2] - row[0][1]
        if len({clip for clip, _, _ in row}) > 1:
            sections.append((t, t + dur))
        t += dur
    if not sections:
        raise RuntimeError("the playlists share every clip; there is nothing different to show")
    early = [s for s in sections if s[0] < EARLY_SECTION_LIMIT] or sections[:1]
    picked, total = [], 0.0
    for start, end in early:
        end = min(end, start + MAX_WINDOW_SECONDS)
        picked.append((start, end))
        total += end - start
        if total >= WINDOW_BUDGET or len(picked) >= MAX_WINDOWS:
            break
    return picked


# --- the credit sheet ------------------------------------------------------------------------
def grab_sheet(job, branches, windows, height, out_path):
    """One ffmpeg pass over every copy (opening a UHD costs minutes, so open each once),
    then keep the frames where the copies differ most and stack them one row per copy.
    Returns a note for the page, or None."""
    tmp = tempfile.mkdtemp(prefix="branchhold-")
    try:
        mp = mountpoint(job)
        read_until = f"{windows[-1][1] + 2:.0f}"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
        for b in branches:
            cmd += ["-skip_frame", "nokey", "-t", read_until,
                    "-playlist", str(int(b["mpls"].split(".")[0])), "-i", f"bluray:{mp}"]
        # HDR frames tone-mapped by nothing come out dim; lift them so text reads on a phone.
        lift = ",eq=brightness=0.06:contrast=1.3" if height >= 2000 else ""
        graph, outputs = [], []
        for i in range(len(branches)):
            # trim+fps per window gives every copy the exact same frame count, so frame N
            # of copy A lines up with frame N of copy B without trusting timestamps.
            segs = []
            graph.append(f"[{i}:v:0]split={len(windows)}" + "".join(f"[w{i}_{w}]" for w in range(len(windows))))
            for w, (start, end) in enumerate(windows):
                graph.append(f"[w{i}_{w}]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS,fps=1[c{i}_{w}]")
                segs.append(f"[c{i}_{w}]")
            graph.append("".join(segs) + f"concat=n={len(windows)}:v=1:a=0,split=2[g{i}][j{i}]")
            graph.append(f"[g{i}]scale={THUMB_W}:{THUMB_H},format=gray[gg{i}]")
            graph.append(f"[j{i}]scale=480:-2{lift}[jj{i}]")
            outputs += ["-map", f"[gg{i}]", "-f", "image2", os.path.join(tmp, f"g{i}_%04d.pgm"),
                        "-map", f"[jj{i}]", "-q:v", "3", "-f", "image2", os.path.join(tmp, f"j{i}_%04d.jpg")]
        cmd += ["-filter_complex", ";".join(graph)] + outputs

        env = dict(os.environ, LIBAACS_PATH="libmmbd", LIBBDPLUS_PATH="libmmbd")
        before = _guiservers()
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=GRAB_TIMEOUT, env=env)
        finally:
            # libmmbd leaves `makemkvcon guiserver` helpers behind; only kill the ones we started.
            for pid in _guiservers() - before:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
        count = min(len(glob.glob(os.path.join(tmp, f"g{i}_*.pgm"))) for i in range(len(branches)))
        if count == 0:
            raise RuntimeError(f"ffmpeg produced no frames (exit {r.returncode}): {r.stderr.strip()[-300:]}")

        thumbs = [[_pgm(os.path.join(tmp, f"g{i}_{f:04d}.pgm")) for f in range(1, count + 1)]
                  for i in range(len(branches))]
        chosen, note = pick_frames(thumbs, count)
        compose_sheet(tmp, branches, chosen, out_path)
        return note
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def pick_frames(thumbs, count):
    """Frame indexes where the copies differ most, earliest first."""
    scores = []
    for f in range(count):
        px = [copy[f] for copy in thumbs]
        total, pairs = 0.0, 0
        for a in range(len(px)):
            for b in range(a + 1, len(px)):
                total += sum(abs(x - y) for x, y in zip(px[a], px[b])) / len(px[a])
                pairs += 1
        scores.append(total / pairs)
    chosen = []
    for f in sorted(range(count), key=lambda i: scores[i], reverse=True):
        if scores[f] < MIN_VISIBLE_DIFF:
            break
        if all(abs(f - c) >= 3 for c in chosen):
            chosen.append(f)
        if len(chosen) == FRAMES_PER_COPY:
            break
    if chosen:
        return sorted(chosen), None
    step = (count - 1) / max(1, FRAMES_PER_COPY - 1)
    evenly = sorted({round(i * step) for i in range(FRAMES_PER_COPY)})
    return evenly, "no visible difference in the grabbed frames; these are evenly spaced samples"


def compose_sheet(tmp, branches, chosen, out_path):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    graph, rows, n = [], [], 0
    for i, b in enumerate(branches):
        tiles = []
        for f in chosen:
            cmd += ["-i", os.path.join(tmp, f"j{i}_{f + 1:04d}.jpg")]
            tiles.append(f"[{n}:v]")
            n += 1
        stack = "".join(tiles) + (f"hstack=inputs={len(tiles)}" if len(tiles) > 1 else "null")
        graph.append(f"{stack},pad=iw:ih+6:0:0:black,drawtext=fontfile={FONT}:text={b['letter']}"
                     f":x=14:y=10:fontsize=44:fontcolor=white:box=1:boxcolor=black@0.75:boxborderw=10[r{i}]")
        rows.append(f"[r{i}]")
    graph.append("".join(rows) + f"vstack=inputs={len(rows)}[out]")
    part = out_path + ".part.jpg"
    cmd += ["-filter_complex", ";".join(graph), "-map", "[out]", "-q:v", "4", "-frames:v", "1", part]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or not os.path.exists(part):
        raise RuntimeError(f"sheet compose failed: {r.stderr.strip()[-300:]}")
    os.replace(part, out_path)


# --- small helpers ----------------------------------------------------------------------------
# linux/cdrom.h
CDROM_DRIVE_STATUS, CDSL_CURRENT = 0x5326, 0x7fffffff
CDS_NO_DISC, CDS_TRAY_OPEN, CDS_DISC_OK = 1, 2, 4


def drive_status(devpath):
    try:
        fd = os.open(devpath, os.O_RDONLY | os.O_NONBLOCK)
        try:
            return fcntl.ioctl(fd, CDROM_DRIVE_STATUS, CDSL_CURRENT)
        finally:
            os.close(fd)
    except OSError:
        return None


def disc_gone(devpath, label):
    """True only on real evidence: the tray is open or empty, or a DIFFERENT disc is in.
    A blank label is not evidence -- right after a MakeMKV/libmmbd session the udev label
    went blank for 30+ s with the disc still in (Eternals and Spider-Man 3, 2026-09-26)."""
    status = drive_status(devpath)
    if status in (CDS_NO_DISC, CDS_TRAY_OPEN):
        return True
    if status == CDS_DISC_OK:
        live = disc_label(devpath)
        return live is not None and live != label
    return False                  # not ready / no info / error: keep waiting


def disc_label(devpath):
    try:
        r = subprocess.run(["lsblk", "-no", "LABEL", devpath], capture_output=True, text=True, timeout=20)
        return r.stdout.strip() or None
    except Exception:
        return None


def _guiservers():
    try:
        r = subprocess.run(["pgrep", "-f", "makemkvcon guiserver"], capture_output=True, text=True, timeout=10)
        return {int(p) for p in r.stdout.split()}
    except Exception:
        return set()


def _pgm(path):
    with open(path, "rb") as f:
        raw = f.read()
    return raw[-(THUMB_W * THUMB_H):]


def _read(hdir, name):
    try:
        with open(os.path.join(hdir, name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write(hdir, name, obj):
    p = os.path.join(hdir, name)
    with open(p + ".tmp", "w") as f:
        json.dump(obj, f)
    os.replace(p + ".tmp", p)


def _ripper_exception():
    try:
        from arm.ripper.utils import RipperException
        return RipperException
    except Exception:          # the selftest runs without ARM's app context
        return RuntimeError
