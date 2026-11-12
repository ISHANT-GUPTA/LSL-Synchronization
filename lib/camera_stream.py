#!/usr/bin/env python3
"""Record a webcam to a video file and publish per-frame timestamps over LSL.

Video itself is NOT sent over LSL (far too much data). Instead:
  * every frame is written to a local video file, and
  * for every frame, one LSL sample [frame_index, arrival_time] is pushed,
    stamped with the time the frame was captured.

After recording, LabRecorder's XDF tells you the exact LSL time of frame N
of the video file, which is what lets you line the video up with every
other stream.

Timestamps
----------
On Linux, the V4L2 driver stamps each frame with CLOCK_MONOTONIC when the
frame is captured. liblsl's local_clock() uses the same clock, so we use the
driver timestamp directly (it is ~25-30 ms earlier than the moment OpenCV
hands us the frame).

On Windows, the camera is opened with Media Foundation (MSMF), which stamps
each frame with QueryPerformanceCounter time, the clock liblsl's local_clock()
uses there. So the same approach works (the stamp is ~40 ms earlier than
arrival). DirectShow is not used: it gives no frame timestamps.

If the driver timestamp looks wrong, we fall back to local_clock() at arrival
and count how often that happened (CSV column timestamp_source).

Usage
-----
Normally you don't run this directly: record_session.py (project root)
starts one copy per camera listed in cameras.toml. To run one camera alone:
Linux:
    .venv/bin/python lib/camera_stream.py --device 0 --name laptop_cam
Windows:
    .venv\\Scripts\\python.exe lib\\camera_stream.py --device 0 --name laptop_cam

Press 'q' in the preview window, or Ctrl+C in the terminal, to stop.
"""

import argparse
import csv
import datetime as dt
import shutil
import signal
import socket
import subprocess
import sys
import os
import time
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

if IS_WINDOWS:
    # Must be set before cv2 is imported. With hardware transforms on, opening
    # some webcams through MSMF takes 10-30 s, or fails at higher resolutions.
    os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")

import cv2
import pylsl

# Driver timestamps further than this from local_clock() are treated as bogus.
MAX_TIMESTAMP_SKEW_S = 1.0

if IS_WINDOWS:
    CAPTURE_API, DRIVER_TS_LABEL = cv2.CAP_MSMF, "msmf"
elif IS_LINUX:
    CAPTURE_API, DRIVER_TS_LABEL = cv2.CAP_V4L2, "v4l2"
else:
    CAPTURE_API, DRIVER_TS_LABEL = cv2.CAP_ANY, "driver"


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--device", required=True,
                   help="camera index (0, 1, 2, ...); on Linux also a path (/dev/video2)")
    p.add_argument("--name", required=True,
                   help="LSL stream name and file prefix, e.g. usb_cam")
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--fps", type=float, default=30.0)
    p.add_argument("--out-dir", default="recordings",
                   help="folder for the video and CSV files (default: recordings/)")
    p.add_argument("--no-preview", action="store_true",
                   help="do not open a preview window")
    p.add_argument("--mains-hz", type=int, choices=(50, 60), default=50,
                   help="mains frequency for anti-flicker (India/Europe: 50, US: 60)")
    return p.parse_args()


