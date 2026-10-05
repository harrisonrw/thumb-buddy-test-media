#!/usr/bin/env python3
"""Rename test media to the scheme in test-media-naming.md using ffprobe.

    <slug>__<res>_<orient>_<fps>_<codec>_<depth>_<duration>[_<extras>].<ext>

Fields ffprobe cannot determine are omitted, so a file with no readable video
stream keeps only its duration (`<slug>__<duration>.<ext>`) and a file with
nothing readable keeps only its slug (`<slug>.<ext>`).

Walks each immediate sub-directory of the media root and renames every file in
it. The slug is taken from the existing filename (everything before `__`, if
present), so re-running is idempotent.

The media root is the first of: the command line, $TEST_MEDIA_ROOT, `root` in
config.toml at the project root, the project root itself.

Dry run by default; pass --apply to actually rename.

    ./rename-test-media.py            # preview
    ./rename-test-media.py --apply    # rename
"""

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import tomllib
from fractions import Fraction
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "config.toml"
ROOT_ENV = "TEST_MEDIA_ROOT"
# Never media categories, even when the media root is the project root.
PROJECT_DIRS = {PROJECT_ROOT / "docs", PROJECT_ROOT / "scripts"}

STANDARD_HEIGHTS = {480, 720, 1080, 1440, 2160, 4320}
CODEC_ALIASES = {"mpeg2video": "mpeg2", "mpeg1video": "mpeg1"}
HDR_TRANSFERS = {"smpte2084": "hdr-pq", "arib-std-b67": "hdr-hlg"}


def media_root(cli_root=None):
    """Directory holding the category sub-directories.

    First of: `cli_root`, $TEST_MEDIA_ROOT, `root` in config.toml, the
    project root.
    """
    if cli_root:
        return cli_root.expanduser().resolve()
    if os.environ.get(ROOT_ENV):
        return Path(os.environ[ROOT_ENV]).expanduser().resolve()
    if CONFIG_FILE.is_file():
        try:
            root = tomllib.loads(CONFIG_FILE.read_text()).get("root")
        except tomllib.TOMLDecodeError as error:
            sys.exit(f"error: {CONFIG_FILE}: {error}")
        if root is not None and not isinstance(root, str):
            sys.exit(f"error: {CONFIG_FILE}: `root` must be a string")
        if root:
            # Relative paths are relative to the config file, not the cwd.
            return (PROJECT_ROOT / Path(root).expanduser()).resolve()
    return PROJECT_ROOT


