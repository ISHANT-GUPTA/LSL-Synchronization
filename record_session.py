#!/usr/bin/env python3
"""Record every camera in cameras.toml (and the Pupil Core) at once, into one folder per session.

Usage
-----
    .venv\\Scripts\\python.exe record_session.py                 # record a session
    .venv\\Scripts\\python.exe record_session.py --label pilot1  # ...with a name
    .venv\\Scripts\\python.exe record_session.py --list          # which device is which?
(on Linux: .venv/bin/python record_session.py ...)

Each session gets its own folder, recordings/<date>_<time>[_<label>]/, holding
for each camera <name>_<stamp>.avi and <name>_<stamp>_frames.csv, plus:
    session.json   the camera settings used, start/stop time, how each camera ended
    session.log    everything the cameras printed (including liblsl's messages)
    <session>.xdf  the LabRecorder recording, if LabRecorder was open
and, if [pupil] is enabled in cameras.toml:
    pupil_clock.csv  Pupil time <-> LSL time pairs (backup of the pupil_clock stream)
    pupil/000/       Pupil Capture's own recording (world + eye videos, gaze, ...)

Pupil Core: Pupil Capture must be running (it is the only program that can
open the headset's cameras). lib/pupil_stream.py relays its gaze and pupil
data to LSL (streams pupil_gaze, pupil_pupils, pupil_clock) and makes it
record into the session folder.

If LabRecorder is open, it is driven automatically: once every camera is live
it records all streams into the session folder, and it stops before the
cameras do. (Needs RCSEnabled=1 in LabRecorder.cfg, which is the default.)
Otherwise start it by hand when the cameras say they are live.
Stop with Ctrl+C here, or q in any preview window: everything stops together.
"""

import argparse
import datetime as dt
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CAMERA_SCRIPT = ROOT / "lib" / "camera_stream.py"
PUPIL_SCRIPT = ROOT / "lib" / "pupil_stream.py"
RECORDINGS = ROOT / "recordings"
DEFAULT_CONFIG = ROOT / "cameras.toml"
IS_WINDOWS = sys.platform == "win32"

CAMERA_DEFAULTS = {"width": 1280, "height": 720, "fps": 30}
CAMERA_KEYS = {"name", "device", *CAMERA_DEFAULTS}
PUPIL_NAME = "pupil"  # process name in the output, and prefix of its LSL streams and files
PUPIL_DEFAULTS = {"enabled": True, "record": True, "host": "127.0.0.1", "port": 50020}
TOP_KEYS = {"camera", "preview", "mains_hz", "labrecorder", "pupil"}
LABRECORDER_PORT = 22345  # LabRecorder's remote control (RCSPort in LabRecorder.cfg)
NAME_OK = re.compile(r"^[A-Za-z0-9_-]+$")
# liblsl's own log lines: "2026-11-12 23:41:20.327 (   0.014s) [  DACC905B] netinterfaces.cpp:36   INFO| ..."
LSL_LOG = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+ \(.*?\)\s+\[.*?\]\s+\S+:\d+\s+(\w+)\|")
LIST_MAX_DEVICES = 10
STOP_TIMEOUT_S = 15


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=str(DEFAULT_CONFIG),
                   help="camera list (default: cameras.toml next to this script)")
    p.add_argument("--label", default=None,
                   help="added to the session folder name, e.g. pilot1 (letters, digits, _ or -)")
    p.add_argument("--no-preview", action="store_true",
                   help="no preview windows, even if cameras.toml says preview = true")
    p.add_argument("--no-labrecorder", action="store_true",
                   help="don't start/stop LabRecorder automatically")
    p.add_argument("--no-pupil", action="store_true",
                   help="leave the Pupil Core out, even if cameras.toml enables it")
    p.add_argument("--list", action="store_true",
                   help="show a snapshot from every camera found, labelled with its device number")
    return p.parse_args()


