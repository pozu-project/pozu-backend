"""
Hourly CRON snapshot: move completed JSONL buffers into the dandiset and upload to DANDI.

Schedule this via PythonAnywhere's "Scheduled tasks" to run once per hour:

    python /home/CodyCBakerPhD/mysite/cron_snapshot.py

What it does:
  1. Finds all completed JSONL buffer files (any hour tag that is not the current hour).
  2. Moves them from derivatives/buffer/ into derivatives/incoming/ as-is.
  3. For the clips dandiset (000474), also moves finished MP4 clips into incoming/.
  4. Runs a single `dandi upload` per dandiset that has new files.
  5. Deletes each uploaded MP4 clip from local disk after a successful upload,
     since clips are large and the DANDI archive is their system of record.
"""

from __future__ import annotations

import datetime
import logging
import os
import pathlib
import shutil
import subprocess
import sys

# =============================================================================
# Config - must stay in sync with pozu_flask_app.py
# =============================================================================

VENV_BIN = "/home/CodyCBakerPhD/.virtualenvs/pozu/bin"
DANDI_BIN = f"{VENV_BIN}/dandi"

api_key_file_path = pathlib.Path("/home/CodyCBakerPhD/dandi_token")
# The empty-string fallback keeps the module importable in development and CI,
# where the deployment token file does not exist; uploads still require it.
EMBER_DANDI_API_KEY = api_key_file_path.read_text().strip() if api_key_file_path.exists() else ""

BBOX_DANDISET_ROOT = pathlib.Path("/home/CodyCBakerPhD/mysite/000469")
LABELS_DANDISET_ROOT = pathlib.Path("/home/CodyCBakerPhD/mysite/000470")
NO_SUBJECT_DANDISET_ROOT = pathlib.Path("/home/CodyCBakerPhD/mysite/000472")
REPORTS_DANDISET_ROOT = pathlib.Path("/home/CodyCBakerPhD/mysite/000473")
CLIPS_DANDISET_ROOT = pathlib.Path("/home/CodyCBakerPhD/mysite/000474")
DANDI_INSTANCE = "https://api-dandi.emberarchive.org/api"

# =============================================================================
# Logging
# =============================================================================

logging.basicConfig(
    level="INFO",
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

# =============================================================================
# Helpers
# =============================================================================


def dandi_upload(dandiset_root: pathlib.Path) -> int:
    """Run `dandi upload` inside *dandiset_root*. Returns the exit code."""
    env = os.environ.copy()
    env["EMBER_DANDI_API_KEY"] = EMBER_DANDI_API_KEY
    env["PATH"] = f"{VENV_BIN}:{env.get('PATH', '')}"

    cmd = [DANDI_BIN, "upload", "--dandi-instance", DANDI_INSTANCE]
    logger.info("Running dandi upload (cwd=%s)", dandiset_root)
    proc = subprocess.run(cmd, cwd=dandiset_root, env=env, capture_output=True, text=True, timeout=300, check=False)
    logger.info("dandi upload rc=%d\nstdout: %s\nstderr: %s", proc.returncode, proc.stdout, proc.stderr)
    return proc.returncode


def stage_completed_buffers(dandiset_root: pathlib.Path, current_hour_tag: str) -> list[pathlib.Path]:
    """Move completed JSONL files from buffer/ to incoming/. Returns moved file paths."""
    buffer_dir = dandiset_root / "derivatives" / "buffer"
    if not buffer_dir.exists():
        return []

    complete = sorted(f for f in buffer_dir.glob("*.jsonl") if current_hour_tag not in f.name)
    if not complete:
        return []

    incoming_dir = dandiset_root / "derivatives" / "incoming"
    incoming_dir.mkdir(parents=True, exist_ok=True)

    moved = []
    for jsonl_file in complete:
        dest = incoming_dir / jsonl_file.name
        shutil.move(str(jsonl_file), str(dest))
        logger.info("Staged %s -> incoming/", jsonl_file.name)
        moved.append(dest)

    return moved


def stage_clip_files(dandiset_root, /) -> list[pathlib.Path]:
    """Move finished MP4 clips from buffer/ to incoming/. Returns moved file paths.

    Clips normally upload to DANDI synchronously inside the web request, so this
    sweep is the retry path: any ``*.mp4`` still sitting in buffer/ is a clip
    whose synchronous upload failed. The web app renames each clip into buffer/
    atomically, so every ``*.mp4`` seen here is complete and safe to stage
    regardless of the hour tag.
    """
    buffer_dir = dandiset_root / "derivatives" / "buffer"
    if not buffer_dir.exists():
        return []

    clips = sorted(buffer_dir.glob("*.mp4"))
    if not clips:
        return []

    incoming_dir = dandiset_root / "derivatives" / "incoming"
    incoming_dir.mkdir(parents=True, exist_ok=True)

    moved = []
    for clip_file in clips:
        dest = incoming_dir / clip_file.name
        shutil.move(str(clip_file), str(dest))
        logger.info("Staged %s -> incoming/", clip_file.name)
        moved.append(dest)

    return moved


def delete_uploaded_clips(staged, /) -> None:
    """Delete uploaded MP4 clips from local disk to reclaim space.

    Only ``*.mp4`` files are removed; JSONL provenance records stay on disk like
    every other dandiset's records.
    """
    for path in staged:
        if path.suffix == ".mp4":
            path.unlink(missing_ok=True)
            logger.info("Deleted uploaded clip %s to reclaim disk space", path.name)


# =============================================================================
# Main
# =============================================================================


def main() -> None:
    current_hour_tag = datetime.datetime.utcnow().strftime("%Y-%m-%d-%H")
    logger.info("cron_snapshot starting (current hour: %s)", current_hour_tag)

    for dandiset_root in [
        BBOX_DANDISET_ROOT,
        LABELS_DANDISET_ROOT,
        NO_SUBJECT_DANDISET_ROOT,
        REPORTS_DANDISET_ROOT,
        CLIPS_DANDISET_ROOT,
    ]:
        staged = stage_completed_buffers(dandiset_root, current_hour_tag)
        if dandiset_root == CLIPS_DANDISET_ROOT:
            staged += stage_clip_files(dandiset_root)
        if not staged:
            continue

        logger.info("Staged %d file(s) for %s; running dandi upload", len(staged), dandiset_root.name)
        rc = dandi_upload(dandiset_root)
        if rc != 0:
            logger.error("dandi upload failed (rc=%d) for %s", rc, dandiset_root.name)
        elif dandiset_root == CLIPS_DANDISET_ROOT:
            delete_uploaded_clips(staged)

    logger.info("cron_snapshot done")


if __name__ == "__main__":
    main()
