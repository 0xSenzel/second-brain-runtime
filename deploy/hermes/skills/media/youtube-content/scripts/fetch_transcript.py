#!/usr/bin/env python3
"""Fetch timestamped YouTube captions with explicit language and track metadata."""

import argparse
import json
import re
from urllib.parse import parse_qs, urlparse


VIDEO_ID = re.compile(r"[a-zA-Z0-9_-]{11}")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
                 "youtu.be", "www.youtu.be", "www.youtube-nocookie.com"}


def extract_video_id(url_or_id: str) -> str:
    value = url_or_id.strip()
    if VIDEO_ID.fullmatch(value):
        return value
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in YOUTUBE_HOSTS:
        raise ValueError("Provide a YouTube URL or an 11-character video ID")
    parts = parsed.path.strip("/").split("/")
    if parsed.hostname in {"youtu.be", "www.youtu.be"}:
        video_id = parts[0]
    elif len(parts) == 2 and parts[0] in {"shorts", "embed", "live"}:
        video_id = parts[1]
    else:
        video_id = parse_qs(parsed.query).get("v", [""])[0]
    if not VIDEO_ID.fullmatch(video_id):
        raise ValueError("Could not determine a valid YouTube video ID")
    return video_id


def format_timestamp(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def fetch_transcript(video_id: str, languages: list | None = None) -> dict:
    from youtube_transcript_api import YouTubeTranscriptApi

    tracks = list(YouTubeTranscriptApi().list(video_id))
    available = list(dict.fromkeys(track.language_code for track in tracks))
    preferences = languages or available
    selected = None
    for language in preferences:
        candidates = [track for track in tracks if track.language_code == language]
        if candidates:
            selected = min(candidates, key=lambda track: track.is_generated)
            break
    if languages is None and tracks:
        # The first available manual track is preferable to generated captions.
        # This chooses an available language; it does not detect the spoken language.
        selected = min(tracks, key=lambda track: track.is_generated)
    if selected is None:
        raise ValueError(f"No requested caption language is available. Available: {', '.join(available) or 'none'}")
    segments = [
        {"text": segment.text, "start": segment.start, "duration": segment.duration}
        for segment in selected.fetch()
    ]
    if not segments or not any(segment["text"].strip() for segment in segments):
        raise ValueError("The selected caption track is empty")
    return {
        "video_id": video_id, "source_url": f"https://www.youtube.com/watch?v={video_id}",
        "language": selected.language, "language_code": selected.language_code,
        "is_generated": selected.is_generated, "available_languages": available,
        "segments": segments,
    }


def error_payload(error: Exception) -> dict:
    code = {
        "TranscriptsDisabled": "transcripts_disabled", "NoTranscriptFound": "language_unavailable",
        "VideoUnavailable": "video_unavailable", "RequestBlocked": "request_blocked",
        "IpBlocked": "request_blocked", "ImportError": "dependency_missing",
        "ModuleNotFoundError": "dependency_missing", "ValueError": "invalid_input_or_track",
    }.get(type(error).__name__, "fetch_failed")
    return {"error": str(error), "error_code": code}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="YouTube URL or video ID")
    parser.add_argument("--language", "-l", help="Preferred language codes in order, such as zh-Hans,en")
    parser.add_argument("--timestamps", "-t", action="store_true")
    parser.add_argument("--text-only", action="store_true")
    args = parser.parse_args()
    try:
        video_id = extract_video_id(args.url)
        languages = [code.strip() for code in args.language.split(",") if code.strip()] if args.language else None
        result = fetch_transcript(video_id, languages)
    except Exception as error:
        print(json.dumps(error_payload(error), ensure_ascii=False))
        raise SystemExit(1) from error
    segments = result["segments"]
    full_text = " ".join(segment["text"] for segment in segments)
    timestamped = "\n".join(f"{format_timestamp(segment['start'])} {segment['text']}" for segment in segments)
    if args.text_only:
        print(timestamped if args.timestamps else full_text)
        return
    result.update(
        segment_count=len(segments), full_text=full_text,
        transcript_end=format_timestamp(max(segment["start"] + segment["duration"] for segment in segments)),
    )
    if args.timestamps:
        result["timestamped_text"] = timestamped
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
