# Architecture

GameFinder intentionally separates search, download, and library staging.

## Search layer

Search is constrained to console/ROM/game categories and known game-capable indexers. That keeps searches fast and avoids unrelated movie/TV/music results.

## Download layer

qBittorrent is used as the source of truth for transfer progress. The app adds torrents to a configurable category so they can be routed to the correct save path and watched separately from other downloads.

## Staging layer

Completed payloads are hardlinked when possible and copied only when a hardlink is not possible. This preserves seeding while letting the final library follow a clean structure.

## Optional RomM widgets

If RomM database/resources are mounted, the landing page can show:

- library counts
- recently added games
- platform/group recommendations
- cover thumbnails

The app should still run if RomM integration is disabled.
