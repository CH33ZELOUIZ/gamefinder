# Architecture

GameFinder keeps the three jobs separate: search, download, and library staging. That makes it easier to debug when one part of the chain gets weird.

## Search layer

Search goes through Prowlarr, but it is scoped to the indexer IDs and game/console categories you configure. The app also does a local cleanup pass so obvious movie, TV, music, book, and junk results do not make it into the list.

Platform matching is intentionally practical rather than perfect. Some indexers categorize things cleanly, some dump everything into broad buckets, and some only give useful hints in the title. GameFinder uses all of that, then sorts the results so the likely matches float up.

## Download layer

qBittorrent is the source of truth for transfer state. GameFinder adds torrents to the configured category, points that category at `QBIT_SAVE_PATH`, and reads qBittorrent back for progress.

The app does not move qBittorrent's files out from under it. That matters because moving completed torrent payloads is a great way to break rechecks and seeding.

## Staging layer

Once a download is complete, GameFinder tries to expose it to RomM under:

```text
/roms/<platform>/roms
```

It prefers hardlinks because they are fast and do not use extra space on the same filesystem. If hardlinks are not possible, it falls back to a normal copy. Either way, the original qBittorrent payload stays where qBittorrent expects it.

Archives are extracted when needed, and desktop-style payloads like Windows/Linux/DOS games are kept together so RomM sees the game as one item instead of a pile of loose support files.

## Optional RomM widgets

If RomM database/resources are available, the homepage can show:

- library counts
- recently added games
- platform/group recommendations
- cover thumbnails from the local RomM artwork cache

If RomM is not wired in, GameFinder should still run. You just get the search/download/staging flow without the nicer homepage widgets.
