# LSL_sync

Record several webcams, a Pupil Core eye tracker and a microphone together over LSL, one folder per session,
then check how well they line up.

Commands below are for Windows. On Linux use `.venv/bin/python` instead of `.venv\Scripts\python.exe`.

## 1. Setup (once)

Needs Python 3.11 or newer.

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Also install:
- **LabRecorder**: keep `RCSEnabled = 1` in `LabRecorder.cfg` (the default), so sessions can start and stop it.
- **Pupil Capture**, only if you use the Pupil Core: keep *Network API > Pupil Remote* on, port 50020 (the default).

## 2. Configure the cameras (once)

Find out which device number is which camera:

```bash
.venv\Scripts\python.exe record_session.py --list
```

and which microphone to use:

```bash
.venv\Scripts\python.exe record_session.py --list-audio
```

Then edit [`cameras.toml`](cameras.toml):

| Setting | Default | Meaning |
|---|---|---|
| `preview` | `true` | live preview window per camera |
| `mains_hz` | `50` | anti-flicker, 50 or 60 (Linux only) |
| `labrecorder` | `true` | start and stop LabRecorder automatically if it is open |
| `[[camera]]` `name`, `device` | required | LSL stream name / file prefix, and device number |
| `[[camera]]` `width`, `height`, `fps` | `1280`, `720`, `30` | capture format |
| `[pupil]` `enabled` | `true` | include the Pupil Core (if the section exists) |
| `[pupil]` `record` | `true` | Pupil Capture saves world and eye videos into the session |
| `[pupil]` `host`, `port` | `127.0.0.1`, `50020` | where Pupil Capture runs |
| `[audio]` `enabled` | `true` | include the microphone (if the section exists) |
| `[audio]` `name` | `mic` | LSL stream name / file prefix |
| `[audio]` `device` | default input | part of the microphone's name, or its number from `--list-audio` |
| `[audio]` `rate`, `channels` | `48000`, `1` | sample rate and channel count |

For the microphone, turn off Windows' audio processing, which gates quiet sound to silence and shifts onset
timing: *Settings > System > Sound > (your microphone) > Audio enhancements: Off*, and raise *Input volume*
so speech peaks well above -30 dBFS (the session prints the peak level every 5 s).

## 3. Record a session

1. Open **LabRecorder**. If using the eye tracker, open **Pupil Capture** too and calibrate it.
2. Start the session:
   ```bash
   .venv\Scripts\python.exe record_session.py --label pilot1
   ```
   When everything is live, LabRecorder starts recording into the session folder by itself.
3. To get a sync check, switch the room lights off and on once, where every camera can see it.
   The switch's click is the sound event for the microphone, so keep the mic near the switch.
4. Stop with **Ctrl+C**, or **q** in any preview window. Everything stops together and a summary is printed.

| Flag | Effect |
|---|---|
| `--label NAME` | add NAME to the session folder name (letters, digits, `_`, `-`) |
| `--config FILE` | use a different config file instead of `cameras.toml` |
| `--no-preview` | no preview windows |
| `--no-labrecorder` | don't drive LabRecorder (start it by hand when told) |
| `--no-pupil` | leave the Pupil Core out |
| `--no-audio` | leave the microphone out |
| `--list` | show a labelled snapshot from each camera, then exit |
| `--list-audio` | list microphones, then exit |

Each session goes into `recordings/<date>_<time>_<label>/`:

| File | Contents |
|---|---|
| `<session>.xdf` | LabRecorder recording of all LSL streams |
| `<camera>_<time>.avi`, `<camera>_<time>_frames.csv` | video, and a backup of each frame's LSL timestamp |
| `pupil/000/` | Pupil Capture's recording (world and eye videos, gaze) |
| `pupil_clock.csv` | Pupil time and LSL time pairs |
| `mic_<time>.wav`, `mic_<time>_blocks.csv` | audio, and a backup of each audio block's LSL timestamp |
| `session.json`, `session.log` | settings, how each part ended, all console output |

If the summary says **WITH PROBLEMS**, a part failed, or a stream is missing or empty in the XDF. Record again.

## 4. Analyse a session

```bash
.venv\Scripts\python.exe analyze_xdf.py recordings\20261113_101500_pilot1
```

Accepts a session folder or an `.xdf` file. It writes `<session>_side_by_side.mp4` (all cameras, including
the Pupil world camera, on one timeline) and `<session>_signals.png` into the session folder. It also prints
frame rates, gaps, Pupil gaze and pupil health, and the offset between cameras at the flash, plus the
microphone's offset at the switch's click. The plot gets an audio loudness panel.
A flash strength below about 8 means no clear flash was found, and a click less than about 20 dB above the
background is not clear either; ignore those offsets.

| Flag | Effect |
|---|---|
| `--out-dir DIR` | write results to DIR instead of the session folder |
| `--search START END` | only look for the flash between these seconds of the shared window |

## Running one part alone (optional)

`record_session.py` normally starts these for you.

One camera:

```bash
.venv\Scripts\python.exe lib\camera_stream.py --device 0 --name laptop_cam
```

Flags: `--width` (1280), `--height` (720), `--fps` (30), `--out-dir` (`recordings`), `--no-preview`,
`--mains-hz {50,60}` (50).

Pupil Core relay (Pupil Capture must be running):

```bash
.venv\Scripts\python.exe lib\pupil_stream.py --out-dir recordings\test
```

Flags: `--host` (127.0.0.1), `--port` (50020), `--name` (`pupil`, prefix of the streams `pupil_gaze`,
`pupil_pupils`, `pupil_clock`), `--no-record` (don't make Pupil Capture record).

Microphone:

```bash
.venv\Scripts\python.exe lib\audio_stream.py --device "Headset Microphone" --out-dir recordings\test
```

Flags: `--device` (default input), `--name` (`mic`), `--rate` (48000), `--channels` (1), `--out-dir`
(`recordings`), `--list` (list microphones and exit).

## Good to know

- Pupil Capture remembers the last session's folder as its recording name. Before recording in Pupil Capture
  by hand, change *Recording session name* in its Recorder menu.
- The Pupil Core's cameras never appear in `--list`: only Pupil Capture can open them.
