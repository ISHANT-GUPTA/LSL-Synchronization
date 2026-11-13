#!/usr/bin/env python3
"""Load a LabRecorder XDF of camera streams and line the videos up.

What it does
------------
1. Loads the XDF with pyxdf. pyxdf applies LSL's clock-offset correction,
   so every stream's timestamps are on one shared timeline.
2. For every 'VideoFrames' stream (from camera_stream.py), reads which video
   file it belongs to (stored in the stream metadata) and pairs each frame
   in that file with its LSL timestamp.
   If the session has a Pupil Core (lib/pupil_stream.py) that recorded, its
   world camera is added as one more camera: the pupil_clock stream maps
   Pupil time to LSL time, which converts the world video's own timestamps.
   The gaze and pupil streams get a short health summary.
3. Writes a side-by-side video. On a common 30 fps timeline, each output
   frame shows, for every camera, the frame that was captured closest to
   that moment. The overlay shows each frame's timestamp, so you can check
   the alignment by eye.
4. Plots, per camera, the frame brightness and the amount of motion
   against LSL time. A flash (brightness) or a clap (motion) shows up as a
   spike in both cameras at the same time. The script estimates each spike's
   onset and the offset between cameras.

Usage
-----
    .venv/bin/python analyze_xdf.py recordings/20261113_101500_pilot1   # a session folder
    .venv/bin/python analyze_xdf.py path/to/recording.xdf
    .venv/bin/python analyze_xdf.py path/to/recording.xdf --out-dir results
(on Windows: .venv\\Scripts\\python.exe analyze_xdf.py ...)

Results are written into the session folder that holds the videos (made by
record_session.py) unless --out-dir is given.
"""

import argparse
import sys
from pathlib import Path

import cv2
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pyxdf

TILE_W, TILE_H = 640, 360   # size of each camera in the side-by-side video
OUT_FPS = 30.0              # frame rate of the side-by-side video
ANALYSIS_W = 160            # frames are shrunk to this width for brightness/motion


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("xdf", help="XDF file recorded by LabRecorder, or a session folder containing one")
    p.add_argument("--out-dir", default=None,
                   help="where to write results (default: the session folder holding the "
                        "videos, or next to the XDF if the videos are in different folders)")
    p.add_argument("--search", type=float, nargs=2, metavar=("START", "END"), default=None,
                   help="only look for the flash between these times (seconds from the start "
                        "of the shared window), e.g. --search 12 16")
    return p.parse_args()


PUPIL_TYPES = {"Gaze", "Pupil", "PupilClock"}  # streams from lib/pupil_stream.py
AUDIO_ENV_S = 0.001  # audio loudness is measured in 1 ms bins


def camera_streams(streams):
    """Return one dict per VideoFrames stream: name, timestamps, frame indices, video path."""
    cams = []
    for s in streams:
        info = s["info"]
        name = info["name"][0]
        if info["type"][0] != "VideoFrames":
            if info["type"][0] not in PUPIL_TYPES | {"Audio"}:
                print(f"  skipping stream '{name}' (type {info['type'][0]})")
            continue
        if len(s["time_stamps"]) == 0:
            print(f"  skipping stream '{name}': no samples")
            continue
        try:
            video = Path(info["desc"][0]["camera"][0]["video_file"][0])
        except (KeyError, IndexError, TypeError):
            print(f"  skipping stream '{name}': no video_file in its metadata")
            continue
        if not video.exists():
            print(f"  skipping stream '{name}': video file not found: {video}")
            continue
        cams.append({
            "name": name,
            "ts": np.asarray(s["time_stamps"], dtype=float),
            "frame_idx": np.asarray(s["time_series"])[:, 0].astype(int),
            "video": video,
        })
    return cams


