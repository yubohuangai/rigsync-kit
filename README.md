# RigSync kit

How to record a synchronized take on the 11 × Pixel 7 rig, and how to turn
the files off the phones into frame-aligned image sequences. The RigSync app
is already installed on the rig's phones.

**Roles.** Phone 1 hosts the Wi-Fi hotspot and is therefore the **leader**,
the only phone with controls; Phones 2 to 11 join as **clients**. The hotspot
name and password are on the label on Phone 1's case.

## Field guide

| step | on | do | expect on screen | wait |
|---|---|---|---|---|
| 1 | Phone 1 | turn the Wi-Fi hotspot on, **then** open RigSync | header `01 <ip> LEADER`, status `wifi AP`, three buttons and two sliders  | about 15 s |
| 2 | Phones 2 to 11 | join Phone 1's hotspot, then open RigSync | header `SYNCED`, line `leader <ip> reply … offset … ago`; clients have no buttons | about 10 s each |
| 3 | Phone 1 | drag the **exposure** and **ISO** sliders | previews neither too bright nor too dark on every phone; a value is sent to all phones when you release the slider | |
| 4 | Phone 1 | tap **ALIGN PHASES** | `phase err` on every phone goes from red (tens of ms) to green (about 0.1 ms or less), with `aligned: err … after N injections` | about 10 s |
| 5 | Phone 1 | tap **RECORD VIDEO**; tap it again to stop | button turns red `RECORDING…`, status `● REC mm:ss`; clients show a toast `Started recording video` | the take |
| 6 | Phone 1 | tap **RESET ALL** once it no longer reads `Waiting` | every phone restarts the app and comes back `SYNCED`; then redo steps 3 and 4 before the next take | about 15 s |

`phase err` is how far this phone's frame timing sits from the leader's;
`injections` are the small frame-timing nudges the app uses to close it.

![Leader in standby](docs/img/leader-standby.png)

**The leader in standby** (here Phone 07 on the bench): header `LEADER`,
status `wifi AP`, the three controls and the two sliders. `phase err
−13.33 ms` in red is the state before alignment; after step 4 it is green.

### Rules of a take

- Hands off every phone from a few seconds before start to a few seconds
  after stop. Touching a phone disturbs its timing, and the damage shows up
  only in post-processing.
- One take per reset: RESET ALL, then exposure, then ALIGN PHASES, then record.
- Before recording, read the status line on the leader: `therm OK`, a battery
  level you can finish the take on, `drops 0`.

### What can go wrong

| symptom | cause | fix |
|---|---|---|
| Phone 1 shows `SYNCED` or no buttons | the app was opened before the hotspot was up, so it did not detect itself as leader | close the app, confirm the hotspot is on, reopen |
| a client never shows `SYNCED` | not on Phone 1's hotspot | check its Wi-Fi, then reopen the app |
| `phase err` stays red on one phone after ALIGN PHASES | that phone missed the alignment | tap ALIGN PHASES again; if it persists, RESET ALL |
| `drops` counting up on the leader | Wi-Fi congestion | move Phone 1 to the middle of the rig, away from other radios |
| RESET ALL reads `Waiting` | the button is gated for a few seconds after the app starts | wait for it to read `RESET ALL` |

## Getting the files off the phones

Each phone saves one video and one timestamp file per take in its shared
storage (the folder name `RecSync` is inherited from the upstream app):

```
RecSync/VID/VID_20260826_020850.mp4
RecSync/20260826_020850.csv
```

Copy them by USB or with `adb pull /sdcard/RecSync/`, and arrange them by
phone number, one take per session folder:

```
<session>/raw/01/VID/VID_20260826_020850.mp4
<session>/raw/01/20260826_020850.csv
<session>/raw/02/VID/VID_20260826_020849.mp4
<session>/raw/02/20260826_020849.csv
…
```

The stamps differ by a second or two across phones because each phone names
files by its own clock; pair takes by order. Details in
[docs/FORMAT.md](docs/FORMAT.md).

Then the tools here turn those files into frame-aligned image sequences
(Python 3.10+, with `ffmpeg` and `ffprobe` on the PATH):

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python tools/run_sync_pipeline.py <session>/raw --threshold 16ms
```

Each camera's `images/` then holds only the synchronized frames, and a row of
`<session>/output/<session>_16ms/matched.csv` names one image per camera. Run
`--help` on any tool for its flags.

## License

MIT, see [LICENSE](LICENSE). The app itself is not part of this repository;
it is a fork of [RecSync-android](https://github.com/MobileRoboticsSkoltech/RecSync-android).
