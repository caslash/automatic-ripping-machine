"""Force ARM to treat a disc as a TV series when its label matches our show registry.

Called from arm/ripper/identify.py right after ARM's own title lookup and before anything acts on video_type.

Why: ARM identifies a disc by searching its LABEL. MARVEL_STUDIOS_WANDAVISION_D1 came back
as the 2023 movie "Lego Marvel Avengers: Code Red" (video_type movie). On a movie, the
MAINFEATURE patch rips ONE title, and the mover's movie route keeps one feature and deletes
the rest - on a TV disc that throws away every episode but one. The registry
(/home/arm/rip/shows.json) already says which labels are TV, so it wins over the lookup.

Never raises: a failure here is logged and ARM continues with its own identification. The
mover independently refuses to take the movie route for a label that matches a show.
"""
import json
import logging
import os
import re

SHOWS = os.environ.get("ARM_SHOWS_JSON", "/home/arm/rip/shows.json")


def _match(label):
    with open(SHOWS) as fh:
        shows = json.load(fh)
    if isinstance(shows, dict):
        shows = [shows]
    for sh in shows:
        pat = sh.get("match")
        if pat and re.search(pat, label):   # .NET patterns in shows.json use (?i) etc - Python-compatible
            return sh
    return None


def _set_lengths(job, sh, label):
    """Per-JOB title length limits from the registry (job.config is this job's own row).

    maxSeconds -> MAXLENGTH: ARM then rips title-by-title and SKIPS anything longer, so a
    Play All is never ripped (apply_length_skip_patch.py marks it ripped=False so the mover's
    count still matches). Nothing in shows.json longer than maxSeconds is ever an episode.
    ripMinSeconds -> MINLENGTH: only for a set with a real episode under the global 600s
    (SpongeBob's 3:35 Reef Blower), instead of lowering MINLENGTH for every disc.
    """
    cfg = getattr(job, "config", None)
    if cfg is None:
        logging.warning(f"show_override: job has no config row - length limits not set for {label}")
        return
    mx = int(sh.get("maxSeconds") or 0)
    mn = int(sh.get("ripMinSeconds") or 0)
    if mx > 0:
        logging.info(f"show_override: MAXLENGTH {cfg.MAXLENGTH} -> {mx} for {label} (Play All titles skipped)")
        cfg.MAXLENGTH = str(mx)
    if mn > 0:
        logging.info(f"show_override: MINLENGTH {cfg.MINLENGTH} -> {mn} for {label}")
        cfg.MINLENGTH = str(mn)


def apply(job, db):
    try:
        label = job.label or ""
        if not label:
            return
        sh = _match(label)
        if not sh:
            return
        name = sh.get("showName", "")
        m = re.match(r"^(.*?)\s*\((\d{4})\)\s*$", name)
        title, year = (m.group(1), m.group(2)) if m else (name, job.year)
        logging.info(f"show_override: label {label} matches registered show '{name}' - "
                     f"overriding ARM ident (was title:{job.title} year:{job.year} "
                     f"video_type:{job.video_type} imdb:{job.imdb_id}) -> series")
        job.video_type = "series"
        # folderPerJob: every disc of the set gets its OWN ARM folder. For sets whose discs all
        # carry the SAME label and MakeMKV file names (SpongeBob "First 100 Episodes": discs 2
        # and 3 both write C7_t09.mkv...), a shared folder would let one disc overwrite another
        # and the mover would read the second disc as a re-rip of the first.
        if sh.get("folderPerJob") and getattr(job, "job_id", None):
            title = f"{title} job{job.job_id}"
        job.title = title
        job.year = year
        job.hasnicetitle = True
        # The lookup's imdb/poster belong to whatever film it wrongly matched.
        job.imdb_id = None
        job.poster_url = None
        _set_lengths(job, sh, label)
        db.session.commit()
    except Exception as err:   # never break a rip over this
        logging.error(f"show_override failed ({err!r}) - keeping ARM's identification")
