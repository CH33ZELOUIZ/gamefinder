# Operations

## Health checks

- `/health` should return HTTP 200.
- `/jobs` should show active and completed transfer/staging state.

## Common issues

### Search is slow

Likely causes:

- too many general-purpose indexers enabled
- broad categories instead of console/game categories
- an indexer timing out

Fix by narrowing indexers/categories and setting reasonable request timeouts.

### Results are missing

Some indexers miscategorize console releases as `Other` or `PC/Games`. Use title/platform matching as a second-pass filter, but still reject obvious movie/TV/music/book categories.

### Stage path is wrong

Check that qBittorrent and the app agree on the same container-visible paths. Most staging bugs are path-contract bugs.
