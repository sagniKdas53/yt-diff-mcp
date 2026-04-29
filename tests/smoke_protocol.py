#!/usr/bin/env python3
"""Smoke-test the yt-diff MCP stdio protocol.

By default this only checks initialize + tools/list. Pass --health to call the
live health_check tool, which requires YT_DIFF_* credentials and network access.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "src" / "yt_diff_mcp" / "server.py"


def send(proc: subprocess.Popen[bytes], obj: dict) -> None:
    data = json.dumps(obj, separators=(",", ":")).encode()
    proc.stdin.write(b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n" + data)  # type: ignore[union-attr]
    proc.stdin.flush()  # type: ignore[union-attr]


def recv(proc: subprocess.Popen[bytes]) -> dict:
    headers: dict[str, str] = {}
    while True:
        line = proc.stdout.readline()  # type: ignore[union-attr]
        if not line:
            stderr = proc.stderr.read().decode("utf-8", "replace")  # type: ignore[union-attr]
            raise RuntimeError(f"server exited before response: {stderr}")
        if line in (b"\r\n", b"\n"):
            break
        key, value = line.decode().strip().split(":", 1)
        headers[key.lower()] = value.strip()
    return json.loads(proc.stdout.read(int(headers["content-length"])).decode())  # type: ignore[union-attr]


def main() -> int:
    proc = subprocess.Popen([sys.executable, str(SERVER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
        init = recv(proc)
        assert init["result"]["serverInfo"]["name"] == "yt-diff-mcp"
        send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = recv(proc)["result"]["tools"]
        names = {tool["name"] for tool in tools}
        expected = {
            # health
            "health_check",
            # create
            "add_playlist",
            "add_video",
            "add_videos",
            # read
            "get_playlist",
            "search_playlists",
            "search_videos",
            "list_individual_videos",
            # update
            "set_playlist_monitoring",
            "download",
            # delete
            "delete_playlist",
            "delete_videos",
            # advanced
            "reindex_all",
            "raw_post",
        }
        missing = expected - names
        if missing:
            print(f"FAIL: missing tools: {', '.join(sorted(missing))}", file=sys.stderr)
            return 1
        if "--health" in sys.argv:
            send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "health_check", "arguments": {}}})
            print(json.dumps(recv(proc), indent=2))
        else:
            print(f"OK: {len(names)} tools discovered: {', '.join(sorted(names))}")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