def load_config(path):
    path = Path(path)
    try:
        with open(path, "rb") as f:
            cfg = tomllib.load(f)
    except FileNotFoundError:
        sys.exit(f"Config not found: {path}")
    except tomllib.TOMLDecodeError as e:
        sys.exit(f"{path.name} is not valid TOML: {e}")

    unknown = set(cfg) - TOP_KEYS
    if unknown:
        sys.exit(f"{path.name}: unknown setting(s) {sorted(unknown)}; expected {sorted(TOP_KEYS)}")
    mains_hz = cfg.get("mains_hz", 50)
    if mains_hz not in (50, 60):
        sys.exit(f"{path.name}: mains_hz must be 50 or 60 (got {mains_hz!r})")

    cameras, names, devices = [], set(), set()
    for i, cam in enumerate(cfg.get("camera", []), 1):
        name, device = cam.get("name"), cam.get("device")
        where = f"{path.name}: camera #{i}"
        if not isinstance(name, str) or not NAME_OK.match(name):
            sys.exit(f"{where}: name must be letters, digits, _ or - (got {name!r})")
        if device is None:
            sys.exit(f"{where} ('{name}'): device is missing")
        unknown = set(cam) - CAMERA_KEYS
        if unknown:
            sys.exit(f"{where} ('{name}'): unknown setting(s) {sorted(unknown)}; "
                     f"expected {sorted(CAMERA_KEYS)}")
        if name in names:
            sys.exit(f"{path.name}: two cameras are named '{name}'")
        if str(device) in devices:
            sys.exit(f"{path.name}: two cameras use device {device}")
        names.add(name)
        devices.add(str(device))
        cameras.append({**CAMERA_DEFAULTS, **cam})
    if not cameras:
        sys.exit(f"{path.name}: no [[camera]] blocks")
    if PUPIL_NAME in names:
        sys.exit(f"{path.name}: the name '{PUPIL_NAME}' is used by the Pupil Core; rename that camera")

    pupil = None
    if "pupil" in cfg:
        if not isinstance(cfg["pupil"], dict):
            sys.exit(f"{path.name}: pupil must be a [pupil] section")
        unknown = set(cfg["pupil"]) - set(PUPIL_DEFAULTS)
        if unknown:
            sys.exit(f"{path.name}: [pupil]: unknown setting(s) {sorted(unknown)}; "
                     f"expected {sorted(PUPIL_DEFAULTS)}")
        pupil = {**PUPIL_DEFAULTS, **cfg["pupil"]}
        if not isinstance(pupil["port"], int):
            sys.exit(f"{path.name}: [pupil]: port must be a number (got {pupil['port']!r})")
        if not pupil.pop("enabled"):
            pupil = None
    return {"preview": bool(cfg.get("preview", True)), "mains_hz": mains_hz,
            "labrecorder": bool(cfg.get("labrecorder", True)), "cameras": cameras, "pupil": pupil}


class LabRecorder:
    """Drives a running LabRecorder through its remote control socket."""

    def __init__(self, port=LABRECORDER_PORT):
        self.sock = socket.create_connection(("localhost", port), timeout=2)

    def cmd(self, text):
        self.sock.sendall((text + "\n").encode())
        try:
            return self.sock.recv(256).decode(errors="replace").strip()
        except socket.timeout:
            return None

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- --list

