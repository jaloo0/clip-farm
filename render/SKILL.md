---
name: clip-farm-render
description: The GitHub Actions half of Clip Farm. Turns landscape stream clips (an mp4 plus a job.json with trim plan and word timings) into 1080x1920 vertical clips - detects the streamer's facecam, aligns landscape to vertical (facecam on top and gameplay below, full-screen camera, or gameplay over a blurred background), cuts dead air, adds hook title, word-by-word captions, streamer credit, normalizes loudness, self-checks and writes post text. Use when asked to render, edit, reframe or fix vertical clips, change the layout or facecam crop, debug the render workflow, or tune captions.
---

# Clip Farm, render half

Runs on a GitHub Actions Linux runner (CPU only). Input from the local half (`cf ship`): `<name>.mp4` (landscape, with a
few seconds of padding) and `<name>.job.json`. Output per clip: `<name>.vertical.mp4`, `.post.txt`, `.cover.jpg`,
`.facecam.jpg` (debug), plus `render-report.json`. Nothing here downloads from Twitch/YouTube or transcribes: word timings
arrive in the job.

## Run it

- Normally `cf ship <batch>` starts it. By hand: `gh workflow run render.yml -f tag=clips-<batch> [-f layout=stack] [-f only=c02]`
- Watch: `gh run watch` · failures: `gh run view --log-failed`
- Locally: put an mp4 and its job.json in `in/`, then `python render_cli.py in out [--layout stack] [--only c02]`
  (needs ffmpeg, `pip install -r requirements.txt`, and `models/face_detection_yunet_2023mar.onnx` from the opencv_zoo repo;
  without it detection finds no faces and the clip falls back to gameplay layout).

## How landscape becomes vertical

Canvas is always **1080x1920**. Each *scene* gets one of three layouts.

1. **stack** (webcam box over the game): webcam cropped into the top panel **1080x760**, gameplay fills the bottom
   panel **1080x1160**. Captions straddle the seam.
2. **fullcam** (big centered face = camera full screen): crop a 9:16 column (`H*1080/1920` wide, full height) centered on
   the face, scale to 1080x1920. Hook high (y=250), captions at y=1180.
3. **gameplay** (no face): full landscape frame scaled to 1080 wide, centered vertically, over a blurred (sigma 40),
   darkened copy of itself filling 1920 tall. Never stretched.

How the scene is decided (`render/scenes.py`, `render/face.py`):

- Every 0.5 s a frame (shrunk to 640 px) goes through the YuNet face detector.
- **Big face** (≥ 20 % of frame height or > 3.5 % of area): centered → fullcam; low/side → big webcam → stack.
- **Small face that stays put** (same place in ≥ 20 % of samples, jitter < 3 % of width) = webcam overlay → stack. A frame
  with no face inside a facecam stream (looking away) keeps the box.
- **Webcam box**: starts at the face, then snaps to the overlay's real border: edges present in *every* sampled frame
  are overlay borders (game edges move and average out). Fewer than 4 edges found → proportional fallback box and the QC
  says "facecam box guessed".
- States are smoothed (±2 samples); scenes under 1.6 s merge into neighbours. A clip can switch layout mid-clip and the
  captions move with it.
- **Game crop in stack**: width `H*1080/1160`, from the centre, shifted ±12 % to avoid the webcam; if still overlapping
  and the webcam sits low, use the strip above it.
- Face stays centered horizontally in the webcam panel, upper part if cropped vertically.

Editing, in one ffmpeg pass:

- `keep` ranges (dead air planned locally) trimmed and concatenated with 12 ms audio fades.
- Captions: 3 words per card, ALL CAPS, yellow current word, black outline, drawn as PNGs by Pillow. Font from `$CF_FONT`,
  then `fonts/*.ttf`, then system fonts. Drop Anton or Montserrat Black into `fonts/` for the classic look.
- Hook title for the first 3.2 s; credit tag "<streamer> on <platform>" top-left unless `credit` is false.
- Audio to **-14 LUFS**, limiter 0.89, second sound-only pass if off by > 0.8 LU.
- Encoder `libx264 -preset medium -crf 18`, ≤ 20 Mbit/s, ≤ 60 fps, AAC 192k, `+faststart`.

## The job file (contract with the local half)

| Field | Meaning |
|---|---|
| `video` | landscape mp4 next to the job |
| `keep` | `[[start,end],…]` seconds inside the mp4 that survive |
| `words` | `[{w,s,e}]` word timings, seconds inside the mp4 |
| `hook`, `caption`, `hashtags`, `streamer`, `platform`, `credit` | text and credit |
| `peak` | seconds inside the mp4 of the payoff (cover frame) |
| `source_url`, `source_t0` | source link in the post text |
| `layout` | optional: `stack` / `fullcam` / `gameplay` |
| `cam`, `face` | optional boxes `[x,y,w,h]` in source pixels; skips detection |

## Fixing a clip that looks wrong

1. Open `<name>.facecam.jpg` (artifact or release): yellow = face, green = webcam box, orange = big face.
2. Smallest fix:
   - Box wrong or cuts the head → set `cam` (+ `face`) in the job JSON, re-run with `only=<id>`.
   - Wrong layout → `layout` in the job, or `-f layout=stack`.
   - NPC faces in the game mistaken for a webcam → `cam` pins it.
   - Webcam missed → lower the detector threshold in `face._detector` (0.65 → 0.5) or set `cam`.
   - Captions too high/low → `CAP_Y`, `HOOK_Y` in `render/render.py`.
   - Panels too tight → `face.CAM_H` / `face.GAME_H` (must add to 1920).
3. Read `render-report.json` first: layout, why, loudness, issues per clip.

## Limits

- About a minute per clip on a 4-core runner. Private repos have a monthly free-minutes quota; keep the repo **private**
  (public would expose the clips).
- Release assets ≤ 2 GB each. Artifacts expire after 7 days; the release keeps files.
- No Twitch/YouTube cookies ever live in GitHub.
- A 720p source upscaled to 1080 wide looks soft; the local half downloads up to 1080p.
