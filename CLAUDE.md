# CLAUDE.md

pxlsqueeze batch-compresses Pixel phone videos with ffmpeg, fixes their rotation, trims dead air, and lets the user approve results in a local review UI before originals are touched.

## Safety

- **Never run `finalize`, `run`, `encode` or `analyze` on real video folders.** `finalize` moves originals to the Trash (`--permanent` deletes them outright), and the other commands write outputs and `.pxlsqueeze/` state next to the user's videos.
- For manual checks, generate fixtures into a temp directory (`python tests/make_fixtures.py <tmpdir>`) and point commands there.

## Dev setup

- Requires Python 3.11+ and ffmpeg 6.1+ built with libx265 (checked at startup by `ff.check_version()`).
- `pip install -e ".[dev]"` for development; `.[orient-ml]` adds the optional CLIP orientation check (~1 GB of PyTorch, only install if working on that).
- `pxlsqueeze doctor` reports what the current setup supports.

## Testing

- Run `pytest -q tests/`.
- Fixture generation needs ffmpeg with **libflite** and **scikit-image**. Without them the tests are **skipped, not failed** — check the pytest summary for skips before reporting that tests pass.

## Architecture

- `cli.py` — Typer CLI; entry point `pxlsqueeze = pxlsqueeze.cli:app`.
- `pipeline.py` — per-item stage runners (analyze → encode → verify) and the background job queue used by the review UI.
- `manifest.py` — persistent per-folder state in `DIR/.pxlsqueeze/manifest.json` (status, user decision, settings).
- `ff.py` — thin wrapper around ffmpeg/ffprobe. Always use argument lists, never a shell.
- `encode.py` — builds and runs the ffmpeg encode for one item.
- `verify.py` — post-encode checks: full decode, duration, perceptual quality.
- `orientation.py` — rotation detection (faces, optional CLIP). Rotations are relative to ffmpeg's auto-rotated frame.
- `speech.py` — speech detection and trim points.
- `models.py` — downloads small ONNX models into the user cache, with fallbacks.
- `calibrate.py` — CRF sweep to pick a quality level.
- `finalize.py` — trashes originals of approved videos and videos marked for the Trash.
- `lock.py` — OS-level lock so only one pxlsqueeze works on a folder at a time.
- `server.py` + `static/` — local review UI (FastAPI, bound to 127.0.0.1 only).

## Conventions

- **Manifest compatibility:** users have existing `manifest.json` files. If the manifest format changes, bump `SCHEMA_VERSION` in `manifest.py` and migrate old manifests on load. Settings that are part of an item's encode identity (codec, CRF, preset) force a re-encode of everything when changed; adding to that set is a user-visible change.
- **Cross-platform:** users are on macOS and Windows. Use `pathlib`, no hard-coded separators or shell commands, and keep both code paths (e.g. `lock.py`'s flock/msvcrt) working.
- **Fallbacks are intentional:** if model downloads fail, pxlsqueeze falls back to OpenCV's Haar cascade and an energy-based speech detector. Keep those paths working.
- **No frontend build step:** `static/` is plain HTML/CSS/JS served as-is. Don't add a bundler or framework.
- **README stays in sync:** the README documents every command and flag. Update it in the same commit as any CLI change.

## Versioning

Bump the version whenever you commit changes that will be pushed to GitHub.

- The version is defined in **two places** that must always match:
  - `pyproject.toml` (`version = "X.Y.Z"`)
  - `pxlsqueeze/__init__.py` (`__version__ = "X.Y.Z"`)
- Follow semantic versioning:
  - **Patch** (`0.4.1` → `0.4.2`): bug fixes and small tweaks.
  - **Minor** (`0.4.1` → `0.5.0`): new features or behavior changes.
  - **Major** (`0.4.1` → `1.0.0`): breaking changes (CLI flags, manifest format, etc.).
- Include the version bump in the same commit as the change (or as the final commit before pushing), not as a separate push.
- If several commits are pushed together, one bump covering all of them is enough.
