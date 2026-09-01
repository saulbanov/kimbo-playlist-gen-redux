#!/usr/bin/env python3
"""kimbo - playlist plumbing for Spotify.

Redux of the old playlist-generator script. Four subcommands:

  import    CSV -> Spotify playlist (ordered, deduped, reports misses)
  export    Spotify playlist -> CSV in the same format
  discover  Genius lyric-density search -> a candidates playlist or CSV
  enrich    add tempo/key columns via GetSongBPM (and Spotify
            audio-features, when your app still has access)

Run `python3 kimbo.py <command> -h` for options. Credentials come from
environment variables; see .env.example.
"""

import argparse
import csv
import os
import re
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip3 install -r requirements.txt")

PITCH_CLASSES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
CSV_HEADER = ["Track name", "Artist name", "Album"]


def warn(msg):
    print("  ! " + msg, file=sys.stderr)


def die(msg):
    sys.exit("error: " + msg)


# ---------------------------------------------------------------- Spotify ---

def spotify_client():
    """OAuth browser flow. Needs SPOTIPY_CLIENT_ID / _SECRET / _REDIRECT_URI.

    Spotify requires the redirect URI to be HTTPS or the loopback literal
    http://127.0.0.1:<port>/... - `localhost` is rejected for new apps.
    """
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyOAuth
    except ImportError:
        die("spotipy not installed: pip3 install -r requirements.txt")
    for var in ("SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET", "SPOTIPY_REDIRECT_URI"):
        if not os.environ.get(var):
            die("%s is not set - copy .env.example to .env, fill it in, and "
                "`export $(grep -v '^#' .env | xargs)` (or use direnv)" % var)
    cache = os.path.expanduser("~/.cache/kimbo/spotify-token")
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    scope = "playlist-modify-public playlist-modify-private playlist-read-private"
    return spotipy.Spotify(auth_manager=SpotifyOAuth(scope=scope, cache_path=cache))


def find_track(sp, title, artist):
    """Return the best-matching track dict, or None."""
    for query in ('track:"%s" artist:"%s"' % (title, artist),
                  "%s %s" % (title, artist)):
        result = sp.search(q=query, type="track", limit=3)
        items = result.get("tracks", {}).get("items", [])
        if items:
            return items[0]
    return None


def playlist_by_title(sp, title):
    """Find a playlist of any visibility owned by the current user."""
    me = sp.me()["id"]
    page = sp.current_user_playlists(limit=50)
    while page:
        for pl in page["items"]:
            if pl["name"] == title and pl["owner"]["id"] == me:
                return pl["id"]
        page = sp.next(page) if page.get("next") else None
    return None


def playlist_track_ids(sp, playlist_id):
    ids, page = [], sp.playlist_items(playlist_id, additional_types=("track",))
    while page:
        for item in page["items"]:
            track = item.get("track") or {}
            if track.get("id"):
                ids.append(track["id"])
        page = sp.next(page) if page.get("next") else None
    return ids


def playlist_rows(sp, playlist_id):
    """(title, artist, album) rows for a playlist, in order."""
    rows, page = [], sp.playlist_items(playlist_id, additional_types=("track",))
    while page:
        for item in page["items"]:
            track = item.get("track") or {}
            if track.get("id"):
                rows.append((track["name"],
                             track["artists"][0]["name"] if track.get("artists") else "",
                             (track.get("album") or {}).get("name", "")))
        page = sp.next(page) if page.get("next") else None
    return rows


# -------------------------------------------------------------------- CSV ---

def read_rows(path):
    """Read (title, artist) rows. Accepts the TuneMyMusic-style header
    'Track name, Artist name, Album' (case-insensitive) or headerless
    two-column title,artist."""
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        first = next(reader, None)
        if first is None:
            die("%s is empty" % path)
        header = [c.strip().lower() for c in first]
        if "track name" in header:
            t_i, a_i = header.index("track name"), header.index("artist name")
        else:
            t_i, a_i = 0, 1
            if len(first) >= 2:
                rows.append((first[t_i].strip(), first[a_i].strip()))
        for row in reader:
            if len(row) >= 2 and row[t_i].strip():
                rows.append((row[t_i].strip(), row[a_i].strip()))
    return rows


