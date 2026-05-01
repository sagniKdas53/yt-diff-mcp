#!/usr/bin/env python3
"""Stdio MCP server for yt-diff.

This server is dependency-free: it implements the small MCP JSON-RPC over
stdio surface required for tool discovery and tool calls. Configure it with
environment variables; do not hard-code credentials.
"""
from __future__ import annotations

import json
import os
import re
import sys
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

DEFAULT_BASE = "http://localhost:8888/ytdiff"
DEFAULT_USERNAME = ""
DEFAULT_PASSWORD = ""
DEFAULT_CACHE = str(Path.home() / ".cache" / "yt-diff-mcp" / "token.json")

BASE = os.environ.get("YT_DIFF_BASE", DEFAULT_BASE).rstrip("/")
USERNAME = os.environ.get("YT_DIFF_USERNAME", DEFAULT_USERNAME)
PASSWORD = os.environ.get("YT_DIFF_PASSWORD", DEFAULT_PASSWORD)
CACHE = Path(os.environ.get("YT_DIFF_TOKEN_CACHE", DEFAULT_CACHE))
TIMEOUT = float(os.environ.get("YT_DIFF_TIMEOUT", "90"))


def log(message: str) -> None:
    print(f"[yt-diff-mcp] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# URL normalization (mirrors normalizeUrl() in process-manager.ts)
# ---------------------------------------------------------------------------

# YouTube video-ID pattern (11 chars, base64url alphabet)
_YT_VIDEO_ID_RE = re.compile(r'^[A-Za-z0-9_-]{11}$')
_YT_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "www.m.youtube.com",
    "youtu.be", "www.youtu.be",
    "youtube-nocookie.com", "www.youtube-nocookie.com",
}
_IWARA_HOSTS_RE = re.compile(r'(?:^|\.)iwara\.tv$')
_STRIP_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                  "utm_content", "fbclid", "gclid", "si", "pp"}


def _extract_yt_video_id(parsed) -> str | None:
    """Extract YouTube video ID from various URL forms."""
    host = parsed.netloc
    path = parsed.path
    qs = dict(re.findall(r'([^&=?]+)=([^&]*)', parsed.query))
    # youtu.be/{id}
    if host in ("youtu.be", "www.youtu.be"):
        vid = path.lstrip("/").split("/")[0]
        if _YT_VIDEO_ID_RE.match(vid):
            return vid
        return None
    # watch?v=ID
    v = qs.get("v", "")
    if v and _YT_VIDEO_ID_RE.match(v):
        return v
    # /shorts/ID or /embed/ID
    m = re.search(r'/(?:shorts|embed)/([A-Za-z0-9_-]{11})', path)
    if m:
        return m.group(1)
    return None


def normalize_url(url: str) -> str:
    """Canonicalize a URL for stable deduplication, mirroring the TS normalizeUrl().

    Steps:
      1. Force https://
      2. Strip trailing slashes from path
      3. Remove tracking query params (utm_*, fbclid, si, pp)
      4. Apply site-specific rules (YouTube video ID extraction, iwara slug strip)
    """
    url = url.strip()
    if not url:
        raise ValueError("url is required")

    from urllib.parse import urlparse, urlunparse, urlencode, parse_qsl
    try:
        parsed = urlparse(url)
    except Exception:
        return url

    # 1. Force https
    parsed = parsed._replace(scheme="https")

    # 2. Strip trailing slashes
    path = parsed.path.rstrip("/") or "/"
    parsed = parsed._replace(path=path)

    # 3. Strip noise query params
    qs_pairs = [(k, v) for k, v in parse_qsl(parsed.query) if k not in _STRIP_PARAMS]

    host = parsed.netloc

    # 4a. YouTube
    if host in _YT_HOSTS:
        vid = _extract_yt_video_id(parsed._replace(query="&".join(f"{k}={v}" for k, v in parse_qsl(parsed.query))))
        if vid:
            return f"https://www.youtube.com/watch?v={vid}"
        # Non-video YouTube URL: normalize host, append /videos to channel handles
        normalized_host = "www.youtube.com"
        normalized_path = parsed.path
        if "/@" in normalized_path and not normalized_path.rstrip("/").endswith("/videos"):
            normalized_path = normalized_path.rstrip("/") + "/videos"
        clean_qs = urlencode(qs_pairs)
        return urlunparse(("https", normalized_host, normalized_path, "", clean_qs, ""))

    # 4b. iwara.tv — strip trailing slug: /video/{id}/{slug} → /video/{id}
    if _IWARA_HOSTS_RE.search(host):
        m = re.match(r'(/video/[A-Za-z0-9]+)', parsed.path)
        if m:
            return urlunparse(("https", host, m.group(1), "", "", ""))

    # 4c. spankbang.com — strip title slug: /{id}/video/{slug} → /{id}/video
    if host == "spankbang.com" or host.endswith(".spankbang.com"):
        m = re.match(r'(/[A-Za-z0-9]+/video)', parsed.path)
        if m:
            return urlunparse(("https", host, m.group(1), "", "", ""))

    # Generic: rebuild with cleaned params
    clean_qs = urlencode(qs_pairs)
    return urlunparse(("https", host, parsed.path, "", clean_qs, ""))


