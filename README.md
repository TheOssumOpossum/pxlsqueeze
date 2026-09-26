# pxlsqueeze

Batch-compress Pixel videos (`PXL_YYYYMMDD_HHMMSSmmm.mp4`) into visually lossless HEVC, fix sideways or upside-down footage, and trim dead air around talking-to-camera clips. Every result gets checked in a local review UI before any original is touched.

Each output sits next to its original as `PXL_20260920_142205874~2.mp4`. Originals are only moved to the Trash when you approve and finalize.

## Install

You need Python 3.11+ and ffmpeg 6.1+ built with libx265.

**macOS**

```sh
brew install ffmpeg python@3.12 pipx
pipx install ./pxlsqueeze
```

**Windows** (PowerShell)

```powershell
winget install Gyan.FFmpeg
winget install Python.Python.3.12
py -m pip install --user pipx
py -m pipx install .\pxlsqueeze
```

To update to a newer version later, unzip it over the old folder and run `pipx install --force ./pxlsqueeze`, or `py -m pipx install --force .\pxlsqueeze` on Windows. Your progress in each video folder is kept. `pxlsqueeze --version` shows which version is installed.

Then check what your setup supports:

```sh
pxlsqueeze doctor
```

The first run downloads two small models (~2.5 MB) into your user cache: a face detector and a speech detector. If either download fails, pxlsqueeze falls back to OpenCV's built-in face cascade and a loudness-based speech detector, and `doctor` tells you which you're on.

**Optional, recommended for scenery:** `pipx inject pxlsqueeze open_clip_torch torch pillow` (or `pip install "pxlsqueeze[orient-ml]"`). This adds a scene-based orientation check for clips without faces. Without it, every faceless clip is marked "Check rotation" for you to eyeball. It downloads roughly 600 MB of PyTorch plus a ~350 MB model the first time.

## Use

```sh
pxlsqueeze calibrate ~/Movies/pixel       # optional first step: pick a quality level
pxlsqueeze run ~/Movies/pixel             # analyze → encode → verify → open the review UI
```

`run` works in stages and can be interrupted and restarted at any point. Rerunning skips videos that are already encoded and checked. It redoes the video that was mid-encode when you stopped, and it retries any that failed. Changing `--crf`, `--codec` or `--preset` re-encodes everything, because those settings are part of how each video was made. State lives in `~/Movies/pixel/.pxlsqueeze/manifest.json`.

**One copy per folder.** Only one pxlsqueeze command can work on a folder at a time. If you start a second one, for example `review` while `run` is still encoding, it stops with a message saying what's already running. `status` and `doctor` always work.

**Recommended first run:** `pxlsqueeze run ~/Movies/pixel --review-first`. This analyzes the folder, then opens the UI without encoding. Check rotation and trim, then press **Encode N videos** in the top bar, or **Encode** on individual videos, and keep the terminal open while they encode.

| Command | What it does |
|---|---|
| `run DIR` | Analyze, encode and check everything, then open the review UI once encoding is finished. `--review-first` opens the UI straight after analysis and leaves encoding for you to start from the UI. |
| `analyze DIR` | Probe, detect rotation and speech. No encoding. `--reanalyze` redoes it but keeps your manual edits. |
| `encode DIR` | Encode anything whose output is missing or out of date. |
| `review DIR` | Open the review UI at any stage. |
| `calibrate DIR` | Encode 10 s samples at several CRFs and print size vs quality. |
| `status DIR` | Table of every video and where it's at. |
| `finalize DIR` | Trash the originals of approved videos, and videos marked for the Trash. Needs a typed confirmation. `--permanent` deletes outright instead of trashing. |
| `doctor` | Check ffmpeg features and models. |
| `--version` | Show the installed version. |

Options for `run` and `encode`:

- `--crf N` sets the quality level. Lower means better quality and bigger files. The default is 20.
- `--codec av1` uses AV1 instead of HEVC. Files come out smaller, but fewer devices play them.
- `--preset` sets the encoder speed.
- `--ffmpeg PATH` points at a specific ffmpeg if it isn't on your PATH.

Settings are remembered per folder.

## Review UI

The UI runs locally at `http://127.0.0.1:8765`.

