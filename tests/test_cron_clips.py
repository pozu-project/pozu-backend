"""Tests for the clip staging and disk-reclaim behavior of cron_snapshot.py."""

import pytest

import cron_snapshot

OLD_HOUR_TAG = "2020-01-01-00"


def _make_clips_dandiset(root, /):
    buffer_dir = root / "derivatives" / "buffer"
    buffer_dir.mkdir(parents=True)
    return buffer_dir


@pytest.fixture
def clips_root(tmp_path, monkeypatch):
    """Point every dandiset root at empty temp dirs and return the clips root."""
    for name in ("BBOX", "LABELS", "NO_SUBJECT", "REPORTS"):
        monkeypatch.setattr(cron_snapshot, f"{name}_DANDISET_ROOT", tmp_path / name)
    clips = tmp_path / "000474"
    monkeypatch.setattr(cron_snapshot, "CLIPS_DANDISET_ROOT", clips)
    return clips


@pytest.mark.ai_generated
def test_stage_clip_files_moves_only_complete_mp4s(clips_root):
    buffer_dir = _make_clips_dandiset(clips_root)
    (buffer_dir / "a.mp4").write_bytes(b"clip-a")
    (buffer_dir / "b.mp4").write_bytes(b"clip-b")
    # A .part file is an in-flight write and must not be staged.
    (buffer_dir / "c.mp4.part").write_bytes(b"partial")

    moved = cron_snapshot.stage_clip_files(clips_root)

    incoming_dir = clips_root / "derivatives" / "incoming"
    assert moved == [incoming_dir / "a.mp4", incoming_dir / "b.mp4"]
    assert sorted(entry.name for entry in buffer_dir.iterdir()) == ["c.mp4.part"]


@pytest.mark.ai_generated
def test_stage_clip_files_handles_missing_buffer(clips_root):
    assert cron_snapshot.stage_clip_files(clips_root) == []


@pytest.mark.ai_generated
def test_main_deletes_clips_after_successful_upload(clips_root, monkeypatch):
    buffer_dir = _make_clips_dandiset(clips_root)
    (buffer_dir / "a.mp4").write_bytes(b"clip-a")
    (buffer_dir / f"{OLD_HOUR_TAG}.jsonl").write_text('{"clip_file": "a.mp4"}\n')

    uploaded = []
    monkeypatch.setattr(cron_snapshot, "dandi_upload", lambda dandiset_root: uploaded.append(dandiset_root) or 0)

    cron_snapshot.main()

    assert uploaded == [clips_root]
    incoming_dir = clips_root / "derivatives" / "incoming"
    # The uploaded MP4 is deleted to reclaim disk; the JSONL provenance remains.
    assert sorted(entry.name for entry in incoming_dir.iterdir()) == [f"{OLD_HOUR_TAG}.jsonl"]


@pytest.mark.ai_generated
def test_main_keeps_clips_when_upload_fails(clips_root, monkeypatch):
    buffer_dir = _make_clips_dandiset(clips_root)
    (buffer_dir / "a.mp4").write_bytes(b"clip-a")

    monkeypatch.setattr(cron_snapshot, "dandi_upload", lambda dandiset_root: 1)

    cron_snapshot.main()

    incoming_dir = clips_root / "derivatives" / "incoming"
    # A failed upload must not destroy the only copy of the clip.
    assert sorted(entry.name for entry in incoming_dir.iterdir()) == ["a.mp4"]