def extract_video_id(url_or_id: str) -> str:
    """Extract a YouTube video ID from a URL or return the input if it's already an ID."""
    text = url_or_id.strip()
    m = re.search(r"(?:v=|shorts/|youtu\.be/)([A-Za-z0-9_-]{6,})", text)
    return m.group(1) if m else text


def http_post(path: str, payload: dict[str, Any], token: str | None = None, timeout: float | None = None) -> tuple[int, Any, str]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, parse_body(raw), raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        return e.code, parse_body(raw), raw


def parse_body(raw: str) -> Any:
    try:
        return json.loads(raw)
    except Exception:
        return raw


def login() -> str:
    if not USERNAME or not PASSWORD:
        raise RuntimeError("YT_DIFF_USERNAME and YT_DIFF_PASSWORD must be set")
    status, body, raw = http_post("/login", {"userName": USERNAME, "password": PASSWORD}, timeout=20)
    if status != 200 or not isinstance(body, dict) or "token" not in body:
        raise RuntimeError(f"login failed {status}: {raw[:500]}")
    token = str(body["token"])
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({"token": token, "created_at": datetime.now(timezone.utc).isoformat(), "base": BASE}), encoding="utf-8")
    return token


def get_token(force_refresh: bool = False) -> str:
    if not force_refresh and CACHE.exists():
        try:
            cached = json.loads(CACHE.read_text(encoding="utf-8"))
            if cached.get("base") == BASE and cached.get("token"):
                return str(cached["token"])
        except Exception:
            pass
    return login()


def authed_post(path: str, payload: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
    token = get_token()
    status, body, raw = http_post(path, payload, token=token, timeout=timeout)
    if status == 401:
        token = get_token(force_refresh=True)
        status, body, raw = http_post(path, payload, token=token, timeout=timeout)
    return {"status_code": status, "body": body, "raw": raw}


def content(obj: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(obj, indent=2, ensure_ascii=False)}]}


def tool_health_check(args: dict[str, Any]) -> dict[str, Any]:
    # Uses an auth-light endpoint and reports whether login works without exposing token/password.
    status, body, raw = http_post("/isregallowed", {"sendStats": False}, timeout=20)
    login_ok = False
    login_status = None
    try:
        if not USERNAME or not PASSWORD:
            raise RuntimeError("YT_DIFF_USERNAME and YT_DIFF_PASSWORD must be set")
        status2, body2, _ = http_post("/login", {"userName": USERNAME, "password": PASSWORD}, timeout=20)
        login_status = status2
        login_ok = status2 == 200 and isinstance(body2, dict) and bool(body2.get("token"))
    except Exception as exc:
        login_status = f"error: {exc}"
    return content({"base": BASE, "is_registration_allowed_status": status, "is_registration_allowed": body, "login_ok": login_ok, "login_status": login_status})


def tool_add_video(args: dict[str, Any]) -> dict[str, Any]:
    url = normalize_url(str(args.get("url", "")))
    payload = {
        "urlList": [url],
        "chunkSize": args.get("chunk_size", 1),
        "monitoringType": args.get("monitoring_type", "N/A"),
        "sleep": bool(args.get("sleep", True)),
    }
    result = authed_post("/list", payload, timeout=TIMEOUT)
    result["cleaned_url"] = url
    return content(result)


def tool_add_videos(args: dict[str, Any]) -> dict[str, Any]:
    urls = args.get("urls") or []
    if not isinstance(urls, list) or not urls:
        raise ValueError("urls must be a non-empty array")
    cleaned = [normalize_url(str(u)) for u in urls]
    payload = {
        "urlList": cleaned,
        "chunkSize": args.get("chunk_size", 1),
        "monitoringType": args.get("monitoring_type", "N/A"),
        "sleep": bool(args.get("sleep", True)),
    }
    result = authed_post("/list", payload, timeout=TIMEOUT)
    result["cleaned_urls"] = cleaned
    return content(result)


