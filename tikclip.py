#!/usr/bin/env python3
"""tikclip - cut videos into TikTok-ready clips.

Examples
  # one 30s clip starting at 1:23
  tikclip.py video.mp4 --at 1:23 --length 30

  # a clip between two timestamps
  tikclip.py video.mp4 --at 1:23 --end 2:05

  # several clips, each 20s, from different timestamps
  tikclip.py video.mp4 --at 0:45 3:10 12:02 --length 20

  # chop the WHOLE video into back-to-back 30s clips (part 1, part 2, ...)
  tikclip.py video.mp4 --every 30

  # works on links too (YouTube, Twitch VODs, X, etc. via yt-dlp)
  tikclip.py "https://youtube.com/watch?v=..." --every 45

  # find the best moments (YouTube 'most replayed' + transcript)
  tikclip.py suggest "https://youtube.com/watch?v=..."

  # upload a clip to a presigned URL (HiggsField media_upload)
  tikclip.py put clips/my_clip.mp4 "https://upload.higgsfield.ai/..."

Every clip is exported as a TikTok-compliant MP4 (H.264/AAC, 1080x1920,
24-60 fps, 3-600 s). A manifest.json is kept in the output folder with a
suggested caption and a "posted" flag per clip.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import importlib.util
import shutil
import string
import subprocess
import sys
from pathlib import Path

TIKTOK_MIN_S = 3.0
TIKTOK_MAX_S = 600.0
CAPTION_MAX = 150
EPS = 1e-3  # 1 ms tolerance for float comparisons
OUT_W, OUT_H = 1080, 1920
STD_RATES = [24000 / 1001, 24.0, 25.0, 30000 / 1001, 30.0, 48.0, 50.0, 60000 / 1001, 60.0]
CAPTION_FIELDS = {"title", "n", "total", "start", "end"}


# ---------------------------------------------------------------- helpers
def die(msg: str) -> "None":
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def parse_time(text: str) -> float:
    """Accept 90, 90.5, 1:30, 01:02:03, 1m30s, 1h2m3s."""
    t = text.strip().lower()
    if re.fullmatch(r"\d+(\.\d+)?", t):
        return float(t)
    if re.fullmatch(r"\d+(:\d{1,2}){1,2}(\.\d+)?", t):
        parts = t.split(":")
        if any(float(p) >= 60 for p in parts[1:]):
            raise argparse.ArgumentTypeError(f"bad timestamp: {text!r} (minutes/seconds must be < 60)")
        secs = 0.0
        for part in parts:
            secs = secs * 60 + float(part)
        return secs
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", t)
    if m and any(m.groups()):
        h, mi, s = (float(g) if g else 0.0 for g in m.groups())
        return h * 3600 + mi * 60 + s
    raise argparse.ArgumentTypeError(f"bad timestamp: {text!r} (use 90, 1:30, 1:02:03 or 1m30s)")


def positive_seconds(text: str) -> float:
    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}")
    if not math.isfinite(v) or v <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive number of seconds: {text!r}")
    return v


def fmt_time(s: float) -> str:
    s = max(0, int(math.floor(s + 0.5)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    # explicit UTF-8: on Windows the default code page can't decode every byte
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def ff_path(p: Path) -> str:
    """Absolute path with file: prefix so names containing ':' aren't read as URLs."""
    return "file:" + str(p.resolve())


def rate(s: str | None) -> float:
    num, _, den = (s or "0/1").partition("/")
    try:
        v = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0
    return v if math.isfinite(v) and v > 0 else 0.0


def scan_duration(path: Path) -> float:
    """Last video packet timestamp - for files with no duration header (e.g. recorded WebM)."""
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "packet=pts_time,duration_time", "-of", "csv=p=0", ff_path(path)])
    end = 0.0
    for line in r.stdout.splitlines():
        parts = [x for x in line.split(",") if x and x != "N/A"]
        try:
            vals = [float(x) for x in parts]
        except ValueError:
            continue
        if vals:
            end = max(end, vals[0] + (vals[1] if len(vals) > 1 else 0))
    return end


