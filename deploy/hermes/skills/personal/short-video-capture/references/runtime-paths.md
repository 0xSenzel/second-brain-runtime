# Runtime paths

Choose paths from the environment that runs the capture, rather than the host that
happens to hold this skill. Verify each executable before invoking it.

| Component | Hermes Linux deployment | Local macOS source |
| --- | --- | --- |
| Python interpreter | `/opt/hermes/.venv/bin/python` | Configured Python with Playwright installed |
| Capture scripts | `/opt/data/scripts/` | `~/.hermes/scripts/` |
| Video cache | `/opt/data/cache/videos/` | Pass a writable output directory explicitly |
| Retained jobs | `/opt/data/video-jobs/` | Pass `--work-root` explicitly |
| Integrated extractor | `/opt/data/scripts/extract_video_info.py` | Verify the installed path; set `HERMES_VIDEO_EXTRACTOR` for the long worker |
| Local speech runtime | `/opt/data/venvs/short-video-asr` | Use the configured local transcription provider |

The local source checkout alone does not establish that the Linux scripts are deployed.
If a required component is absent, report its path and the available fallback. Avoid
runtime package installation and custom scraping scripts.

The Douyin collector uses a persistent anonymous browser profile. On Linux it uses Xvfb
when a display is absent. Leave that profile in place; it is separate from scratch media.
Its location is selected by `DOUYIN_PROFILE` and defaults to `/opt/data/cache/douyin-profile`.
For a local run, point that variable at a writable directory. Pass the output directory
as the second argument to either capture script.

Both collectors prune recognized scratch media older than six hours on a later capture.
Before resuming, check whether the source remains available. Long-video preparation
copies the source outside that cache. Keep extracted text available until `brain_capture` succeeds;
the captured source note embeds the source text and survives scratch expiration.
