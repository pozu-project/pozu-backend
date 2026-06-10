"""
Hourly CRON snapshot: move completed JSONL buffers into the dandiset and upload to DANDI.

Schedule this via PythonAnywhere's "Scheduled tasks" to run once per hour:

    python /home/CodyCBakerPhD/mysite/cron_snapshot.py

What it does:
  1. Finds all completed JSONL buffer files (any hour tag that is not the current hour).
  2. Moves them from derivatives/buffer/ into derivatives/incoming/ as-is.
  3. Runs a single `dandi upload` per dandiset that has new files.
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
EMBER_DANDI_API_KEY = api_key_file_path.read_text().strip()

BBOX_DANDISET_ROOT = pathlib.Path("/home/CodyCBakerPhD/mysite/000469")
LABELS_DANDISET_ROOT = pathlib.Path("/home/CodyCBakerPhD/mysite/000470")
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


# =============================================================================
# Main
# =============================================================================


def main() -> None:
    current_hour_tag = datetime.datetime.utcnow().strftime("%Y-%m-%d-%H")
    logger.info("cron_snapshot starting (current hour: %s)", current_hour_tag)

    for dandiset_root in [BBOX_DANDISET_ROOT, LABELS_DANDISET_ROOT]:
        staged = stage_completed_buffers(dandiset_root, current_hour_tag)
        if staged:
            logger.info("Staged %d file(s) for %s; running dandi upload", len(staged), dandiset_root.name)
            rc = dandi_upload(dandiset_root)
            if rc != 0:
                logger.error("dandi upload failed (rc=%d) for %s", rc, dandiset_root.name)

    logger.info("cron_snapshot done")


if __name__ == "__main__":
    main()
