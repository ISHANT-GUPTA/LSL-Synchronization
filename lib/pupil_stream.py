#!/usr/bin/env python3
"""Relay Pupil Core gaze and pupil data from Pupil Capture to LSL, in LSL time.

Pupil Capture must be running (only it can open the headset's cameras). This
script talks to it over its network API (Pupil Remote, port 50020) and:
  * publishes three LSL streams:
      <name>_gaze    type Gaze        confidence, norm_pos_x/y, gaze_point_3d_x/y/z,
                                      eye (0, 1, or 2 = both), pupil_timestamp
      <name>_pupils  type Pupil       eye, confidence, norm_pos_x/y, diameter_px,
                                      diameter_3d_mm, method (2 = 2d, 3 = 3d), pupil_timestamp
      <name>_clock   type PupilClock  pupil_time, offset, round_trip_ms (every 2 s)
  * optionally makes Pupil Capture record (world + eye videos, its own gaze
    files) into <out-dir>/pupil/000/, and names that folder in the streams'
    metadata so analyze_xdf.py can line the world video up with other cameras.

Timestamps
----------
Pupil stamps every sample with its own clock ("Pupil time"). Every 2 s we ask
Pupil Capture for its time and note local_clock() just before and after; the
quickest of 8 round trips gives offset = LSL time - Pupil time to within
about half a round trip (~0.1 ms on the same computer). Every gaze and pupil
sample is pushed with timestamp = Pupil time + offset (median of recent
estimates), i.e. the moment the eye camera captured the frame, in LSL time.
<name>_clock records the offsets, so any Pupil timestamp (e.g. world video
frames) can be converted to LSL time afterwards.

Usage
-----
Normally started by record_session.py (see [pupil] in cameras.toml). Alone:
    .venv\\Scripts\\python.exe lib\\pupil_stream.py --out-dir recordings\\test
"""

import argparse
import collections
import csv
import math
import signal
import socket
import statistics
import sys
import time
from pathlib import Path

import msgpack
import pylsl
import zmq

SYNC_EVERY_S = 2.0
SYNC_QUERIES = 8
REPORT_EVERY_S = 5.0
PREFER_3D_WITHIN_S = 1.0   # use 2d pupil data for an eye only if it has had no 3d data this long
LOST_AFTER_S = 10.0        # give up if Pupil Capture has been silent this long
NAN = float("nan")

GAZE_CHANNELS = [("confidence", "normalized"), ("norm_pos_x", "normalized"), ("norm_pos_y", "normalized"),
                 ("gaze_point_3d_x", "mm"), ("gaze_point_3d_y", "mm"), ("gaze_point_3d_z", "mm"),
                 ("eye", "0/1/2=both"), ("pupil_timestamp", "seconds")]
PUPIL_CHANNELS = [("eye", "id"), ("confidence", "normalized"), ("norm_pos_x", "normalized"),
                  ("norm_pos_y", "normalized"), ("diameter_px", "pixels"), ("diameter_3d_mm", "mm"),
                  ("method", "2d/3d"), ("pupil_timestamp", "seconds")]
CLOCK_CHANNELS = [("pupil_time", "seconds"), ("offset", "seconds"), ("round_trip_ms", "ms")]


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1", help="computer running Pupil Capture")
    p.add_argument("--port", type=int, default=50020, help="Pupil Remote port (Network API menu)")
    p.add_argument("--name", default="pupil", help="prefix of the LSL stream names")
    p.add_argument("--out-dir", required=True,
                   help="session folder: pupil_clock.csv goes here, Pupil's recording in pupil/")
    p.add_argument("--no-record", action="store_true",
                   help="don't make Pupil Capture record; only relay data to LSL")
    return p.parse_args()


class PupilRemote:
    """Request/reply connection to Pupil Remote. Recreated after a timeout (a REQ
    socket that missed its reply can't be used again)."""

    def __init__(self, ctx, host, port):
        self.ctx, self.addr = ctx, f"tcp://{host}:{port}"
        self.sock = None
        self._connect()

    def _connect(self):
        if self.sock is not None:
            self.sock.close(linger=0)
        self.sock = self.ctx.socket(zmq.REQ)
        self.sock.setsockopt(zmq.RCVTIMEO, 2000)
        self.sock.setsockopt(zmq.LINGER, 0)
        self.sock.connect(self.addr)

    def ask(self, text):
        """Send a command, return the reply, or None (and reconnect) on timeout."""
        try:
            self.sock.send_string(text)
            return self.sock.recv_string()
        except zmq.Again:
            self._connect()
            return None

    def close(self):
        self.sock.close(linger=0)


