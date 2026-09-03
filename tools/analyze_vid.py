from __future__ import annotations

import argparse
import cv2
import os
import sys
import statistics
import subprocess
from datetime import datetime
from pathlib import Path
import re
import shutil

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))
from sync import default_output_root  # noqa: E402

CONFIG = dict(
    log_file=None,  # default: <session>/output/video_analysis.log, beside raw/
    start_cam=1,
    end_cam=None,   # inclusive; None = auto-detect
    truncate=False,
    truncate_from="end",
    pad_csv=False,
    pad_to="end",  # "end" | "begin" — where to append missing timestamp lines
    # marker = explicit __POSTPROCESS__ labels (default); extrapolate/repeat_last = numeric (can look like real ts)
    pad_fill="marker",
    pad_marker="extra1",
    clear_log=False,
)


def ensure_file_exists(file_path):
    file_path = Path(file_path)
    if file_path.parent:
        file_path.parent.mkdir(parents=True, exist_ok=True)
    if not file_path.exists():
        file_path.touch()




def get_csv_path_from_video(video_path):
    video_name = Path(video_path).stem
    match = re.search(r"VID_((\d|_)+)", video_name)
    if not match:
        return None
    video_date = match.group(1)
    csv_path = Path(video_path).parent.parent / f"{video_date}.csv"
    return csv_path


def resolve_csv_path(video_path):
    csv_path = get_csv_path_from_video(video_path)
    if csv_path is None:
        return None
    if csv_path.exists():
        return csv_path
    root = csv_path.parent
    candidates = list(root.glob("*.csv")) + list(root.glob("*.CSV"))
    if len(candidates) == 1:
        return candidates[0]
    for candidate in candidates:
        if candidate.stem == csv_path.stem:
            return candidate
    return csv_path


def list_vid_mp4_files(vid_dir: Path):
    """All *.mp4 in VID dir, excluding backup copies named * _ori.mp4."""
    return [p for p in vid_dir.glob("*.mp4") if not p.stem.endswith("_ori")]


def find_video_and_csv(root_path):
    """
    Input:
        root_path = C:\...\raw\09

    Automatically finds:
        video_path = root_path / VID / *.mp4   (must contain exactly one; *_ori.mp4 ignored)
        csv_path   = root_path / <timestamp>.csv
    """
    root = Path(root_path)
    vid_dir = root / "VID"

    videos = list_vid_mp4_files(vid_dir)
    if len(videos) == 0:
        return None, None  # instead of raising error
    if len(videos) > 1:
        raise RuntimeError(f"Multiple .mp4 files found in {vid_dir}, expected only one.")

    video_path = videos[0]
    video_name = video_path.stem  # VID_20251116_121905

    # Extract timestamp
    match = re.search(r"VID_(\d{8}_\d{6})", video_name)
    if not match:
        raise ValueError(f"Filename does not contain timestamp: {video_name}")

    timestamp = match.group(1)

    # CSV should sit in the root directory
    csv_path = root / f"{timestamp}.csv"
    if not csv_path.exists():
        # Fallback: look for any CSV in the root matching the timestamp.
        candidates = list(root.glob("*.csv")) + list(root.glob("*.CSV"))
        if len(candidates) == 1:
            csv_path = candidates[0]
        else:
            for candidate in candidates:
                if candidate.stem == timestamp:
                    csv_path = candidate
                    break

    return str(video_path), str(csv_path)


def count_csv_lines(csv_path):
    if csv_path is None:
        return None, "missing_path"
    path = Path(csv_path)
    if not path.exists():
        return None, "not_found"
    try:
        with path.open("r", encoding="utf-8") as f:
            return sum(1 for _ in f), None
    except Exception as exc:
        return None, str(exc)


def get_camera_ids(base_root, start_cam=1, end_cam=None):
    base_root = Path(base_root)
    if not base_root.exists():
        return []
    cam_ids = []
    for entry in base_root.iterdir():
        if not entry.is_dir():
            continue
        if not entry.name.isdigit():
            continue
        cam_id = int(entry.name)
        if cam_id < start_cam:
            continue
        if end_cam is not None and cam_id > end_cam:
            continue
        cam_ids.append(cam_id)
    return sorted(cam_ids)


