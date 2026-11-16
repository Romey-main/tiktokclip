---
name: tiktok-clipper
description: Turn a video link (YouTube, Twitch, X...) into TikTok clips and post them. Use whenever the user pastes a video link and wants it clipped, asks for clip ideas or the best moments of a video, gives a time range to clip, or asks to post a clip to TikTok. Cuts the clip on the user's PC with tikclip.py, writes a matching title plus hashtags, uploads it through HiggsField and opens the TikTok post form.
---

# TikTok clipper

The user pastes a link. Find the best moments or use the time range they gave. Cut the clip on their PC, write a title and hashtags, upload it, and open the TikTok post form. The user's only manual step is tapping **Post** in the form, because TikTok requires it.

## Tools this needs

- **Desktop Commander** (`start_process`, `read_file`): runs commands on the user's Windows PC. YouTube blocks cloud servers, so downloads must run on the PC.
- **HiggsField connector**: `media_upload`, `media_confirm`, `tiktok_accounts`, `tiktok_connect`, `tiktok_reconnect`, `tiktok_prepare_publish`, `tiktok_publish_status`.
- **tikclip.py**: the clipper, at `%USERPROFILE%\tiktokclip\tikclip.py`. Source: https://github.com/Romey-main/tiktokclip

If Desktop Commander or HiggsField is missing, say which one and stop. Don't try to work around it.

## User defaults (use unless they say otherwise)

- Post straight to the profile: `mode: DIRECT_POST`.
- Public: `privacy_level: PUBLIC_TO_EVERYONE`.
- Caption: a short title that matches what is said in the clip, then 3 relevant hashtags, then `#fyp`. Example: `Dana White on taking risks #CEO #UFC #Dana #fyp`. The caption must be 150 characters or less (tikclip enforces this).
- If the user names hashtags, use theirs. #fyp is always added. tikclip keeps at most 3 hashtags plus #fyp, so if the user names more than 3, ask which 3 to keep.

## Running commands on the PC

- Desktop Commander runs commands in **PowerShell** on Windows, and the commands below are written for PowerShell. If a command runs in `cmd` instead, swap `$HOME` for `%USERPROFILE%` and use double quotes.
- Use `python` (not `python3`). If that's not found, use `py`.
- Always use absolute paths. Output folder: `%USERPROFILE%\tiktok-clips` (in PowerShell: `$HOME\tiktok-clips`).
- Before the first command of a session, update the tool: `git -C "$HOME\tiktokclip" pull`. If the folder doesn't exist, run `git clone https://github.com/Romey-main/tiktokclip "$HOME\tiktokclip"`.
- Downloads can take a minute or two. Wait for the process to finish and read its full output.
- **Quote any text you write into a command in single quotes**: titles, captions and URLs. Inside double quotes PowerShell expands `$` (so "How he made $100" turns into "How he made "), and straight `"` characters break arguments. Write an apostrophe inside single quotes as two apostrophes: `--title 'Dana''s $100 bet'`. Don't use straight double quotes in titles; use “curly quotes” instead.
- After cutting, compare the `caption:` line tikclip prints with the caption you intended. If they differ, fix the command and cut again before uploading.

## Workflow

### 1. The user pastes a link

Always start by reading the video. This doesn't download it, it only fetches the info and captions, so it's quick:

```
python "$HOME\tiktokclip\tikclip.py" suggest '<URL>' -o "$HOME\tiktok-clips"
```

This prints the title, length, YouTube's "most replayed" peaks with what is said in each, and the chapters. It also writes `transcript.txt`, a timestamped transcript, in the output folder. Read it with `read_file`.

`transcript.txt` has one timestamp roughly every 15 seconds. When you need an exact sentence start, read the caption file in `$HOME\tiktok-clips\_suggest\` (`video.<lang>.json3`: `events[].tStartMs` plus `segs[].utf8`, or a `.vtt`). Otherwise start a second or two early so the first word isn't cut off.

**If the user gave a time range** (e.g. "1:10:50 to 1:12:28"), read the transcript lines inside that range so the title matches what's said. If there are no captions, ask what the clip is about. Then go to step 3.

**Otherwise,** use the peaks and the transcript to offer options (step 2). For long videos, read the parts around each peak, or skim the whole thing if there are no peaks.

### 2. Offer 3-5 options

Use the replay peaks and the transcript together. The peaks show what viewers rewatch, and the transcript shows what makes a good standalone clip. For each option:

- **Time range:** start at the beginning of a sentence or the hook, and end on a complete thought. Aim for 20-90 seconds. Use the transcript timestamps to adjust the peak's range.
- **Title:** a short hook (about 60 characters or less) that matches what is actually said. No clickbait that misrepresents the clip.
- **Hashtags:** 3 that fit the topic and the people in it, plus `#fyp`.
- **Why it works:** one line, e.g. "most replayed part of the video" or "strong quote".

