from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys
import cv2
import glob
import numpy as np
import pandas as pd
from pathlib import Path
import re
import logging
import shutil
import time
from concurrent.futures import ProcessPoolExecutor


ALLOWED_EXTENSIONS = ['.jpg', '.jpeg', '.npy', '.png', '.pcd']


def default_output_root(root) -> Path:
    """Default home for pipeline outputs: <session>/output when root is <session>/raw,
    else <root>/output. Results live beside the data, never inside the tools checkout."""
    p = Path(root).expanduser().resolve()
    session = p.parent if (p.name.lower() == "raw" and p.parent != p) else p
    return session / "output"


import argparse

def parse_args():
    parser = argparse.ArgumentParser(description="Multi-video timestamp matching")

    parser.add_argument("root", help="Session root (.../raw with 01/, 02/, …).")
    parser.add_argument(
        "--output-root",
        default=None,
        metavar="DIR",
        help="Where <slug>_<threshold>/ goes (default: <session>/output, beside raw/).",
    )

    parser.add_argument(
        "--cams",
        type=int,
        default=None,
        help="Number of camera folders (optional; auto-detect if omitted)"
    )

    parser.add_argument(
        "--threshold",
        type=str,
        default="16ms",
        help="Matching tolerance (ns, us, ms, s). Example: 16ms"
    )

    parser.add_argument(
        "--extract",
        action="store_true",
        help="Extract frames to <cam>/images/ (skipped if that folder already exists), then run matching.",
    )

    parser.add_argument(
        "--session-slug",
        default=None,
        metavar="NAME",
        help=(
            "Short label for the output folder (default: folder above 'raw', "
            "e.g. .../data/sync0403/raw → sync0403)."
        ),
    )

    parser.add_argument(
        "--skip-match",
        action="store_true",
        help=(
            "Do not write matched.csv / matched_full.csv. Use with --extract to only "
            "extract frames and rename from per-camera CSVs after you already ran a match pass."
        ),
    )

    parser.add_argument(
        "--cpu",
        action="store_true",
        help="Force CPU-only frame extraction (default: use GPU if available).",
    )

    return parser.parse_args()


def get_camera_ids(root, max_cams=None):
    root_path = Path(root)
    if not root_path.exists():
        return []
    cam_ids = []
    for entry in root_path.iterdir():
        if not entry.is_dir():
            continue
        if not entry.name.isdigit():
            continue
        cam_id = int(entry.name)
        if max_cams is not None and cam_id > max_cams:
            continue
        cam_ids.append(cam_id)
    return sorted(cam_ids)


def collect_video_paths(root, num_cams):
    video_paths = []
    valid_indices = []

    cam_ids = get_camera_ids(root, num_cams)
    if not cam_ids and num_cams is not None:
        cam_ids = list(range(1, num_cams + 1))
    if not cam_ids:
        raise RuntimeError("No camera folders found.")

    for i in cam_ids:
        cam_dir = Path(root) / f"{i:02d}" / "VID"
        # Ignore backup copies * _ori.mp4 (same convention as analyze_vid.py)
        mp4s = [p for p in cam_dir.glob("*.mp4") if not p.stem.endswith("_ori")]

        if len(mp4s) == 0:
            logging.warning(f"[WARN] No video found in {cam_dir}, skipping this camera.")
            continue

        if len(mp4s) > 1:
            logging.warning(f"[WARN] Multiple videos in {cam_dir}, using the first one.")

        video_paths.append(str(mp4s[0]))
        valid_indices.append(i)

    if len(video_paths) == 0:
        raise RuntimeError("No valid cameras found.")

    logging.info(f"Using cameras: {valid_indices}")
    return video_paths


def detect_gpu_count():
    """Return the number of available NVIDIA GPUs, or 0 if none/nvidia-smi missing."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return len([l for l in result.stdout.strip().splitlines() if l.strip()])
    except Exception:
        pass
    return 0


def probe_gpu_decoder():
    """Return True if h264_cuvid is listed in FFmpeg's decoders."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-decoders"], capture_output=True, text=True, timeout=5
        )
        return "h264_cuvid" in result.stdout or "hevc_cuvid" in result.stdout
    except Exception:
        return False