def run_ffprobe(args, path):
    """Run ffprobe and return parsed JSON, or {} if the file is unreadable."""
    cmd = ["ffprobe", "-v", "error", *args, "-of", "json", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
        return json.loads(out)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return {}


def parse_fraction(value):
    try:
        frac = Fraction(value.replace(":", "/"))
    except (AttributeError, ValueError, ZeroDivisionError):
        return None
    return frac if frac > 0 else None


def rotation_of(stream):
    """Clockwise display rotation in degrees (0, 90, 180, 270)."""
    for side_data in stream.get("side_data_list", []):
        if "rotation" in side_data:
            # Display matrix rotation is counter-clockwise; the scheme (like
            # the legacy `rotate` tag) is clockwise.
            return round(-float(side_data["rotation"])) % 360
    try:
        return int(stream.get("tags", {}).get("rotate", 0)) % 360
    except ValueError:
        return 0


def res_token(width, height):
    short_edge = min(width, height)
    if short_edge in STANDARD_HEIGHTS:
        return f"{short_edge}p"
    return f"{width}x{height}"


def orient_token(width, height):
    if width == height:
        return "sq"
    return "land" if width > height else "port"


def fps_token(stream):
    fps = parse_fraction(stream.get("avg_frame_rate")) or parse_fraction(
        stream.get("r_frame_rate")
    )
    if fps is None:
        return None
    # Three decimals keeps 23.976 intact while 29.970 -> 29.97 and 60.000 -> 60.
    return f"{float(fps):.3f}".rstrip("0").rstrip(".") + "fps"


def codec_token(stream):
    name = stream["codec_name"]
    if name == "prores":
        return "prores4444" if "4444" in stream.get("profile", "") else "prores422"
    name = CODEC_ALIASES.get(name, name)
    return re.sub(r"[^a-z0-9]", "", name.lower())


def depth_token(stream):
    bits = stream.get("bits_per_raw_sample")
    if bits and str(bits).isdigit() and int(bits) > 0:
        return f"{int(bits)}bit"
    pix_fmt = stream.get("pix_fmt")
    if not pix_fmt:
        return None
    # yuv420p10le, p010le, gbrp12be ...; anything without a suffix is 8-bit.
    match = re.search(r"(\d+)(?:le|be)$", pix_fmt)
    return f"{int(match.group(1))}bit" if match else "8bit"


def duration_token(probe, stream):
    raw = probe.get("format", {}).get("duration") or (stream or {}).get("duration")
    try:
        total = round(float(raw))
    except (TypeError, ValueError):
        return None
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


def is_vfr(path, stream):
    """True if the gaps between presentation timestamps vary meaningfully."""
    data = run_ffprobe(
        ["-select_streams", str(stream["index"]), "-show_entries", "packet=pts"], path
    )
    pts = sorted(
        int(p["pts"]) for p in data.get("packets", []) if str(p.get("pts", "")).lstrip("-").isdigit()
    )
    deltas = [b - a for a, b in zip(pts, pts[1:])]
    if len(deltas) < 2:
        return False
    # Tolerate timebase rounding (e.g. Matroska's 1 ms ticks: 33, 33, 34 ...).
    tolerance = max(2, 0.1 * statistics.median(deltas))
    return max(deltas) - min(deltas) > tolerance


def chroma_extra(stream):
    if stream.get("codec_name") == "prores":
        return None  # already in the codec token
    pix_fmt = stream.get("pix_fmt", "")
    if re.match(r"yuvj?a?422|yuyv|uyvy", pix_fmt):
        return "422"
    if re.match(r"yuvj?a?444|gbr|rgb|bgr", pix_fmt):
        return "444"
    return None


def metadata_block(path):
    """Metadata fields joined by underscores; unknown fields are left out."""
    probe = run_ffprobe(["-show_format", "-show_streams"], path)
    streams = probe.get("streams", [])
    video = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video"
            and not s.get("disposition", {}).get("attached_pic")
            and s.get("codec_name")
            and s.get("width")
            and s.get("height")
        ),
        None,
    )
    duration = duration_token(probe, video)

    if video is None:
        return duration or ""

    width, height = video["width"], video["height"]
    rotation = rotation_of(video)
    sar = parse_fraction(video.get("sample_aspect_ratio")) or Fraction(1)

    # Orientation is what the viewer sees: apply pixel aspect, then rotation.
    display_w, display_h = width * sar, Fraction(height)
    if rotation in (90, 270):
        display_w, display_h = display_h, display_w

    extras = [
        HDR_TRANSFERS.get(video.get("color_transfer")),
        "vfr" if is_vfr(path, video) else None,
        f"rot{rotation}" if rotation else None,
        chroma_extra(video),
        None if any(s.get("codec_type") == "audio" for s in streams) else "noaudio",
        "anamorphic" if sar != 1 else None,
    ]
    fields = [
        res_token(width, height),
        orient_token(display_w, display_h),
        fps_token(video),
        codec_token(video),
        depth_token(video),
        duration,
        "-".join(e for e in extras if e),
    ]
    return "_".join(f for f in fields if f)


def slug_of(path):
    stem = path.name[: -len(path.suffix)] if path.suffix else path.name
    stem = stem.split("__", 1)[0]
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    return slug or "untitled"


def new_name(path):
    block = metadata_block(path)
    separator = "__" if block else ""
    return f"{slug_of(path)}{separator}{block}{path.suffix.lower()}"


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "root",
        nargs="?",
        type=Path,
        help=f"directory containing the category sub-directories "
        f"(default: ${ROOT_ENV}, then `root` in {CONFIG_FILE.name}, then the project root)",
    )
    parser.add_argument("--apply", action="store_true", help="rename files (default is a dry run)")
    args = parser.parse_args()

    root = media_root(args.root)
    if not root.is_dir():
        sys.exit(f"error: media root {root} is not a directory")

    files = sorted(
        f
        for d in root.iterdir()
        if d.is_dir() and not d.name.startswith(".") and d not in PROJECT_DIRS
        for f in d.iterdir()
        if f.is_file() and not f.name.startswith(".")
    )

    renamed = skipped = 0
    for src in files:
        dst = src.with_name(new_name(src))
        rel_src = src.relative_to(root)
        if dst.name == src.name:
            print(f"ok       {rel_src}")
            continue
        # samefile allows case-only renames on case-insensitive filesystems.
        if dst.exists() and not dst.samefile(src):
            print(f"SKIP     {rel_src} -> {dst.name} (target exists)", file=sys.stderr)
            skipped += 1
            continue
        print(f"{'rename' if args.apply else 'would'}   {rel_src}\n      -> {dst.relative_to(root)}")
        if args.apply:
            src.rename(dst)
        renamed += 1

    verb = "renamed" if args.apply else "to rename"
    print(f"\n{renamed} {verb}, {skipped} skipped, {len(files) - renamed - skipped} already correct")
    if renamed and not args.apply:
        print("Dry run. Re-run with --apply to rename.")
    return 1 if skipped else 0


if __name__ == "__main__":
    sys.exit(main())
