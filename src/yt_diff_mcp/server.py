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


def clean_youtube_url(url: str) -> str:
    """Keep stable identifiers and drop tracking/noisy YouTube params."""
    url = url.strip()
    if not url:
        raise ValueError("url is required")
    m = re.search(r"(?:youtube\.com/(?:watch\?.*?v=|shorts/)|youtu\.be/)([A-Za-z0-9_-]{6,})", url)
    if not m:
        return url
    video_id = m.group(1)
    return f"https://www.youtube.com/watch?v={video_id}"


def extract_video_id(url_or_id: str) -> str:
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
    url = clean_youtube_url(str(args.get("url", "")))
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
    cleaned = [clean_youtube_url(str(u)) for u in urls]
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


def tool_search_videos(args: dict[str, Any]) -> dict[str, Any]:
    query = args.get("query", "")
    if args.get("video_id"):
        query = "global:" + extract_video_id(str(args["video_id"]))
    payload = {
        "start": int(args.get("start", 0)),
        "stop": int(args.get("stop", 20)),
        "sortDownloaded": bool(args.get("sort_downloaded", False)),
        "query": query,
        "url": args.get("playlist_url", "init"),
    }
    return content(authed_post("/getsub", payload, timeout=TIMEOUT))


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
    payload: dict[str, Any] = {"urlList": [str(u) for u in urls]}
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


def tool_raw_post(args: dict[str, Any]) -> dict[str, Any]:
    path = str(args.get("path", "")).strip()
    if not path.startswith("/"):
        raise ValueError("path must start with /")
    allowed = {"/getplay", "/getsub", "/list", "/download", "/watch", "/delplay", "/delsub", "/getfile", "/getfiles", "/refreshfile", "/refreshfiles", "/reindexall", "/isregallowed"}
    if path not in allowed:
        raise ValueError(f"path not allowed: {path}")
    payload = args.get("payload") or {}
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    if path == "/isregallowed":
        status, body, raw = http_post(path, payload, timeout=TIMEOUT)
        return content({"status_code": status, "body": body, "raw": raw})
    return content(authed_post(path, payload, timeout=TIMEOUT))


TOOLS: dict[str, tuple[str, dict[str, Any], Callable[[dict[str, Any]], dict[str, Any]]]] = {
    "health_check": ("Check yt-diff reachability and authentication status without exposing credentials.", {"type": "object", "properties": {}}, tool_health_check),
    "add_video": ("Add one video URL to yt-diff. YouTube URLs are cleaned to their watch?v= ID form.", {"type": "object", "properties": {"url": {"type": "string"}, "chunk_size": {"type": ["integer", "string"], "default": 1}, "monitoring_type": {"type": "string", "default": "N/A"}, "sleep": {"type": "boolean", "default": True}}, "required": ["url"]}, tool_add_video),
    "add_videos": ("Add multiple video URLs/playlists to yt-diff in one listing request.", {"type": "object", "properties": {"urls": {"type": "array", "items": {"type": "string"}}, "chunk_size": {"type": ["integer", "string"], "default": 1}, "monitoring_type": {"type": "string", "default": "N/A"}, "sleep": {"type": "boolean", "default": True}}, "required": ["urls"]}, tool_add_videos),
    "search_playlists": ("Search/list playlists using yt-diff /getplay.", {"type": "object", "properties": {"query": {"type": "string", "default": ""}, "start": {"type": "integer", "default": 0}, "stop": {"type": "integer", "default": 20}, "sort": {"type": "string", "default": "updatedAt"}, "order": {"type": "string", "default": "2"}}}, tool_search_playlists),
    "search_videos": ("Search/list videos using yt-diff /getsub. Provide video_id to search global:<id>.", {"type": "object", "properties": {"query": {"type": "string", "default": ""}, "video_id": {"type": "string"}, "playlist_url": {"type": "string", "default": "init"}, "start": {"type": "integer", "default": 0}, "stop": {"type": "integer", "default": 20}, "sort_downloaded": {"type": "boolean", "default": False}}}, tool_search_videos),
    "set_playlist_monitoring": ("Update monitoring/watch mode for a playlist URL via /watch.", {"type": "object", "properties": {"url": {"type": "string"}, "watch": {"type": "string", "default": "N/A"}}, "required": ["url"]}, tool_set_playlist_monitoring),
    "download": ("Trigger download for one or more video URLs via /download.", {"type": "object", "properties": {"url": {"type": "string"}, "urls": {"type": "array", "items": {"type": "string"}}, "playlist_url": {"type": "string"}}}, tool_download),
    "reindex_all": ("Trigger yt-diff reindex-all job via /reindexall.", {"type": "object", "properties": {"start": {"type": ["integer", "string"]}, "stop": {"type": ["integer", "string"]}, "site_filter": {"type": "string"}, "chunk_size": {"type": ["integer", "string"]}}}, tool_reindex_all),
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
