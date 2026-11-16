# TikTok clipper

Cuts a video (a file, or a link) into vertical 1080x1920 clips that meet TikTok's upload rules, then posts them through your connected TikTok account.

## How to use it with Claude

Send a video (upload it, or paste a link) and say what you want:

| You say | What happens |
|---|---|
| "clip 1:23, 30 seconds" | one 30 s clip starting at 1:23 |
| "clip 4:10 to 4:45" | one clip between those times |
| "clips at 0:45, 3:10, 12:02, 20 s each" | three clips |
| "chop the whole thing into 30 s clips" | part 1, part 2, ... back to back |
| "only 10:00 to 20:00, every 45 s" | chops just that section |

Claude cuts the clips, uploads each one, and opens a TikTok publish form for every clip. You check the caption and privacy settings and press **Post**. TikTok requires that approval for each post, so posting can't run completely unattended.

## Running it yourself

Needs Python 3.9+, ffmpeg, and `pip install yt-dlp` if you want to use links. On Windows, type `python` instead of `python3`. If ffmpeg is missing, run `winget install Gyan.FFmpeg` and then open a new terminal.

With a link, only the part you're clipping gets downloaded, not the whole video.

```bash
python3 tikclip.py video.mp4 --at 1:23 --length 30
python3 tikclip.py video.mp4 --at 1:23 --end 2:05
python3 tikclip.py video.mp4 --at 0:45 3:10 12:02 --length 20
python3 tikclip.py video.mp4 --every 30
python3 tikclip.py video.mp4 --every 45 --from 10:00 --to 20:00
python3 tikclip.py "https://youtube.com/watch?v=..." --every 30
```

Options:

- `--format blur` (default): shows the full frame over a blurred copy of itself. Good for gameplay, charts and screen recordings.
- `--format crop`: fills the screen and cuts off the sides. Good when the action is in the middle.
- `--format pad`: shows the full frame with black bars.
- `--tags trading nasdaq daytrader`: up to 3 hashtags added to every caption. `#fyp` is always added, so the default caption looks like `My stream - part 1/5 #trading #nasdaq #daytrader #fyp`. If a long title would push the caption past 150 characters, the title gets shortened and the hashtags are kept.
- `--caption "{title} pt {n}"`: caption template. The fields are `{title} {n} {total} {start} {end}`.
- `--title "My stream"`: title used in captions. Defaults to the file name.
- `-o folder`: where the clips go. Defaults to `clips/`.
- `--dry-run`: shows the plan without rendering anything.

Timestamps can be written as `90`, `1:30`, `1:02:03` or `1m30s`.

When you use `--every`, a leftover tail under 3 s gets merged into the previous clip, because TikTok rejects clips under 3 s. Each clip is capped at 600 s.

Each run writes `manifest.json` with every clip's file, times, caption and posted status.

## Heads up

- Only post content you own or have permission to use. Clipping someone else's videos can get them taken down and get your account struck.
- Posting a lot of near-identical clips in a short time can get flagged as spam. Spacing them out works better.