def tool_search_playlists(args: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "start": int(args.get("start", 0)),
        "stop": int(args.get("stop", 20)),
        "sort": args.get("sort", "updatedAt"),
        "order": str(args.get("order", "2")),
        "query": args.get("query", ""),
    }
    return content(authed_post("/getplay", payload, timeout=TIMEOUT))


def get_sublist_payload(args: dict[str, Any], default_playlist_url: str = "init") -> dict[str, Any]:
    query = args.get("query", "")
    if args.get("video_id"):
        query = "global:" + extract_video_id(str(args["video_id"]))
    return {
        "start": int(args.get("start", 0)),
        "stop": int(args.get("stop", 20)),
        "sortDownloaded": bool(args.get("sort_downloaded", False)),
        "query": query,
        "url": args.get("playlist_url", default_playlist_url),
    }


def tool_search_videos(args: dict[str, Any]) -> dict[str, Any]:
    """Search/list videos in a playlist sublist via /getsub."""
    return content(authed_post("/getsub", get_sublist_payload(args), timeout=TIMEOUT))


def tool_list_individual_videos(args: dict[str, Any]) -> dict[str, Any]:
    """List standalone videos stored in yt-diff's system playlist named None."""
    payload = get_sublist_payload(args, default_playlist_url="None")
    payload["url"] = "None"
    result = authed_post("/getsub", payload, timeout=TIMEOUT)
    if isinstance(result.get("body"), dict) and isinstance(result["body"].get("rows"), list):
        rows = result["body"]["rows"]
        result["newest_item"] = rows[-1] if rows else None
    return content(result)


def tool_set_playlist_monitoring(args: dict[str, Any]) -> dict[str, Any]:
    url = str(args.get("url", "")).strip()
    if not url:
        raise ValueError("url is required")
    watch = str(args.get("watch", "N/A"))
    return content(authed_post("/watch", {"url": url, "watch": watch}, timeout=TIMEOUT))


def tool_download(args: dict[str, Any]) -> dict[str, Any]:
    urls = args.get("urls") or ([] if not args.get("url") else [args.get("url")])
    if not isinstance(urls, list) or not urls:
        raise ValueError("provide url or urls")
    payload: dict[str, Any] = {"urlList": [normalize_url(str(u)) for u in urls]}
    if args.get("playlist_url"):
        payload["playListUrl"] = str(args["playlist_url"])
    return content(authed_post("/download", payload, timeout=TIMEOUT))


def tool_reindex_all(args: dict[str, Any]) -> dict[str, Any]:
    payload = {k: v for k, v in {
        "start": args.get("start"),
        "stop": args.get("stop"),
        "siteFilter": args.get("site_filter"),
        "chunkSize": args.get("chunk_size"),
    }.items() if v is not None}
    return content(authed_post("/reindexall", payload, timeout=TIMEOUT))


def tool_add_playlist(args: dict[str, Any]) -> dict[str, Any]:
    """Add a playlist, channel, or profile URL to yt-diff via /list.

    Use this for playlist/channel/profile URLs. For individual video URLs use
    add_video or add_videos instead. Monitoring stays N/A by default; use
    set_playlist_monitoring afterward to change it.
    """
    url = str(args.get("playlist_url", "")).strip()
    if not url:
        raise ValueError("playlist_url is required")
    payload = {
        "urlList": [url],
        "chunkSize": args.get("chunk_size", 50),
        "monitoringType": args.get("monitoring_type", "N/A"),
        "sleep": bool(args.get("sleep", True)),
    }
    result = authed_post("/list", payload, timeout=TIMEOUT)
    result["playlist_url"] = url
    return content(result)