def write_rows(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADER)
        writer.writerows(rows)


# ----------------------------------------------------------------- import ---

def cmd_import(args):
    rows = read_rows(args.csv)
    print("Read %d rows from %s" % (len(rows), args.csv))
    sp = spotify_client()

    resolved, misses = [], []
    for i, (title, artist) in enumerate(rows, 1):
        track = find_track(sp, title, artist)
        if track:
            resolved.append(track["id"])
            got = "%s - %s" % (track["artists"][0]["name"], track["name"])
            want = "%s - %s" % (artist, title)
            flag = "" if got.lower() == want.lower() else "   (matched: %s)" % got
            print("  %3d. %s%s" % (i, want, flag))
        else:
            misses.append((i, title, artist))
            warn("%3d. NO MATCH: %s - %s" % (i, artist, title))

    if misses:
        print("\n%d of %d rows had no match - fix titles or add by hand:" % (len(misses), len(rows)))
        for i, title, artist in misses:
            print("  row %d: %s - %s" % (i, artist, title))
    if args.dry_run:
        print("\n--dry-run: nothing written to Spotify.")
        return

    playlist_id = args.playlist_id
    if not playlist_id:
        title = args.title or os.path.splitext(os.path.basename(args.csv))[0]
        playlist_id = playlist_by_title(sp, title)
        if playlist_id:
            print("\nUsing existing playlist '%s'" % title)
        else:
            playlist_id = sp.user_playlist_create(
                sp.me()["id"], title, public=args.public,
                description="Imported by kimbo")["id"]
            print("\nCreated %s playlist '%s'" % ("public" if args.public else "private", title))

    if args.replace:
        sp.playlist_replace_items(playlist_id, [])
        existing = set()
    else:
        existing = set(playlist_track_ids(sp, playlist_id))

    to_add = [tid for tid in resolved if tid not in existing]
    seen = set()
    to_add = [t for t in to_add if not (t in seen or seen.add(t))]
    for start in range(0, len(to_add), 100):
        sp.playlist_add_items(playlist_id, to_add[start:start + 100])
    print("Added %d tracks (%d already present, %d unmatched)."
          % (len(to_add), len(resolved) - len(to_add), len(misses)))
    print("Playlist: https://open.spotify.com/playlist/" + playlist_id)


# ----------------------------------------------------------------- export ---

def cmd_export(args):
    sp = spotify_client()
    playlist_id = args.playlist_id or playlist_by_title(sp, args.title or "")
    if not playlist_id:
        die("playlist not found - pass --playlist-id or an exact --title you own")
    rows = playlist_rows(sp, playlist_id)
    write_rows(args.out, rows)
    print("Wrote %d rows to %s (TuneMyMusic-compatible)" % (len(rows), args.out))


# --------------------------------------------------------------- discover ---

def clean_lyrics(text):
    """Strip the scrape junk lyricsgenius leaves in: the '... Lyrics' header
    line and the trailing 'Embed' counter."""
    if not text:
        return ""
    lines = text.splitlines()
    if lines and lines[0].rstrip().endswith("Lyrics"):
        lines = lines[1:]
    text = "\n".join(lines)
    return re.sub(r"\d*Embed\s*$", "", text).strip()


def density(lyrics, terms):
    """Percent of lyric characters covered by whole-word term matches.
    Word boundaries stop 'oil' matching 'boiling' - the original's
    substring count did not."""
    text = lyrics.lower()
    if not text:
        return 0.0
    covered = 0
    for term in terms:
        pattern = r"\b" + re.escape(term.lower()) + r"\b"
        covered += len(re.findall(pattern, text)) * len(term)
    return covered / len(text) * 100


def genius_client():
    token = os.environ.get("GENIUS_TOKEN")
    if not token:
        die("GENIUS_TOKEN is not set (free at genius.com/api-clients)")
    try:
        import lyricsgenius
    except ImportError:
        die("lyricsgenius not installed: pip3 install -r requirements.txt")
    genius = lyricsgenius.Genius(token, verbose=False, retries=2)
    return genius