class Clock:
    """Tracks offset = LSL time - Pupil time."""

    def __init__(self, remote):
        self.remote = remote
        self.recent = collections.deque(maxlen=5)
        self.first = None

    def measure(self):
        """Best of several round trips. Returns (lsl_mid, pupil_time, offset, rtt) or None."""
        best = None
        for _ in range(SYNC_QUERIES):
            before = pylsl.local_clock()
            reply = self.remote.ask("t")
            after = pylsl.local_clock()
            if reply is None:
                return None
            rtt = after - before
            if best is None or rtt < best[3]:
                mid = (before + after) / 2
                best = (mid, float(reply), mid - float(reply), rtt)
        self.recent.append(best[2])
        if self.first is None:
            self.first = best[2]
        return best

    @property
    def offset(self):
        return statistics.median(self.recent)


def make_outlet(name, stype, channels, meta):
    info = pylsl.StreamInfo(name=name, type=stype, channel_count=len(channels),
                            nominal_srate=pylsl.IRREGULAR_RATE, channel_format=pylsl.cf_double64,
                            source_id=f"{socket.gethostname()}:{name}")
    desc = info.desc()
    chans = desc.append_child("channels")
    for label, unit in channels:
        ch = chans.append_child("channel")
        ch.append_child_value("label", label)
        ch.append_child_value("unit", unit)
    pupil = desc.append_child("pupil")
    for k, v in meta.items():
        pupil.append_child_value(k, str(v))
    return pylsl.StreamOutlet(info)