def tool_get_playlist(args: dict[str, Any]) -> dict[str, Any]:
    """Fetch a playlist by URL and return its metadata plus sub-list video count.

    Calls /getplay with query='url:<playlist_url>' to find the playlist row(s),
    then calls /getsub to get the total video count for that playlist.
    Use playlist_url='None' to inspect the individual-videos bucket.
    This is the primary validation tool: if count > 0 the playlist has items.
    """
    playlist_url = str(args.get("playlist_url", "")).strip()
    if not playlist_url:
        raise ValueError("playlist_url is required")

    # Fetch playlist metadata row(s)
    getplay_payload: dict[str, Any] = {
        "start": 0,
        "stop": 20,
        "sort": "updatedAt",
        "order": "2",
        "query": f"url:{playlist_url}",
    }
    playlist_result = authed_post("/getplay", getplay_payload, timeout=TIMEOUT)

    # Fetch sub-list video count (stop=1 is enough to get the count field)
    getsub_payload: dict[str, Any] = {
        "start": 0,
        "stop": 1,
        "sortDownloaded": False,
        "query": "",
        "url": playlist_url,
    }
    getsub_result = authed_post("/getsub", getsub_payload, timeout=TIMEOUT)

    video_count: int | None = None
    if isinstance(getsub_result.get("body"), dict):
        video_count = getsub_result["body"].get("count")

    return content({
        "playlist_url": playlist_url,
        "playlist": playlist_result,
        "video_count": video_count,
    })


def tool_delete_playlist(args: dict[str, Any]) -> dict[str, Any]:
    """Delete a playlist entry from yt-diff via /delplay.

    The backend blocks deletion of the 'None' system playlist.
    By default none of the flags are set, so this is a no-op unless at least
    one of delete_all_videos or delete_playlist is True.

    Flags:
      delete_all_videos  — remove all video references from this playlist
      delete_playlist    — delete the playlist row itself
      cleanup            — also delete files from disk (only if delete_playlist
                          or delete_all_videos is set)
    """
    playlist_url = str(args.get("playlist_url", "")).strip()
    if not playlist_url:
        raise ValueError("playlist_url is required")
    payload = {
        "playListUrl": playlist_url,
        "deleteAllVideosInPlaylist": bool(args.get("delete_all_videos", False)),
        "deletePlaylist": bool(args.get("delete_playlist", False)),
        "cleanUp": bool(args.get("cleanup", False)),
    }
    return content(authed_post("/delplay", payload, timeout=TIMEOUT))


def tool_delete_videos(args: dict[str, Any]) -> dict[str, Any]:
    """Remove video entries from a playlist sublist via /delsub.

    Provide either mapping_ids (preferred, from /getsub row 'id' field) or
    video_urls — at least one must be non-empty.

    Flags:
      cleanup              — delete downloaded files from disk; only has effect
                            for videos where downloadStatus is true
      delete_video_mappings — remove the mapping row from the sublist
                             (default true)
      delete_videos_in_db  — hard-delete the VideoMetadata row entirely
                             (default false; use with care)
    """
    playlist_url = str(args.get("playlist_url", "")).strip()
    if not playlist_url:
        raise ValueError("playlist_url is required")
    mapping_ids = args.get("mapping_ids") or []
    video_urls = args.get("video_urls") or []
    if not isinstance(mapping_ids, list):
        raise ValueError("mapping_ids must be an array")
    if not isinstance(video_urls, list):
        raise ValueError("video_urls must be an array")
    if not mapping_ids and not video_urls:
        raise ValueError("provide at least one of mapping_ids or video_urls")
    payload: dict[str, Any] = {
        "playListUrl": playlist_url,
        "mappingIds": [str(m) for m in mapping_ids],
        "videoUrls": [str(u) for u in video_urls],
        "cleanUp": bool(args.get("cleanup", False)),
        "deleteVideoMappings": bool(args.get("delete_video_mappings", True)),
        "deleteVideosInDB": bool(args.get("delete_videos_in_db", False)),
    }
    return content(authed_post("/delsub", payload, timeout=TIMEOUT))


def tool_raw_post(args: dict[str, Any]) -> dict[str, Any]:
    path = str(args.get("path", "")).strip()
    if not path.startswith("/"):
        raise ValueError("path must start with /")
    allowed = {"/getplay", "/getsub", "/list", "/download", "/watch", "/delplay", "/delsub", "/getfile", "/getfiles", "/refreshfile", "/refreshfiles", "/reindexall", "/dedup", "/isregallowed"}
    if path not in allowed:
        raise ValueError(f"path not allowed: {path}")
    payload = args.get("payload") or {}
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    if path == "/isregallowed":
        status, body, raw = http_post(path, payload, timeout=TIMEOUT)
        return content({"status_code": status, "body": body, "raw": raw})
    return content(authed_post(path, payload, timeout=TIMEOUT))


