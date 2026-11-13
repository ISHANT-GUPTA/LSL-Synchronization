#!/usr/bin/env python3
"""Record a microphone to a WAV file and publish it over LSL as an Audio stream.

The audio itself goes over LSL (48 kHz mono int16 is only ~100 KB/s), so the
XDF holds it on the same timeline as the cameras. A WAV copy and a CSV of block
timestamps are kept in the session folder as a backup.

Timestamps
----------
The sound card delivers samples in blocks (~10 ms). Each block is stamped with
local_clock() when it arrives, minus the input latency the driver reports, and
that stamp is given to the block's last sample. Arrival times jitter by ~1 ms
and the sound card's clock runs slightly off its nominal rate (the jack mic on
this laptop: +20 ppm, ~1.2 ms per minute). The stream declares its nominal
rate, so pyxdf (dejitter, on by default) fits a straight line through the
stamps when loading: that removes the jitter and the drift. What stays is any
constant latency the driver doesn't report; a clap seen by a camera and heard
by the mic measures it (analyze_xdf.py reports it).

Usage
-----
Normally started by record_session.py (see [audio] in cameras.toml). Alone:
    .venv\\Scripts\\python.exe lib\\audio_stream.py --list
    .venv\\Scripts\\python.exe lib\\audio_stream.py --device "Headset Microphone" --out-dir recordings\\test
"""

import argparse
import csv
import queue
import signal
import socket
import sys
import time
import wave
from pathlib import Path

import numpy as np
import pylsl
import sounddevice as sd

REPORT_EVERY_S = 5.0
IS_WINDOWS = sys.platform == "win32"
PREFERRED_API = "Windows WASAPI" if IS_WINDOWS else None  # lowest-latency shared-mode API


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--device", default=None,
                   help="input device: part of its name (e.g. \"Headset Microphone\") or its number "
                        "from --list (default: the system's default input)")
    p.add_argument("--name", default="mic", help="LSL stream name and file prefix")
    p.add_argument("--rate", type=int, default=48000, help="sample rate in Hz")
    p.add_argument("--channels", type=int, default=1, help="number of channels to record")
    p.add_argument("--out-dir", default="recordings", help="folder for the WAV and CSV files")
    p.add_argument("--list", action="store_true", help="list input devices and exit")
    return p.parse_args()


def input_devices():
    """(index, name, host API, max channels, default rate) of every input device."""
    apis = sd.query_hostapis()
    return [(i, d["name"], apis[d["hostapi"]]["name"], d["max_input_channels"], d["default_samplerate"])
            for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]


def list_devices():
    devs = input_devices()
    shown = [d for d in devs if PREFERRED_API is None or d[2] == PREFERRED_API]
    print("Input devices" + (f" ({PREFERRED_API}; others hidden)" if PREFERRED_API else "") + ":")
    for i, name, api, ch, rate in shown:
        print(f"  {i:>3}  {name}  ({ch} ch, {rate:g} Hz)")
    print('Use part of the name as device = "..." under [audio] in cameras.toml.')


def find_device(spec):
    """Device index for a number or a name fragment, preferring the low-latency host API."""
    devs = input_devices()
    if spec is None:
        return sd.default.device[0]
    if str(spec).isdigit():
        return int(spec)
    matches = [d for d in devs if str(spec).lower() in d[1].lower()]
    if not matches:
        sys.exit(f"No input device matches {spec!r}. Run with --list to see them.")
    preferred = [d for d in matches if d[2] == PREFERRED_API] or matches
    return preferred[0][0]


def make_outlet(name, rate, channels, dev_name, latency, wav_path):
    info = pylsl.StreamInfo(name=name, type="Audio", channel_count=channels, nominal_srate=rate,
                            channel_format=pylsl.cf_int16, source_id=f"{socket.gethostname()}:{name}")
    desc = info.desc()
    chans = desc.append_child("channels")
    for c in range(channels):
        ch = chans.append_child("channel")
        ch.append_child_value("label", f"ch{c}")
        ch.append_child_value("unit", "int16 full scale")
    dev = desc.append_child("audio")
    dev.append_child_value("device", dev_name)
    dev.append_child_value("reported_input_latency_s", f"{latency:.6f}")
    dev.append_child_value("wav_file", str(wav_path.resolve()))
    dev.append_child_value("hostname", socket.gethostname())
    # Chunks of ~10 ms; the outlet buffers up to 60 s of audio for slow consumers.
    return pylsl.StreamOutlet(info, chunk_size=0, max_buffered=60)


