# kimbo-playlist-gen-redux

A command-line tool that moves playlists between spreadsheets and Spotify, and finds songs by what their lyrics actually say. You give it a simple song list and it builds the playlist in your Spotify account, in your order, telling you which songs it couldn't find. It can also run the other direction — turn any playlist you own back into a list — and it can go hunting: give it a phrase like "company store" and it scores songs by how much of their lyrics that phrase occupies, collecting the hits for you to review. Built for one person curating thematic playlists faster than clicking through an app.

## How it works

You start with a song list — a spreadsheet with two columns, song and artist, in the order you want them to play. The tool opens a browser window once so you can approve access to your Spotify account, then works down the list: it looks up each song, matches it to a real recording, and adds it to a playlist, keeping your order. Songs it can't confidently find are not silently dropped — it prints them at the end so you can fix a spelling or add them by hand. Running it again on the same list is safe: songs already in the playlist are skipped, not duplicated.

The reverse trip works the same way. Point it at any playlist you own and it writes the song list back out as a spreadsheet — the same format other transfer services accept, so a playlist built here can be carried on to Apple Music or YouTube through those services.

Discovery is the part no app does. You give it a phrase, and it searches a lyrics database for songs, reads each song's full lyrics, and measures how much of the text your phrase takes up. Songs above a threshold you set are collected into a separate candidates playlist — never into your real ones — so a human decision still sits between the machine's guesses and the playlist you'd actually play for someone. Finally, an enrichment pass can look up each song's tempo and musical key from a public database and write them into your spreadsheet, so you can re-order a long playlist by feel.

## Nitty gritty

### Setup

```
pip3 install -r requirements.txt
python3 kimbo.py setup
```

`setup` is a guided walkthrough: it takes you step by step through creating the Spotify app (including the exact redirect-URI rule Spotify now enforces), the Genius token, and the GetSongBPM key, writes them to a git-ignored `.env`, and tests each one on the spot. Enter skips the optional ones — Genius is only for `discover`, GetSongBPM only for `enrich`. kimbo loads `.env` automatically on every run, so there is nothing to export.

Re-run `python3 kimbo.py setup` any time to change a value (Enter keeps what's there), or `python3 kimbo.py setup --check` to just validate what's configured. The first real command opens a browser once for Spotify OAuth; the token caches at `~/.cache/kimbo/spotify-token`.

Manual fallback: copy `.env.example` to `.env` and fill it in yourself — the file documents each variable.

### Commands

```
python3 kimbo.py import   --csv examples/suit-and-tie-sessions.csv --title "Suit and Tie Sessions"
python3 kimbo.py import   --csv list.csv --playlist-id 3cEYpjA9oz9GiPac4AsH4n   # append to existing
python3 kimbo.py export   --title "Oilfield Songs" --out oilfield.csv
python3 kimbo.py discover -q "company store" -m "company store" "sixteen tons" --dry-run
python3 kimbo.py discover -q oilfield --threshold 2.0 --pages 8
python3 kimbo.py enrich   --csv list.csv                # writes list-enriched.csv
python3 kimbo.py enrich   --playlist-id 3cEYpjA9...     # tempo/key for a live playlist
```

Useful flags: `setup --check` (validate credentials); `import --dry-run` (resolve and report, write nothing), `--replace` (clear first), `--public` (playlists default to private); `discover --source genius|spotify|both`, `--min-lyrics` (skip stub pages).


### The playlist rack

`playlists/` ships the ten curated playlists this tool was built around — the devil-in-a-suit protest rack (suit-and-tie sessions, new wave, company town, the groove, austerity isles, black waters, oil anti-canon, the 115-track full flow and 58-track tight cut) plus "Nowhere to Make Out," the place-as-pleasure set seeded from Paul Cauthen's "Bayou By You." Each has a CSV (`kimbo.py import` or TuneMyMusic) and a TXT twin (Spotlistr). `playlists/README.md` has the table and the known search misses. Import one with:

```
python3 kimbo.py import --csv playlists/10-nowhere-to-make-out.csv --title "Nowhere to Make Out"
```

### CSV format

Header `Track name, Artist name, Album` (TuneMyMusic's format — the files in `examples/` and the devil-in-a-suit `playlists/` folder are already in it), or headerless `title,artist`. Album is carried but never used for matching.

### Tempo and key: the honest state

Spotify's `audio-features` endpoint was deprecated **27 Nov 2024**; apps without previously-approved extended quota get 403s, and there is no replacement. `enrich` still tries it first (grandfathered apps work) and falls back to **GetSongBPM**, whose free API returns tempo and key by search — coverage is decent for known songs, thin for prewar blues and Bandcamp-tier releases. Their terms require a visible link back to getsongbpm.com wherever the data is published. For accurate, complete values the real options are local: **librosa/Essentia** analysis of audio files you own, or **Mixed In Key / rekordbox** if the goal is DJ-grade key matching — both analyze actual audio rather than looking anything up. A natural next command is `resort`: run `enrich`, then reorder a playlist in place (`playlist_replace_items`) by tempo band within theme blocks.

### TuneMyMusic integration: what's real

TuneMyMusic has **no public API** — the CSV format *is* the integration. `import` replaces it entirely for Spotify (direct API, order preserved, misses reported). For every other platform, `export` produces the CSV TuneMyMusic accepts, so the path to Apple Music/YouTube/Tidal is: `kimbo export` → upload at tunemymusic.com → pick destination. Soundiiz accepts the same file.

### What changed from the original playlist-generator

- Fixed the Spotify pagination bug (a page *counter* was passed as `offset`, which Spotify counts in tracks — the loop crawled one item per pass and its exit condition never fired).
- All loops bounded (`--pages`); no more kill-it-when-bored.
- Lyric scoring uses word boundaries ("oil" no longer matches "boiling") and strips Genius scrape junk (the "… Lyrics" header, the "Embed" tail) before measuring.
- Playlists default to private; the original forced public and only *found* public ones.
- Order is preserved on import; the original added in whatever order search returned.
- Misses are reported with row numbers instead of vanishing in a bare `except`.
- Discovery writes to a candidates playlist, never a real one.
- Dropped: the empty Musixmatch stub that ran unconditionally at the end.

### Gotchas

- `lyricsgenius` scrapes genius.com HTML; it breaks when Genius changes markup and sometimes returns empty lyrics. `discover` treats those as skips, not crashes.
- Genius `search_songs` matches titles/metadata, then kimbo scores full lyrics. True lyric-text search isn't in Genius's public API, so discovery breadth is bounded by what the query surfaces — run several phrasings.
- GetSongBPM rate-limits the free tier; `enrich` sleeps 0.6s between lookups. A 115-track list takes ~90 seconds.
- Two different recordings of one song (single vs. album) have different Spotify IDs; the duplicate guard is by ID, so a re-import can occasionally re-add the *other* recording. Fix by hand when it happens.