def wait_for(sub, prefix, timeout):
    """Wait for a notification whose topic starts with prefix; return its payload or None.
    Other messages that arrive meanwhile are dropped."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if sub.poll(100):
            parts = sub.recv_multipart()
            if parts[0].startswith(prefix.encode()):
                return msgpack.loads(parts[1], raw=False)
    return None


def gaze_sample(topic, g):
    eyes = topic.split(".")[2] if topic.count(".") >= 2 else ""
    eye = 2 if eyes == "01" else (int(eyes) if eyes in ("0", "1") else -1)
    p3 = g.get("gaze_point_3d") or (NAN, NAN, NAN)
    x, y = g.get("norm_pos", (NAN, NAN))
    return [g.get("confidence", NAN), x, y, *p3, eye, g["timestamp"]]


def pupil_sample(p, method):
    x, y = p.get("norm_pos", (NAN, NAN))
    return [p["id"], p.get("confidence", NAN), x, y, p.get("diameter", NAN),
            p.get("diameter_3d", NAN) if method == 3 else NAN, method, p["timestamp"]]


def main():
    sys.stdout.reconfigure(line_buffering=True)
    args = parse_args()
    tag = f"[{args.name}]"

    stop = False

    def handle_stop(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, handle_stop)

    ctx = zmq.Context()
    remote = PupilRemote(ctx, args.host, args.port)
    version = remote.ask("v")
    if version is None:
        sys.exit(f"{tag} Pupil Capture is not answering at {args.host}:{args.port}. Start Pupil "
                 f"Capture first (and check Network API > Pupil Remote is on), or set "
                 f"enabled = false under [pupil] in cameras.toml.")
    sub_port = remote.ask("SUB_PORT")

    sub = ctx.socket(zmq.SUB)
    sub.setsockopt(zmq.LINGER, 0)
    sub.connect(f"tcp://{args.host}:{sub_port}")
    for topic in ("gaze.", "pupil.", "notify.recording."):
        sub.setsockopt_string(zmq.SUBSCRIBE, topic)

    clock = Clock(remote)
    first = clock.measure()
    if first is None:
        sys.exit(f"{tag} Pupil Capture stopped answering while measuring its clock.")
    print(f"{tag} Pupil Capture {version}: clock offset {first[2] * 1e3:+.3f} ms "
          f"(round trip {first[3] * 1e3:.2f} ms)")

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    rec_dir = None
    if not args.no_record:
        time.sleep(0.3)  # let the SUB connection settle so the notification isn't missed
        target = (out_dir / "pupil").as_posix()
        if remote.ask(f"R {target}") is None:
            sys.exit(f"{tag} Pupil Capture did not answer the start-recording request.")
        started = wait_for(sub, "notify.recording.started", 10)
        if started is None:
            sys.exit(f"{tag} Pupil Capture did not start recording. Is it already recording? "
                     f"Stop that recording, or set record = false under [pupil].")
        rec_dir = Path(started["rec_path"]).resolve()
        print(f"{tag} Pupil Capture is recording -> {rec_dir}")

    meta = {"pupil_capture_version": version, "host": args.host, "port": args.port,
            "recording_dir": str(rec_dir) if rec_dir else "", "hostname": socket.gethostname()}
    gaze_out = make_outlet(f"{args.name}_gaze", "Gaze", GAZE_CHANNELS, meta)
    pupil_out = make_outlet(f"{args.name}_pupils", "Pupil", PUPIL_CHANNELS, meta)
    clock_out = make_outlet(f"{args.name}_clock", "PupilClock", CLOCK_CHANNELS, meta)

    clock_csv = open(out_dir / f"{args.name}_clock.csv", "w", newline="")
    clock_writer = csv.writer(clock_csv)
    clock_writer.writerow(["lsl_time", "pupil_time", "offset", "round_trip_ms"])

    def push_clock(m):
        clock_out.push_sample([m[1], m[2], m[3] * 1e3], m[0])
        clock_writer.writerow([f"{m[0]:.6f}", f"{m[1]:.6f}", f"{m[2]:.9f}", f"{m[3] * 1e3:.3f}"])
        clock_csv.flush()

    push_clock(first)
    print(f"{tag} LSL streams {args.name}_gaze, {args.name}_pupils, {args.name}_clock are live. "
          f"Ctrl+C to stop.")

    last_3d = {0: -math.inf, 1: -math.inf}
    counts = collections.Counter()
    conf = {0: [], 1: []}
    last_data = time.monotonic()
    next_sync = time.monotonic() + SYNC_EVERY_S
    next_report = time.monotonic() + REPORT_EVERY_S
    report_t0 = time.monotonic()
    lost = False

    try:
        while not stop:
            if sub.poll(100):
                while True:
                    try:
                        parts = sub.recv_multipart(zmq.NOBLOCK)
                    except zmq.Again:
                        break
                    topic = parts[0].decode(errors="replace")
                    msg = msgpack.loads(parts[1], raw=False)
                    now = time.monotonic()
                    if topic.startswith("gaze."):
                        gaze_out.push_sample(gaze_sample(topic, msg), msg["timestamp"] + clock.offset)
                        counts["gaze"] += 1
                        last_data = now
                    elif topic.startswith("pupil."):
                        eye = msg.get("id")
                        method = 3 if topic.endswith("3d") else 2
                        if method == 3:
                            last_3d[eye] = now
                        elif now - last_3d.get(eye, -math.inf) < PREFER_3D_WITHIN_S:
                            continue  # this eye has 3d data; skip the duplicate 2d datum
                        pupil_out.push_sample(pupil_sample(msg, method), msg["timestamp"] + clock.offset)
                        counts[f"eye{eye}"] += 1
                        conf.setdefault(eye, []).append(msg.get("confidence", 0.0))
                        last_data = now
                    elif topic.startswith("notify.recording.stopped") and rec_dir:
                        print(f"{tag} WARNING: Pupil Capture's recording was stopped in Pupil Capture.")
                        rec_dir = None

            now = time.monotonic()
            if now >= next_sync:
                next_sync = now + SYNC_EVERY_S
                m = clock.measure()
                if m is not None:
                    push_clock(m)
            if now - last_data > LOST_AFTER_S:
                print(f"{tag} no data from Pupil Capture for {LOST_AFTER_S:.0f} s "
                      f"(closed? eye windows closed?)", file=sys.stderr)
                lost = True
                break
            if now >= next_report:
                span = now - report_t0
                parts = [f"gaze {counts['gaze'] / span:5.1f} Hz"]
                for eye in (0, 1):
                    if counts[f"eye{eye}"]:
                        c = statistics.median(conf[eye])
                        parts.append(f"eye{eye} {counts[f'eye{eye}'] / span:5.1f} Hz conf {c:.2f}")
                    else:
                        parts.append(f"eye{eye} no data")
                drift_ms = (clock.offset - clock.first) * 1e3
                consumers = "yes" if gaze_out.have_consumers() else "no"
                low = any(counts[f"eye{e}"] and statistics.median(conf[e]) < 0.6 for e in (0, 1))
                print(f"{tag} " + " | ".join(parts) + f" | clock drift {drift_ms:+.2f} ms"
                      f" | consumers {consumers}" + ("  <-- LOW CONFIDENCE (headset on? pupils visible?)"
                                                     if low else ""))
                counts.clear()
                conf = {0: [], 1: []}
                report_t0, next_report = now, now + REPORT_EVERY_S
    finally:
        if rec_dir:
            remote.ask("r")
            stopped = wait_for(sub, "notify.recording.stopped", 10)
            print(f"{tag} Pupil Capture recording "
                  + (f"saved: {rec_dir}" if stopped else "stop requested, but no confirmation; check Pupil Capture"))
        clock_csv.close()
        sub.close()
        remote.close()
        ctx.term()
        print(f"{tag} stopped.")
    if lost:
        sys.exit(1)


if __name__ == "__main__":
    main()