def cmd_discover(args):
    terms = args.matches or [args.query]
    print("Query: %r   scoring terms: %s   threshold: %.2f%%"
          % (args.query, terms, args.threshold))
    genius = genius_client()
    sp = None

    candidates = []          # (score, title, artist)
    scanned = 0

    def consider(title, artist, lyrics):
        nonlocal scanned
        scanned += 1
        lyrics = clean_lyrics(lyrics)
        if len(lyrics) < args.min_lyrics:
            return
        score = density(lyrics, terms)
        marker = "HIT " if score >= args.threshold else "    "
        print("  %s%5.2f%%  %s - %s" % (marker, score, artist, title))
        if score >= args.threshold:
            candidates.append((score, title, artist))

    if args.source in ("genius", "both"):
        for page in range(1, args.pages + 1):
            try:
                response = genius.search_songs(args.query, per_page=args.per_page, page=page)
            except Exception as exc:                       # network / scrape flake
                warn("genius search failed on page %d: %s" % (page, exc))
                break
            hits = response.get("hits", [])
            if not hits:
                break
            for hit in hits:
                result = hit["result"]
                try:
                    lyrics = genius.lyrics(song_url=result["url"]) or ""
                except Exception as exc:
                    warn("lyrics fetch failed for %s: %s" % (result.get("full_title"), exc))
                    continue
                consider(result["title"], result["primary_artist"]["name"], lyrics)

    if args.source in ("spotify", "both"):
        sp = sp or spotify_client()
        limit = 50
        for page in range(args.pages):
            result = sp.search(q="track:%s" % args.query, type="track",
                               limit=limit, offset=page * limit)  # offset in TRACKS (original passed a page counter)
            items = result.get("tracks", {}).get("items", [])
            if not items:
                break
            for track in items:
                title, artist = track["name"], track["artists"][0]["name"]
                try:
                    song = genius.search_song(title, artist=artist, get_full_info=False)
                except Exception:
                    song = None
                consider(title, artist, song.lyrics if song else "")

    candidates.sort(reverse=True)
    print("\n%d candidates from %d songs scanned." % (len(candidates), scanned))
    if args.dry_run or not candidates:
        return
    sp = sp or spotify_client()
    title = args.title or ("candidates - %s" % args.query)
    # Resolve and add, preserving score order.
    playlist_id = args.playlist_id or playlist_by_title(sp, title) or sp.user_playlist_create(
        sp.me()["id"], title, public=False,
        description="kimbo discover: %s" % args.query)["id"]
    existing = set(playlist_track_ids(sp, playlist_id))
    added = 0
    for score, song_title, artist in candidates:
        track = find_track(sp, song_title, artist)
        if track and track["id"] not in existing:
            sp.playlist_add_items(playlist_id, [track["id"]])
            existing.add(track["id"])
            added += 1
    print("Added %d to '%s' - it is a CANDIDATES list; curate before merging."
          % (added, title))
    print("Playlist: https://open.spotify.com/playlist/" + playlist_id)


# ----------------------------------------------------------------- enrich ---

def spotify_features(sp, track_ids):
    """Batch audio-features. Returns {} with a notice if the app lacks
    access - Spotify deprecated the endpoint on 2024-11-27 for apps
    without prior extended quota."""
    features = {}
    try:
        for start in range(0, len(track_ids), 100):
            batch = sp.audio_features(track_ids[start:start + 100]) or []
            for feat in batch:
                if feat:
                    key = "%s%s" % (PITCH_CLASSES[feat["key"]] if feat["key"] >= 0 else "?",
                                    "m" if feat.get("mode") == 0 else "")
                    features[feat["id"]] = (round(feat["tempo"]), key, "spotify")
    except Exception:
        warn("audio-features unavailable (endpoint deprecated 2024-11-27 for "
             "new apps) - falling back to GetSongBPM")
    return features