- **Side-by-side players.** The original and the new file play in sync, and sound comes from the original. The original's preview is rotated to match whatever rotation you've chosen.
- **Timeline.** Speech segments are drawn in blue; the parts that will be cut are hatched. You can drag the trim handles, or use the arrow keys with a handle focused (hold Shift for 1 s steps).
- **Needs a look.** This tab is your to-do list: every video still waiting on a decision. That includes videos not encoded yet, encoded ones waiting for approval, ones with warnings (amber border), and failures. A video leaves the list once you approve, reject or trash it. **All** shows everything.
- **Ready.** Only videos that are encoded, passed their check, and are waiting for your decision. It's the quickest way through a batch: press `A` or `R` and the next video opens automatically. Videos with a warning still appear here with their amber chip, so glance at those before approving.
- **Rotation and trim controls.** Rotate left, rotate right, flip, use detected, start/end at playhead, keep whole video. Changing either on an encoded video marks it "Changes not encoded", and approval is blocked until you click **Encode with changes**.
- **Summary bar.** The top bar counts every video in the folder once: **encoded** (has a new file), **trashed**, **kept as is** (you rejected the new file, or it didn't save enough), and **to go** (still needs encoding or failed). Next to that it shows the space already **freed** by finalizing, and how much more is **pending** until you finalize. Hover any number for a definition. `pxlsqueeze status` prints the same summary.
- **Encoding from the UI.** **Encode N videos** in the top bar covers videos with no new file yet, videos with unencoded changes, and videos that failed last time. They run one at a time with progress shown at the top. The per-video button (`E`) changes with the video's state:
  - **Encode**: no new file yet. Use it after checking a video that needs no changes.
  - **Encode with changes**: your rotation or trim edits aren't in the new file yet.
  - **Encode anyway**: the last attempt barely saved space, so the original was kept. This makes the new file and keeps it regardless.
  - **Re-encode**: makes an up-to-date new file again with the same settings.
  - **Check again**: re-runs the quality check on a new file that's waiting for one.
  - **Try again**: the last attempt failed.
  - **Queued**, **Encoding…** or **Checking…**: it's already in progress.

  In the video list, blue chips show the same thing at a glance: **Encoding 42%**, which fills in as it goes, **Checking**, or **Queued**. Hover a Queued chip to see its place in line. While a video has one of these, its "Not encoded yet", "Changes not encoded", "Kept original" or "Error" chip is hidden.
- **Frame check.** This pulls a full-resolution PNG from both files at the playhead and shows them with a wipe slider and an actual-size view. Use it to judge quality, because the browser's playback is not a reliable guide.
- **Approve / Reject / Trash video.** **Undo** (`U`) clears a decision.
  - **Approve**: the new file will replace the original. Nothing happens to your files until you finalize.
  - **Reject**: keeps the original and deletes the new file straight away. Undo clears the decision, but the new file has to be encoded again.
  - **Trash video**: the original and any new file both go to the Trash when you finalize, with no copy kept. Use this for accidental clips you don't want at all.
- **Finalize.** **Move N originals to Trash** in the top bar carries out approvals and trash marks. It re-checks each approved new file first, and any file that fails the check keeps its original.
- **Short clips.** Clips of 3 seconds or less are flagged "Short clip" as likely accidental recordings. They aren't removed automatically: the header's **Mark N short clips for Trash** button marks them all at once, and you can review them in the **Trash** tab and keep any you want. Change the cutoff with `--short-seconds N` on `run` or `review`; it's remembered for the folder.
- **Keyboard shortcuts:**
  - `A` approve, `R` reject, `T` trash video, `U` undo, `E` encode.
  - `J`/`K` next/previous video.
  - `Space` play/pause, `←`/`→` step one frame.
  - `[` / `]` set trim start/end at the playhead.

If your browser can't play HEVC (for example Firefox, or Windows without the HEVC Video Extensions), the UI makes a small H.264 preview copy automatically. Safari and Chrome on a Mac play the files directly.

## How it decides things

- **Compression.** libx265 `-preset slow -crf 20`, 10-bit kept for HDR, colour tags copied, audio stream-copied, variable frame rate preserved. Creation time, GPS location and the file's modified date carry over.
  - If an untouched video comes out less than 10% smaller, the output is discarded and the original kept. This is common when the phone already recorded in HEVC.
- **Quality check.** After encoding, each file is fully decoded, its duration is checked, and it is compared frame-by-frame with the original using VMAF. If your ffmpeg lacks libvmaf, SSIM is used instead.
  - Flag thresholds: VMAF below 95 mean or 90 on the worst 1% of frames; SSIM below 0.97 mean or 0.94.
- **Rotation.** Eight frames are sampled and each is tried at 0/90/180/270°. The rotation where faces are found most confidently wins, provided it beats the runner-up by 2×. Otherwise the CLIP scene check runs if it's installed. Otherwise the video is left as-is and flagged.
  - Videos that are genuinely landscape and already upright stay landscape.
  - Rotation stored in the file's metadata (the normal case) is already respected; only content that's actually sideways gets turned.
- **Trim.** Speech is detected with Silero VAD. A clip counts as talking-to-camera when all of these hold:
  - it has at least 3 s of speech;
  - speech covers at least 25% of the clip;
  - a reasonably large face is on screen in at least half the sampled frames. This rule stops background chatter from trimming scenery clips.

  The kept part runs from 1 s before the first word to 1 s after the last.

## Things to know

- **HDR10+.** Dynamic metadata can't be carried over by ffmpeg. The static HDR look is kept, and affected videos get a note in the UI.
- **Google Photos.** If your storage concern is Google Photos rather than local disk, uploading the `~2` files won't free quota unless the originals are also removed there.
- **Extra Pixel file types** such as `PXL_…​.LS.mp4` are skipped unless you pass `--include-variants`. Files that already have a `~2` output are never overwritten.
- **Logs.**
  - Main log: `.pxlsqueeze/pxlsqueeze.log`.
  - Per-video ffmpeg logs: `.pxlsqueeze/cache/logs/`.
  - Finalize logs: `.pxlsqueeze/finalize-*.log`.
- **Cleanup.** Once you're finished with a folder, you can delete `.pxlsqueeze/` to reclaim the cached frames and previews.

## Development

```sh
pip install -e ".[dev]"
pytest -q tests/          # builds synthetic clips; needs ffmpeg with libflite + scikit-image
python tests/make_fixtures.py /tmp/fx && pxlsqueeze run /tmp/fx --preset ultrafast
```

Layout:

- `ff.py` wraps ffmpeg and ffprobe.
- `orientation.py` and `speech.py` hold the detectors.
- `encode.py` and `verify.py` build and check the encodes.
- `pipeline.py` holds the stage runners and the UI's job queue.
- `server.py` and `static/` are the review UI.
- `manifest.py` holds all the state.
