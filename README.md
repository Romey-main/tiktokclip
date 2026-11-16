# TikTok clipper

Cuts a video (a file, or a link) into vertical 1080x1920 clips that meet TikTok's upload rules, then posts them through your connected TikTok account.

## Easiest way: paste a link in the Claude desktop app

Once this is set up, you paste a link and Claude suggests the best moments. You pick one (or give your own time range), and Claude cuts it on your PC, writes a title and hashtags, and opens the TikTok post form. You tap **Post**.

### One-time setup (Windows)

1. **Install the Claude desktop app** from [claude.ai/download](https://claude.ai/download) and sign in.
2. **Install Python, ffmpeg and yt-dlp.** In a terminal, run each line:
   ```
   winget install Python.Python.3.12
   winget install Gyan.FFmpeg
   python -m pip install -U yt-dlp
   ```
3. **Get this tool:**
   ```
   git clone https://github.com/Romey-main/tiktokclip "$HOME\tiktokclip"
   ```
4. **Turn on Desktop Commander** in the Claude desktop app's extensions or connectors settings. It lets Claude run commands on your PC, which it needs because YouTube blocks downloads from cloud servers.
5. **Turn on the HiggsField connector**, with your TikTok account connected through it.
6. **Add the skill.** Zip the `skill/tiktok-clipper` folder, or use the `tiktok-clipper-skill.zip` you were sent. Then upload it in the Claude app's settings, in the Skills section under Capabilities.
7. **Restart the Claude app** so it picks up ffmpeg and Python.

### Then each time

Paste a link into a chat in the Claude desktop app, for example:

- `https://youtube.com/watch?v=...` gives you 3-5 clip options to choose from.
- `https://youtube.com/watch?v=... clip 1:10:50 to 1:12:28` cuts that exact range.

Pick an option, then fill in the post form that appears and tap **Post**.

Some things are out of the tool's hands. TikTok requires you to tap Post on every post, so it can't be fully hands-off. If YouTube downloads start failing, run `python -m pip install -U yt-dlp`, because YouTube changes often.

## Finding the best moments

```bash
python tikclip.py suggest "https://youtube.com/watch?v=..."
```

This prints YouTube's "most replayed" peaks, best first, with what's said in each. It also writes a timestamped `transcript.txt` and `suggest.json`. Options: `--count 5`, `--length 45` (target seconds), `--min 15`, `--max 90`.

## Using it with Claude in other places

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
