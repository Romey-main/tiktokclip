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

Every clip is exported as a TikTok-compliant MP4 (H.264/AAC, 1080x1920,
24-60 fps, 3-600 s). A manifest.json is kept in the output folder with a
suggested caption and a "posted" flag per clip.
"""
from __future__ import annotations

import argparse
import json
import math
import re
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
    return subprocess.run(cmd, capture_output=True, text=True)


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


def download(url: str, workdir: Path) -> Path:
    if shutil.which("yt-dlp") is None:
        die("yt-dlp is not installed (pip install yt-dlp)")
    workdir.mkdir(parents=True, exist_ok=True)
    tmpl = str(workdir / "%(title).80B [%(id)s].%(ext)s")
    print(f"downloading {url} ...")
    r = subprocess.run(["yt-dlp", "--no-playlist", "-f",
                        "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b",
                        "--merge-output-format", "mp4", "-o", tmpl,
                        "--print", "after_move:filepath", url],
                       capture_output=True, text=True)
    if r.returncode != 0:
        die(f"download failed:\n{r.stderr.strip()[-1500:]}")
    path = Path(r.stdout.strip().splitlines()[-1])
    if not path.exists():
        die(f"download finished but file not found: {path}")
    return path


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
            m = json.loads(path.read_text())
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
    check_caption_template(args.caption)

    out_dir = Path(args.out)
    if re.match(r"^https?://", args.source):
        src = download(args.source, out_dir / "_downloads")
    else:
        src = Path(args.source).expanduser()
        if not src.is_file():
            die(f"file not found: {src}")

    info = probe(src)
    if info["duration"] <= 0:
        die("couldn't determine the video length")
    clips = plan_clips(args, info["duration"])
    title = args.title or re.sub(r"\s*\[[^\]]+\]$", "", src.stem)
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
        name = f"{stem}_{i:03d}_{fmt_time(s).replace(':', '-')}.mp4"
        caption = make_caption(args.caption, title, hashtags, n=i, total=total,
                               start=fmt_time(s), end=fmt_time(e))
        print(f"  [{i}/{total}] {fmt_time(s)} -> {fmt_time(e)}  ({e - s:.1f}s)  {name}")
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
                 "start": s, "end": e, "seconds": round(e - s, 3), "caption": caption,
                 "posted": bool(prev.get("posted", False))}
        if name in by_file:
            manifest["clips"][manifest["clips"].index(prev)] = entry
        else:
            manifest["clips"].append(entry)
        by_file[name] = entry
        mpath.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    print(f"done. manifest: {mpath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