def extract_frames_gpu(video_path_output):
    """
    Extract frames using CUDA GPU decoding. gpu_id is assigned cyclically across
    available GPUs so 11 cameras spread over 3 GPUs automatically.
    Falls back to CPU if the GPU command fails.
    """
    video_path, output_dir, gpu_id = video_path_output
    video_path = str(video_path)
    output_dir = str(output_dir)

    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    output_pattern = os.path.join(output_dir, "%06d.jpg")
    video_name = os.path.basename(video_path)

    name_lower = video_name.lower()
    if name_lower.endswith(".hevc") or name_lower.endswith(".h265") or "hevc" in name_lower:
        decoder = "hevc_cuvid"
    else:
        decoder = "h264_cuvid"

    gpu_cmd = [
        "ffmpeg", "-hwaccel", "cuda", "-hwaccel_device", str(gpu_id),
        "-c:v", decoder, "-vsync", "0", "-i", video_path,
        "-q:v", "1", output_pattern, "-loglevel", "error",
    ]
    logging.info(f"[{video_name}] Starting FFmpeg GPU{gpu_id} extraction...")
    ret = subprocess.run(gpu_cmd, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode

    if ret != 0:
        logging.warning(f"[{video_name}] GPU extraction failed (exit {ret}), falling back to CPU...")
        cpu_cmd = [
            "ffmpeg", "-threads", "0", "-vsync", "0", "-i", video_path,
            "-q:v", "1", output_pattern, "-loglevel", "error",
        ]
        subprocess.run(cpu_cmd, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    logging.info(f"[{video_name}] Extraction complete → {output_dir}")
    return video_path


def extract_frames_cpu(video_path_output):
    """
    Extract frames from a video using CPU only (no GPU).
    Each video runs in its own process; threads_per_worker divides cores evenly across workers.
    """
    video_path, output_dir, threads_per_worker = video_path_output
    video_path = str(video_path)
    output_dir = str(output_dir)

    # Clean and recreate directory
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    output_pattern = os.path.join(output_dir, "%06d.jpg")
    video_name = os.path.basename(video_path)

    cmd = [
        "ffmpeg", "-threads", str(threads_per_worker), "-vsync", "0",
        "-i", video_path, "-q:v", "1", output_pattern, "-loglevel", "error",
    ]
    logging.info(f"[{video_name}] Starting FFmpeg extraction...")
    subprocess.run(cmd, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    logging.info(f"[{video_name}] Extraction complete → {output_dir}")
    return video_path


def extract_frames(video_path, output_dir, gpu=True, script_only=False):
    """
    Extract exact original frames from a video using FFmpeg.
    - Uses GPU decoding if available (hevc_cuvid / h264_cuvid)
    - Preserves all original frames (no resampling)
    - Optionally generates and saves a .sh script instead of executing directly
    """

    # Remove output dir if exists
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    video_name = os.path.basename(video_path)
    output_pattern = os.path.join(output_dir, "%06d.jpg")

    # Decide GPU codec
    # ffprobe can tell us, but we infer from extension
    if gpu:
        if video_name.lower().endswith(".hevc") or video_name.lower().endswith(".h265") or "hevc" in video_name.lower():
            decoder = "hevc_cuvid"
        else:
            decoder = "h264_cuvid"
    else:
        decoder = "h264"

    cmd = (
        f'ffmpeg -vsync 0 -i "{video_path}" -q:v 1 "{output_pattern}"'
    )

    # Save a script for reproducibility
    script_path = os.path.join(output_dir, f"extract_{Path(video_name).stem}.sh")
    with open(script_path, "w") as f:
        f.write("#!/bin/bash\n")
        f.write(cmd + "\n")

    os.chmod(script_path, 0o755)

    if script_only:
        logging.info(f"[{video_name}] Extraction script saved to {script_path}")
        return

    logging.info(f"Running FFmpeg GPU extraction for {video_name}")
    start_time = time.time()

    ret = os.system(cmd)
    if ret != 0:
        raise RuntimeError(f"FFmpeg extraction failed for {video_path}")

    elapsed = time.time() - start_time
    logging.info(f"[{video_name}] Extracted frames → {output_dir} in {elapsed:.1f}s")


def raw_session_slug(data_root: str) -> str:
    """
    .../data/sync0403/raw → sync0403
    .../my_dataset → my_dataset
    """
    p = Path(data_root).resolve()
    name = p.name
    if name.lower() == "raw" and p.parent != p:
        slug = p.parent.name
    else:
        slug = name
    slug = re.sub(r"[^\w\-]+", "_", slug, flags=re.ASCII).strip("_") or "session"
    return slug[:80]


def format_threshold_filename(threshold_ns):
    """Convert nanoseconds to a compact filename-safe string like 30ms or 250us."""
    if threshold_ns % 1e9 == 0:
        return f"{int(threshold_ns / 1e9)}s"
    elif threshold_ns % 1e6 == 0:
        return f"{int(threshold_ns / 1e6)}ms"
    elif threshold_ns % 1e3 == 0:
        return f"{int(threshold_ns / 1e3)}us"
    else:
        return f"{threshold_ns}ns"


def parse_duration_ns(duration_str):
    """Parse human-friendly duration strings to nanoseconds."""
    units = {
        "ns": 1,
        "us": int(1e3),
        "ms": int(1e6),
        "s":  int(1e9)
    }

    match = re.fullmatch(r"(\d+(?:\.\d+)?)(ns|us|ms|s)", duration_str.strip())
    if not match:
        raise ValueError(f"Invalid duration format: '{duration_str}'")

    value, unit = match.groups()
    return int(float(value) * units[unit])


def setup_logger(log_file_path):
    """
    Configures logging to write messages to both a file and the terminal.
    If the file exists, new messages will be appended.
    """
    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] %(message)s',
        handlers=[
            logging.FileHandler(log_file_path, mode='a', encoding='utf-8'),
            logging.StreamHandler()
        ],
        force=True
    )


def load_timestamp_csv_int_rows(csv_path: str, label: str = "") -> pd.DataFrame:
    """
    Load single-column timestamp CSV.

    - ``t`` (int64): sort/merge key only.
    - ``ts_exact`` (str): exact characters from the file for that row.

    Nanosecond values exceed float53's exact range (~9e15); ``merge_asof`` promotes
    columns with NaNs to float64 and corrupts large ints. Output columns must use
    ``ts_exact``, not float-rounded values.

    Rows that are not integers (e.g. analyze_vid POSTPROCESS pad lines) are dropped.
    """
    p = Path(csv_path)
    raw = pd.read_csv(p, header=None, names=["t_raw"], dtype=str, engine="python")
    raw["t_raw"] = raw["t_raw"].str.strip()
    t = pd.to_numeric(raw["t_raw"], errors="coerce")
    dropped = int(t.isna().sum())
    if dropped:
        logging.warning(
            "%sDropped %d non-integer row(s) from %s",
            f"[{label}] " if label else "",
            dropped,
            p,
        )
    valid = t.notna()
    if not valid.any():
        raise ValueError(f"No integer timestamp rows in {p}")
    out = pd.DataFrame(
        {
            "t": t[valid].astype(np.int64).to_numpy(),
            "ts_exact": raw.loc[valid, "t_raw"].to_numpy(),
        }
    ).reset_index(drop=True)
    return out


def match_frames_from_csv(csv_path_left, csv_path_right, output_csv_path, threshold_ns):
    """
    Match frame timestamps from two CSV files instead of image filenames.
    Each CSV should have one timestamp per line (as integer nanoseconds).
    """
    df_left = load_timestamp_csv_int_rows(csv_path_left, "left")
    df_right = load_timestamp_csv_int_rows(csv_path_right, "right")

    left = pd.DataFrame({
        't': df_left["t"],
        'left': df_left["ts_exact"],
    })

    right = pd.DataFrame({
        't': df_right["t"],
        'right': df_right["ts_exact"],
    })

    # Perform matching
    df = pd.merge_asof(
        left.sort_values('t'),
        right.sort_values('t'),
        on='t',
        tolerance=threshold_ns,
        allow_exact_matches=True,
        direction='nearest'
    )
    df = df.dropna()
    df = df.drop(columns=['t']).reset_index(drop=True)

    # Save result
    df.to_csv(output_csv_path, index=False)
    logging.info(f"Matched {df.shape[0]} frame pairs from CSVs (threshold = {threshold_ns / 1e6:.1f} ms) → {output_csv_path}")


def match_frames_full_from_csv(csv_path_left, csv_path_right, output_csv_path, threshold_ns):
    """
    Match all timestamps from left CSV with right CSV (full match), including unmatched.
    Each CSV should have one timestamp per line (as integer nanoseconds).
    """
    df_left = load_timestamp_csv_int_rows(csv_path_left, "left")
    df_right = load_timestamp_csv_int_rows(csv_path_right, "right")

    left_df = pd.DataFrame({
        't': df_left["t"],
        'left': df_left["ts_exact"],
    })

    right_df = pd.DataFrame({
        't': df_right["t"],
        'right': df_right["ts_exact"],
    })

    # Match frames with merge_asof
    matched_df = pd.merge_asof(
        left_df.sort_values('t'),
        right_df.sort_values('t'),
        on='t',
        tolerance=threshold_ns,
        allow_exact_matches=True,
        direction='nearest'
    )

    matched_df = matched_df.reset_index(drop=True)
    full_output_df = matched_df[['left', 'right']].copy()

    full_output_df['left'] = full_output_df['left'].fillna('').astype(str)
    full_output_df['right'] = full_output_df['right'].fillna('').astype(str)

    # Add match status
    full_output_df["matched"] = full_output_df["right"].apply(lambda x: x != "")
    full_output_df.to_csv(output_csv_path, index=False)

    logging.info(f"Full match completed: {len(full_output_df)} entries → {output_csv_path}")

def match_frames_from_csv_multi(csv_path_left, csv_path_right_list, output_csv_path, threshold_ns):
    df_left = load_timestamp_csv_int_rows(csv_path_left, "device_0")
    base_df = pd.DataFrame({"t": df_left["t"], "left": df_left["ts_exact"]})

    for idx, csv_path_right in enumerate(csv_path_right_list):
        df_right = load_timestamp_csv_int_rows(csv_path_right, f"device_{idx + 1}")
        shifted_idx = idx + 1
        right_df = pd.DataFrame({
            "t": df_right["t"],
            f"right{shifted_idx}": df_right["ts_exact"],
        })

        base_df = pd.merge_asof(
            base_df.sort_values('t'),
            right_df.sort_values('t'),
            on='t',
            tolerance=threshold_ns,
            allow_exact_matches=True,
            direction='nearest'
        )

    # Drop all rows with any missing values
    base_df.dropna(inplace=True)

    # Drop the 't' column used for merge key
    base_df.drop(columns=['t'], inplace=True)

    base_df.to_csv(output_csv_path, index=False, header=False)
    logging.info(f"Multi-match → {output_csv_path}")


def match_frames_full_from_csv_multi(csv_path_left, csv_path_right_list, output_csv_path, threshold_ns, duration_0):
    df_left = load_timestamp_csv_int_rows(csv_path_left, "device_0")
    base_df = pd.DataFrame({"t": df_left["t"], "left": df_left["ts_exact"]})

    # Merge each right stream
    for idx, csv_path_right in enumerate(csv_path_right_list, start=1):
        df_right = load_timestamp_csv_int_rows(csv_path_right, f"device_{idx}")
        right_df = pd.DataFrame({
            "t": df_right["t"],
            f"right{idx}": df_right["ts_exact"],
        })
        base_df = pd.merge_asof(
            base_df.sort_values('t'),
            right_df.sort_values('t'),
            on='t',
            tolerance=threshold_ns,
            allow_exact_matches=True,
            direction='nearest'
        )

    base_df = base_df.reset_index(drop=True)

    # Create matched flags for each right stream
    num_rights = len(csv_path_right_list)
    for idx in range(1, num_rights + 1):
        base_df[f'right{idx}'] = base_df[f'right{idx}'].fillna('').astype(str)
        base_df[f'matched_{idx}'] = base_df[f'right{idx}'].apply(lambda x: x != '')

    # We don't need 't' in the output
    base_df.drop(columns=['t'], inplace=True)

    # ---- Stats (generalized) ----
    total_frames = len(base_df)
    matched_counts = {idx: int(base_df[f'matched_{idx}'].sum()) for idx in range(1, num_rights + 1)}

    all_mask = base_df[[f'matched_{idx}' for idx in range(1, num_rights + 1)]].all(axis=1)
    any_mask = base_df[[f'matched_{idx}' for idx in range(1, num_rights + 1)]].any(axis=1)

    matched_all = int(all_mask.sum())
    matched_any = int(any_mask.sum())

    # matched_i_only: matched to stream i and to no other streams
    only_counts = {}
    for idx in range(1, num_rights + 1):
        others = [f'matched_{j}' for j in range(1, num_rights + 1) if j != idx]
        only_mask = base_df[f'matched_{idx}'] & (~base_df[others].any(axis=1))
        only_counts[idx] = int(only_mask.sum())

    # ---- Logging ----
    logging.info(f"Total frames in device_0: {total_frames}")
    for idx in range(1, num_rights + 1):
        logging.info(f"matched_{idx}: {matched_counts[idx]}")
    logging.info(f"matched_all: {matched_all}")
    for idx in range(1, num_rights + 1):
        logging.info(f"matched_{idx}_only: {only_counts[idx]}")
    logging.info(f"matched_any (matched to at least one right stream): {matched_any}")

    frame_rate_ratio = matched_all / duration_0 if duration_0 > 0 else 0
    logging.info(f"Output video FPS = matched_all / length of video 0 = {frame_rate_ratio:.2f}")

    # Save
    base_df.to_csv(output_csv_path, index=False)
    logging.info(f"Multi-match (full) → {output_csv_path}")


def parse_duration_seconds(duration_str):
    units = {
        'ns': 1e-9,
        'us': 1e-6,
        'ms': 1e-3,
        's': 1.0
    }
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(ns|us|ms|s)", duration_str.strip())
    if not match:
        raise ValueError(f"Invalid duration format: {duration_str}")
    value, unit = match.groups()
    return float(value) * units[unit]


def get_csv_path_from_video(video_path):
    """
    Given a video file path, returns the corresponding timestamp CSV file path
    by parsing the video name and assuming the CSV is located in the parent directory.
    """
    video_name = Path(video_path).stem
    match = re.search(r"VID_((\d|_)+)", video_name)
    if not match:
        raise ValueError(f"[ERROR] Video name format is incorrect: {video_name}")
    video_date = match.group(1)
    csv_path = Path(video_path).parent.parent / f"{video_date}.csv"
    return csv_path


def count_csv_nonempty_timestamp_lines(csv_path: Path) -> int:
    """Same rule as extract_frame_data: one timestamp per non-empty line."""
    with csv_path.open(encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def get_video_frame_count_opencv(video_path: str) -> int:
    cap = cv2.VideoCapture(video_path)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    return n


def _ffprobe_stream_int(video_path: str, *extra_entries: str) -> int | None:
    """Run ffprobe for a single integer stream property; return None if unavailable."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        *extra_entries,
        "-of", "default=nokey=1:noprint_wrappers=1", str(video_path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
        return int(out)
    except (ValueError, OSError):
        return None


def get_video_frame_count(video_path: str) -> int:
    """Reliable frame count via ffprobe.

    cv2's CAP_PROP_FRAME_COUNT returns 0 for some phone H.264/HEVC clips on
    Windows OpenCV builds, so prefer ffprobe: nb_frames (fast, from container
    metadata) when present, else -count_frames nb_read_frames (exact, decodes).
    """
    n = _ffprobe_stream_int(video_path, "-show_entries", "stream=nb_frames")
    if n is not None and n > 0:
        return n
    n = _ffprobe_stream_int(
        video_path, "-count_frames", "-show_entries", "stream=nb_read_frames"
    )
    if n is not None and n > 0:
        return n
    raise RuntimeError(f"Could not determine frame count via ffprobe for {video_path}")


def validate_extract_csv_vs_video(video_path: str) -> None:
    """
    FFmpeg will emit one image per frame; extract_frame_data renames using one CSV line per frame.
    Fail before extraction if counts disagree.
    """
    csv_path = get_csv_path_from_video(video_path)
    if not csv_path.is_file():
        raise FileNotFoundError(f"[ERROR] Timestamp CSV not found: {csv_path}")
    n_lines = count_csv_nonempty_timestamp_lines(csv_path)
    n_frames = get_video_frame_count_opencv(video_path)
    if n_lines != n_frames:
        raise ValueError(
            f"CSV vs video length mismatch for camera extracting from {video_path}:\n"
            f"  OpenCV reports {n_frames} frames in the video\n"
            f"  {csv_path} has {n_lines} non-empty lines\n"
            f"Fix the CSV or re-export the clip before running --extract."
        )


def extract_frame_data(target_dir, video_path):
    """
    Renames extracted frames in `target_dir` using timestamps
    from the original CSV associated with `video_path`.
    Assumes frame count == timestamp count.
    """
    csv_path = get_csv_path_from_video(video_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"[ERROR] Timestamp CSV not found: {csv_path}")

    timestamps = [line.strip() for line in csv_path.open() if line.strip()]
    target_dir = Path(target_dir)

    def natural_key(f):
        return [int(text) if text.isdigit() else text.lower()
                for text in re.split(r'(\d+)', f.name)]

    frame_files = sorted(
        [f for f in target_dir.iterdir() if f.suffix.lower() in ALLOWED_EXTENSIONS],
        key=natural_key
    )
    if len(frame_files) != len(timestamps):
        raise ValueError(
            f"Frame count ({len(frame_files)}) does not match timestamp count ({len(timestamps)})"
        )

    for frame_file, ts in zip(frame_files, timestamps):
        new_name = target_dir / f"{ts}{frame_file.suffix}"
        frame_file.rename(new_name)

    logging.info(
        f"[{video_path}] Renamed {len(frame_files)} frames in {target_dir} using original CSV timestamps."
    )


def extract_session_frames(video_paths, *, cpu_only=False, force=False, validate=True):
    """Extract frames for each camera into <cam>/images/ and rename to CSV timestamps.

    - Skips a camera whose images/ already exists unless ``force`` (the per-video
      worker then cleans + re-extracts that folder).
    - ``validate=True`` checks CSV-line vs video-frame counts before ffmpeg (used by
      the sync.py CLI). The orchestrator validates earlier via its own ffprobe gate
      and passes ``validate=False``; ``extract_frame_data`` still compares the actual
      extracted-frame count to CSV lines as the final backstop.
    - GPU decoding when available, else CPU (or force CPU with ``cpu_only``).
    """
    video_output_pairs = []
    for video_path in video_paths:
        output_dir = Path(video_path).parent.parent / "images"
        if output_dir.is_dir() and not force:
            logging.info(f"Skipping extraction: {output_dir} already exists")
            continue
        video_output_pairs.append((video_path, str(output_dir)))

    if not video_output_pairs:
        logging.info("No cameras need extraction (images/ already present for all).")
        return

    if validate:
        logging.info(
            "Checking CSV line count vs video frame count for each camera before extraction."
        )
        for video_path, _ in video_output_pairs:
            validate_extract_csv_vs_video(video_path)

    # --- Decide GPU vs CPU ---
    use_gpu = not cpu_only
    if use_gpu:
        gpu_count = detect_gpu_count()
        if gpu_count == 0 or not probe_gpu_decoder():
            logging.warning("No CUDA GPU decoder found; falling back to CPU extraction.")
            use_gpu = False

    extract_start = time.time()

    if use_gpu:
        logging.info(
            f"GPU extraction: {len(video_output_pairs)} camera(s) distributed across {gpu_count} GPU(s)"
        )
        tasks = [
            (vp, od, i % gpu_count)
            for i, (vp, od) in enumerate(video_output_pairs)
        ]
        with ProcessPoolExecutor(max_workers=len(tasks)) as executor:
            for video_path in executor.map(extract_frames_gpu, tasks):
                output_dir = Path(video_path).parent.parent / "images"
                extract_frame_data(str(output_dir), video_path)
    else:
        max_workers = min(len(video_output_pairs), multiprocessing.cpu_count())
        threads_per_worker = max(1, multiprocessing.cpu_count() // max_workers)
        logging.info(
            f"CPU extraction: {len(video_output_pairs)} camera(s), "
            f"{max_workers} worker(s) x {threads_per_worker} thread(s) each"
        )
        tasks = [(vp, od, threads_per_worker) for vp, od in video_output_pairs]
        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            for video_path in executor.map(extract_frames_cpu, tasks):
                output_dir = Path(video_path).parent.parent / "images"
                extract_frame_data(str(output_dir), video_path)

    elapsed = time.time() - extract_start
    logging.info(f"Extraction complete: {len(video_output_pairs)} camera(s) in {elapsed:.1f}s")


def main():
    # --- Parse command-line arguments ---
    args = parse_args()
    root = str(Path(args.root).expanduser().resolve())
    args.root = root
    num_videos = args.cams
    threshold_str = args.threshold
    threshold_ns = parse_duration_ns(threshold_str)
    extract_flag = args.extract

    # --- Collect video paths ---
    video_paths = collect_video_paths(root, num_videos)
    num_videos = len(video_paths)   # update real count
    video_path_0 = video_paths[0]
    video_name_0 = Path(video_path_0).stem

    session_slug = args.session_slug or raw_session_slug(root)
    threshold_str_for_filename = format_threshold_filename(threshold_ns)
    # e.g. <session>/output/sync0403_16ms/
    out_root = Path(args.output_root) if args.output_root else default_output_root(root)
    base_output = out_root / f"{session_slug}_{threshold_str_for_filename}"
    base_output.mkdir(parents=True, exist_ok=True)

    # --- Setup logger ---
    log_file_path = str(base_output / "match.log")
    setup_logger(log_file_path)
    logging.info("==== New matching session started ====")
    logging.info(f"Loaded {num_videos} video paths from command line")
    logging.info(f"Output directory: {base_output}")

    # --- Get video 0 duration ---
    cap = cv2.VideoCapture(video_path_0)
    frame_count_0 = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    duration_0 = frame_count_0 / fps if fps > 0 else 0
    cap.release()

    csv_paths = [get_csv_path_from_video(vp) for vp in video_paths]
    csv_0 = csv_paths[0]
    right_csvs = csv_paths[1:]

    if args.skip_match and not extract_flag:
        logging.warning(
            "--skip-match without --extract: no matching and no extraction; nothing to do."
        )
        return

    matched_csv_path = str(base_output / "matched.csv")
    matched_csv_full_path = str(base_output / "matched_full.csv")

    # --- Cross-camera match first (cheap); avoids heavy extract if matching fails ---
    if not args.skip_match:
        match_frames_from_csv_multi(csv_0, right_csvs, matched_csv_path, threshold_ns)
        match_frames_full_from_csv_multi(
            csv_0, right_csvs, matched_csv_full_path, threshold_ns, duration_0
        )
    else:
        logging.info("Skipping multi-match (--skip-match).")

    # --- Extract frames if requested (after match; validate CSV vs video length before ffmpeg) ---
    if extract_flag:
        extract_session_frames(video_paths, cpu_only=args.cpu, force=False, validate=True)
    else:
        logging.info("Frame extraction is disabled.")


if __name__ == "__main__":
    main()