def pupil_world_camera(streams):
    """The Pupil Core's world camera as one more camera, with its frames in LSL time.

    Pupil Capture stamps world frames in Pupil time (world_timestamps.npy).
    The pupil_clock stream holds (Pupil time, LSL time) pairs measured during
    the session; a straight-line fit through them converts Pupil time to LSL time.
    """
    clock = next((s for s in streams if s["info"]["type"][0] == "PupilClock"), None)
    if clock is None:
        return None
    prefix = clock["info"]["name"][0].removesuffix("_clock")
    try:
        rec = clock["info"]["desc"][0]["pupil"][0]["recording_dir"][0]
    except (KeyError, IndexError, TypeError):
        rec = None
    if not rec:
        print(f"  {prefix}: Pupil Capture did not record (record = false?), so no world video")
        return None
    rec = Path(rec)
    video, stamps = rec / "world.mp4", rec / "world_timestamps.npy"
    if not (video.exists() and stamps.exists()):
        print(f"  {prefix}: world video not found in {rec}")
        return None
    pupil_t = np.asarray(clock["time_series"], dtype=float)[:, 0]
    lsl_t = np.asarray(clock["time_stamps"], dtype=float)
    if len(lsl_t) < 2:
        print(f"  {prefix}: too few pupil_clock samples to convert the world video's timestamps")
        return None
    slope, intercept = np.polyfit(pupil_t, lsl_t, 1)
    resid_ms = 1000 * np.abs(lsl_t - (slope * pupil_t + intercept)).max()
    world_lsl = slope * np.load(stamps) + intercept
    # Only frames recorded while the XDF was (clock samples come every ~2 s).
    keep = np.nonzero((world_lsl >= lsl_t[0] - 2.5) & (world_lsl <= lsl_t[-1] + 2.5))[0]
    print(f"  {prefix}: world video {video.parent.name}/{video.name}, Pupil->LSL offset "
          f"{1000 * (intercept + (slope - 1) * pupil_t.mean()):+.3f} ms, "
          f"clock fit within {resid_ms:.3f} ms")
    if len(keep) < 2:
        print(f"  {prefix}: no world frames overlap the XDF recording")
        return None
    return {"name": f"{prefix}_world", "ts": world_lsl[keep], "frame_idx": keep, "video": video,
            "pupil": True}


def audio_tracks(streams):
    """One dict per Audio stream (lib/audio_stream.py): name, and loudness per 1 ms bin in LSL time.

    pyxdf has already fitted the timestamps to the stream's nominal rate
    (dejitter), which removes the sound card's clock drift and arrival jitter.
    """
    tracks = []
    for s in streams:
        if s["info"]["type"][0] != "Audio" or len(s["time_stamps"]) < 2:
            continue
        name = s["info"]["name"][0]
        ts = np.asarray(s["time_stamps"], dtype=float)
        x = np.abs(np.asarray(s["time_series"], dtype=np.float32)).max(axis=1) / 32768.0
        edges = np.arange(ts[0], ts[-1], AUDIO_ENV_S)
        idx = np.searchsorted(ts, edges)
        env = np.maximum.reduceat(x, idx[:-1]) if len(idx) > 1 else x[:1]
        rate = (len(ts) - 1) / (ts[-1] - ts[0])
        print(f"  {name}: audio {rate:.1f} Hz over {ts[-1] - ts[0]:.1f} s, peak "
              f"{20 * np.log10(max(x.max(), 1e-6)):.1f} dBFS")
        tracks.append({"name": name, "t": edges[:-1], "env": env})
    return tracks


def audio_onset(track, around, t0, half_window=0.5):
    """Start of the sharpest sound within +-half_window s of `around` (relative to t0).

    Returns (onset relative to t0, strength in dB above the background), or (None, 0).
    The onset is where loudness first rises 20% of the way from the background
    to the peak, just before the peak.
    """
    t = track["t"] - t0
    sel = np.where(np.abs(t - around) <= half_window)[0]
    if len(sel) < 50:
        return None, 0.0
    env = track["env"][sel]
    rise = np.diff(env, prepend=env[0])
    k = int(np.argmax(rise))                      # steepest jump in loudness
    before = env[max(0, k - 200):max(1, k - 5)]   # ~0.2 s before it
    floor = float(np.median(before)) if len(before) else float(env.min())
    peak = float(env[k:k + 30].max())
    strength = 20 * np.log10(max(peak, 1e-6) / max(floor, 1e-6))
    level = floor + 0.2 * (peak - floor)
    j = k
    while j > 0 and env[j - 1] > level:
        j -= 1
    return float(t[sel[j]]), strength


