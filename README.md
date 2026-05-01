# yt-diff MCP Server

A small stdio [Model Context Protocol](https://modelcontextprotocol.io/) server for controlling a [`yt-diff`](https://github.com/sagniKdas53/yt-diff) deployment through its HTTP API.

It is intentionally dependency-free at runtime: it speaks MCP JSON-RPC over stdio directly and uses Python's standard library for HTTP requests.

## Tools

### Content Discovery & Search

| Tool | Description |
| --- | --- |
| `health_check` | Check API reachability and login status without exposing credentials. |
| `search_playlists` | Search/list top-level playlist rows via `/getplay`. |
| `search_videos` | Search/list videos in a playlist sublist via `/getsub`; set `playlist_url` to the playlist URL. Standalone videos live under `playlist_url: "None"`. |
| `list_individual_videos` | Convenience wrapper for `/getsub` with `url: "None"`; returns standalone videos and `newest_item` as the last returned row. |
| `get_playlist` | Fetch a playlist by URL, returning its metadata row and the total video count in one call. |

### Adding Content

| Tool | Description |
| --- | --- |
| `add_playlist` | Add a playlist, channel, or profile URL to yt-diff via `/list`. Use this for multi-video collections; for single videos use `add_video`. |
| `add_video` | Add one video URL. URLs are normalized before submission: YouTube video IDs are extracted, iwara.tv slugs are stripped, tracking params removed. |
| `add_videos` | Add multiple video URLs in one listing request. Each URL is normalized individually. |

### Managing Content

| Tool | Description |
| --- | --- |
| `set_playlist_monitoring` | Update a playlist's monitoring mode (`Start`, `End`, `Full`, `N/A`) via `/watch`. |
| `download` | Trigger downloads for one or more video URLs via `/download`. URLs are normalized before submission. |
| `delete_playlist` | Delete a playlist entry via `/delplay`. Flags control whether mappings, DB records, and disk files are removed. |
| `delete_videos` | Remove video entries from a playlist sublist via `/delsub`. Accepts `mapping_ids` (preferred) or `video_urls`. |

### Maintenance

| Tool | Description |
| --- | --- |
| `reindex_all` | Trigger `/reindexall` to refresh metadata for all tracked videos from yt-dlp. |
| `deduplicate` | Scan the database for videos stored under multiple different URLs (same `videoId`, different `videoUrl` PK) and optionally merge them. **Defaults to `dry_run: true`** — safe to call for inspection. Set `dry_run: false` only when ready to apply changes. Accepts optional `site_filter` (e.g. `"iwara.tv"`) to scope the scan. |
| `raw_post` | Advanced allow-listed POST escape hatch for yt-diff endpoints (`/getplay`, `/getsub`, `/list`, `/download`, `/watch`, `/delplay`, `/delsub`, `/getfile`, `/getfiles`, `/refreshfile`, `/refreshfiles`, `/reindexall`, `/dedup`, `/isregallowed`). |

## Configuration

Set these environment variables in your MCP client config:

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `YT_DIFF_BASE` | No | `http://localhost:8888/ytdiff` | yt-diff API base URL including the URL prefix. |
| `YT_DIFF_USERNAME` | Yes | empty | yt-diff username. |
| `YT_DIFF_PASSWORD` | Yes | empty | yt-diff password. |
| `YT_DIFF_TOKEN_CACHE` | No | `~/.cache/yt-diff-mcp/token.json` | Token cache path. |
| `YT_DIFF_TIMEOUT` | No | `90` | HTTP timeout in seconds. |

> Do not commit real credentials. Use environment variables or your MCP client's secret manager.

## Install from a local checkout

```bash
git clone https://github.com/YOUR_USERNAME/yt-diff-mcp.git
cd yt-diff-mcp
python3 -m pip install .
```

Then run:

```bash
yt-diff-mcp
```

## MCP client config examples

### Hermes Agent

```yaml
mcp_servers:
  yt_diff:
    command: "yt-diff-mcp"
    timeout: 120
    connect_timeout: 30
    env:
      YT_DIFF_BASE: "http://192.168.0.110:8888/ytdiff"
      YT_DIFF_USERNAME: "your_username"
      YT_DIFF_PASSWORD: "your_password"
```

If you do not install the package, run directly from the repo:

```yaml
mcp_servers:
  yt_diff:
    command: "python3"
    args: ["/path/to/yt-diff-mcp/src/yt_diff_mcp/server.py"]
    timeout: 120
    connect_timeout: 30
    env:
      YT_DIFF_BASE: "http://192.168.0.110:8888/ytdiff"
      YT_DIFF_USERNAME: "your_username"
      YT_DIFF_PASSWORD: "your_password"
```

### Claude Desktop-style config

```json
{
  "mcpServers": {
    "yt_diff": {
      "command": "yt-diff-mcp",
      "env": {
        "YT_DIFF_BASE": "http://192.168.0.110:8888/ytdiff",
        "YT_DIFF_USERNAME": "your_username",
        "YT_DIFF_PASSWORD": "your_password"
      }
    }
  }
}
```

## yt-diff data model

yt-diff has two related lists:

1. **Playlists list** — top-level entries created by listing playlist/profile/channel URLs.
2. **Sub list** — videos mapped inside a specific playlist. To fetch videos for a playlist, call `/getsub` with the playlist URL in the `url` field.

Standalone single-video URLs do not belong to a real playlist. yt-diff places them in a system playlist named `None`. To verify or browse individually added videos, use `list_individual_videos`, or call `search_videos` with `playlist_url: "None"`. When listing individual videos without a query, the newest standalone item should appear at the end of the returned rows.

## Quick protocol smoke test

```bash
python3 tests/smoke_protocol.py
```

This starts the server, sends `initialize` and `tools/list`, and verifies that tools are returned. It does not require a live yt-diff instance unless you pass `--health`.

## Notes

- `/list` requires `urlList` to be an array; this server handles that for `add_video` and `add_videos`.
- The server retries once with a fresh token after a `401`.
- `raw_post` is restricted to known yt-diff API paths; it is not an arbitrary HTTP client.
- All video URLs submitted through `add_video`, `add_videos`, and `download` are normalized client-side before sending to the API:
  - YouTube video IDs are extracted from any URL form: `youtu.be/{id}`, `m.youtube.com/watch?v={id}`, `watch?v={id}&list=...&start_radio=1`, `shorts/{id}`, etc. All resolve to `https://www.youtube.com/watch?v={id}`.
  - iwara.tv title slugs are stripped: `/video/{id}/{slug}` → `/video/{id}`.
  - Tracking/noise parameters (`utm_*`, `fbclid`, `gclid`, `si`, `pp`) are removed from all URLs.
  - Protocol is forced to `https://`.
  - The yt-diff backend applies the same normalization independently — this is defense-in-depth.
- `deduplicate` should be run with `dry_run: true` first to preview changes. Once satisfied, re-run with `dry_run: false` to merge. Use `site_filter` to limit the scan to one site.