def list_cameras(config_path, show=True):
    """Open device 0, 1, 2, ... and return a grid image with one labelled snapshot per camera."""
    os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")  # silence warnings for missing devices
    sys.path.insert(0, str(CAMERA_SCRIPT.parent))
    import camera_stream  # sets up the same capture backend as recording uses
    import numpy as np
    cv2 = camera_stream.cv2
    try:  # in case cv2 was imported before the environment variable was set
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
    except AttributeError:
        pass

    # Label snapshots with the names cameras.toml gives them, if it loads.
    known = {}
    try:
        with open(config_path, "rb") as f:
            known = {str(c.get("device")): c.get("name") for c in tomllib.load(f).get("camera", [])}
    except (OSError, tomllib.TOMLDecodeError):
        pass

    print(f"Looking for cameras (device 0-{LIST_MAX_DEVICES - 1}) ...")
    tiles = []
    for i in range(LIST_MAX_DEVICES):
        cap = cv2.VideoCapture(i, camera_stream.CAPTURE_API)
        if not cap.isOpened():
            cap.release()
            continue
        frame = None
        for _ in range(15):  # skip the first frames while auto-exposure settles
            ok, f = cap.read()
            if ok:
                frame = f
        cap.release()
        name = known.get(str(i))
        in_toml = f"  = {name} in cameras.toml" if name else "  (not in cameras.toml)"
        if frame is None:
            print(f"  device {i}: opens but gives no picture (in use by another app?)")
            continue
        print(f"  device {i}: found{in_toml}")
        tile = cv2.resize(frame, (480, 270), interpolation=cv2.INTER_AREA)
        cv2.rectangle(tile, (0, 0), (480, 34), (0, 0, 0), -1)
        cv2.putText(tile, f"device = {i}" + (f"  ({name})" if name else ""), (10, 24),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
        tiles.append(tile)

    print("(The Pupil Core's cameras never appear here: only Pupil Capture can open them.)")
    if not tiles:
        print("No cameras found. Close other apps that use them"
              + (", and check Settings > Privacy & security > Camera." if IS_WINDOWS else "."))
        return None
    per_row = 3
    while len(tiles) % per_row and len(tiles) > per_row:
        tiles.append(np.zeros_like(tiles[0]))
    rows = [np.hstack(tiles[k:k + per_row]) for k in range(0, len(tiles), per_row)]
    grid = np.vstack(rows)
    if show:
        print("Close the window (or press any key in it) to finish.")
        win = "Cameras found (any key closes)"
        cv2.imshow(win, grid)
        while cv2.getWindowProperty(win, cv2.WND_PROP_VISIBLE) >= 1:
            if cv2.waitKey(100) != -1:
                break
        cv2.destroyAllWindows()
    return grid


# ---------------------------------------------------------------- recording

def make_session_dir(label):
    if label is not None and not NAME_OK.match(label):
        sys.exit(f"--label must be letters, digits, _ or - (got {label!r})")
    name = dt.datetime.now().strftime("%Y%m%d_%H%M%S") + (f"_{label}" if label else "")
    session_dir = RECORDINGS / name
    session_dir.mkdir(parents=True, exist_ok=False)
    return session_dir


def start_camera(cam, cfg, session_dir):
    cmd = [sys.executable, str(CAMERA_SCRIPT),
           "--device", str(cam["device"]), "--name", cam["name"],
           "--width", str(cam["width"]), "--height", str(cam["height"]), "--fps", str(cam["fps"]),
           "--out-dir", str(session_dir), "--mains-hz", str(cfg["mains_hz"])]
    if not cfg["preview"]:
        cmd.append("--no-preview")
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    # Own process group, so Ctrl+C here reaches only this script, which then
    # stops every camera itself (and stops the rest when one camera ends).
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WINDOWS
             else {"start_new_session": True})
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", env=env, **group)


def start_pupil(pupil, session_dir):
    cmd = [sys.executable, str(PUPIL_SCRIPT), "--name", PUPIL_NAME, "--out-dir", str(session_dir),
           "--host", pupil["host"], "--port", str(pupil["port"])]
    if not pupil["record"]:
        cmd.append("--no-record")
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WINDOWS
             else {"start_new_session": True})
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", env=env, **group)


def stop_camera(proc):
    """Ask a camera to stop the same way Ctrl+C would, so it closes its files cleanly."""
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.CTRL_BREAK_EVENT if IS_WINDOWS else signal.SIGINT)
    except OSError:
        pass


class Output:
    """Prints camera output (minus liblsl's chatter) and keeps all of it in session.log."""

    def __init__(self, log_path, names):
        self.log = open(log_path, "w", encoding="utf-8")
        self.lock = threading.Lock()
        self.waiting = set(names)
        self.all_live = threading.Event()

    def pump(self, name, proc):
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            if not line.startswith(f"[{name}]"):
                line = f"[{name}] {line}"
            m = LSL_LOG.match(line[len(name) + 3:])
            with self.lock:
                self.log.write(line + "\n")
                self.log.flush()
                if not (m and m.group(1) == "INFO"):
                    print(line, flush=True)
                # camera_stream.py: "... stream 'x' ... is live"; pupil_stream.py: "... streams ... are live"
                if ("is live" in line or "are live" in line) and name in self.waiting:
                    self.waiting.discard(name)
                    if not self.waiting:
                        self.all_live.set()

    def say(self, text):
        with self.lock:
            print(text, flush=True)
            self.log.write(text + "\n")
            self.log.flush()

    def close(self):
        self.log.close()


# Exit codes of a process ended by Ctrl+C/Ctrl+Break before it could handle it
# (i.e. while it was still starting up): Windows STATUS_CONTROL_C_EXIT, POSIX -SIGINT.
INTERRUPTED_CODES = {0xC000013A, -signal.SIGINT}


def exit_status(result):
    code = result["exit_code"]
    if code == 0:
        return "ok"
    if result["killed"]:
        return "killed (did not stop)"
    if code in INTERRUPTED_CODES:
        return "stopped while starting"
    return f"failed (exit code {code})"


