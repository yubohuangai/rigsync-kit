"""Tests for the one-command sync preprocessing pipeline (run_sync_pipeline.py).

Fixtures build tiny real videos with ffmpeg (cv2's CAP_PROP_FRAME_COUNT is
unreliable for these clips, returning 0), so frame counting is exercised against
genuine footage rather than mocks.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import run_sync_pipeline as rsp  # noqa: E402
import sync  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH"
)


# --------------------------------------------------------------------------- #
# fixture helpers
# --------------------------------------------------------------------------- #
def _make_video(path: Path, n_frames: int, fps: int = 10) -> None:
    """Write an H.264 clip with exactly n_frames frames."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=size=64x48:rate={fps}",
         "-frames:v", str(n_frames), "-pix_fmt", "yuv420p", "-c:v", "libx264",
         str(path), "-loglevel", "error"],
        check=True,
    )


def _make_camera(raw: Path, cam: str, n_frames: int, timestamps_ns, csv_lines=None):
    """Build raw/<cam>/VID/VID_<stem>.mp4 + raw/<cam>/<stem>.csv."""
    stem = f"20260101_0000{cam}"
    _make_video(raw / cam / "VID" / f"VID_{stem}.mp4", n_frames)
    if csv_lines is None:
        csv_lines = len(timestamps_ns)
    lines = [str(int(t)) for t in timestamps_ns[:csv_lines]]
    (raw / cam / f"{stem}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# evaluate_metadata (pure decision logic)
# --------------------------------------------------------------------------- #
def test_evaluate_metadata_flags_mismatch():
    # rows: (camera_label, n_frames, n_csv_lines)
    report = rsp.evaluate_metadata([("01", 365, 365), ("02", 362, 360)])
    assert report.all_ok is False
    assert report.mismatched == ["02"]


def test_report_table_marks_each_camera():
    report = rsp.evaluate_metadata([("01", 365, 365), ("02", 362, 360)])
    assert "OK" in report.table
    assert "MISMATCH" in report.table
    assert "02" in report.table
    assert "-2" in report.table  # 360 - 362


# --------------------------------------------------------------------------- #
# reliable frame counter (ffprobe, not cv2)
# --------------------------------------------------------------------------- #
def test_get_video_frame_count_matches_real_frames(tmp_path):
    v = tmp_path / "clip.mp4"
    _make_video(v, n_frames=6)
    # ffprobe count == actual extractable frames; this is what the gate predicts
    assert sync.get_video_frame_count(str(v)) == 6


# --------------------------------------------------------------------------- #
# orchestrator: metadata gate aborts without modifying anything
# --------------------------------------------------------------------------- #
def test_run_pipeline_aborts_on_metadata_mismatch_without_writing(tmp_path):
    raw = tmp_path / "raw"
    ts = [i * 10 ** 8 for i in range(6)]
    _make_camera(raw, "01", n_frames=6, timestamps_ns=ts)
    _make_camera(raw, "02", n_frames=6, timestamps_ns=ts, csv_lines=5)  # short CSV
    out = tmp_path / "exp"

    rc = rsp.run_pipeline(str(raw), threshold="200ms", output_root=str(out))

    assert rc == 2  # gate abort code
    assert not (raw / "01" / "images").exists()
    assert not (raw / "02" / "images").exists()
    assert not out.exists() or not any(out.iterdir())  # no exp/<slug>_<label>/ written


# --------------------------------------------------------------------------- #
# orchestrator: happy path produces synchronized image sequences
# --------------------------------------------------------------------------- #
def test_run_pipeline_happy_path_produces_synced_sequences(tmp_path):
    raw = tmp_path / "raw"
    ms = 10 ** 6  # 1 ms in ns
    ts1 = [i * 100 * ms for i in range(6)]                          # 0,100,...,500 ms
    ts2 = [i * 100 * ms + 5 * ms for i in range(5)] + [5000 * ms]   # 5 align, last far off
    _make_camera(raw, "01", n_frames=6, timestamps_ns=ts1)
    _make_camera(raw, "02", n_frames=6, timestamps_ns=ts2)
    out = tmp_path / "exp"

    rc = rsp.run_pipeline(
        str(raw), threshold="50ms", session_slug="sess",
        output_root=str(out), cpu_only=True,
    )
    assert rc == 0

    # threshold label wired straight into the matched.csv path
    matched = out / "sess_50ms" / "matched.csv"
    assert matched.exists()

    img1, img2 = raw / "01" / "images", raw / "02" / "images"

    def jpgs(d):
        return sorted(p.name for p in d.glob("*.jpg"))

    # 5 matched frames remain in images/, 1 unmatched moved aside, per camera
    assert len(jpgs(img1)) == 5
    assert len(jpgs(img2)) == 5
    assert len(jpgs(img1 / "unmatched")) == 1
    assert len(jpgs(img2 / "unmatched")) == 1
    # frames are renamed to their CSV timestamps; the far-off cam01 frame is unmatched
    assert (img1 / "0.jpg").exists()
    assert (img1 / "unmatched" / f"{5 * 100 * ms}.jpg").exists()  # 500000000.jpg


def test_run_pipeline_dry_run_writes_nothing(tmp_path):
    raw = tmp_path / "raw"
    ts = [i * 10 ** 8 for i in range(6)]
    _make_camera(raw, "01", n_frames=6, timestamps_ns=ts)
    _make_camera(raw, "02", n_frames=6, timestamps_ns=ts)
    out = tmp_path / "exp"

    rc = rsp.run_pipeline(
        str(raw), threshold="200ms", session_slug="sess",
        output_root=str(out), dry_run=True,
    )
    assert rc == 0
    assert not (raw / "01" / "images").exists()
    assert not out.exists() or not any(out.iterdir())


def test_default_output_root_sits_beside_raw(tmp_path):
    raw = tmp_path / "sess" / "raw"
    raw.mkdir(parents=True)
    assert sync.default_output_root(raw) == (tmp_path / "sess").resolve() / "output"
    plain = tmp_path / "plain"
    plain.mkdir()
    assert sync.default_output_root(plain) == plain.resolve() / "output"
