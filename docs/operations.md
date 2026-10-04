# Operations

## Health checks

- `/health` should return HTTP 200.
- `/jobs` should show active downloads, completed imports, and the current qBittorrent ROM category view.

## Common issues

### Search is slow

Usually one of these is the problem:

- too many broad/general-purpose indexers in `GAMEFINDER_INDEXER_IDS`
- categories that include movies/TV/music instead of just games/console results
- one slow indexer holding up the whole search

Trim the indexer list first. GameFinder works best when it searches the few sources that are actually useful for games.

### Results are missing

Some indexers mislabel game releases as `Other`, `PC/Games`, or something equally unhelpful. GameFinder does a second pass using title/platform hints, but it still rejects obvious non-game categories.

If a real result is missing, try a simpler title search and make sure the indexer ID is included in `GAMEFINDER_INDEXER_IDS`.

### Stage path is wrong

This is almost always a path-mapping issue. qBittorrent reports the path it knows about, but GameFinder needs to see the same files inside its own container.

Check these together:

- `QBIT_SAVE_PATH`
- `QBIT_CONTAINER_ROOT`
- `ROMS_HOST_PATH`
- the `/roms` mount in `docker-compose.yml`

If qBittorrent downloads to `/downloads/roms/_incoming`, and GameFinder sees that same host folder as `/roms/_incoming`, set:

```env
QBIT_CONTAINER_ROOT=/downloads/roms
```

### Downloads finish but RomM does not show them yet

GameFinder can put files in the right folder before RomM has indexed them. Check `/jobs`: a `verifying` job usually means the file is staged and GameFinder is waiting for RomM autoscan/database visibility.

### qBittorrent and GameFinder disagree

Use the "Live qBittorrent ROMs" section on `/jobs`. It reads the qBittorrent category directly and is useful for spotting category/path mismatches or torrents that were added outside GameFinder.