def probe(path: Path) -> dict:
    r = run(["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", ff_path(path)])
    if r.returncode != 0:
        die(f"can't read video {path}: {r.stderr.strip()}")
    info = json.loads(r.stdout)
    streams = info.get("streams", [])
    # skip cover art (mp3/m4a album pictures show up as a 1-frame "video")
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")), None)
    if v is None:
        die(f"{path.name} has no video in it (audio-only files can't be clipped for TikTok)")
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    audio = next((s for s in audios if s.get("disposition", {}).get("default")), None) or \
        (audios[0] if audios else None)

    # plan against the VIDEO track's length, not the container's (audio may run longer)
    vdur = float(v.get("duration") or 0)
    if vdur <= 0:
        fdur = float(info.get("format", {}).get("duration") or 0)
        vdur = fdur if fdur > 0 else scan_duration(path)
    else:
        fdur = float(info.get("format", {}).get("duration") or 0)
        if fdur > 0:
            vdur = min(vdur, fdur)

    avg, real = rate(v.get("avg_frame_rate")), rate(v.get("r_frame_rate"))
    fps = avg or real or 30.0
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    rot = int(float(v.get("tags", {}).get("rotate", rot)))
    if abs(rot) % 180 == 90:
        w, h = h, w
    hdr = v.get("color_transfer") in ("smpte2084", "arib-std-b67")
    return {"duration": vdur, "fps": fps, "width": w, "height": h,
            "audio_index": audio["index"] if audio else None, "hdr": hdr}


def output_fps(fps: float) -> float:
    """Snap to the nearest standard rate TikTok accepts (24-60)."""
    if fps < 23:
        return 30.0
    return min(STD_RATES, key=lambda r: abs(r - min(fps, 60.0)))


def ytdlp_cmd() -> list[str]:
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    if importlib.util.find_spec("yt_dlp") is not None:
        # pip on Windows often installs yt-dlp.exe into a folder that isn't on PATH
        return [sys.executable, "-m", "yt_dlp"]
    die("yt-dlp is not installed (pip install yt-dlp)")


