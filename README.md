# reelm-encoder

Turns a video file into HLS (the streaming format the Reelm player uses) and
uploads it to **Cloudflare R2**, where storage is cheap and delivery to viewers
is free. Runs on your own PC; uses an NVIDIA GPU (NVENC) when available.

```
episode.mp4 ──ffmpeg──▶ 1080p / 720p / 480p in ~6 s chunks ──upload──▶ R2 ──▶ Reelm player
```

## Setup

1. **ffmpeg**: `winget install Gyan.FFmpeg` (found automatically; or set `FFMPEG_DIR`).
2. **Cloudflare R2** (dashboard → R2 Object Storage):
   - create a bucket, e.g. `reelm-videos`;
   - bucket → Settings → **Public Development URL** → enable (testing). For
     production, connect a **custom domain** instead (r2.dev is rate-limited);
   - bucket → Settings → **CORS policy** → add (so the website may load the videos):
     ```json
     [{ "AllowedOrigins": ["http://localhost:3000", "http://127.0.0.1:3000"],
        "AllowedMethods": ["GET", "HEAD"], "AllowedHeaders": ["*"], "MaxAgeSeconds": 86400 }]
     ```
     (add your real site, e.g. `https://reelm.tv`, when deployed);
   - R2 → **Manage API tokens** → Create → *Object Read & Write*, limited to that bucket.
3. Copy `.env.example` to `.env` and fill it in. Never commit `.env`.
4. `uv run reelm-encoder check`

## Use

```
uv run reelm-encoder encode "D:\videos\episode 1.mp4"
```

It prints the stream URL and duration. Then in the Reelm admin (`/admin`):

1. **Videos → New**: provider `external`, status `ready`, paste the URL, set the duration, Save.
2. Open the episode (or movie), choose that video, set its **Duration**, Save.

Other commands:

```
uv run reelm-encoder encode clip.mp4 --no-upload --keep   # only encode, keep files in reelm-encoder/output
uv run reelm-encoder encode clip.mp4 --cpu                # don't use the GPU
uv run reelm-encoder delete <video-id or stream URL>      # remove a video from R2
```

Each video is stored under `videos/<video-id>/` in the bucket. Replacing or
deleting an episode in the admin does **not** remove its R2 files yet: run
`delete` with the id (it's the folder name in the stream URL).

## Notes

- Never upscales: a 720p source gives 720p + 480p. Wide (cinema) and portrait videos keep their shape.
- HDR videos are converted to normal (SDR) colour; surround sound is mixed to stereo; with
  several audio tracks, the default one is used.
- The web page only answers on this PC (127.0.0.1) and ignores requests from other websites.
- Files are cached "forever" by browsers/CDN; that's safe because every upload gets a new id.
- Only upload videos you have the rights to stream.

## Development

```
uv run pytest      # includes real encodes of a generated clip (CPU, and GPU if present)
uv run ruff check .
```
