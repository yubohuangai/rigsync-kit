"""One-command sync preprocessing pipeline.

Orchestrates the existing preprocessing steps into a single run:

    discover cameras -> metadata gate -> timestamp match -> extract+rename
    -> move unmatched

producing frame-synchronized image sequences under each camera's images/.
The metadata gate is read-only: if any camera's video frame count disagrees
with its CSV line count, the run pauses (prints a report and aborts) without
modifying anything.

Scope is preprocessing only; the LED time-code decode / sync-error eval is a
separate downstream consumer (the syncbench decode tools).
"""
from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import sync  # noqa: E402
import move_unmatched  # noqa: E402

GATE_ABORT_CODE = 2


def _video_duration_seconds(video_path: str) -> float:
    """Best-effort clip duration (seconds) for the match FPS stat; 0.0 if unknown."""
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nokey=1:noprint_wrappers=1", str(video_path),
    ]
    try:
        return float(subprocess.run(cmd, capture_output=True, text=True).stdout.strip())
    except (ValueError, OSError):
        return 0.0


@dataclass
class MetadataReport:
    """Result of comparing per-camera video frame counts vs CSV line counts."""

    rows: List[Tuple[str, int, int]]
    all_ok: bool
    mismatched: List[str] = field(default_factory=list)

    @property
    def table(self) -> str:
        """Human-readable per-camera table: cam | frames | csv_lines | delta | status."""
        header = f"{'cam':>4}  {'frames':>8}  {'csv_lines':>9}  {'delta':>6}  status"
        lines = [header, "-" * len(header)]
        for label, n_frames, n_lines in self.rows:
            delta = n_lines - n_frames
            status = "OK" if delta == 0 else "MISMATCH"
            lines.append(
                f"{label:>4}  {n_frames:>8}  {n_lines:>9}  {delta:>+6}  {status}"
            )
        return "\n".join(lines)


def evaluate_metadata(rows: List[Tuple[str, int, int]]) -> MetadataReport:
    """Decide whether every camera's frame count matches its CSV line count.

    rows: (camera_label, n_frames, n_csv_lines) per camera.
    """
    mismatched = [label for (label, n_frames, n_lines) in rows if n_frames != n_lines]
    return MetadataReport(rows=rows, all_ok=(len(mismatched) == 0), mismatched=mismatched)


def gather_metadata(video_paths: List[str]) -> List[Tuple[str, int, int]]:
    """For each camera video, collect (camera_label, n_frames, n_csv_lines).

    Frame count comes from ffprobe (sync.get_video_frame_count) — the exact
    extractable-frame count, which is what the gate predicts. Read-only.
    """
    rows = []
    for vp in video_paths:
        label = Path(vp).parent.parent.name
        n_frames = sync.get_video_frame_count(vp)
        csv_path = sync.get_csv_path_from_video(vp)
        n_lines = sync.count_csv_nonempty_timestamp_lines(Path(csv_path))
        rows.append((label, n_frames, n_lines))
    return rows


def run_pipeline(
    root: str,
    *,
    threshold: str = "16ms",
    match_mode: str = "full",
    cpu_only: bool = False,
    force_extract: bool = False,
    dry_run: bool = False,
    output_root: str | None = None,
    session_slug: str | None = None,
) -> int:
    """Run the full preprocessing pipeline; return 0 on success, GATE_ABORT_CODE on a
    metadata mismatch (in which case nothing on disk is modified)."""
    root = str(Path(root).expanduser().resolve())
    video_paths = sync.collect_video_paths(root, None)

    # --- metadata gate (read-only) ---
    report = evaluate_metadata(gather_metadata(video_paths))
    print(report.table)
    if not report.all_ok:
        print(
            f"\nPAUSED: {len(report.mismatched)} camera(s) have a frame/CSV mismatch: "
            f"{report.mismatched}. Nothing was modified.\n"
            "Re-export the clip(s), or reconcile with analyze_vid.py "
            "(--truncate / --pad_csv), then re-run."
        )
        return GATE_ABORT_CODE

    # --- resolve output paths (threshold label wired straight through to the move step) ---
    threshold_ns = sync.parse_duration_ns(threshold)
    label = sync.format_threshold_filename(threshold_ns)
    slug = session_slug or sync.raw_session_slug(root)
    exp_dir = Path(output_root) if output_root else sync.default_output_root(root)
    base_output = exp_dir / f"{slug}_{label}"
    matched_csv = base_output / "matched.csv"
    matched_full_csv = base_output / "matched_full.csv"

    if dry_run:
        print(
            f"\n[dry-run] would: match -> {matched_csv}\n"
            f"          extract+rename frames for {len(video_paths)} camera(s)\n"
            f"          move unmatched (mode={match_mode}). Nothing written."
        )
        return 0

    base_output.mkdir(parents=True, exist_ok=True)
    sync.setup_logger(str(base_output / "pipeline.log"))
    logging.info("==== sync pipeline started ==== root=%s threshold=%s", root, label)

    csv_paths = [str(sync.get_csv_path_from_video(vp)) for vp in video_paths]
    duration_0 = _video_duration_seconds(video_paths[0])

    # --- stage: timestamp match ---
    sync.match_frames_from_csv_multi(csv_paths[0], csv_paths[1:], str(matched_csv), threshold_ns)
    sync.match_frames_full_from_csv_multi(
        csv_paths[0], csv_paths[1:], str(matched_full_csv), threshold_ns, duration_0
    )

    # --- stage: extract + rename (gate already validated counts via ffprobe) ---
    sync.extract_session_frames(
        video_paths, cpu_only=cpu_only, force=force_extract, validate=False
    )

    # --- stage: move unmatched ---
    move_unmatched.move_unmatched_images(root, str(matched_csv), match_mode=match_mode)

    logging.info("==== sync pipeline complete ==== matched.csv=%s", matched_csv)
    return 0


def main():
    import argparse

    p = argparse.ArgumentParser(
        description=(
            "One-command sync preprocessing: metadata gate -> timestamp match -> "
            "extract+rename -> move unmatched. Produces synchronized image sequences."
        )
    )
    p.add_argument("root", help="Session raw/ dir holding the camera folders 01/, 02/, ...")
    p.add_argument(
        "--output-root", default=None, metavar="DIR",
        help="Where <slug>_<threshold>/matched.csv goes (default: <session>/output, beside raw/).",
    )
    p.add_argument("--threshold", default="16ms", help="Match tolerance (ns/us/ms/s). Default 16ms.")
    p.add_argument("--session-slug", default=None, metavar="NAME")
    p.add_argument("--match-mode", default="full", choices=["first", "full"])
    p.add_argument("--cpu", action="store_true", help="Force CPU-only frame extraction.")
    p.add_argument("--force-extract", action="store_true", help="Re-extract even if images/ exists.")
    p.add_argument("--dry-run", action="store_true", help="Report planned actions; write nothing.")
    args = p.parse_args()

    return run_pipeline(
        args.root,
        threshold=args.threshold,
        match_mode=args.match_mode,
        cpu_only=args.cpu,
        force_extract=args.force_extract,
        dry_run=args.dry_run,
        session_slug=args.session_slug,
        output_root=args.output_root,
    )


if __name__ == "__main__":
    raise SystemExit(main())
