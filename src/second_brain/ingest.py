from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup
from pypdf import PdfReader
from youtube_transcript_api import YouTubeTranscriptApi

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}


@dataclass(frozen=True)
class IngestedSource:
    kind: str
    title: str
    content: str
    source_url: str = ""


class SourceIngestor:
    def ingest(self, value: str, *, kind: str | None = None) -> IngestedSource:
        path = Path(value).expanduser()
        if path.exists() and path.is_file():
            return self._read_file(path.resolve(), kind=kind)
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme in {"http", "https"}:
            if (parsed.hostname or "").lower() in YOUTUBE_HOSTS:
                return self._fetch_youtube(value)
            return self._fetch_web(value)
        title = _first_heading(value) or "Captured Note"
        return IngestedSource(kind=kind or "note", title=title, content=value)

    def _read_file(self, path: Path, *, kind: str | None = None) -> IngestedSource:
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            reader = PdfReader(path)
            content = "\n\n".join(page.extract_text() or "" for page in reader.pages).strip()
            inferred_kind = "pdf"
        elif suffix in {".md", ".markdown", ".txt"}:
            content = path.read_text(encoding="utf-8", errors="replace")
            inferred_kind = "skill" if path.name.casefold() == "skill.md" else "note"
        else:
            raise ValueError(f"Unsupported local file type: {suffix or '(none)'}")
        title = _first_heading(content) or path.stem
        return IngestedSource(
            kind=kind or inferred_kind,
            title=title,
            content=content,
            source_url=path.as_uri(),
        )

    def _fetch_web(self, url: str) -> IngestedSource:
        request = urllib.request.Request(url, headers={"User-Agent": "LocalSecondBrain/0.1"})
        with urllib.request.urlopen(request, timeout=30) as response:
            content_type = response.headers.get_content_type()
            body = response.read(8_000_000)
        if content_type == "application/pdf" or url.lower().endswith(".pdf"):
            raise ValueError("Remote PDFs must be downloaded first so their exact source is preserved")
        html = body.decode("utf-8", errors="replace")
        return extract_article(html, url)

    def _fetch_youtube(self, url: str) -> IngestedSource:
        video_id = _youtube_id(url)
        if not video_id:
            raise ValueError("Could not determine the YouTube video ID")
        tracks = list(YouTubeTranscriptApi().list(video_id))
        if not tracks:
            raise ValueError("No caption tracks are available for this YouTube video")
        selected = min(tracks, key=lambda track: track.is_generated)
        transcript = selected.fetch()
        caption_type = "generated" if selected.is_generated else "manual"
        lines = [f"Caption language: {selected.language} ({selected.language_code}); track: {caption_type}."]
        for snippet in transcript:
            text = getattr(snippet, "text", None)
            start = getattr(snippet, "start", None)
            if text is None and isinstance(snippet, dict):
                text = snippet.get("text")
                start = snippet.get("start")
            if not text:
                continue
            timestamp = _timestamp(float(start or 0))
            lines.append(f"[{timestamp}] {text}")
        if len(lines) == 1:
            raise ValueError("No transcript was available for this YouTube video")
        title = self._youtube_title(url) or f"YouTube {video_id}"
        return IngestedSource(kind="youtube", title=title, content="\n".join(lines), source_url=url)

    def _youtube_title(self, url: str) -> str:
        endpoint = "https://www.youtube.com/oembed?" + urllib.parse.urlencode({"url": url, "format": "json"})
        try:
            request = urllib.request.Request(endpoint, headers={"User-Agent": "LocalSecondBrain/0.1"})
            with urllib.request.urlopen(request, timeout=10) as response:
                return str(json.loads(response.read()).get("title") or "")
        except Exception:
            return ""


def extract_article(html: str, url: str) -> IngestedSource:
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    for element in soup(["script", "style", "nav", "footer", "aside", "noscript", "svg"]):
        element.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    lines = [line.strip() for line in root.get_text("\n").splitlines() if line.strip()]
    content = "\n\n".join(lines)
    if not content:
        raise ValueError("The webpage did not contain readable article text")
    return IngestedSource(
        kind="article",
        title=title or _first_heading(content) or url,
        content=content,
        source_url=url,
    )


def _first_heading(text: str) -> str:
    match = re.search(r"(?m)^#\s+(.+?)\s*$", text)
    return match.group(1).strip() if match else ""


def _youtube_id(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if (parsed.hostname or "").lower() == "youtu.be":
        return parsed.path.strip("/").split("/")[0]
    if any(parsed.path.startswith(f"/{kind}/") for kind in ("shorts", "embed", "live")):
        parts = parsed.path.strip("/").split("/")
        return parts[1] if len(parts) > 1 else ""
    return urllib.parse.parse_qs(parsed.query).get("v", [""])[0]


def _timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"
