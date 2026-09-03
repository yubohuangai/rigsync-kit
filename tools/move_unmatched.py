import os
import sys
import csv
import pandas as pd
from tqdm import tqdm
import shutil
from collections import defaultdict
import re
from pathlib import Path


ALLOWED_EXTENSIONS = (".png", ".jpg", ".jpeg")

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))
from sync import default_output_root  # noqa: E402


import argparse


def raw_session_slug(data_root: str) -> str:
    """
    .../data/0411/raw -> 0411
    .../my_dataset -> my_dataset
    """
    p = Path(data_root).resolve()
    name = p.name
    if name.lower() == "raw" and p.parent != p:
        slug = p.parent.name
    else:
        slug = name
    slug = re.sub(r"[^\w\-]+", "_", slug, flags=re.ASCII).strip("_") or "session"
    return slug[:80]


def default_matched_csv_path(raw_root: str, exp_threshold: str) -> Path:
    """<session>/output/<slug>_<threshold>/matched.csv (same convention as sync.py)."""
    slug = raw_session_slug(raw_root)
    return default_output_root(raw_root) / f"{slug}_{exp_threshold}" / "matched.csv"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Move unmatched images out of images/, or move them back (undo)."
    )
    parser.add_argument("root", help="Session root (.../raw with 01/, 02/, …).")
    parser.add_argument(
        "--exp-threshold",
        default="16ms",
        metavar="LABEL",
        help=(
            "Folder name segment after the session slug under output/exp/ when using "
            "the default --csv (default: 16ms → .../exp/<slug>_16ms/matched.csv)."
        ),
    )
    parser.add_argument(
        "--csv",
        default=None,
        help=(
            "Path to sync output matched.csv. If omitted (and not --move-back), defaults to "
            "<repo>/output/exp/<slug>_<exp-threshold>/matched.csv where <slug> is the folder "
            "above 'raw' (e.g. .../data/0411/raw → 0411)."
        ),
    )
    parser.add_argument(
        "--move-back",
        action="store_true",
        help=(
            "Undo: move every image from .../<cam>/images/unmatched/ back to .../<cam>/images/. "
            "Does not use --csv."
        ),
    )

    parser.add_argument(
        "--video_paths",
        required=False,
        nargs="+",
        help=(
            "List of video paths. Example:\n"
            " --video_paths /mnt/.../01/VID/a.mp4 /mnt/.../02/VID/b.mp4 ..."
        )
    )

    parser.add_argument(
        "--match_mode",
        default="full",
        choices=["first", "full"],
        help="Alignment mode"
    )

    args = parser.parse_args()
    args.root = str(Path(args.root).expanduser().resolve())
    if not args.move_back and args.csv is None:
        args.csv = str(default_matched_csv_path(args.root, args.exp_threshold))
    return args



def find_image_dirs(root):
    """
    Scan root directory for subfolders named like '01', '02', ..., '11'
    and return their 'images' subfolder.
    """
    img_dirs = []

    for entry in os.listdir(root):
        if re.fullmatch(r"\d{2}", entry):  # match 01, 02, 03 ... 11
            img_dir = os.path.join(root, entry, "images")
            if os.path.isdir(img_dir):
                img_dirs.append(img_dir)

    img_dirs.sort()  # ensure order 01→11
    return img_dirs