def tool_deduplicate(args: dict[str, Any]) -> dict[str, Any]:
    """Scan the database for duplicate video records (same videoId, different videoUrl)
    and optionally merge them. Always defaults to dry_run=True for safety.
    """
    payload: dict[str, Any] = {
        "dryRun": bool(args.get("dry_run", True)),
    }
    if args.get("site_filter"):
        payload["siteFilter"] = str(args["site_filter"])
    return content(authed_post("/dedup", payload, timeout=TIMEOUT))


TOOLS: dict[str, tuple[str, dict[str, Any], Callable[[dict[str, Any]], dict[str, Any]]]] = {
    "health_check": ("Check yt-diff reachability and authentication status without exposing credentials.", {"type": "object", "properties": {}}, tool_health_check),
    # --- Create ---
    "add_playlist": (
        "Add a playlist, channel, or profile URL to yt-diff via /list. "
        "Use this for playlist/channel/profile URLs; for individual video URLs use add_video or add_videos. "
        "Monitoring defaults to N/A — use set_playlist_monitoring to change it afterward.",
        {
            "type": "object",
            "properties": {
                "playlist_url": {"type": "string"},
                "chunk_size": {"type": ["integer", "string"], "default": 50},
                "monitoring_type": {"type": "string", "default": "N/A"},
                "sleep": {"type": "boolean", "default": True},
            },
            "required": ["playlist_url"],
        },
        tool_add_playlist,
    ),
    "add_video": ("Add one video URL to yt-diff. YouTube URLs are cleaned to their watch?v= ID form.", {"type": "object", "properties": {"url": {"type": "string"}, "chunk_size": {"type": ["integer", "string"], "default": 1}, "monitoring_type": {"type": "string", "default": "N/A"}, "sleep": {"type": "boolean", "default": True}}, "required": ["url"]}, tool_add_video),
    "add_videos": ("Add multiple video URLs/playlists to yt-diff in one listing request.", {"type": "object", "properties": {"urls": {"type": "array", "items": {"type": "string"}}, "chunk_size": {"type": ["integer", "string"], "default": 1}, "monitoring_type": {"type": "string", "default": "N/A"}, "sleep": {"type": "boolean", "default": True}}, "required": ["urls"]}, tool_add_videos),
    # --- Read ---
    "get_playlist": (
        "Fetch a playlist by URL and return its metadata plus sub-list video count in one call. "
        "Uses /getplay (url:<playlist_url> query) then /getsub to get the count. "
        "Use playlist_url='None' to inspect the individual-videos bucket. "
        "If video_count > 0 the playlist has items (validation).",
        {
            "type": "object",
            "properties": {
                "playlist_url": {"type": "string"},
            },
            "required": ["playlist_url"],
        },
        tool_get_playlist,
    ),
    "search_playlists": ("Search/list playlists using yt-diff /getplay.", {"type": "object", "properties": {"query": {"type": "string", "default": ""}, "start": {"type": "integer", "default": 0}, "stop": {"type": "integer", "default": 20}, "sort": {"type": "string", "default": "updatedAt"}, "order": {"type": "string", "default": "2"}}}, tool_search_playlists),
    "search_videos": ("Search/list videos in a playlist sublist using yt-diff /getsub. Set playlist_url to the playlist URL; standalone videos live under playlist_url='None'. Provide video_id to search global:<id>.", {"type": "object", "properties": {"query": {"type": "string", "default": ""}, "video_id": {"type": "string"}, "playlist_url": {"type": "string", "default": "init"}, "start": {"type": "integer", "default": 0}, "stop": {"type": "integer", "default": 20}, "sort_downloaded": {"type": "boolean", "default": False}}}, tool_search_videos),
    "list_individual_videos": ("List/search standalone single videos stored in yt-diff's system playlist named None. Without a query, the newest individual video should be the last returned row and is also returned as newest_item.", {"type": "object", "properties": {"query": {"type": "string", "default": ""}, "video_id": {"type": "string"}, "start": {"type": "integer", "default": 0}, "stop": {"type": "integer", "default": 20}, "sort_downloaded": {"type": "boolean", "default": False}}}, tool_list_individual_videos),
    # --- Update ---
    "set_playlist_monitoring": ("Update monitoring/watch mode for a playlist URL via /watch.", {"type": "object", "properties": {"url": {"type": "string"}, "watch": {"type": "string", "default": "N/A"}}, "required": ["url"]}, tool_set_playlist_monitoring),
    "download": ("Trigger download for one or more video URLs via /download.", {"type": "object", "properties": {"url": {"type": "string"}, "urls": {"type": "array", "items": {"type": "string"}}, "playlist_url": {"type": "string"}}}, tool_download),
    # --- Delete ---
    "delete_playlist": (
        "Delete a playlist entry from yt-diff via /delplay. "
        "The 'None' system playlist cannot be deleted. "
        "All boolean flags default to false — set at least one of delete_all_videos or delete_playlist to true, otherwise nothing is deleted. "
        "cleanup only deletes files from disk when combined with delete_playlist or delete_all_videos.",
        {
            "type": "object",
            "properties": {
                "playlist_url": {"type": "string"},
                "delete_all_videos": {"type": "boolean", "default": False},
                "delete_playlist": {"type": "boolean", "default": False},
                "cleanup": {"type": "boolean", "default": False},
            },
            "required": ["playlist_url"],
        },
        tool_delete_playlist,
    ),
    "delete_videos": (
        "Remove video entries from a playlist sublist via /delsub. "
        "Provide mapping_ids (the 'id' field from /getsub rows, preferred) or video_urls — at least one must be non-empty. "
        "cleanup deletes downloaded files from disk; only has effect when downloadStatus is true. "
        "delete_video_mappings (default true) removes the sublist mapping row. "
        "delete_videos_in_db (default false) hard-deletes the VideoMetadata record.",
        {
            "type": "object",
            "properties": {
                "playlist_url": {"type": "string"},
                "mapping_ids": {"type": "array", "items": {"type": "string"}, "default": []},
                "video_urls": {"type": "array", "items": {"type": "string"}, "default": []},
                "cleanup": {"type": "boolean", "default": False},
                "delete_video_mappings": {"type": "boolean", "default": True},
                "delete_videos_in_db": {"type": "boolean", "default": False},
            },
            "required": ["playlist_url"],
        },
        tool_delete_videos,
    ),
    # --- Advanced ---
    "reindex_all": ("Trigger yt-diff reindex-all job via /reindexall.", {"type": "object", "properties": {"start": {"type": ["integer", "string"]}, "stop": {"type": ["integer", "string"]}, "site_filter": {"type": "string"}, "chunk_size": {"type": ["integer", "string"]}}}, tool_reindex_all),
    "deduplicate": (
        "Scan the database for videos stored under multiple different URLs (same videoId, different videoUrl PK) "
        "and optionally merge them into one canonical record. "
        "Always defaults to dry_run=True — set dry_run=False only when you are ready to apply changes. "
        "Use site_filter (e.g. 'iwara.tv') to scope the scan to one site.",
        {
            "type": "object",
            "properties": {
                "dry_run": {"type": "boolean", "default": True},
                "site_filter": {"type": "string"},
            },
        },
        tool_deduplicate,
    ),
    "raw_post": ("Advanced: POST an object payload to an allow-listed yt-diff API path.", {"type": "object", "properties": {"path": {"type": "string"}, "payload": {"type": "object"}}, "required": ["path"]}, tool_raw_post),
}


