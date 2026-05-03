# yt-diff MCP Server: AI Agent Onboarding Guide

Welcome to the `yt-diff` MCP Server! This guide will help you understand how to use the available tools to interact with the `yt-diff` video archiving system. 

## Overview

The `yt-diff` system allows users to track, download, and manage video playlists from various supported platforms (like YouTube, Iwara, X/Twitter). The MCP server exposes a JSON-RPC interface over standard input/output, enabling AI agents like you to perform operations on the backend without manually sending HTTP requests.

## Authentication & Account Management

The MCP server handles authentication state for you. You can check registration status, create an account, and log in.

- **`check_sign_up_allowed`**: Call this to see if the server administrator allows new user registrations.
- **`sign_up`**: If registration is allowed, create a new account by providing a `username` and `password`.
- **`login`**: Authenticate using `username` and `password`. Upon success, the server will cache a token that is automatically used for all subsequent authenticated tool calls.
- **`health_check`**: Verify reachability and authentication status.

## Core Workflows

### 1. Adding and Monitoring Content

When the user asks you to track a new playlist or video:
- Use **`add_playlist`** to submit a playlist, channel, or profile URL.
- Use **`add_video`** or **`add_videos`** for individual video URLs. 
- Use **`set_playlist_monitoring`** to change how a playlist is monitored (e.g., `Start`, `Full`).

### 2. Searching and Retrieving Data

To explore the database:
- Use **`search_playlists`** to list tracked playlists.
- Use **`get_playlist`** to fetch metadata and the total video count for a specific playlist. Use `playlist_url="None"` to inspect standalone videos.
- Use **`search_videos`** to browse videos inside a specific playlist.
- Use **`list_individual_videos`** to browse standalone videos that are not part of any tracked playlist.

### 3. File Operations

If the user wants to download a video or trigger an update:
- Use **`download`** to manually queue the download of specific videos.
- Use **`reindex_all`** to force a metadata refresh across all tracked videos.

### 4. Database Maintenance

If the user wants to clean up data:
- Use **`deduplicate`** to scan for duplicate video or playlist records (usually caused by URL fragmentation). It defaults to `dry_run=True`. You can target `unlisted`, `playlists`, or `both`.
- Use **`delete_videos`** to remove videos from a playlist sublist, optionally cleaning up downloaded files from disk (`cleanup=True`) and hard-deleting the metadata (`delete_videos_in_db=True`).
- Use **`delete_playlist`** to remove a tracked playlist, optionally deleting all associated videos and files.

## Best Practices

1. **URL Normalization**: The backend strictly canonicalizes URLs (e.g., stripping tracking parameters, converting `youtu.be` to `youtube.com/watch?v=`). The URLs you submit will be cleaned before insertion.
2. **Dry Runs First**: Always run destructive operations like `deduplicate` with `dry_run=True` first, summarize the proposed changes to the user, and then proceed with `dry_run=False` only after explicit confirmation.
3. **Handle Authentication Errors**: If a tool call fails due to authentication, you may need to ask the user for credentials to run the `login` tool.
4. **Use `get_playlist` for Validation**: To verify if a playlist exists and has videos, `get_playlist` is your best tool.
