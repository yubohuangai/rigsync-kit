# Data format

What RigSync writes on each phone, and what the tools in `tools/` expect
and produce. Describes the RigSync build of 2026-09 (app repository commit
`2fe4dac`). If a later app build changes any of this, this file changes with
it.

## On each phone, per take

Two files, in the phone's shared storage (the folder is still called
`RecSync`, inherited from the upstream app this one was forked from):

| file | content |
|---|---|
| `RecSync/VID/VID_<stamp>.mp4` | the H.264 video of the take |
| `RecSync/<stamp>.csv` | one line per video frame, in frame order |

`<stamp>` is `yyyyMMdd_HHmmss` from **that phone's own clock** at the moment
recording started. Stamps of the same take therefore differ by a second or
two across phones (`20260826_020848` … `020850` in one real session). Pair
takes across phones by order, never by equal stamps.

Each CSV line is a single integer with no header and no other columns:

```
1787731726253701689
1787731726287038279
1787731726320374867
```

It is the frame's sensor timestamp in **nanoseconds on the leader's clock**.
The app keeps every client's clock aligned to the leader (Phone 1), and the
phase alignment shifts each client's frame timing onto the leader's, so
values from different phones are directly comparable: two frames with nearly
equal values were exposed at nearly the same instant. The CSV has exactly as
many lines as the video has frames; the tools check this before doing
anything (see the metadata gate below).

## Layout the tools expect

```
<session>/raw/
├── 01/
│   ├── VID/VID_20260826_020850.mp4
│   └── 20260826_020850.csv      # stem = video stem without the VID_ prefix
├── 02/
│   ├── VID/VID_20260826_020849.mp4
│   └── 20260826_020849.csv
└── …
```

- Camera folder = the phone's two-digit number. Folder `01` is the reference
  camera: the first column of `matched.csv` and the camera every other one is
  matched against.
- One take per `raw/` folder. If a `VID/` holds several videos the tools take
  the first by name and warn; make one session folder per take instead.
- Videos named `*_ori.mp4` are ignored (crop backups from a cropping step).
- A camera folder with an empty `VID/` is skipped.

## What the tools produce

`tools/run_sync_pipeline.py <session>/raw --threshold 16ms` runs five
stages and writes:

| path | content |
|---|---|
| `<session>/output/<session>_16ms/matched.csv` | one row per synchronized moment; one column per camera in folder order; each value is that camera's CSV timestamp for the frame, verbatim. No header. |
| `<session>/output/<session>_16ms/matched_full.csv` | the same match with a header, empty cells where a camera had no partner, and one `matched_N` flag column per camera |
| `<session>/output/<session>_16ms/pipeline.log` | the run log with per-camera match counts |
| `<session>/raw/NN/images/<timestamp>.jpg` | every extracted frame, named by its CSV timestamp |
| `<session>/raw/NN/images/unmatched/` | frames that had no partner within the threshold on some camera |

A frame's file name equals its CSV value, so a row of `matched.csv` names one
image file per camera: the synchronized multi-view sample.

**Matching rule.** For each reference frame, the nearest frame of every other
camera is taken if its timestamp differs by at most the threshold; a row
survives into `matched.csv` only if every camera has a partner. 16 ms is just
under half of the 33.3 ms frame period at 30 fps, so each reference frame can
claim at most one partner per camera.

**Metadata gate.** Before touching the disk the pipeline compares each
video's frame count (ffprobe) with its CSV line count and stops on any
mismatch, printing the table. A mismatch means a take was cut short on that
phone; reconcile explicitly with `tools/analyze_vid.py --truncate` (drop
excess CSV lines) or `--pad_csv` (add marker lines, which the matcher then
drops), then re-run.