def download(url: str, workdir: Path, section: tuple[float, float] | None = None) -> Path:
    """Download a video. With `section`, only that time range is fetched."""
    workdir.mkdir(parents=True, exist_ok=True)
    tag = f" {section[0]:g}-{section[1]:g}" if section else ""
    tmpl = str(workdir / f"%(title).80B [%(id)s]{tag}.%(ext)s")
    cmd = ytdlp_cmd() + ["--no-playlist", "--encoding", "utf-8", "-f",
                         "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b",
                         "--merge-output-format", "mp4", "-o", tmpl,
                         "--print", "after_move:filepath"]
    if shutil.which("node"):
        cmd += ["--js-runtimes", "node"]  # YouTube needs a JS runtime; deno is used if present
    if section:
        # exact cut points (re-encodes only the edges), so clip times line up
        cmd += ["--download-sections", f"*{section[0]:.3f}-{section[1]:.3f}",
                "--force-keyframes-at-cuts"]
        print(f"downloading {fmt_time(section[0])}-{fmt_time(section[1])} of {url} ...")
    else:
        print(f"downloading {url} ...")
    r = subprocess.run(cmd + [url], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        die(f"download failed:\n{r.stderr.strip()[-1500:]}")
    lines = r.stdout.strip().splitlines()
    path = Path(lines[-1]) if lines else None
    if path is None or not path.exists():
        die(f"download finished but file not found: {path}")
    return path


def download_section(args) -> tuple[float, float] | None:
    """The part of the video the requested clips need, or None for the whole thing."""
    if args.every is None and args.at:
        lo = min(args.at)
        if args.end is not None:
            hi = args.end
        elif args.length is not None:
            hi = max(args.at) + args.length
        else:
            return None  # "--at T" alone runs to the end of the video
    elif args.every is not None and args.to is not None:
        lo, hi = (args.from_ or 0.0), args.to
    else:
        return None
    return (lo, hi) if hi > lo else None


# ---------------------------------------------------------------- planning
def plan_clips(args, duration: float) -> list[tuple[float, float]]:
    """Return a list of (start, end) seconds."""
    clips: list[tuple[float, float]] = []
    if args.every is not None:
        n = args.every
        if not TIKTOK_MIN_S - EPS <= n <= TIKTOK_MAX_S + EPS:
            die(f"--every must be between {TIKTOK_MIN_S:g} and {TIKTOK_MAX_S:g} seconds")
        start = args.from_ if args.from_ is not None else 0.0
        stop = min(args.to if args.to is not None else duration, duration)
        if start >= stop:
            die(f"--from {fmt_time(start)} must be before --to/the end ({fmt_time(stop)})")
        i = 0
        while True:
            t = round(start + i * n, 3)  # multiply, don't accumulate float error
            if t >= stop - EPS:
                break
            clips.append((t, round(min(t + n, stop), 3)))
            i += 1
        # a tiny leftover tail gets merged into the previous clip if it fits,
        # otherwise dropped (TikTok rejects clips under 3 s)
        if len(clips) >= 2 and clips[-1][1] - clips[-1][0] < TIKTOK_MIN_S - EPS:
            tail = clips.pop()
            prev = clips.pop()
            if tail[1] - prev[0] <= TIKTOK_MAX_S + EPS:
                clips.append((prev[0], tail[1]))
            else:
                clips.append(prev)
    else:
        if not args.at:
            die("tell me what to clip: --at TIMESTAMP [--length N | --end T], or --every N")
        if args.end is not None and len(args.at) > 1:
            die("--end only works with a single --at; use --length for multiple clips")
        for s in args.at:
            if s >= duration - EPS:
                die(f"start {fmt_time(s)} is past the end of the video ({fmt_time(duration)})")
            if args.end is not None:
                e = args.end
            elif args.length is not None:
                e = s + args.length
            else:
                e = duration  # just "--at T": run to the end (capped below)
            if e <= s:
                die(f"end {fmt_time(e)} is not after start {fmt_time(s)}")
            if e - s > TIKTOK_MAX_S + EPS:
                print(f"note: clip at {fmt_time(s)} capped to {TIKTOK_MAX_S:g}s (TikTok max)",
                      file=sys.stderr)
            clips.append((round(s, 3), round(min(e, duration, s + TIKTOK_MAX_S), 3)))

    bad = [c for c in clips if c[1] - c[0] < TIKTOK_MIN_S - EPS]
    for c in bad:
        print(f"skipping {fmt_time(c[0])}-{fmt_time(c[1])}: shorter than {TIKTOK_MIN_S:g}s "
              "(TikTok minimum)", file=sys.stderr)
    clips = [c for c in clips if c not in bad]
    if not clips:
        die("nothing to clip")
    return clips


# ---------------------------------------------------------------- captions
def check_caption_template(tpl: str) -> None:
    try:
        fields = [f for _, f, _, _ in string.Formatter().parse(tpl) if f is not None]
    except ValueError as exc:
        die(f"bad --caption template: {exc}")
    for f in fields:
        if f not in CAPTION_FIELDS:
            die(f"bad --caption field {{{f}}}; allowed: "
                + " ".join("{%s}" % x for x in sorted(CAPTION_FIELDS)))


MAX_TAGS = 4  # #fyp + up to 3 more
ALWAYS_TAG = "fyp"


def build_hashtags(tags: list[str] | None) -> str:
    """Clean user tags, always include #fyp, cap at MAX_TAGS. Returns '#a #b #fyp'."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags or []:
        for piece in re.split(r"[\s,]+", raw):
            t = re.sub(r"[^\w]", "", piece.lstrip("#"))
            if t and t.lower() not in seen and t.lower() != ALWAYS_TAG:
                seen.add(t.lower())
                out.append(t)
    if len(out) > MAX_TAGS - 1:
        print(f"note: keeping {MAX_TAGS - 1} hashtags + #{ALWAYS_TAG} "
              f"(dropped: {' '.join('#' + t for t in out[MAX_TAGS - 1:])})", file=sys.stderr)
        out = out[:MAX_TAGS - 1]
    out.append(ALWAYS_TAG)
    return " ".join("#" + t for t in out)


def make_caption(tpl: str, title: str, hashtags: str = "", **kw) -> str:
    # hashtags are never truncated; the title shrinks to make room for them
    limit = CAPTION_MAX - (len(hashtags) + 1 if hashtags else 0)
    cap = tpl.format(title=title, **kw)
    if len(cap) > limit and "{title}" in tpl:
        room = limit - len(tpl.format(title="", **kw)) - 1
        title = (title[:max(room, 0)].rstrip() + "…") if room > 0 else ""
        cap = tpl.format(title=title, **kw)
    cap = cap[:limit].rstrip()
    return f"{cap} {hashtags}".strip() if hashtags else cap


# ---------------------------------------------------------------- rendering
TONEMAP = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
           "tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,")


def video_filter(mode: str, out_fps: float, hdr: bool) -> str:
    # fps conversion goes FIRST: after an overlay it drops the final frame
    pre = f"[0:v]{TONEMAP if hdr else ''}fps={out_fps:.6g}"
    if mode == "blur":
        f = (f"{pre},split=2[bg][fg];"
             f"[bg]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
             f"crop={OUT_W}:{OUT_H},boxblur=20:2,eq=brightness=-0.08[bg2];"
             f"[fg]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease[fg2];"
             f"[bg2][fg2]overlay=(W-w)/2:(H-h)/2")
    elif mode == "crop":
        f = (f"{pre},scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
             f"crop={OUT_W}:{OUT_H}")
    elif mode == "pad":
        f = (f"{pre},scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,"
             f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2:black")
    else:
        die(f"unknown --format {mode}")
    return f + ",setsar=1,format=yuv420p[v]"


def render(src: Path, info: dict, start: float, end: float, out: Path, mode: str) -> None:
    dur = end - start
    has_audio = info["audio_index"] is not None
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", ff_path(src)]
    if not has_audio:
        cmd += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=44100:cl=stereo"]
    cmd += ["-filter_complex", video_filter(mode, output_fps(info["fps"]), info["hdr"]),
            "-map", "[v]",
            "-map", f"0:{info['audio_index']}" if has_audio else "1:a:0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-profile:v", "high",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
            "-movflags", "+faststart", "-t", f"{dur:.3f}", ff_path(out)]
    r = run(cmd)
    if r.returncode != 0:
        die(f"ffmpeg failed on {out.name}:\n{r.stderr.strip()[-1500:]}")
    # sanity check: the file must really contain video of a TikTok-legal length
    chk = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
               "stream=duration,width,height", "-of", "json", ff_path(out)])
    st = (json.loads(chk.stdout or "{}").get("streams") or [None])[0]
    vlen = float((st or {}).get("duration") or 0)
    if not st or vlen < TIKTOK_MIN_S - 0.1:
        out.unlink(missing_ok=True)
        die(f"{out.name}: rendered clip has {'no video' if not st else f'only {vlen:.2f}s of video'}"
            " - source may be damaged at that point")


# ---------------------------------------------------------------- manifest
def load_manifest(path: Path) -> dict:
    if path.exists():
        try:
            m = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(m.get("clips"), list):
                return m
        except (json.JSONDecodeError, AttributeError):
            pass
        print(f"warning: {path} unreadable, starting a new one", file=sys.stderr)
    return {"clips": []}


# ---------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Cut a video (file or link) into TikTok-ready vertical clips.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("source", help="video file path or URL")
    p.add_argument("--at", nargs="+", type=parse_time, metavar="TIME",
                   help="clip start time(s), e.g. 1:23 or 83")
    p.add_argument("--length", type=positive_seconds, metavar="SEC",
                   help="clip length in seconds (with --at)")
    p.add_argument("--end", type=parse_time, metavar="TIME",
                   help="clip end time (with a single --at)")
    p.add_argument("--every", type=positive_seconds, metavar="SEC",
                   help="chop the whole video into back-to-back clips of SEC seconds")
    p.add_argument("--from", dest="from_", type=parse_time, metavar="TIME",
                   help="with --every: start chopping here")
    p.add_argument("--to", type=parse_time, metavar="TIME",
                   help="with --every: stop chopping here")
    p.add_argument("--format", choices=["blur", "crop", "pad"], default="blur",
                   help="how to make it vertical: blur (default, blurred background), "
                        "crop (fill screen, cut sides), pad (black bars)")
    p.add_argument("--caption", default="{title} - part {n}/{total}",
                   help="caption template; fields: {title} {n} {total} {start} {end}")
    p.add_argument("--tags", nargs="+", metavar="TAG",
                   help="up to 3 hashtags added to every caption (#fyp is always added), "
                        "e.g. --tags trading nasdaq daytrader")
    p.add_argument("--title", help="title used in captions (default: file name)")
    p.add_argument("-o", "--out", default="clips", help="output folder (default: clips)")
    p.add_argument("--dry-run", action="store_true", help="just print the plan")
    args = p.parse_args(argv)

    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            die(f"{tool} is not installed")
    if args.every is not None and args.at:
        die("use either --at or --every, not both")
    if args.length is not None and args.end is not None:
        die("use either --length or --end, not both")
    if (args.from_ is not None or args.to is not None) and args.every is None:
        die("--from/--to only apply to --every (use --at/--end for single clips)")
    if args.end is not None and args.at and len(args.at) == 1 and args.end <= args.at[0]:
        die(f"end {fmt_time(args.end)} is not after start {fmt_time(args.at[0])}")
    if args.every is not None and args.to is not None and args.to <= (args.from_ or 0.0):
        die(f"--to {fmt_time(args.to)} must be after --from {fmt_time(args.from_ or 0.0)}")
    check_caption_template(args.caption)

    out_dir = Path(args.out)
    offset = 0.0  # where the downloaded file starts in the original video
    if re.match(r"^https?://", args.source):
        section = download_section(args)
        src = download(args.source, out_dir / "_downloads", section)
        if section:
            offset = section[0]
            if args.at:
                args.at = [a - offset for a in args.at]
            if args.end is not None:
                args.end -= offset
            if args.from_ is not None:
                args.from_ -= offset
            if args.to is not None:
                args.to -= offset
    else:
        src = Path(args.source).expanduser()
        if not src.is_file():
            die(f"file not found: {src}")

    info = probe(src)
    if info["duration"] <= 0:
        die("couldn't determine the video length" +
            (f" - is {fmt_time(offset)} past the end of the video?" if offset else ""))
    clips = plan_clips(args, info["duration"])
    title = args.title or re.sub(r"\s*\[[^\]]+\].*$", "", src.stem) or src.stem
    total = len(clips)

    print(f"source: {src.name}  ({fmt_time(info['duration'])}, "
          f"{info['width']}x{info['height']}, {info['fps']:.2f}fps"
          f"{', HDR->SDR' if info['hdr'] else ''})")
    print(f"making {total} clip(s) -> {out_dir}/  [format: {args.format}, "
          f"{output_fps(info['fps']):.3g}fps]")

    stem = re.sub(r"[^\w\-]+", "_", title).strip("_")[:50] or "clip"
    hashtags = build_hashtags(args.tags)
    planned = []
    for i, (s, e) in enumerate(clips, 1):
        # names, captions and printed times use the ORIGINAL video's timeline
        os_, oe = s + offset, e + offset
        name = f"{stem}_{i:03d}_{fmt_time(os_).replace(':', '-')}.mp4"
        caption = make_caption(args.caption, title, hashtags, n=i, total=total,
                               start=fmt_time(os_), end=fmt_time(oe))
        print(f"  [{i}/{total}] {fmt_time(os_)} -> {fmt_time(oe)}  ({e - s:.1f}s)  {name}")
        print(f"         caption: {caption}")
        planned.append((i, s, e, name, caption))
    if args.dry_run:
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    mpath = out_dir / "manifest.json"
    manifest = load_manifest(mpath)
    by_file = {c.get("file"): c for c in manifest["clips"]}
    for i, s, e, name, caption in planned:
        render(src, info, s, e, out_dir / name, args.format)
        prev = by_file.get(name, {})
        entry = {"n": i, "file": name, "path": str((out_dir / name).resolve()),
                 "source": str(src.resolve()), "title": title, "format": args.format,
                 "start": round(s + offset, 3), "end": round(e + offset, 3),
                 "seconds": round(e - s, 3), "caption": caption,
                 "posted": bool(prev.get("posted", False))}
        if name in by_file:
            manifest["clips"][manifest["clips"].index(prev)] = entry
        else:
            manifest["clips"].append(entry)
        by_file[name] = entry
        mpath.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"done. manifest: {mpath}")
    return 0


# ---------------------------------------------------------------- suggest
def fetch_info_and_captions(url: str, workdir: Path) -> tuple[dict, Path | None]:
    """Metadata (incl. YouTube 'most replayed' heatmap) + captions, without the video."""
    workdir.mkdir(parents=True, exist_ok=True)
    for old in workdir.glob("*"):
        if old.is_file():
            old.unlink()
    cmd = ytdlp_cmd() + ["--no-playlist", "--encoding", "utf-8", "--skip-download",
                         "--ignore-errors", "--write-info-json", "--write-subs",
                         "--write-auto-subs", "--sub-langs", "en.*,en", "--sub-format",
                         "json3/vtt", "-o", str(workdir / "video.%(ext)s")]
    if shutil.which("node"):
        cmd += ["--js-runtimes", "node"]
    print(f"reading {url} ...", file=sys.stderr)
    r = subprocess.run(cmd + [url], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    info_path = workdir / "video.info.json"
    if not info_path.exists():
        die(f"couldn't read the video info:\n{r.stderr.strip()[-1500:]}")
    info = json.loads(info_path.read_text(encoding="utf-8"))
    subs = [p for p in workdir.glob("video.*") if p.suffix in (".json3", ".vtt")]
    manual = set((info.get("subtitles") or {}).keys())  # human-made tracks

    def rank(p: Path) -> tuple:
        lang = p.name[len("video."):-len(p.suffix)]
        return (lang not in manual,          # human captions first
                lang != "en",                # then plain English auto captions
                lang != "en-orig",
                p.suffix != ".json3", p.name)
    subs.sort(key=rank)
    return info, (subs[0] if subs else None)


def parse_vtt(path: Path) -> list[tuple[float, str]]:
    """WebVTT captions -> [(start_seconds, text)]."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for block in re.split(r"\n\s*\n", raw.replace("\r", "")):
        lines = block.strip().split("\n")
        for i, line in enumerate(lines):
            m = re.match(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})\s+-->", line)
            if m:
                h, mi, s, ms = (int(g) if g else 0 for g in m.groups())
                text = " ".join(re.sub(r"<[^>]+>", "", t) for t in lines[i + 1:]).strip()
                if text:
                    out.append((h * 3600 + mi * 60 + s + ms / 1000, re.sub(r"\s+", " ", text)))
                break
    return out


