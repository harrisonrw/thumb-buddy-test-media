#!/usr/bin/env python3
"""Generate test-media fixtures from one source clip using ffmpeg.

Takes a short excerpt of the source and writes it out in many encodings, so
the content stays constant and only the technical properties vary. Each output
is named by probing it with rename-test-videos.py, so names always match the
scheme in test-media-naming.md.

Groups (default: all):

    matrix    codec x bit depth (h264, hevc, av1, vp9, prores422, mpeg2)
    res       resolution ladder, up to the source's own size
    fps       common frame rates
    variants  rotation, VFR, no audio, 4:2:2, 4:4:4, anamorphic, HDR tags,
              portrait and square crops
    negative  broken and misleading files, written to <root>/negative

Existing files are never overwritten; a variant whose name is already taken
is skipped.

<root> is the media root: the first of --root, $TEST_MEDIA_ROOT, `root` in
config.toml at the project root, the project root itself.

    ./generate-test-media.py action-sports/skiing__*.mp4
    ./generate-test-media.py clip.mov --groups matrix negative --start 12
    ./generate-test-media.py clip.mov --list
"""

import argparse
import importlib.util
import random
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
GROUPS = ["matrix", "res", "fps", "variants", "negative"]

H264 = ["-c:v", "libx264", "-preset", "medium", "-crf", "20"]
HEVC = ["-c:v", "libx265", "-preset", "medium", "-crf", "22", "-tag:v", "hvc1"]
AV1 = ["-c:v", "libsvtav1", "-preset", "8", "-crf", "32"]
VP9 = ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-row-mt", "1", "-cpu-used", "4"]
# Proxy keeps ProRes fixtures small; the codec token is prores422 either way.
PRORES = ["-c:v", "prores_ks", "-profile:v", "proxy"]
MPEG2 = ["-c:v", "mpeg2video", "-q:v", "4"]

AUDIO = {
    ".mp4": ["-c:a", "aac", "-b:a", "128k"],
    ".webm": ["-c:a", "libopus", "-b:a", "96k"],
    ".mov": ["-c:a", "pcm_s16le"],
    ".mpg": ["-c:a", "mp2", "-b:a", "192k"],
}


def hdr_tags(transfer):
    # Tags the stream as HDR without re-grading it: enough to exercise HDR
    # detection and tone-mapping paths, but the picture is not true HDR.
    return f"setparams=color_primaries=bt2020:color_trc={transfer}:colorspace=bt2020nc"


@dataclass
class Variant:
    name: str
    group: str
    video: list
    pix_fmt: str = "yuv420p"
    ext: str = ".mp4"
    pre: tuple = ()  # filters applied before scaling
    post: tuple = ()  # filters applied after scaling
    opts: tuple = ()  # extra output options
    height: int | None = None  # short-edge override (res ladder)
    rotation: int | None = None  # clockwise display rotation to tag
    audio: bool = True


VARIANTS = [
    Variant("h264-8bit", "matrix", H264),
    Variant("h264-10bit", "matrix", H264, "yuv420p10le"),
    Variant("hevc-8bit", "matrix", HEVC),
    Variant("hevc-10bit", "matrix", HEVC, "yuv420p10le"),
    Variant("hevc-12bit", "matrix", HEVC, "yuv420p12le"),
    Variant("av1-8bit", "matrix", AV1),
    Variant("av1-10bit", "matrix", AV1, "yuv420p10le"),
    Variant("vp9-8bit", "matrix", VP9, ext=".webm"),
    Variant("vp9-10bit", "matrix", VP9, "yuv420p10le", ext=".webm"),
    Variant("prores422", "matrix", PRORES, "yuv422p10le", ext=".mov"),
    Variant("mpeg2", "matrix", MPEG2, ext=".mpg"),
    *(Variant(f"{h}p", "res", H264, height=h) for h in (480, 720, 1080, 1440, 2160)),
    Variant("23.976fps", "fps", H264, pre=("fps=24000/1001",)),
    Variant("25fps", "fps", H264, pre=("fps=25",)),
    Variant("29.97fps", "fps", H264, pre=("fps=30000/1001",)),
    Variant("59.94fps", "fps", H264, pre=("fps=60000/1001",)),
    Variant("rot90", "variants", H264, rotation=90),
    Variant("rot270", "variants", H264, rotation=270),
    # Dropping an irregular subset of frames leaves uneven timestamp gaps.
    Variant("vfr", "variants", H264, pre=("select='not(eq(mod(n,7),3))'",), opts=("-fps_mode", "vfr")),
    Variant("noaudio", "variants", H264, audio=False),
    Variant("422", "variants", H264, "yuv422p"),
    Variant("444", "variants", H264, "yuv444p"),
    Variant("anamorphic", "variants", H264, post=("scale=trunc(iw*3/8)*2:ih", "setsar=4/3")),
    Variant("hdr-pq", "variants", HEVC, "yuv420p10le", post=(hdr_tags("smpte2084"),)),
    Variant("hdr-hlg", "variants", HEVC, "yuv420p10le", post=(hdr_tags("arib-std-b67"),)),
    Variant("port", "variants", H264, pre=("crop='trunc(min(iw,ih*9/16)/2)*2':ih",)),
    Variant("sq", "variants", H264, pre=("crop='min(iw,ih)':'min(iw,ih)'",)),
]