def summarize_pupil(streams):
    """Print rate and confidence of the Pupil Core gaze/pupil streams."""
    for s in streams:
        stype = s["info"]["type"][0]
        if stype not in ("Gaze", "Pupil"):
            continue
        name = s["info"]["name"][0]
        ts = np.asarray(s["time_stamps"], dtype=float)
        if len(ts) < 2:
            print(f"  {name}: no samples")
            continue
        data = np.asarray(s["time_series"], dtype=float)
        labels = [c["label"][0] for c in s["info"]["desc"][0]["channels"][0]["channel"]]
        conf, eye = data[:, labels.index("confidence")], data[:, labels.index("eye")]
        per_eye = ", ".join(
            f"{'both' if e == 2 else f'eye{int(e)}'} {int((eye == e).sum())} samples, "
            f"median confidence {np.median(conf[eye == e]):.2f}"
            for e in np.unique(eye))
        print(f"  {name}: {len(ts) / (ts[-1] - ts[0]):.0f} Hz over {ts[-1] - ts[0]:.1f} s; {per_eye}")


def read_frames(cam):
    """Read the frames of the video that fall inside the recorded LSL window.

    Frame k of the video file is the sample with frame_index == k, because
    camera_stream.py writes and stamps frames in the same order starting at 0.
    """
    wanted = dict(zip(cam["frame_idx"], range(len(cam["frame_idx"]))))
    first, last = cam["frame_idx"].min(), cam["frame_idx"].max()
    cap = cv2.VideoCapture(str(cam["video"]))
    cap.set(cv2.CAP_PROP_POS_FRAMES, first)
    tiles = [None] * len(cam["frame_idx"])
    small = [None] * len(cam["frame_idx"])
    k = first
    while k <= last:
        ok, frame = cap.read()
        if not ok:
            break
        if k in wanted:
            j = wanted[k]
            tiles[j] = cv2.resize(frame, (TILE_W, TILE_H), interpolation=cv2.INTER_AREA)
            h = int(frame.shape[0] * ANALYSIS_W / frame.shape[1])
            small[j] = cv2.cvtColor(cv2.resize(frame, (ANALYSIS_W, h), interpolation=cv2.INTER_AREA),
                                    cv2.COLOR_BGR2GRAY).astype(np.float32)
        k += 1
    cap.release()
    missing = sum(t is None for t in tiles)
    if missing:
        print(f"  WARNING: {cam['name']}: {missing} frames listed in the XDF are missing from the video")
    keep = [i for i, t in enumerate(tiles) if t is not None]
    cam["ts"] = cam["ts"][keep]
    cam["frame_idx"] = cam["frame_idx"][keep]
    cam["tiles"] = [tiles[i] for i in keep]
    cam["small"] = [small[i] for i in keep]


def frame_signals(cam):
    """Mean brightness per frame, and motion = mean absolute change from the previous frame."""
    small = np.stack(cam["small"])
    brightness = small.mean(axis=(1, 2))
    motion = np.concatenate([[0.0], np.abs(np.diff(small, axis=0)).mean(axis=(1, 2))])
    return brightness, motion


def robust_z(x):
    """How unusual each value is compared with the typical spread (median/MAD z-score)."""
    mad = np.median(np.abs(x - np.median(x))) or 1e-9
    return (x - np.median(x)) / (1.4826 * mad)


def find_common_flash(cams, signals, t0, search=None):
    """Find the moment where brightness jumps up in EVERY camera at once.

    Looking at cameras one by one fails when someone moves a lot in front of one
    of them; requiring the jump in all cameras at once picks out the flash.
    Returns the approximate event time (relative to t0) and its strength (the
    weakest camera's z-score, so it is only high if every camera saw it).
    """
    grid = np.arange(0, min(c["ts"][-1] for c in cams) - t0, 0.002)
    per_cam = []
    for cam in cams:
        t = cam["ts"] - t0
        z = robust_z(np.diff(signals[cam["name"]][0]))
        # Spread each jump over +-1 frame so cameras whose frames don't line up
        # exactly still overlap on the grid.
        z_grid = np.interp(grid, t[1:], z)
        z_grid = np.maximum.reduce([np.roll(z_grid, k) for k in range(-17, 18)])
        per_cam.append(z_grid)
    common = np.min(per_cam, axis=0)
    if search is not None:
        common[(grid < search[0]) | (grid > search[1])] = -np.inf
    i = int(np.argmax(common))
    return grid[i], common[i]