def start_labrecorder(out, session_dir):
    """Point LabRecorder at the session folder and start recording. None if it isn't reachable."""
    try:
        lr = LabRecorder()
    except OSError:
        out.say("LabRecorder is not running (or its remote control is off), so it will not be "
                "started automatically.")
        return None
    lr.cmd("update")
    time.sleep(2.0)  # give it time to find the streams
    lr.cmd("select all")
    lr.cmd(f"filename {{root:{session_dir.as_posix()}}} {{template:{session_dir.name}.xdf}}")
    lr.cmd("start")
    out.say("LabRecorder: selected all streams and started recording.")
    return lr


def xdf_sample_chunks(path):
    """How many sample chunks each stream has in an XDF file, read without decoding samples.

    LabRecorder writes a chunk every ~0.5 s for every stream that delivers data,
    so 0 means it recorded the stream's header but no data at all.
    Returns {stream name: chunk count}, or None if the file can't be read.
    """
    names, counts = {}, {}
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"XDF:":
                return None
            while True:
                size_len = f.read(1)
                if not size_len:
                    break
                length = int.from_bytes(f.read(size_len[0]), "little")
                head = f.read(min(length, 6))  # 2-byte tag + 4-byte stream id
                if len(head) < 2:
                    break
                tag = int.from_bytes(head[:2], "little")
                sid = int.from_bytes(head[2:6], "little") if len(head) == 6 else None
                if tag == 2:  # stream header (XML)
                    xml = f.read(length - len(head))
                    m = re.search(rb"<name>(.*?)</name>", xml)
                    names[sid] = m.group(1).decode(errors="replace") if m else f"stream {sid}"
                    counts.setdefault(sid, 0)
                else:
                    if tag == 3:  # samples
                        counts[sid] = counts.get(sid, 0) + 1
                    f.seek(length - len(head), os.SEEK_CUR)
    except OSError:
        return None
    return {names.get(sid, f"stream {sid}"): n for sid, n in counts.items()}


def write_session_json(path, info):
    path.write_text(json.dumps(info, indent=2), encoding="utf-8")