def check_duplicates(csv_path):
    """
    Check for duplicates across rows only.
    - Duplicates within the same row are ignored.
    - If a number appears in multiple rows, it’s treated as a duplicate.
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    number_rows = defaultdict(set)  # {number: set of row indices}

    with open(csv_path, newline='') as f:
        reader = csv.reader(f)
        for row_idx, row in enumerate(reader):
            unique_in_row = set(row)  # ignore within-row duplicates
            for num in unique_in_row:
                number_rows[num].add(row_idx)

    duplicates = {num: rows for num, rows in number_rows.items() if len(rows) > 1}

    if duplicates:
        print("Found duplicates across rows in CSV:")
        for num, rows in duplicates.items():
            print(f"{num} appears in rows {sorted(rows)}")
        return True
    return False


def infer_image_dirs_from_videos(video_paths):
    """
    video_path: /mnt/.../01/VID/xxx.mp4
    return:     /mnt/.../01/images
    """
    img_dirs = []
    for vp in video_paths:
        vid_dir = os.path.dirname(vp)        # .../01/VID
        cam_root = os.path.dirname(vid_dir)  # .../01
        img_dir = os.path.join(cam_root, "images")
        img_dirs.append(img_dir)
    return img_dirs


def move_unmatched_images(root, csv_path, match_mode="full"):
    if not csv_path or not os.path.exists(csv_path):
        raise FileNotFoundError(f"Matched CSV not found or not defined: {csv_path}")

    if match_mode not in ["first", "full"]:
        raise ValueError(f"Invalid match_mode: {match_mode}. Must be 'first' or 'full'.")

    match_df = pd.read_csv(csv_path, header=None)
    image_dirs = find_image_dirs(root)
    num_videos = len(image_dirs)

    if num_videos != match_df.shape[1]:
        print(f"Warning: CSV columns ({match_df.shape[1]}) != video count ({num_videos})")

    # --- Check input image dirs exist ---
    for img_dir in image_dirs:
        if not os.path.exists(img_dir):
            raise FileNotFoundError(f"Input image directory not found: {img_dir}. Aborting run.")

    unmatched_dirs = [os.path.join(d, "unmatched") for d in image_dirs]
    for d in unmatched_dirs:
        os.makedirs(d, exist_ok=True)

    # --- "full" mode ---
    if match_mode == "full":
        if check_duplicates(csv_path):
            print("Aborting move: duplicates found in CSV while in 'full' match mode.")
            return
        matched_ids = {i: set(match_df[i].astype(str).tolist()) for i in range(num_videos)}

    # --- "first" mode ---
    elif match_mode == "first":
        start_ids = [str(match_df.iloc[0, i]) for i in range(num_videos)]
        matched_ids = {}
        min_length = None

        for i, img_dir in enumerate(image_dirs):
            if not os.path.exists(img_dir):
                print(f"Warning: directory not found: {img_dir}")
                continue

            # Consider both jpg and png files
            files = sorted([f for f in os.listdir(img_dir) if f.lower().endswith(ALLOWED_EXTENSIONS)])
            frame_ids = [os.path.splitext(f)[0] for f in files]

            if start_ids[i] not in frame_ids:
                raise ValueError(f"Start ID {start_ids[i]} not found in {img_dir}")

            start_index = frame_ids.index(start_ids[i])
            aligned_ids = frame_ids[start_index:]  # from start to end
            matched_ids[i] = aligned_ids

            if min_length is None:
                min_length = len(aligned_ids)
            else:
                min_length = min(min_length, len(aligned_ids))

        for i in range(num_videos):
            matched_ids[i] = set(matched_ids[i][:min_length])

    # --- Move unmatched frames ---
    for i in range(num_videos):
        img_dir = image_dirs[i]
        unmatched_dir = unmatched_dirs[i]
        if not os.path.exists(img_dir):
            continue

        moved_count = 0
        for f in tqdm(os.listdir(img_dir), desc=f"Processing video {i+1}"):
            if f.lower().endswith(ALLOWED_EXTENSIONS):
                img_id = os.path.splitext(f)[0]
                if img_id not in matched_ids[i]:
                    src_path = os.path.join(img_dir, f)
                    dst_path = os.path.join(unmatched_dir, f)
                    shutil.move(src_path, dst_path)
                    moved_count += 1

        remaining = len([f for f in os.listdir(img_dir) if f.lower().endswith(ALLOWED_EXTENSIONS)])
        print(f"[Summary] Cam {i+1}: moved {moved_count} unmatched frames, {remaining} matched frames remain in {img_dir}")


def move_back_unmatched(root):
    """
    Undo move_unmatched: restore files from <root>/<cam>/images/unmatched/ to <root>/<cam>/images/.
    Refuses to overwrite an existing file in images/.
    """
    image_dirs = find_image_dirs(root)
    if not image_dirs:
        raise FileNotFoundError(f"No .../NN/images/ directories found under {root}")

    for i, img_dir in enumerate(image_dirs):
        unmatched_dir = os.path.join(img_dir, "unmatched")
        if not os.path.isdir(unmatched_dir):
            print(f"[Skip] Cam {i + 1}: no unmatched folder: {unmatched_dir}")
            continue

        to_move = [
            f
            for f in os.listdir(unmatched_dir)
            if f.lower().endswith(ALLOWED_EXTENSIONS)
        ]
        moved_count = 0
        for f in tqdm(to_move, desc=f"Move back cam {i + 1}"):
            src = os.path.join(unmatched_dir, f)
            dst = os.path.join(img_dir, f)
            if os.path.exists(dst):
                raise FileExistsError(
                    f"Refusing to overwrite existing file (remove or rename it first): {dst}"
                )
            shutil.move(src, dst)
            moved_count += 1

        # Remove unmatched dir if empty (ignore if still has non-image debris)
        try:
            if os.path.isdir(unmatched_dir) and not os.listdir(unmatched_dir):
                os.rmdir(unmatched_dir)
        except OSError:
            pass

        total_in_images = len(
            [f for f in os.listdir(img_dir) if f.lower().endswith(ALLOWED_EXTENSIONS)]
        )
        print(
            f"[Summary] Cam {i + 1}: moved back {moved_count} frames into {img_dir} "
            f"({total_in_images} image files there now)"
        )


if __name__ == "__main__":
    args = parse_args()
    if args.move_back:
        move_back_unmatched(args.root)
    else:
        move_unmatched_images(args.root, args.csv, args.match_mode)