Present them like this:

```
1. 1:11:05 -> 1:12:02 (57s) - most replayed
   "Dana White on why he bet everything on the UFC"
   #UFC #DanaWhite #entrepreneur #fyp
   Says: "...nobody believed in it, I put every dollar I had..."
```

Ask which one(s) to post. The user can pick several, change a title, or give their own time range.

### 3. Cut the clip

For one clip:

```
python "$HOME\tiktokclip\tikclip.py" '<URL>' --at <START> --end <END> --title '<TITLE>' --tags <TAG1> <TAG2> <TAG3> --caption '{title}' -o "$HOME\tiktok-clips"
```

- Pass tags without `#`. #fyp is added automatically.
- `--caption '{title}'` leaves out "part 1/1" for a single clip. Leave `--caption` off for a series (`--every N`), so each one gets "part 2/5" and so on.
- For several picks, run the command once per pick. Each one downloads only its own section.
- The output prints the clip's file name and its `caption:` line. `manifest.json` in the output folder has `path` and `caption` for every clip.

### 4. Upload it to HiggsField

1. Call `media_upload` with `filename` set to the clip's file name and `content_type: "video/mp4"`. Keep the `upload_url`, `media_id` and `url` it returns.
2. Upload from the PC. The URL contains `&` and `%`, so put it in **single quotes** in PowerShell:
   ```
   python "$HOME\tiktokclip\tikclip.py" put "<CLIP PATH>" '<UPLOAD_URL>'
   ```
   It must print `HTTP 200`. If it fails with `HTTP 412`, the file is already uploaded (an earlier try worked), so go on to `media_confirm`.
3. Call `media_confirm` with `media_id` and `type: "video"`. Use the confirmed `url` in the next step.

### 5. Open the TikTok post form

1. Call `tiktok_accounts` and use the `connector_id` of the account whose status is `active`.
   - If an account has status `error`, call `tiktok_reconnect` and give the user the link.
   - If there are no accounts, call `tiktok_connect` and give the user the link.
   - The link expires in about 10 minutes. Once the user has approved it, call `tiktok_accounts` again and check the account is `active`.
2. Call `tiktok_prepare_publish` with:
   - `connector_id`
   - `mode: "DIRECT_POST"`, `media_type: "VIDEO"`
   - `video_url`: the confirmed HiggsField `url`
   - `title`: the clip's caption, exactly as tikclip printed it
   - `privacy_level: "PUBLIC_TO_EVERYONE"`
3. The form appears in the chat. Tell the user to fill in:
   - Comments, Duet and Stitch: on or off, their choice.
   - Commercial content: **No**, unless it's an ad or paid partnership.
   - AI-generated: **No**, since this is real footage.
   - Music: optional.
   - Then tap **Post**.

Never call `tiktok_publish` yourself, and don't treat a chat reply as consent. Only the form can post.

For several clips, open one form per clip. Suggest spacing the posts out, because many near-identical posts at once can get flagged as spam.

### 6. After posting

If the form returns a `publish_id`, check `tiktok_publish_status` once or twice. TikTok can take a few minutes to process a post. Tell the user when it's live.

## Problems and fixes

| Symptom | Fix |
|---|---|
| `ffmpeg is not installed` | Run `winget install Gyan.FFmpeg`, then fully restart the Claude app so it sees the new PATH. |
| `python` not found | Try `py`. If that fails too, install Python from python.org with "Add to PATH" ticked. |
| `download failed`, "Sign in to confirm you're not a bot", "Requested format is not available" | YouTube changes often. Update yt-dlp with `python -m pip install -U yt-dlp` and try again. |
| `suggest` shows no replay data | That's normal for newer or less-watched videos. Pick moments from `transcript.txt`. |
| No captions | Ask the user for a time range, or use the chapters. |
| `put` fails with HTTP 403 | The upload link expired or the URL got mangled. Call `media_upload` again for a new URL, and keep it in single quotes. |
| No post form shows up | This app can't display connector forms. Use the Claude desktop app or claude.ai chat, or have the user post the clip manually from tiktok.com/upload with the printed caption. |

## Be upfront about copyright

If the video clearly isn't the user's own content (e.g. a podcast, a show, a sports broadcast), mention once per video that a public repost can be muted or taken down for copyright. Then do what they ask.