def getsongbpm_lookup(session, api_key, title, artist):
    url = "https://api.getsong.co/search/"
    params = {"api_key": api_key, "type": "both",
              "lookup": "song:%s artist:%s" % (title, artist)}
    try:
        response = session.get(url, params=params, timeout=15)
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        warn("getsongbpm failed for %s - %s: %s" % (artist, title, exc))
        return None
    hits = data.get("search")
    if not isinstance(hits, list) or not hits:
        return None
    hit = hits[0]
    tempo, key = hit.get("tempo"), hit.get("key_of")
    return (tempo, key, "getsongbpm") if tempo or key else None


def cmd_enrich(args):
    if not args.csv and not args.playlist_id:
        die("pass --csv or --playlist-id")
    sp = spotify_client() if args.playlist_id else None
    if args.playlist_id:
        rows3 = playlist_rows(sp, args.playlist_id)
        ids = playlist_track_ids(sp, args.playlist_id)
    else:
        rows3 = [(t, a, "") for t, a in read_rows(args.csv)]
        ids = []

    by_id = {}
    if ids and sp:
        by_id = spotify_features(sp, ids)

    api_key = os.environ.get("GETSONGBPM_KEY")
    session = requests.Session()
    out_rows = []
    for i, (title, artist, album) in enumerate(rows3):
        tempo, key, source = "", "", ""
        if ids and i < len(ids) and ids[i] in by_id:
            tempo, key, source = by_id[ids[i]]
        elif api_key:
            hit = getsongbpm_lookup(session, api_key, title, artist)
            if hit:
                tempo, key, source = hit
            time.sleep(0.6)          # be polite; free tier rate-limits
        out_rows.append([title, artist, album, tempo, key, source])
        print("  %-7s %-4s %s - %s" % (tempo or "-", key or "-", artist, title))

    out = args.out or ((args.csv or "playlist").rsplit(".csv", 1)[0] + "-enriched.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADER + ["Tempo", "Key", "Source"])
        writer.writerows(out_rows)
    filled = sum(1 for r in out_rows if r[3] or r[4])
    print("\nWrote %s (%d/%d rows enriched)." % (out, filled, len(out_rows)))
    if not api_key:
        warn("GETSONGBPM_KEY not set - only Spotify-side data was attempted. "
             "Free key at getsongbpm.com/api")
    else:
        print("Note: GetSongBPM's terms require a visible link back to "
              "getsongbpm.com wherever you publish their data.")


# -------------------------------------------------------------------- CLI ---

def main():
    parser = argparse.ArgumentParser(prog="kimbo", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import", help="CSV -> Spotify playlist, order preserved")
    p.add_argument("--csv", required=True)
    p.add_argument("--title", help="playlist name (default: CSV filename)")
    p.add_argument("--playlist-id", help="add to an existing playlist instead")
    p.add_argument("--public", action="store_true", help="create as public (default private)")
    p.add_argument("--replace", action="store_true", help="clear the playlist first")
    p.add_argument("--dry-run", action="store_true", help="resolve and report, write nothing")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("export", help="Spotify playlist -> CSV")
    p.add_argument("--playlist-id")
    p.add_argument("--title", help="exact name of a playlist you own")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("discover", help="Genius lyric-density search -> candidates")
    p.add_argument("--query", "-q", required=True)
    p.add_argument("--matches", "-m", nargs="*", help="terms to score (default: the query)")
    p.add_argument("--threshold", "-bt", type=float, default=1.5,
                   help="min %% of lyric characters covered (default 1.5)")
    p.add_argument("--pages", type=int, default=5, help="search pages to scan (bounded, default 5)")
    p.add_argument("--per-page", type=int, default=10)
    p.add_argument("--source", choices=["genius", "spotify", "both"], default="genius")
    p.add_argument("--title", help="candidates playlist name")
    p.add_argument("--playlist-id")
    p.add_argument("--min-lyrics", type=int, default=200,
                   help="skip lyric texts shorter than this many characters")
    p.add_argument("--dry-run", action="store_true", help="score and print, add nothing")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("enrich", help="add tempo/key columns")
    p.add_argument("--csv")
    p.add_argument("--playlist-id")
    p.add_argument("--out")
    p.set_defaults(func=cmd_enrich)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