NEGATIVES = {
    "zero-byte": "empty file",
    "not-a-video": "plain text with a .mp4 extension",
    "truncated-moov-atom": "cut off before the index, so nothing is readable",
    "truncated-mdat": "index intact but the second half of the frames is missing",
    "corrupt-frames": "index intact but frame data is overwritten in places",
    "audio-only": "MP4 container with no video stream",
    "wrong-extension": "valid MP4 named .mkv",
}


def load_renamer():
    sys.dont_write_bytecode = True  # keep __pycache__ out of the fixtures tree
    spec = importlib.util.spec_from_file_location("rename_test_media", HERE / "rename-test-media.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


renamer = load_renamer()


def ffmpeg(*args):
    """Run ffmpeg quietly; return None on success or the last error line."""
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-y", *map(str, args)],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return None
    lines = result.stderr.strip().splitlines()
    return lines[-1] if lines else f"ffmpeg exited {result.returncode}"


def available_encoders():
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True
    ).stdout
    return {line.split()[1] for line in out.splitlines() if len(line.split()) > 1}


def scale_filter(height):
    """Scale so the short edge is `height`, keeping aspect and even dimensions."""
    # Rounding to even dimensions nudges the pixel aspect off 1:1; reset it.
    return f"scale=w='if(gt(iw,ih),-2,{height})':h='if(gt(iw,ih),{height},-2)',setsar=1"


def encode(variant, source, dest, start, duration, height):
    filters = [*variant.pre]
    target = variant.height or height
    if target:
        filters.append(scale_filter(target))
    filters += [*variant.post, f"format={variant.pix_fmt}"]
    audio = ["-map", "0:a:0?", *AUDIO[variant.ext]] if variant.audio else ["-an"]

    error = ffmpeg(
        "-ss", start, "-t", duration, "-i", source,
        "-map", "0:v:0", "-map_metadata", "-1", "-map_chapters", "-1",
        "-vf", ",".join(filters),
        *variant.video, *variant.opts, *audio,
        dest,
    )  # fmt: skip
    if error or variant.rotation is None:
        return error

    # Tag the rotation in a remux so the stored pixels stay unrotated, the way
    # a phone writes them. The display matrix is counter-clockwise.
    tagged = dest.with_name(f"rot-{dest.name}")
    error = ffmpeg("-display_rotation", -variant.rotation, "-i", dest, "-map", "0", "-c", "copy", tagged)
    if error:
        tagged.unlink(missing_ok=True)
        return error
    tagged.replace(dest)
    return None


def finalize(temp, root):
    """Give a freshly written file its probed name, unless that name is taken."""
    final = temp.with_name(renamer.new_name(temp))
    try:
        shown = final.relative_to(root)
    except ValueError:
        shown = final
    if final.exists():
        temp.unlink()
        print(f"exists   {shown}")
        return False
    temp.rename(final)
    print(f"created  {shown}")
    return True


def temp_path(directory, slug, name, ext):
    # Everything before `__` is the slug the renamer will keep.
    return directory / f"{slug}__gen-{name}{ext}"