def parse_captions(path: Path | None) -> list[tuple[float, str]]:
    if path is None:
        return []
    return parse_vtt(path) if path.suffix == ".vtt" else parse_json3(path)


def parse_json3(path: Path) -> list[tuple[float, str]]:
    """YouTube json3 captions -> [(start_seconds, text)]."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for ev in data.get("events", []):
        segs = ev.get("segs")
        if not segs:
            continue
        text = "".join(s.get("utf8", "") for s in segs).replace("\n", " ").strip()
        if text:
            out.append((ev.get("tStartMs", 0) / 1000.0, re.sub(r"\s+", " ", text)))
    return out


def transcript_blocks(lines: list[tuple[float, str]], block_s: float = 15.0) -> list[tuple[float, str]]:
    """Merge caption lines into ~15 s blocks so a long video stays readable."""
    blocks: list[tuple[float, str]] = []
    cur_start, cur = None, []
    for t, text in lines:
        if cur_start is None:
            cur_start = t
        if t - cur_start >= block_s and cur:
            blocks.append((cur_start, " ".join(cur)))
            cur_start, cur = t, []
        if not cur or cur[-1] != text:  # auto captions repeat lines
            cur.append(text)
    if cur:
        blocks.append((cur_start, " ".join(cur)))
    return blocks


def text_between(lines: list[tuple[float, str]], s: float, e: float, limit: int = 280) -> str:
    words = " ".join(t for ts, t in lines if s <= ts < e)
    return words if len(words) <= limit else words[:limit].rsplit(" ", 1)[0] + " …"


def heat_per_second(heatmap: list[dict], duration: float) -> list[float]:
    n = int(math.ceil(duration))
    heat = [0.0] * max(n, 1)
    for seg in heatmap or []:
        try:
            a, b, v = float(seg["start_time"]), float(seg["end_time"]), float(seg["value"])
        except (KeyError, TypeError, ValueError):
            continue
        for i in range(max(0, int(a)), min(n, int(math.ceil(b)))):
            heat[i] = max(heat[i], v)
    return heat


def pick_moments(heat: list[float], duration: float, count: int, length: float,
                 skip_intro: float) -> list[tuple[float, float, float]]:
    """Greedy: the `count` non-overlapping windows with the highest average replay heat."""
    L = max(1, int(round(length)))
    n = len(heat)
    if n <= L:
        return [(0.0, duration, sum(heat) / max(n, 1))]
    prefix = [0.0]
    for h in heat:
        prefix.append(prefix[-1] + h)
    scores = [((prefix[s + L] - prefix[s]) / L, s) for s in range(int(skip_intro), n - L + 1)]
    if not scores:
        return []
    ordered = sorted(heat[int(skip_intro):])
    median, peak = ordered[len(ordered) // 2], ordered[-1]
    if peak - median < 0.1:
        return []  # flat replay graph: nothing stands out
    # a window must stand out from the OTHER windows (judging against single seconds
    # would average away a short, sharp spike)
    wscores = sorted(sc for sc, _ in scores)
    w_median, w_peak = wscores[len(wscores) // 2], wscores[-1]
    floor = w_median + 0.25 * (w_peak - w_median)

    def same_hill(a: int, b: int, score_a: float, score_b: float) -> bool:
        """True if the replay graph never dips between two windows (one peak, not two)."""
        def top(s: int) -> int:  # where the window's replay graph is highest
            seg = heat[s:s + L]
            return s + max(range(len(seg)), key=seg.__getitem__)
        lo, hi = sorted((top(a), top(b)))
        dip = min(heat[lo:hi + 1]) if hi > lo else peak
        return dip >= floor and dip >= 0.7 * min(score_a, score_b)

    scores.sort(reverse=True)
    chosen: list[tuple[float, float, float]] = []
    for score, s in scores:
        if score <= 0 or score < floor or len(chosen) >= count:
            break
        # one pick per replay peak
        if all(abs(s - a) >= 1.5 * L and not same_hill(s, int(a), score, sa)
               for a, _, sa in chosen):
            chosen.append((float(s), float(min(s + L, duration)), score))
    return sorted(chosen, key=lambda c: -c[2])


def snap_to_captions(s: float, e: float, lines: list[tuple[float, str]],
                     min_len: float, max_len: float, duration: float) -> tuple[float, float]:
    """Start on a caption boundary just before s, end on one just after e."""
    starts = [t for t, _ in lines]
    if starts:
        before = [t for t in starts if t <= s + 0.5]
        if before and s - before[-1] <= 4:
            s = before[-1]
        after = [t for t in starts if t >= e - 0.5]
        if after and after[0] - e <= 4:
            e = after[0]
    e = min(max(e, s + min_len), s + max_len, duration)
    return round(max(s, 0.0), 1), round(e, 1)


def suggest_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="tikclip.py suggest",
        description="Find the best moments of a video to clip: YouTube's 'most replayed' "
                    "peaks plus a timestamped transcript.")
    p.add_argument("url")
    p.add_argument("--count", type=int, default=5, help="how many moments to suggest (default 5)")
    p.add_argument("--length", type=positive_seconds, default=45,
                   help="target clip length in seconds (default 45)")
    p.add_argument("--min", dest="min_len", type=positive_seconds, default=15)
    p.add_argument("--max", dest="max_len", type=positive_seconds, default=90)
    p.add_argument("-o", "--out", default="clips", help="output folder (default: clips)")
    args = p.parse_args(argv)
    if args.count < 1:
        die("--count must be at least 1")
    if not args.min_len <= args.length <= args.max_len:
        die("--length must be between --min and --max")
    if not re.match(r"^https?://", args.url):
        die("suggest needs a video link")

    work = Path(args.out) / "_suggest"
    info, subs_path = fetch_info_and_captions(args.url, work)
    duration = float(info.get("duration") or 0)
    if duration <= 0:
        die("couldn't determine the video length")
    lines = parse_captions(subs_path)
    heatmap = info.get("heatmap") or []

    # the start of a video always looks "replayed" (everyone watches it): skip it
    first_seg_end = min((float(h.get("end_time", 0)) for h in heatmap), default=0.0)
    skip_intro = max(first_seg_end, min(30.0, 0.1 * duration))
    moments = []
    if heatmap:
        for s, e, score in pick_moments(heat_per_second(heatmap, duration), duration,
                                        args.count, args.length, skip_intro):
            s, e = snap_to_captions(s, e, lines, args.min_len, args.max_len, duration)
            if e - s >= TIKTOK_MIN_S:
                moments.append({"start": s, "end": e, "start_hms": fmt_time(s),
                                "end_hms": fmt_time(e), "seconds": round(e - s, 1),
                                "replay_score": round(score, 3),
                                "says": text_between(lines, s, e)})

    blocks = transcript_blocks(lines)
    tpath = Path(args.out) / "transcript.txt"
    tpath.parent.mkdir(parents=True, exist_ok=True)
    tpath.write_text("\n".join(f"[{fmt_time(t)}] {txt}" for t, txt in blocks) + "\n",
                     encoding="utf-8")
    chapters = [{"start_hms": fmt_time(float(c.get("start_time", 0))), "title": c.get("title", "")}
                for c in info.get("chapters") or []]
    result = {"url": info.get("webpage_url") or args.url, "title": info.get("title"),
              "channel": info.get("channel") or info.get("uploader"),
              "duration": duration, "duration_hms": fmt_time(duration),
              "has_replay_data": bool(heatmap), "has_transcript": bool(lines),
              "moments": moments, "chapters": chapters, "transcript_file": str(tpath.resolve())}
    (Path(args.out) / "suggest.json").write_text(json.dumps(result, indent=2, ensure_ascii=False),
                                                 encoding="utf-8")

    print(f"title:    {result['title']}")
    print(f"channel:  {result['channel']}")
    print(f"length:   {result['duration_hms']}")
    if moments:
        print("\nmost replayed moments (best first):")
        for i, m in enumerate(moments, 1):
            print(f"  {i}. {m['start_hms']} -> {m['end_hms']}  ({m['seconds']:.0f}s, "
                  f"replay {m['replay_score']:.2f})")
            if m["says"]:
                print(f"     \"{m['says']}\"")
    elif heatmap:
        print("\nreplay data shows no stand-out moments - pick moments from the transcript")
    else:
        print("\nno 'most replayed' data for this video - pick moments from the transcript")
    if chapters:
        print("\nchapters:")
        for c in chapters:
            print(f"  {c['start_hms']}  {c['title']}")
    print(f"\ntranscript: {tpath.resolve()}" if lines else "\nno captions available")
    print(f"details:    {(Path(args.out) / 'suggest.json').resolve()}")
    return 0


# ---------------------------------------------------------------- put
def put_main(argv: list[str]) -> int:
    """Upload a file to a presigned URL (e.g. from HiggsField media_upload)."""
    import urllib.error
    import urllib.request
    p = argparse.ArgumentParser(prog="tikclip.py put",
                                description="Upload a clip to a presigned upload URL.")
    p.add_argument("file")
    p.add_argument("url", help="the upload_url (put it in quotes)")
    p.add_argument("--content-type", default="video/mp4")
    args = p.parse_args(argv)
    path = Path(args.file).expanduser()
    if not path.is_file():
        die(f"file not found: {path}")
    size = path.stat().st_size
    with path.open("rb") as fh:
        req = urllib.request.Request(args.url, data=fh, method="PUT", headers={
            "Content-Type": args.content_type, "Content-Length": str(size),
            "If-None-Match": "*"})
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                print(f"HTTP {resp.status} - uploaded {path.name} ({size / 1e6:.1f} MB)")
                return 0
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:500]
            die(f"upload failed: HTTP {exc.code}\n{body}")
        except urllib.error.URLError as exc:
            die(f"upload failed: {exc.reason}")
    return 1


def safe_output() -> None:
    """UTF-8 output that never crashes. On Windows a piped stdout (Git Bash, Desktop
    Commander) defaults to the ANSI code page and dies on emoji in video titles."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


if __name__ == "__main__":
    safe_output()
    if len(sys.argv) > 1 and sys.argv[1] == "suggest":
        sys.exit(suggest_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "put":
        sys.exit(put_main(sys.argv[2:]))
    sys.exit(main())