def main():
    sys.stdout.reconfigure(line_buffering=True)
    args = parse_args()
    if args.list:
        list_devices()
        return
    tag = f"[{args.name}]"

    stop = False

    def handle_stop(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, handle_stop)

    dev = find_device(args.device)
    dev_info = sd.query_devices(dev)
    dev_name = f"{dev_info['name']} ({sd.query_hostapis(dev_info['hostapi'])['name']})"

    blocks = queue.Queue()

    def callback(indata, frames, t, status):
        blocks.put((pylsl.local_clock(), indata.copy(), bool(status.input_overflow)))

    try:
        stream = sd.InputStream(device=dev, samplerate=args.rate, channels=args.channels,
                                dtype="int16", callback=callback)
    except Exception as e:  # PortAudioError, ValueError for bad channel counts, ...
        sys.exit(f"{tag} Could not open {dev_name} at {args.rate} Hz, {args.channels} ch: {e}")
    latency = float(stream.latency)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    wav_path = out_dir / f"{args.name}_{stamp}.wav"
    csv_path = out_dir / f"{args.name}_{stamp}_blocks.csv"
    wav = wave.open(str(wav_path), "wb")
    wav.setnchannels(args.channels)
    wav.setsampwidth(2)
    wav.setframerate(args.rate)
    csv_file = open(csv_path, "w", newline="")
    csv_writer = csv.writer(csv_file)
    csv_writer.writerow(["first_sample", "frames", "lsl_timestamp_last_sample", "arrival_time"])

    outlet = make_outlet(args.name, args.rate, args.channels, dev_name, latency, wav_path)
    print(f"{tag} {dev_name}: {args.rate} Hz, {args.channels} ch, reported input latency "
          f"{latency * 1e3:.1f} ms")
    print(f"{tag} audio  -> {wav_path}")
    print(f"{tag} blocks -> {csv_path}")

    total = 0
    overflows = 0
    peak = 0
    first_arrival = None
    report_t0 = time.monotonic()
    try:
        stream.start()
        print(f"{tag} LSL stream '{args.name}' (type Audio) is live. Ctrl+C to stop.")
        while not stop:
            try:
                arrival, data, overflow = blocks.get(timeout=0.2)
            except queue.Empty:
                if time.monotonic() - report_t0 > 3 and total == 0:
                    sys.exit(f"{tag} no audio arriving from {dev_name}")
                continue
            frames = len(data)
            ts_last = arrival - latency
            outlet.push_chunk(data, ts_last)
            wav.writeframes(data.tobytes())
            csv_writer.writerow([total, frames, f"{ts_last:.6f}", f"{arrival:.6f}"])
            if first_arrival is None:
                first_arrival = (arrival, total + frames)
            total += frames
            overflows += overflow
            peak = max(peak, int(np.abs(data.astype(np.int32)).max()))

            now = time.monotonic()
            if now - report_t0 >= REPORT_EVERY_S:
                db = 20 * np.log10(max(peak, 1) / 32768)
                # Effective sample rate so far, from samples received vs elapsed LSL time.
                rate_ppm = ""
                if arrival - first_arrival[0] > 20:  # shorter spans are dominated by start-up buffering
                    eff = (total - first_arrival[1]) / (arrival - first_arrival[0])
                    rate_ppm = f" | rate {(eff / args.rate - 1) * 1e6:+.0f} ppm"
                print(f"{tag} peak {db:6.1f} dBFS{rate_ppm} | overflows {overflows} | "
                      f"consumers {'yes' if outlet.have_consumers() else 'no'}"
                      + ("  <-- SILENT? (muted, wrong device?)" if db < -60 else "")
                      + ("  <-- CLIPPING (lower the mic level)" if db > -0.5 else ""))
                peak, report_t0 = 0, now
    finally:
        stream.stop()
        stream.close()
        # Drain what arrived before the stream stopped.
        while not blocks.empty():
            arrival, data, _ = blocks.get_nowait()
            outlet.push_chunk(data, arrival - latency)
            wav.writeframes(data.tobytes())
            csv_writer.writerow([total, len(data), f"{arrival - latency:.6f}", f"{arrival:.6f}"])
            total += len(data)
        wav.close()
        csv_file.close()
        print(f"{tag} stopped after {total / args.rate:.1f} s of audio, {overflows} overflows.")


if __name__ == "__main__":
    main()