def lock_frame_rate(device, mains_hz, name):
    """Stop auto-exposure from lowering the frame rate, and set anti-flicker.

    Many webcams ship with exposure_dynamic_framerate=1, which lets them drop
    to ~15 fps in dim light to expose longer. For synchronisation we need a
    steady frame rate, so turn it off (the image may get slightly darker or
    noisier; add light rather than re-enabling it).

    These are V4L2 driver settings: they reset when the camera is unplugged,
    so we apply them every time. Needs v4l2-ctl (Ubuntu package v4l-utils).

    On Windows there is no equivalent tool that OpenCV can drive, so we only
    warn; the 5-second fps report shows if the camera slows down.
    """
    if not IS_LINUX:
        print(f"[{name}] NOTE: frame rate and anti-flicker can only be locked on Linux. "
              f"If fps drops in dim light, add light.")
        return
    dev_path = f"/dev/video{device}" if str(device).isdigit() else str(device)
    if shutil.which("v4l2-ctl") is None:
        print(f"[{name}] WARNING: v4l2-ctl not found (install v4l-utils); cannot lock "
              f"the frame rate, camera may drop to 15 fps in dim light.")
        return
    wanted = {"exposure_dynamic_framerate": 0,
              "power_line_frequency": 1 if mains_hz == 50 else 2}
    supported = subprocess.run(["v4l2-ctl", "-d", dev_path, "--list-ctrls"],
                               capture_output=True, text=True).stdout
    for ctrl, value in wanted.items():
        if ctrl not in supported:
            continue  # this camera doesn't have the control: nothing to do
        r = subprocess.run(["v4l2-ctl", "-d", dev_path, "-c", f"{ctrl}={value}"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[{name}] WARNING: could not set {ctrl}={value}: {r.stderr.strip()}")


def open_camera(device, width, height, fps):
    dev = int(device) if str(device).isdigit() else device
    cap = cv2.VideoCapture(dev, CAPTURE_API)
    if not cap.isOpened():
        hint = " (is another app using it? check Settings > Privacy > Camera)" if IS_WINDOWS else ""
        sys.exit(f"Could not open camera {device!r}{hint}")
    # MJPG keeps USB bandwidth low enough to run two cameras at 720p at once.
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FPS, fps)
    # Keep the driver's default buffer count (V4L2: usually 4). With only 1
    # buffer the driver drops every other frame while we are busy writing the
    # previous one. Extra buffering adds no timing error, since we use the
    # driver's timestamp. (MSMF manages its own small queue; dropped frames
    # still show up as gaps because its timestamps are real capture times.)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    f = cap.get(cv2.CAP_PROP_FPS) or fps
    return cap, w, h, f


def make_outlet(name, device, w, h, fps, video_path):
    info = pylsl.StreamInfo(
        name=name,
        type="VideoFrames",
        channel_count=2,
        # Irregular rate: frames can be dropped, and this stops pyxdf from
        # "dejittering" (smoothing) the timestamps as if they were evenly spaced.
        nominal_srate=pylsl.IRREGULAR_RATE,
        channel_format=pylsl.cf_double64,
        source_id=f"{socket.gethostname()}:{name}",
    )
    desc = info.desc()
    chans = desc.append_child("channels")
    for label, unit in (("frame_index", "count"), ("arrival_time", "seconds")):
        ch = chans.append_child("channel")
        ch.append_child_value("label", label)
        ch.append_child_value("unit", unit)
    cam = desc.append_child("camera")
    cam.append_child_value("device", str(device))
    cam.append_child_value("width", str(w))
    cam.append_child_value("height", str(h))
    cam.append_child_value("nominal_fps", f"{fps:g}")
    cam.append_child_value("video_file", str(video_path.resolve()))
    cam.append_child_value("hostname", socket.gethostname())
    cam.append_child_value("capture_backend", DRIVER_TS_LABEL)
    return pylsl.StreamOutlet(info)


def main():
    sys.stdout.reconfigure(line_buffering=True)
    args = parse_args()

    # Install the stop handlers first, so a stop request that arrives while the
    # camera is still opening ends the run cleanly instead of killing the process
    # with the video file half-written.
    stop = False

    def handle_sigint(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)
    if hasattr(signal, "SIGBREAK"):  # Windows: Ctrl+Break / console close
        signal.signal(signal.SIGBREAK, handle_sigint)

    cap, w, h, fps = open_camera(args.device, args.width, args.height, args.fps)
    lock_frame_rate(args.device, args.mains_hz, args.name)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    video_path = out_dir / f"{args.name}_{stamp}.avi"
    csv_path = out_dir / f"{args.name}_{stamp}_frames.csv"

    writer = cv2.VideoWriter(str(video_path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (w, h))
    if not writer.isOpened():
        sys.exit(f"Could not open video writer for {video_path}")

    outlet = make_outlet(args.name, args.device, w, h, fps, video_path)

    # Backup copy of the timestamps, in case the LSL recording is missing.
    csv_file = open(csv_path, "w", newline="")
    csv_writer = csv.writer(csv_file)
    csv_writer.writerow(["frame_index", "lsl_timestamp", "arrival_time", "timestamp_source"])

    print(f"[{args.name}] camera {args.device}: {w}x{h} @ {fps:g} fps")
    print(f"[{args.name}] video  -> {video_path}")
    print(f"[{args.name}] frames -> {csv_path}")
    print(f"[{args.name}] LSL stream '{args.name}' (type VideoFrames) is live. 'q' or Ctrl+C to stop.")

    frame_index = 0
    fallback_count = 0
    gap_count = 0
    last_ts = None
    report_t0 = time.monotonic()
    report_frames = 0
    win = f"{args.name} (q to quit)"

    try:
        while not stop:
            if not cap.grab():
                if not stop:  # grab() is also interrupted by Ctrl+C
                    print(f"[{args.name}] camera stopped delivering frames", file=sys.stderr)
                break
            arrival = pylsl.local_clock()
            driver_ts = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0

            if driver_ts > 0 and abs(arrival - driver_ts) < MAX_TIMESTAMP_SKEW_S:
                ts, source = driver_ts, DRIVER_TS_LABEL
            else:
                ts, source = arrival, "arrival"
                fallback_count += 1

            ok, frame = cap.retrieve()
            if not ok:
                continue

            writer.write(frame)
            outlet.push_sample([float(frame_index), arrival], ts)
            csv_writer.writerow([frame_index, f"{ts:.6f}", f"{arrival:.6f}", source])

            # A gap of more than 1.5 frame periods means a frame was dropped.
            if last_ts is not None and ts - last_ts > 1.5 / fps:
                gap_count += 1
            last_ts = ts
            frame_index += 1
            report_frames += 1

            if not args.no_preview:
                cv2.imshow(win, frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

            now = time.monotonic()
            if now - report_t0 >= 5.0:
                rate = report_frames / (now - report_t0)
                print(f"[{args.name}] {rate:5.1f} fps | "
                      f"frames {frame_index} | gaps {gap_count} | "
                      f"consumers {'yes' if outlet.have_consumers() else 'no'}"
                      + ("  <-- LOW FPS (too dark? CPU busy?)" if rate < 0.8 * fps else ""))
                report_t0, report_frames = now, 0
    finally:
        cap.release()
        writer.release()
        csv_file.close()
        cv2.destroyAllWindows()
        print(f"[{args.name}] stopped after {frame_index} frames, {gap_count} gaps.")
        if fallback_count:
            print(f"[{args.name}] WARNING: {fallback_count} frames used arrival time "
                  f"instead of the driver timestamp.")


if __name__ == "__main__":
    main()