def onset_time(t, brightness, around, half_window=0.3):
    """Sub-frame time at which brightness crosses halfway between before and after the jump.

    Uses only frames within +-half_window seconds of `around`.
    """
    sel = np.where(np.abs(t - around) <= half_window)[0]
    if len(sel) < 4:
        return None
    b = brightness[sel]
    k = int(np.argmax(np.diff(b)))            # steepest rise inside the window
    before = np.median(b[max(0, k - 5):k + 1])
    after = np.max(b[k + 1:k + 4])
    half = (before + after) / 2
    for j in range(max(0, k - 2), len(b) - 1):
        if b[j] < half <= b[j + 1]:
            frac = (half - b[j]) / (b[j + 1] - b[j])
            return t[sel[j]] + frac * (t[sel[j + 1]] - t[sel[j]])
    return t[sel[k + 1]]


def write_side_by_side(cams, t0, t1, out_path):
    grid_t = np.arange(t0, t1, 1.0 / OUT_FPS)
    writer = cv2.VideoWriter(str(out_path), cv2.VideoWriter_fourcc(*"mp4v"), OUT_FPS,
                             (TILE_W * len(cams), TILE_H))
    for t in grid_t:
        tiles = []
        shown_ts = []
        for cam in cams:
            # Nearest frame in time to the grid point.
            j = int(np.clip(np.searchsorted(cam["ts"], t), 1, len(cam["ts"]) - 1))
            if abs(cam["ts"][j - 1] - t) <= abs(cam["ts"][j] - t):
                j -= 1
            tile = cam["tiles"][j].copy()
            shown_ts.append(cam["ts"][j])
            label = f"{cam['name']}  frame {cam['frame_idx'][j]}  t={cam['ts'][j]:.3f}s"
            cv2.rectangle(tile, (0, 0), (TILE_W, 28), (0, 0, 0), -1)
            cv2.putText(tile, label, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
            tiles.append(tile)
        out = np.hstack(tiles)
        spread_ms = (max(shown_ts) - min(shown_ts)) * 1000
        footer = f"timeline t={t - t0:7.3f}s   frames shown are {spread_ms:4.1f} ms apart"
        cv2.rectangle(out, (0, TILE_H - 26), (out.shape[1], TILE_H), (0, 0, 0), -1)
        cv2.putText(out, footer, (8, TILE_H - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1, cv2.LINE_AA)
        writer.write(out)
    writer.release()
    return len(grid_t)


def main():
    args = parse_args()
    xdf_path = Path(args.xdf)
    if xdf_path.is_dir():
        found = sorted(xdf_path.glob("*.xdf"))
        if len(found) != 1:
            sys.exit(f"Expected one .xdf in {xdf_path}, found {len(found)}"
                     + (": " + ", ".join(p.name for p in found) if found else ""))
        xdf_path = found[0]
    elif not xdf_path.exists():
        sys.exit(f"Not found: {xdf_path}")

    print(f"Loading {xdf_path}")
    streams, _ = pyxdf.load_xdf(str(xdf_path))
    cams = camera_streams(streams)
    world = pupil_world_camera(streams)
    if world:
        cams.append(world)
    if not cams:
        sys.exit("No usable VideoFrames streams found.")
    summarize_pupil(streams)
    audio = audio_tracks(streams)

    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        # Keep results with the recording session they belong to (the folder of
        # the camera videos; the Pupil world video sits deeper, in pupil/000/).
        folders = {cam["video"].parent for cam in cams if not cam.get("pupil")}
        out_dir = folders.pop() if len(folders) == 1 else xdf_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Results go to {out_dir}")

    for cam in cams:
        print(f"  reading {cam['name']}: {cam['video'].name}")
        read_frames(cam)

    # Shared window: the part of the timeline every camera covers.
    t0 = max(c["ts"][0] for c in cams)
    t1 = min(c["ts"][-1] for c in cams)
    print(f"\nShared window: {t1 - t0:.2f} s")
    w = max(12, max(len(c["name"]) for c in cams) + 2)
    print(f"{'stream':<{w}}{'frames':>8}{'fps':>8}{'gaps':>6}   first frame in window")
    for cam in cams:
        dt = np.diff(cam["ts"])
        gaps = int((dt > 1.5 * np.median(dt)).sum())
        first = int(np.searchsorted(cam["ts"], t0))
        # Average rate (frames / time): the median interval misleads for cameras
        # whose frame spacing alternates (the Pupil world camera: ~32 and ~36 ms).
        fps = (len(cam["ts"]) - 1) / (cam["ts"][-1] - cam["ts"][0])
        print(f"{cam['name']:<{w}}{len(cam['ts']):>8}{fps:>8.2f}{gaps:>6}   "
              f"frame {cam['frame_idx'][first]} of {cam['video'].name}")

    # Side-by-side video.
    sbs_path = out_dir / (xdf_path.stem + "_side_by_side.mp4")
    n = write_side_by_side(cams, t0, t1, sbs_path)
    print(f"\nSide-by-side video ({n} frames): {sbs_path}")

    # Brightness / motion signals, and the flash seen by all cameras.
    signals = {cam["name"]: frame_signals(cam) for cam in cams}
    event_t, strength = find_common_flash(cams, signals, t0, args.search)
    onsets = {cam["name"]: onset_time(cam["ts"] - t0, signals[cam["name"]][0], event_t)
              for cam in cams}

    fig, axes = plt.subplots(3 if audio else 2, 1, figsize=(12, 8.5 if audio else 6), sharex=True)
    for cam in cams:
        b, m = signals[cam["name"]]
        t = cam["ts"] - t0
        axes[0].plot(t, b, label=cam["name"])
        axes[1].plot(t, m, label=cam["name"])
    for tr in audio:
        keep = (tr["t"] >= t0) & (tr["t"] <= t1)
        axes[2].plot(tr["t"][keep] - t0, tr["env"][keep], lw=0.6, label=tr["name"])
    if audio:
        axes[2].set_ylabel("audio loudness (peak per ms)")
    for ax in axes:
        ax.axvline(event_t, color="k", ls="--", lw=0.8, alpha=0.6)
    axes[0].set_ylabel("mean brightness")
    axes[1].set_ylabel("motion (frame difference)")
    axes[-1].set_xlabel("time since start of shared window (s)")
    for ax in axes:
        ax.legend()
        ax.grid(alpha=0.3)
    fig.suptitle(xdf_path.name)
    fig.tight_layout()
    plot_path = out_dir / (xdf_path.stem + "_signals.png")
    fig.savefig(plot_path, dpi=120)
    print(f"\nPlot: {plot_path}")

    print(f"\nFlash seen by all cameras at ~{event_t:.3f} s (strength {strength:.1f}; "
          f"below ~8 means no clear common flash, so ignore the numbers below)")
    ref = cams[0]["name"]
    frame_ms = 1000 * np.median(np.diff(cams[0]["ts"]))
    print(f"{'stream':<{w}}{'onset (s)':>11}{'vs ' + ref:>16}")
    for cam in cams:
        o = onsets[cam["name"]]
        if o is None or onsets[ref] is None:
            print(f"{cam['name']:<{w}}{'n/a':>11}")
            continue
        print(f"{cam['name']:<{w}}{o:>11.4f}{1000 * (o - onsets[ref]):>+13.1f} ms")
    for tr in audio:
        o, s = audio_onset(tr, event_t, t0)
        if o is None or onsets[ref] is None or s < 6:  # no sound event at all near the flash
            print(f"{tr['name']:<{w}}{'n/a':>11}")
            continue
        print(f"{tr['name']:<{w}}{o:>11.4f}{1000 * (o - onsets[ref]):>+13.1f} ms   "
              f"(sound {s:.0f} dB above background"
              + ("; below ~20 dB is not a clear click, ignore" if s < 20 else "") + ")")
    if audio:
        print(" Audio: the light switch's click is the sound event. A camera onset can lag the click\n"
              " by up to a frame (33 ms), since light changes during a frame show up half-way.")
    print(f"(one frame = {frame_ms:.0f} ms; offsets well under a frame mean the streams are in sync.\n"
          f" Onsets are where brightness is halfway up, interpolated between frames.)")


if __name__ == "__main__":
    main()