def read_message() -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if line == b"":
            return None
        line = line.decode("ascii", "replace").strip()
        if line == "":
            break
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.lower()] = v.strip()
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    data = sys.stdin.buffer.read(length)
    return json.loads(data.decode("utf-8"))


def send_message(message: dict[str, Any]) -> None:
    data = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(data)}\r\n\r\n".encode("ascii") + data)
    sys.stdout.buffer.flush()


def result_response(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def error_response(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle(req: dict[str, Any]) -> dict[str, Any] | None:
    method = req.get("method")
    msg_id = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        return result_response(msg_id, {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "yt-diff-mcp", "version": "1.0.0"},
        })
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return result_response(msg_id, {})
    if method == "tools/list":
        return result_response(msg_id, {"tools": [
            {"name": name, "description": desc, "inputSchema": schema}
            for name, (desc, schema, _func) in TOOLS.items()
        ]})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in TOOLS:
            return error_response(msg_id, -32602, f"unknown tool: {name}")
        try:
            return result_response(msg_id, TOOLS[name][2](args))
        except Exception as exc:
            log(traceback.format_exc())
            return result_response(msg_id, {"isError": True, "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}]})
    if msg_id is not None:
        return error_response(msg_id, -32601, f"method not found: {method}")
    return None


def main() -> None:
    log(f"starting with base={BASE}")
    while True:
        req = read_message()
        if req is None:
            break
        resp = handle(req)
        if resp is not None:
            send_message(resp)


if __name__ == "__main__":
    main()