def get_frame_count_ffmpeg(video_path):
    """Get reliable frame count using ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-count_frames", "-select_streams", "v:0",
        "-show_entries", "stream=nb_read_frames",
        "-of", "default=nokey=1:noprint_wrappers=1",
        str(video_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return int(result.stdout.strip())
    except:
        return None


def get_frame_count_fast(video_path):
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=nb_frames",
        "-of", "default=nokey=1:noprint_wrappers=1",
        str(video_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return int(result.stdout.strip())
    except:
        return None


def _read_csv_nonempty_stripped_lines(csv_path: Path) -> list[str]:
    with csv_path.open("r", encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]


def _sanitize_pad_marker(s: str) -> str:
    """Safe token for CSV lines / frame basename stems (alnum, _, -)."""
    t = (s or "").strip() or "PAD"
    t = re.sub(r"[^\w\-]+", "_", t, flags=re.ASCII)
    return (t[:64] or "PAD")


def _infer_timestamp_step_ns(timestamps: list[int], fps: float | None) -> int:
    if len(timestamps) >= 2:
        diffs = [b - a for a, b in zip(timestamps, timestamps[1:]) if b > a]
        if diffs:
            return int(statistics.median(diffs))
    if fps is not None and fps > 0:
        return max(1, int(round(1e9 / fps)))
    raise ValueError(
        "pad_fill=extrapolate needs ≥2 ascending integer timestamps or valid video FPS."
    )


def pad_timestamp_csv(
    csv_path: Path,
    target_nlines: int,
    pad_to: str,
    pad_fill: str,
    fps: float | None,
    pad_marker: str = "PAD",
) -> tuple[int, int]:
    """
    Extend CSV until line count == target_nlines.
    pad_to: "end" → append after last line; "begin" → prepend before first line.
    pad_fill:
      "marker" → lines contain fixed __POSTPROCESS__ and optional prefix (not real capture timestamps).
      "extrapolate" → synthetic ints from median step / FPS (can be mistaken for real data).
      "repeat_last" → duplicate edge int for each added line.
    Returns (count_before, count_after).
    """
    lines = _read_csv_nonempty_stripped_lines(csv_path)
    n = len(lines)
    if n >= target_nlines:
        return n, n

    n_add = target_nlines - n
    if pad_fill == "marker":
        label = _sanitize_pad_marker(pad_marker)
        if pad_to == "end":
            extra = [
                f"{label}__POSTPROCESS__append_{i:06d}" for i in range(1, n_add + 1)
            ]
            new_lines = lines + extra
        elif pad_to == "begin":
            extra = [
                f"{label}__POSTPROCESS__prepend_{i:06d}" for i in range(1, n_add + 1)
            ]
            new_lines = extra + lines
        else:
            raise ValueError("pad_to must be 'begin' or 'end'.")
        csv_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        return n, len(new_lines)

    try:
        ts = [int(x) for x in lines]
    except ValueError as e:
        raise ValueError(
            f"CSV padding ({pad_fill}) requires existing lines to be integers: {e}"
        ) from e

    if pad_fill == "repeat_last":
        if pad_to == "end":
            extra = [ts[-1]] * n_add
            new_ts = ts + extra
        elif pad_to == "begin":
            extra = [ts[0]] * n_add
            new_ts = extra + ts
        else:
            raise ValueError("pad_to must be 'begin' or 'end'.")
    elif pad_fill == "extrapolate":
        step = _infer_timestamp_step_ns(ts, fps)
        if pad_to == "end":
            last = ts[-1]
            new_ts = ts + [last + step * (i + 1) for i in range(n_add)]
        elif pad_to == "begin":
            first = ts[0]
            head = [first - step * (i + 1) for i in range(n_add)]
            head.reverse()
            new_ts = head + ts
        else:
            raise ValueError("pad_to must be 'begin' or 'end'.")
    else:
        raise ValueError("pad_fill must be 'marker', 'extrapolate', or 'repeat_last'.")

    csv_path.write_text("\n".join(str(x) for x in new_ts) + "\n", encoding="utf-8")
    return n, len(new_ts)


def analyze_video(
    video_path: str,
    log_file: str,
    truncate=False,
    truncate_from="end",
    pad_csv=False,
    pad_to="end",
    pad_fill="marker",
    pad_marker="extra1",
    print_log=False,
    csv_path_override: str | None = None,
):
    """
    Analyze video & optionally truncate CSV if timestamp_count > frame_count.
    truncate_from: "end" (default) → cut extra lines at the end,
                   "begin" → cut extra lines at the beginning.
    Optionally pad CSV when timestamp_count < frame_count (see pad_csv / pad_to / pad_fill / pad_marker).
    If csv_path_override is set, that file is used instead of resolve_csv_path(video_path).
    """
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video not found: {video_path}")

    if csv_path_override:
        csv_path = Path(os.path.abspath(os.path.expanduser(csv_path_override)))
        if not csv_path.is_file():
            raise FileNotFoundError(f"CSV not found: {csv_path}")
    else:
        csv_path = resolve_csv_path(video_path)
    timestamp_count = None
    if csv_path is None:
        csv_msg = "Video name format did not match expected pattern. No CSV checked."
    else:
        timestamp_count, csv_err = count_csv_lines(csv_path)
        if timestamp_count is None:
            root = Path(video_path).parent.parent
            candidates = list(root.glob("*.csv")) + list(root.glob("*.CSV"))
            if csv_err == "not_found":
                csv_msg = (
                    f"CSV not found: {csv_path}\n"
                    f"CSV candidates in {root}: {[c.name for c in candidates]}"
                )
            else:
                csv_msg = f"CSV error ({csv_err}): {csv_path}"
        else:
            csv_msg = f"CSV path  : {csv_path} (lines: {timestamp_count})"

    log_start = None
    if print_log:
        try:
            log_start = os.path.getsize(log_file)
        except OSError:
            log_start = 0
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"\n============= Video Analysis: {datetime.now()} =============\n")
        f.write(f"Video path: {video_path}\n")
        f.write(f"{csv_msg}\n")

        # --- OpenCV properties ---
        fps = None
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            f.write(f"Failed to open video with OpenCV: {video_path}\n")
            frame_count = None
        else:
            frame_count_ffmpeg = get_frame_count_fast(video_path)
            frame_count = frame_count_ffmpeg or int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = cap.get(cv2.CAP_PROP_FPS)
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            duration = frame_count / fps if fps > 0 else 0
            bitrate_kbps = None
            try:
                size_bytes = os.path.getsize(video_path)
                bitrate_kbps = (size_bytes * 8) / max(duration, 1e-6) / 1000.0
            except OSError:
                bitrate_kbps = None

            f.write("OpenCV video properties:\n")
            if timestamp_count is not None:
                f.write(f"Timestamp count  : {timestamp_count}\n")
            f.write(f"Frame count      : {frame_count}\n")
            f.write(f"Frame rate (FPS) : {fps:.2f}\n")
            f.write(f"Resolution       : {width}x{height}\n")
            f.write(f"Duration (sec)   : {duration:.2f}\n")
            if bitrate_kbps is not None:
                f.write(f"Bitrate (kbps)   : {bitrate_kbps:.1f}\n")

        cap.release()

        # --- Truncate CSV if needed ---
        if truncate and csv_path and timestamp_count and frame_count:
            if timestamp_count > frame_count:
                backup_csv = csv_path.parent / f"{csv_path.stem}_ori{csv_path.suffix}"
                shutil.copy(csv_path, backup_csv)
                f.write(f"[TRUNCATE] CSV backed up to {backup_csv}\n")

                with open(csv_path, "r", encoding="utf-8") as infile:
                    lines = infile.readlines()

                if truncate_from == "end":
                    new_lines = lines[:frame_count]
                elif truncate_from == "begin":
                    new_lines = lines[-frame_count:]
                else:
                    raise ValueError(f"Invalid truncate_from='{truncate_from}', must be 'begin' or 'end'.")

                with open(csv_path, "w", encoding="utf-8") as outfile:
                    outfile.writelines(new_lines)

                f.write(f"[TRUNCATE] CSV truncated ({truncate_from}) from {timestamp_count} → {frame_count} lines\n")

        # --- Pad CSV if timestamp lines < frame count (e.g. sync extract_frame_data) ---
        if pad_csv and csv_path and frame_count:
            path_p = Path(csv_path)
            if path_p.exists():
                n_nonempty = len(_read_csv_nonempty_stripped_lines(path_p))
                if n_nonempty < frame_count:
                    backup_csv = path_p.parent / f"{path_p.stem}_ori{path_p.suffix}"
                    if not backup_csv.exists():
                        shutil.copy(path_p, backup_csv)
                        f.write(f"[PAD] CSV backed up to {backup_csv}\n")
                    try:
                        old_c, new_c = pad_timestamp_csv(
                            path_p,
                            frame_count,
                            pad_to,
                            pad_fill,
                            fps,
                            pad_marker=pad_marker,
                        )
                        f.write(
                            f"[PAD] CSV padded ({pad_to}, fill={pad_fill}"
                            + (f", marker={pad_marker!r}" if pad_fill == "marker" else "")
                            + f") {old_c} → {new_c} lines (target={frame_count} frames)\n"
                        )
                    except ValueError as exc:
                        f.write(f"[PAD] skipped: {exc}\n")

        # --- FFmpeg probe ---
        try:
            ffmpeg_cmd = ["ffmpeg", "-i", video_path]
            result = subprocess.run(ffmpeg_cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            # f.write("FFmpeg output:\n")
            # f.write(result.stderr)
        except FileNotFoundError:
            f.write("FFmpeg not found. Please ensure it is installed and in PATH.\n")
    if print_log:
        with open(log_file, "r", encoding="utf-8") as f:
            if log_start is None:
                print(f.read())
            else:
                f.seek(log_start)
                print(f.read())



def _resolve_cli_input_path(raw: str) -> Path:
    p = Path(os.path.abspath(os.path.expanduser(raw)))
    if not p.exists():
        raise FileNotFoundError(f"Path not found: {p}")
    return p


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze videos and CSV timestamp files.")
    parser.add_argument(
        "path",
        metavar="PATH",
        help="Camera root (.../raw with 01/, 02/, …) or one video file.",
    )
    parser.add_argument(
        "--csv",
        default=None,
        metavar="PATH",
        help="When PATH is a video file: use this CSV instead of auto-resolving from the filename.",
    )
    parser.add_argument("--log_file", default=CONFIG["log_file"])
    parser.add_argument("--start_cam", type=int, default=CONFIG["start_cam"])
    parser.add_argument("--end_cam", type=int, default=CONFIG["end_cam"])
    parser.add_argument("--truncate", action="store_true", default=CONFIG["truncate"])
    parser.add_argument("--truncate_from", default=CONFIG["truncate_from"], choices=["begin", "end"])
    parser.add_argument(
        "--pad_csv",
        action="store_true",
        default=CONFIG["pad_csv"],
        help="If CSV has fewer lines than video frames, add lines (see --pad_to, --pad_fill).",
    )
    parser.add_argument(
        "--pad_to",
        default=CONFIG["pad_to"],
        choices=["begin", "end"],
        help='Where to insert missing lines: "end" (default) or "begin".',
    )
    parser.add_argument(
        "--pad_fill",
        default=CONFIG["pad_fill"],
        choices=["marker", "extrapolate", "repeat_last"],
        help="marker: lines with __POSTPROCESS__ + --pad_marker (default, obvious postprocessing). "
        "extrapolate/repeat_last: numeric only (can look like real timestamps).",
    )
    parser.add_argument(
        "--pad_marker",
        default=CONFIG["pad_marker"],
        metavar="LABEL",
        help='With pad_fill=marker: prefix token (default from CONFIG; e.g. extra1 → extra1__POSTPROCESS__append_000001).',
    )
    parser.add_argument("--clear_log", action="store_true", default=CONFIG["clear_log"])
    parser.add_argument("--print_log", action="store_true", default=True)
    args = parser.parse_args()

    try:
        input_path = _resolve_cli_input_path(str(args.path))
    except (FileNotFoundError, ValueError) as e:
        print(str(e))
        raise SystemExit(1) from e

    if args.log_file is None:
        # single video: <session>/raw/NN/VID/x.mp4 -> raw is three levels up
        raw_dir = input_path.parent.parent.parent if input_path.is_file() else input_path
        args.log_file = str(default_output_root(raw_dir) / "video_analysis.log")
    log_file = args.log_file

    ensure_file_exists(log_file)

    if args.clear_log:
        open(log_file, "w", encoding="utf-8").close()

    if input_path.is_file():
        video_path = str(input_path)
        print(f"--- Single video: {video_path} ---")
        try:
            analyze_video(
                video_path,
                log_file,
                truncate=args.truncate,
                truncate_from=args.truncate_from,
                pad_csv=args.pad_csv,
                pad_to=args.pad_to,
                pad_fill=args.pad_fill,
                pad_marker=args.pad_marker,
                print_log=args.print_log,
                csv_path_override=args.csv,
            )
            print(f"✓ Finished: {video_path}")
        except Exception as e:
            print(f"✗ Error: {e}")
            raise SystemExit(1) from e
        raise SystemExit(0)

    base_root = input_path
    if args.csv:
        print("Note: --csv applies only to a single video file; ignoring --csv for directory scan.")

    cam_ids = get_camera_ids(base_root, args.start_cam, args.end_cam)
    if not cam_ids:
        print(f"No camera folders found under {base_root}")
    for cam_id in cam_ids:
        root_path = base_root / f"{cam_id:02d}"
        print(f"\n--- Processing {root_path} ---")

        try:
            video_path, _ = find_video_and_csv(root_path)
            if video_path is None:
                print(f"Skipping {root_path} (no video found)")
                continue

            analyze_video(
                video_path,
                log_file,
                truncate=args.truncate,
                truncate_from=args.truncate_from,
                pad_csv=args.pad_csv,
                pad_to=args.pad_to,
                pad_fill=args.pad_fill,
                pad_marker=args.pad_marker,
                print_log=args.print_log,
            )
            print(f"✓ Finished: {video_path}")

        except Exception as e:
            print(f"✗ Error in {root_path}: {e}")