def record(cfg, config_path, label):
    session_dir = make_session_dir(label)
    names = [c["name"] for c in cfg["cameras"]]
    pupil = cfg["pupil"]
    sources = names + ([PUPIL_NAME] if pupil else [])
    info = {
        "session": session_dir.name,
        "folder": str(session_dir),
        "hostname": socket.gethostname(),
        "platform": sys.platform,
        "config_file": str(Path(config_path).resolve()),
        "preview": cfg["preview"],
        "mains_hz": cfg["mains_hz"],
        "cameras": cfg["cameras"],
        "pupil": pupil,
        "started": dt.datetime.now().isoformat(timespec="seconds"),
    }
    write_session_json(session_dir / "session.json", info)

    out = Output(session_dir / "session.log", sources)
    out.say(f"Session folder: {session_dir}")
    out.say(f"Starting {len(names)} camera(s): " + ", ".join(
        f"{c['name']} (device {c['device']})" for c in cfg["cameras"]))
    if pupil:
        out.say(f"Starting Pupil Core relay (Pupil Capture at {pupil['host']}:{pupil['port']}, "
                + ("recording into pupil/" if pupil["record"] else "not recording") + ")")

    stop_requested = threading.Event()

    def on_signal(*_):
        stop_requested.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, on_signal)

    procs, pumps = {}, []
    for cam in cfg["cameras"]:
        procs[cam["name"]] = start_camera(cam, cfg, session_dir)
        t = threading.Thread(target=out.pump, args=(cam["name"], procs[cam["name"]]), daemon=True)
        t.start()
        pumps.append(t)
    if pupil:
        procs[PUPIL_NAME] = start_pupil(pupil, session_dir)
        t = threading.Thread(target=out.pump, args=(PUPIL_NAME, procs[PUPIL_NAME]), daemon=True)
        t.start()
        pumps.append(t)
    everything = f"All {len(names)} cameras" + (" and the Pupil Core" if pupil else "")
    stream_names = names + ([f"{PUPIL_NAME}_gaze", f"{PUPIL_NAME}_pupils", f"{PUPIL_NAME}_clock"]
                            if pupil else [])

    announced = False
    reason = None
    lr = None
    xdf_path = session_dir / f"{session_dir.name}.xdf"
    while reason is None:
        if stop_requested.is_set():
            reason = "Ctrl+C"
        else:
            for name, proc in procs.items():
                code = proc.poll()
                if code is not None:
                    if code != 0:
                        reason = f"{name} failed with exit code {code}"
                    elif name == PUPIL_NAME:
                        reason = "the Pupil Core relay stopped"
                    else:
                        reason = f"{name} stopped (q pressed?)"
                    break
        if reason is None:
            if not announced and out.all_live.is_set():
                announced = True
                lr = start_labrecorder(out, session_dir) if cfg["labrecorder"] else None
                if lr:
                    first = f"{everything} are live and LabRecorder is recording to\n  {xdf_path}"
                else:
                    first = (f"{everything} are live. In LabRecorder: click Update, tick "
                             + ", ".join(stream_names) + ", then Start.")
                out.say("\n" + "=" * 70 + "\n" + first
                        + "\nTo stop: Ctrl+C here, or q in any preview window.\n" + "=" * 70)
            time.sleep(0.2)

    if lr:
        # Stop the XDF first, so it ends while every stream is still running.
        out.say(f"\nStopping LabRecorder ({reason}) ...")
        lr.cmd("stop")
        lr.close()
        time.sleep(1.0)
    out.say(f"\nStopping all cameras ({reason}) ...")
    for proc in procs.values():
        stop_camera(proc)
    deadline = time.monotonic() + STOP_TIMEOUT_S
    killed = set()
    for name, proc in procs.items():
        try:
            proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            killed.add(name)
    for t in pumps:
        t.join(timeout=2)

    results = {}
    for name, proc in procs.items():
        files = sorted(p.name for p in session_dir.glob(f"{name}_*"))
        if name == PUPIL_NAME:
            files += [f"{r.relative_to(session_dir)}{os.sep} (world + eye videos)"
                      for r in sorted(session_dir.glob("pupil/[0-9][0-9][0-9]"))]
        results[name] = {"exit_code": proc.returncode, "killed": name in killed, "files": files}
    xdf = xdf_path if xdf_path.exists() else None
    # Check that LabRecorder actually got data from every stream we started.
    xdf_problem = None
    if xdf:
        chunks = xdf_sample_chunks(xdf)
        if chunks is None:
            xdf_problem = "could not read the XDF file"
        else:
            missing = [s for s in stream_names if s not in chunks]
            empty = [s for s in stream_names if chunks.get(s) == 0]
            if missing or empty:
                xdf_problem = "; ".join(
                    ([f"not in the XDF: {', '.join(missing)}"] if missing else [])
                    + ([f"NO DATA recorded for: {', '.join(empty)}"] if empty else []))
        info["xdf_sample_chunks"] = chunks
    info.update(stopped=dt.datetime.now().isoformat(timespec="seconds"),
                stop_reason=reason, results=results, xdf=str(xdf) if xdf else None,
                xdf_problem=xdf_problem)
    write_session_json(session_dir / "session.json", info)

    ok = all(r["exit_code"] == 0 for r in results.values()) and xdf_problem is None
    out.say("\nSession " + ("finished" if ok else "finished WITH PROBLEMS") + f": {session_dir}")
    for name, r in results.items():
        out.say(f"  {name:<14} {exit_status(r):<26} " + (", ".join(r["files"]) or "no files"))
    if xdf:
        out.say(f"  {'LabRecorder':<14} {'ok' if xdf_problem is None else 'PROBLEM':<26} {xdf.name}")
        if xdf_problem:
            out.say(f"  {'':<14} {xdf_problem}. Record the session again"
                    + (" (Pupil data is also in pupil/000/, in Pupil time; pupil_clock.csv "
                       "converts it to LSL time)." if pupil and pupil["record"] else "."))
        exe = Path(sys.executable)
        try:
            exe = exe.relative_to(ROOT)
        except ValueError:
            pass
        out.say(f"\nAnalyse it with:\n  {exe} analyze_xdf.py \"{session_dir.relative_to(ROOT)}\"")
    elif lr:
        out.say("  LabRecorder: no XDF appeared in the session folder; check LabRecorder.")
    elif announced:
        out.say("Stop the LabRecorder recording too, if it is still running.")
    if pupil and pupil["record"]:
        out.say("\nNote: Pupil Capture now remembers this session's pupil/ folder as its recording "
                "name. Before recording in Pupil Capture by hand, change 'Recording session name' "
                "in its Recorder menu.")
    out.close()
    return 0 if ok else 1


def main():
    sys.stdout.reconfigure(line_buffering=True)
    args = parse_args()
    if args.list:
        list_cameras(args.config)
        return 0
    cfg = load_config(args.config)
    if args.no_preview:
        cfg["preview"] = False
    if args.no_labrecorder:
        cfg["labrecorder"] = False
    if args.no_pupil:
        cfg["pupil"] = None
    return record(cfg, args.config, args.label)


if __name__ == "__main__":
    sys.exit(main())