def generate_negatives(source, directory, root, start, duration, height):
    created = failed = 0
    directory.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as work:
        work = Path(work)
        base = work / "base.mp4"  # index (moov) at the end
        fast = work / "fast.mp4"  # index at the front
        audio = work / "audio.mp4"
        error = encode(Variant("base", "negative", H264), source, base, start, duration, height)
        error = error or ffmpeg("-i", base, "-c", "copy", "-movflags", "+faststart", fast)
        if error:
            print(f"FAILED   negative group: {error}", file=sys.stderr)
            return 0, len(NEGATIVES)

        base_bytes, fast_bytes = base.read_bytes(), fast.read_bytes()

        corrupt = bytearray(fast_bytes)
        rng = random.Random(0)
        for _ in range(20):
            offset = rng.randrange(len(corrupt) * 3 // 10, len(corrupt) * 9 // 10)
            corrupt[offset : offset + 2048] = rng.randbytes(len(corrupt[offset : offset + 2048]))

        contents = {
            "zero-byte": (".mp4", b""),
            "not-a-video": (".mp4", b"This is a text file, not a video.\n"),
            "truncated-moov-atom": (".mp4", base_bytes[: len(base_bytes) * 6 // 10]),
            "truncated-mdat": (".mp4", fast_bytes[: len(fast_bytes) // 2]),
            "corrupt-frames": (".mp4", bytes(corrupt)),
            "wrong-extension": (".mkv", base_bytes),
        }
        audio_error = ffmpeg("-i", base, "-vn", "-map", "0:a:0", "-c", "copy", audio)
        if audio_error:
            print(f"FAILED   audio-only: {audio_error}", file=sys.stderr)
            failed += 1
        else:
            contents["audio-only"] = (".mp4", audio.read_bytes())

    for slug, (ext, data) in contents.items():
        temp = temp_path(directory, slug, "negative", ext)
        temp.write_bytes(data)
        created += finalize(temp, root)
    return created, failed


def source_short_edge(source):
    probe = renamer.run_ffprobe(["-select_streams", "v:0", "-show_streams"], source)
    streams = probe.get("streams", [])
    if not streams or not streams[0].get("width"):
        sys.exit(f"error: no video stream found in {source}")
    return min(streams[0]["width"], streams[0]["height"])


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("source", type=Path, help="clip to derive the fixtures from")
    parser.add_argument("--groups", nargs="+", choices=GROUPS, default=GROUPS, metavar="GROUP")
    parser.add_argument("--start", type=float, default=0, help="excerpt start in seconds (default: 0)")
    parser.add_argument("--duration", type=float, default=5, help="excerpt length in seconds (default: 5)")
    parser.add_argument(
        "--height",
        type=int,
        default=1080,
        help="short edge of the outputs, never upscaled; 0 keeps the source size (default: 1080)",
    )
    parser.add_argument("--slug", help="slug for the outputs (default: the source's slug)")
    parser.add_argument("--out", type=Path, help="directory for the outputs (default: the source's directory)")
    parser.add_argument(
        "--root",
        type=Path,
        help=f"media root; negative cases go in <root>/negative "
        f"(default: ${renamer.ROOT_ENV}, then `root` in {renamer.CONFIG_FILE.name}, then the project root)",
    )
    parser.add_argument("--list", action="store_true", help="list what would be generated and exit")
    args = parser.parse_args()

    if not args.source.is_file():
        sys.exit(f"error: {args.source} is not a file")
    source = args.source.resolve()
    root = renamer.media_root(args.root)
    out = (args.out or source.parent).resolve()
    slug = renamer.slug_of(Path(args.slug)) if args.slug else renamer.slug_of(source)

    short_edge = source_short_edge(source)
    height = min(args.height, short_edge) if args.height else 0
    encoders = available_encoders()

    planned = [
        v
        for v in VARIANTS
        if v.group in args.groups and not (v.height and v.height > short_edge)
    ]

    if args.list:
        for v in planned:
            print(f"{v.group:<9} {v.name}")
        if "negative" in args.groups:
            for name, description in NEGATIVES.items():
                print(f"{'negative':<9} {name}: {description}")
        return 0

    created = failed = 0
    out.mkdir(parents=True, exist_ok=True)
    for v in planned:
        encoder = v.video[1]
        if encoder not in encoders:
            print(f"SKIP     {v.name} (ffmpeg has no {encoder} encoder)", file=sys.stderr)
            failed += 1
            continue
        temp = temp_path(out, slug, v.name, v.ext)
        error = encode(v, source, temp, args.start, args.duration, height)
        if error:
            temp.unlink(missing_ok=True)
            print(f"FAILED   {v.name}: {error}", file=sys.stderr)
            failed += 1
            continue
        created += finalize(temp, root)

    if "negative" in args.groups:
        n_created, n_failed = generate_negatives(
            source, root / "negative", root, args.start, args.duration, height
        )
        created += n_created
        failed += n_failed

    print(f"\n{created} created, {failed} failed or skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
