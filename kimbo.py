#!/usr/bin/env python3
"""kimbo - playlist plumbing for Spotify.

Redux of the old playlist-generator script. Six subcommands:

  setup     guided walkthrough: get every credential, store and test them
  import    CSV -> Spotify playlist (ordered, deduped, reports misses);
            point it at a directory to import every CSV in one run
  export    Spotify playlist -> CSV in the same format
  discover  Genius lyric-density search -> a candidates playlist or CSV
  enrich    add tempo/key columns via GetSongBPM (and Spotify
            audio-features, when your app still has access)
  resort    reorder a playlist in place by tempo, key (Camelot wheel),
            or key-then-tempo
  flow      chain tracks by key/tempo/energy so the playlist plays smoothly

Run `python3 kimbo.py <command> -h` for options. Credentials come from
environment variables; see .env.example.
"""

import argparse
import csv
import json
import math
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
        from spotipy.oauth2 import SpotifyOAuth, SpotifyPKCE
    except ImportError:
        die("spotipy not installed: pip3 install -r requirements.txt")
    for var in ("SPOTIPY_CLIENT_ID", "SPOTIPY_REDIRECT_URI"):
        if not os.environ.get(var):
            die("%s is not set - run `python3 kimbo.py setup` for a guided "
                "walkthrough (or copy .env.example to .env and fill it in; "
                ".env is loaded automatically)" % var)
    cache = os.path.expanduser("~/.cache/kimbo/spotify-token")
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    scope = "playlist-modify-public playlist-modify-private playlist-read-private"
    if os.environ.get("SPOTIPY_CLIENT_SECRET"):
        auth = SpotifyOAuth(scope=scope, cache_path=cache)
    else:
        # No secret on hand: PKCE proves identity with a one-time code
        # challenge instead. Spotify's recommended flow for desktop apps,
        # and it needs only the client ID.
        auth = SpotifyPKCE(scope=scope, cache_path=cache,
                           open_browser=True)
    return spotipy.Spotify(auth_manager=auth)


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


def create_playlist(sp, name, public=False, description=""):
    """POST /me/playlists. Spotify removed POST /users/{id}/playlists for
    Development Mode apps on 2026-03-09; spotipy's user_playlist_create
    still calls it and 403s. Use the new method when spotipy has one,
    else hit the endpoint directly."""
    for method in ("current_user_playlist_create", "me_playlist_create"):
        fn = getattr(sp, method, None)
        if fn:
            return fn(name, public=public, description=description)["id"]
    return sp._post("me/playlists", payload={"name": name, "public": public,
                                             "collaborative": False,
                                             "description": description})["id"]


def item_track(item):
    """The track inside a playlist-items row. The 2026 API renamed the
    field from 'track' to 'item'; accept both."""
    return item.get("item") or item.get("track") or {}


def playlist_track_ids(sp, playlist_id):
    ids, page = [], sp.playlist_items(playlist_id, additional_types=("track",))
    while page:
        for item in page["items"]:
            track = item_track(item)
            if track.get("id"):
                ids.append(track["id"])
        page = sp.next(page) if page.get("next") else None
    return ids


def playlist_rows(sp, playlist_id):
    """(title, artist, album) rows for a playlist, in order."""
    rows, page = [], sp.playlist_items(playlist_id, additional_types=("track",))
    while page:
        for item in page["items"]:
            track = item_track(item)
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


def title_from_filename(path, prefix=""):
    """'playlists/06-black-waters.csv' -> 'Black Waters'. Strips a leading
    NN- ordering prefix, un-dashes, and title-cases, leaving small words
    lowercase except at the start."""
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = re.sub(r"^\d+[-_]", "", stem)
    small = {"a", "an", "and", "the", "to", "of", "in", "on", "by"}
    parts = stem.replace("_", "-").split("-")
    words = []
    for i, word in enumerate(parts):
        minor = word.lower() in small and 0 < i < len(parts) - 1
        words.append(word if minor else word.capitalize())
    return (prefix + " " if prefix else "") + " ".join(words)


def import_one(args, sp, csv_path, title):
    rows = read_rows(csv_path)
    print("\nRead %d rows from %s" % (len(rows), csv_path))

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
        return 0, len(misses)

    playlist_id = args.playlist_id
    if not playlist_id:
        playlist_id = playlist_by_title(sp, title)
        if playlist_id:
            print("\nUsing existing playlist '%s'" % title)
        else:
            playlist_id = create_playlist(sp, title, public=args.public,
                                          description="Imported by kimbo")
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
    if getattr(args, "resort", None):
        resort_playlist(sp, playlist_id, args.resort, analyze=getattr(args, "analyze", False))
    return len(to_add), len(misses)


def cmd_import(args):
    """Dispatch one CSV or every CSV in a directory."""
    if os.path.isdir(args.csv):
        paths = sorted(os.path.join(args.csv, n) for n in os.listdir(args.csv)
                       if n.lower().endswith(".csv"))
        if not paths:
            die("no .csv files in %s" % args.csv)
        if args.title:
            die("--title makes no sense for a directory - titles come from "
                "the filenames (use --prefix to namespace them)")
        if args.playlist_id:
            die("--playlist-id makes no sense for a directory - it would pile "
                "every playlist into one")
        print("Importing %d playlists from %s\n" % (len(paths), args.csv))
        for path in paths:
            print("  %-42s -> %s" % (os.path.basename(path),
                                     title_from_filename(path, args.prefix)))
        summary = []
        for path in paths:
            title = title_from_filename(path, args.prefix)
            print("\n" + "=" * 60 + "\n%s" % title)
            result = import_one(args, sp_shared(args), path, title)
            summary.append((title,) + (result or (0, 0)))
        print("\n" + "=" * 60 + "\n%s" % ("Dry run complete - nothing written."
                                          if args.dry_run else "Done."))
        for title, added, missed in summary:
            print("  %-34s %3d %s, %d unmatched"
                  % (title, added, "would add" if args.dry_run else "added", missed))
        if args.dry_run:
            print("\nRe-run without --dry-run to create these playlists.")
        return
    title = args.title or title_from_filename(args.csv, args.prefix)
    import_one(args, sp_shared(args), args.csv, title)


_SP = []


def sp_shared(args):
    """One authenticated client reused across a directory import."""
    if not _SP:
        _SP.append(spotify_client())
    return _SP[0]


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
        die("GENIUS_TOKEN is not set - run `python3 kimbo.py setup` "
            "(free token at genius.com/api-clients)")
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
    playlist_id = args.playlist_id or playlist_by_title(sp, title) or create_playlist(
        sp, title, public=False, description="kimbo discover: %s" % args.query)
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



# ---------------------------------------------------------------- analyze ---
# Fallback for tracks GetSongBPM doesn't know: fetch Deezer's keyless 30-second
# preview and compute tempo + key locally. Spotify computed these for its whole
# catalog and stopped serving them (Nov 2024); this is the do-it-yourself route.
# Measured: band recordings come back within a few BPM and the right key at
# 0.7-0.8 confidence; solo fingerpicked blues (no drummer) beat-tracks badly,
# so low-confidence keys are blanked rather than trusted.

KK_MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
KK_MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
ANALYZE_MIN_CONF = 0.5


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def deezer_preview(session, title, artist):
    """30-second preview URL for the studio recording, or None. Gates on the
    artist and rejects live/karaoke/tribute hits unless the title asks."""
    try:
        response = session.get("https://api.deezer.com/search",
                               params={"q": "%s %s" % (title, artist), "limit": 8}, timeout=15)
        hits = response.json().get("data") or []
    except (requests.RequestException, ValueError):
        return None
    want_artist, want_title = _norm(artist), _norm(title)
    bad = ("live", "karaoke", "tribute", "cover", "instrumental")
    for hit in hits:
        got_artist = _norm((hit.get("artist") or {}).get("name"))
        got_title = _norm(hit.get("title"))
        if not hit.get("preview"):
            continue
        if not (want_artist in got_artist or got_artist in want_artist):
            continue
        if any(b in got_title and b not in want_title for b in bad):
            continue
        if want_title not in got_title and got_title not in want_title:
            continue
        return hit["preview"]
    return None


def analyze_preview(session, url):
    """(tempo, key, confidence) from a preview MP3, or None."""
    try:
        import numpy as np
        import librosa
    except ImportError:
        die("--analyze needs librosa: pip3 install librosa soundfile")
    import tempfile, warnings
    warnings.filterwarnings("ignore")
    try:
        audio = session.get(url, timeout=30).content
    except requests.RequestException:
        return None
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(audio)
        path = f.name
    try:
        y, sr = librosa.load(path, sr=22050, mono=True)
    except Exception:
        return None
    finally:
        os.unlink(path)
    tempo = float(np.atleast_1d(librosa.feature.tempo(y=y, sr=sr))[0])
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr).mean(axis=1)
    best = (-2.0, "")
    for shift in range(12):
        for suffix, profile in (("", KK_MAJOR), ("m", KK_MINOR)):
            score = float(np.corrcoef(np.roll(profile, shift), chroma)[0, 1])
            if score > best[0]:
                best = (score, PITCH_CLASSES[shift] + suffix)
    return round(tempo), best[1], round(best[0], 2)


def analyze_track(session, title, artist):
    """(tempo, key, source) or None. Low-confidence keys are blanked."""
    url = deezer_preview(session, title, artist)
    if not url:
        return None
    result = analyze_preview(session, url)
    if not result:
        return None
    tempo, key, conf = result
    if conf < ANALYZE_MIN_CONF:
        key = ""
    return str(tempo), key, "analyzed(%.2f)" % conf


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


BPM_CACHE_PATH = os.path.expanduser("~/.cache/kimbo/getsongbpm-cache.json")


def load_bpm_cache(path=None):
    """Remembered lookups: {"title|artist": [tempo, key]}.

    Grown one real playlist at a time - which is the honest version of
    "just scrape every song ever"."""
    try:
        with open(path or BPM_CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (IOError, OSError, ValueError):
        return {}


def save_bpm_cache(cache, path=None):
    """Best effort - a cache we cannot write is not worth failing a run for."""
    path = path or BPM_CACHE_PATH
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            json.dump(cache, f)
    except (IOError, OSError) as exc:
        warn("could not write %s: %s" % (path, exc))


def getsongbpm_notice(api_key):
    """The credit GetSongBPM's terms require, or the nudge to get a key."""
    if not api_key:
        warn("GETSONGBPM_KEY not set - only Spotify-side data was attempted. "
             "Free key at getsongbpm.com/api")
    else:
        print("Note: GetSongBPM's terms require a visible link back to "
              "getsongbpm.com wherever you publish their data.")


def gather_tempo_key(sp, rows3, ids, analyze=False):
    """(tempo, key, source) per row, aligned with rows3.

    Spotify's own features first for grandfathered apps, then the local
    cache, then a GetSongBPM lookup, then (with analyze=True) a Deezer
    preview analyzed locally."""
    by_id = {}
    if ids and sp:
        by_id = spotify_features(sp, ids)

    api_key = os.environ.get("GETSONGBPM_KEY")
    cache = load_bpm_cache() if (api_key or analyze) else {}
    session = requests.Session()
    found, learned = [], False
    for i, (title, artist, album) in enumerate(rows3):
        tempo, key, source = "", "", ""
        if ids and i < len(ids) and ids[i] in by_id:
            tempo, key, source = by_id[ids[i]]
        elif api_key:
            slot = ("%s|%s" % (title, artist)).lower()
            if slot in cache:
                tempo, key, source = cache[slot][0], cache[slot][1], "cache"
            else:
                hit = getsongbpm_lookup(session, api_key, title, artist)
                if hit:
                    tempo, key, source = hit
                    cache[slot], learned = [tempo, key], True
                time.sleep(0.6)          # be polite; free tier rate-limits
        if not (tempo or key) and analyze:
            slot = ("%s|%s" % (title, artist)).lower()
            if slot in cache and cache[slot][0]:
                tempo, key, source = cache[slot][0], cache[slot][1], "cache"
            else:
                hit = analyze_track(session, title, artist)
                if hit:
                    tempo, key, source = hit
                    cache[slot], learned = [tempo, key], True
        found.append((tempo, key, source))
        print("  %-7s %-4s %s - %s" % (tempo or "-", key or "-", artist, title))
    if learned:
        save_bpm_cache(cache)
    return found


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

    out_rows = [[title, artist, album, tempo, key, source]
                for (title, artist, album), (tempo, key, source)
                in zip(rows3, gather_tempo_key(sp, rows3, ids, analyze=args.analyze))]

    out = args.out or ((args.csv or "playlist").rsplit(".csv", 1)[0] + "-enriched.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADER + ["Tempo", "Key", "Source"])
        writer.writerows(out_rows)
    filled = sum(1 for r in out_rows if r[3] or r[4])
    print("\nWrote %s (%d/%d rows enriched)." % (out, filled, len(out_rows)))
    getsongbpm_notice(os.environ.get("GETSONGBPM_KEY"))



# ------------------------------------------------------------------- flow ---

# Enharmonic flats -> the sharp names used in PITCH_CLASSES.
FLAT_TO_SHARP = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#",
                 "Bb": "A#", "Cb": "B", "Fb": "E"}

# (root as sharp name, is_minor) -> Camelot code. The full 24-key wheel:
# neighbours on the wheel (+/-1, or the same number in the other letter)
# are the pairs DJs treat as harmonically compatible.
CAMELOT = {
    ("G#", True): "1A",  ("B", False): "1B",
    ("D#", True): "2A",  ("F#", False): "2B",
    ("A#", True): "3A",  ("C#", False): "3B",
    ("F", True): "4A",   ("G#", False): "4B",
    ("C", True): "5A",   ("D#", False): "5B",
    ("G", True): "6A",   ("A#", False): "6B",
    ("D", True): "7A",   ("F", False): "7B",
    ("A", True): "8A",   ("C", False): "8B",
    ("E", True): "9A",   ("G", False): "9B",
    ("B", True): "10A",  ("D", False): "10B",
    ("F#", True): "11A", ("A", False): "11B",
    ("C#", True): "12A", ("E", False): "12B",
}

FLOW_W_KEY = 1.0         # weight: harmonic compatibility
FLOW_W_BPM = 1.0         # weight: tempo distance
FLOW_W_ARC = 0.8         # weight: fit to the energy arc
FLOW_UNKNOWN_PEN = 0.3   # neutral penalty when key or tempo is unknown
FLOW_SMOOTH_KEY = 0.25   # max key penalty still called "smooth"
FLOW_SMOOTH_BPM = 0.09   # max tempo log2-distance still called "smooth" (~6%)

# Energy curve per arc as (position 0..1, energy 1..5) breakpoints, linearly
# interpolated. The arc is the part you pick per occasion; the key and tempo
# math below is the same either way.
ARC_BREAKPOINTS = {
    "flat":   None,                                   # no arc: pure key/tempo chaining
    "steady": [(0.0, 3.0), (1.0, 3.0)],               # hold one level (bike ride)
    "party":  [(0.0, 2.0), (0.15, 3.0), (0.7, 5.0),   # warm up, build, peak,
               (0.85, 5.0), (1.0, 2.5)],              # wind down
    "chill":  [(0.0, 3.5), (1.0, 1.5)],               # gentle descent
    "build":  [(0.0, 1.5), (1.0, 5.0)],               # straight climb (workout)
}

FLOW_CSV_HEADER = CSV_HEADER + ["Tempo", "Key", "Camelot", "Energy",
                                "Vibe", "Source"]

# Handed to whatever assistant the user already has. Tempo and key are
# lookups; how a track FEELS is the judgment call, so we ask for it rather
# than pretending a number implies it.
FLOW_TAG_PROMPT = """You are tagging songs for playlist ordering. For each track in the CSV
below, fill in the Energy column with a rating from 1 (sleepy, ambient)
to 5 (peak, floor-filling), judging how the track FEELS, not how fast it
is - a delicate piano piece can be fast and still low energy. Fill in the
Vibe column with one word (e.g. dreamy, swagger, sunshine, brooding).
Reply with ONLY the completed CSV: same columns, same row order, nothing
else. If you don't know a track, rate it 3 and guess the vibe from the
title."""


def normalize_key(raw):
    """('A#', True) for 'Bbm'. None when missing or unparseable.

    Accepts what GetSongBPM and djay screenshots actually contain: flats,
    unicode accidentals, and 'minor'/'min'/'m' spellings."""
    if raw is None:
        return None
    text = str(raw).replace("\u266f", "#").replace("\u266d", "b").strip()
    if not text or text == "?":
        return None
    is_minor = False
    lowered = text.lower()
    for suffix in ("minor", "min", "m"):        # longest first
        if lowered.endswith(suffix):
            text, is_minor = text[:-len(suffix)], True
            break
    else:
        for suffix in ("major", "maj"):
            if lowered.endswith(suffix):
                text = text[:-len(suffix)]
                break
    text = text.strip()
    if not text:
        return None
    root = text[0].upper() + text[1:].lower()
    root = FLAT_TO_SHARP.get(root, root)
    return (root, is_minor) if root in PITCH_CLASSES else None


def to_camelot(raw):
    """'Am' -> '8A'. None when the key is missing or unparseable."""
    parts = normalize_key(raw)
    return CAMELOT.get(parts) if parts else None


def camelot_parts(code):
    """'11A' -> (11, 'A')."""
    return int(code[:-1]), code[-1]


def key_penalty(camelot_a, camelot_b):
    """0.0 for the same key, rising with distance around the wheel.

    An unknown key on either side scores FLOW_UNKNOWN_PEN - worse than a
    neighbour, better than the far side of the wheel."""
    if not camelot_a or not camelot_b:
        return FLOW_UNKNOWN_PEN
    num_a, letter_a = camelot_parts(camelot_a)
    num_b, letter_b = camelot_parts(camelot_b)
    ring = min((num_a - num_b) % 12, (num_b - num_a) % 12)
    return min(1.0, 0.15 * ring + 0.25 * (0 if letter_a == letter_b else 1))


def bpm_gap(tempo_a, tempo_b):
    """(log2 distance, ratio) for the closest of half, same, or double time.

    174 into 87 is a clean beatmatch rather than a jarring jump - the real
    DJ move that a naive tempo sort mistakes for whiplash. Callers must
    check both tempos are known and positive first; this does not."""
    best = None
    for ratio in (0.5, 1.0, 2.0):
        dist = abs(math.log2(tempo_b * ratio / tempo_a))
        if best is None or dist < best[0] - 1e-9:
            best = (dist, ratio)
        elif abs(dist - best[0]) <= 1e-9 and ratio == 1.0:
            best = (dist, ratio)          # a straight match wins a tie
    return best


def bpm_penalty(tempo_a, tempo_b):
    """0.0 for an exact (or half/double) match, 1.0 for a wide jump."""
    if not tempo_a or not tempo_b or tempo_a <= 0 or tempo_b <= 0:
        return FLOW_UNKNOWN_PEN
    return min(1.0, bpm_gap(tempo_a, tempo_b)[0] / 0.5)

def arc_targets(name, n):
    """Target energy for each of n slots, or n Nones for the 'flat' arc."""
    breakpoints = ARC_BREAKPOINTS[name]
    if breakpoints is None:
        return [None] * n
    targets = []
    for i in range(n):
        position = 0.0 if n == 1 else float(i) / (n - 1)
        energy = breakpoints[-1][1]           # past the end: hold the last
        for (pos_a, energy_a), (pos_b, energy_b) in zip(breakpoints, breakpoints[1:]):
            if position <= pos_b:
                frac = 0.0 if pos_b == pos_a else (position - pos_a) / (pos_b - pos_a)
                energy = energy_a + (energy_b - energy_a) * frac
                break
        targets.append(energy)
    return targets


def effective_energies(tracks):
    """Per-track energy on a 1-5 scale.

    A tagged Energy column always wins. Without one we fall back to a crude
    tempo rank, which is exactly the judgment tempo cannot make: a delicate
    155 BPM piano piece reads as high energy here and is not."""
    n = len(tracks)
    if any(track.get("energy") is not None for track in tracks):
        return [float(min(5.0, max(1.0, track["energy"])))
                if track.get("energy") is not None else 3.0 for track in tracks]
    known = [i for i in range(n) if tracks[i].get("tempo")]
    if not known:
        return [3.0] * n
    energies = [3.0] * n
    for rank, i in enumerate(sorted(known, key=lambda i: (tracks[i]["tempo"], i))):
        energies[i] = (3.0 if len(known) == 1
                       else 1.0 + 4.0 * rank / (len(known) - 1))
    return energies


def transition_cost(prev, cand, cand_energy, slot_target):
    """What it costs to play cand straight after prev in this arc slot."""
    cost = FLOW_W_KEY * key_penalty(prev["camelot"], cand["camelot"])
    cost += FLOW_W_BPM * bpm_penalty(prev["tempo"], cand["tempo"])
    if slot_target is not None:
        cost += FLOW_W_ARC * abs(cand_energy - slot_target) / 4.0
    return cost


def order_tracks(tracks, arc):
    """Input indices in play order.

    Greedy: from each track take the cheapest next one. Not optimal - that
    is a travelling-salesman problem - but deterministic and good enough to
    hear. Ties go to the lowest index."""
    n = len(tracks)
    if n <= 1:
        return list(range(n))
    targets = arc_targets(arc, n)
    energies = effective_energies(tracks)
    if arc == "flat":
        start = 0                              # keep the opener the user chose
    else:
        start = min(range(n), key=lambda i: (abs(energies[i] - targets[0]), i))
    order, used = [start], set([start])
    for slot in range(1, n):
        prev, best, best_cost = tracks[order[-1]], None, None
        for i in range(n):
            if i in used:
                continue
            cost = transition_cost(prev, tracks[i], energies[i], targets[slot])
            if best_cost is None or cost < best_cost - 1e-9:
                best, best_cost = i, cost
        order.append(best)
        used.add(best)
    return order

def _flow_float(text):
    """'98' -> 98.0; blank or junk -> None. Missing data stays missing."""
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def _flow_num(value):
    """98.0 -> '98', None -> ''."""
    return "" if value is None else "%g" % value


def read_enriched_rows(path):
    """Track dicts from a CSV.

    Everything past title and artist is optional: a plain two-column list
    reads fine, it just has nothing to order by until `enrich` has run."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    if not rows:
        die("%s is empty" % path)
    header = [c.strip().lower() for c in rows[0]]
    if "track name" in header:
        index = dict((name, i) for i, name in enumerate(header))
        body = rows[1:]
    else:
        index = {"track name": 0, "artist name": 1}
        body = rows                      # headerless: row 1 is a track
    tracks = []
    for row in body:
        cell = lambda name: (row[index[name]].strip()
                             if name in index and index[name] < len(row) else "")
        title = cell("track name")
        if not title:
            continue
        key = cell("key")
        tracks.append({"title": title, "artist": cell("artist name"),
                       "album": cell("album"), "tempo": _flow_float(cell("tempo")),
                       "key": key, "camelot": to_camelot(key),
                       "energy": _flow_float(cell("energy")),
                       "vibe": cell("vibe"), "source": cell("source")})
    return tracks


def join_quality(prev, cand):
    """(note, smooth, comparable) for the join from prev into cand.

    Missing data is checked first and reported as '?' rather than guessed:
    bpm_gap does arithmetic on both tempos and cannot take a None."""
    have_key = bool(prev["camelot"] and cand["camelot"])
    have_tempo = bool(prev["tempo"] and cand["tempo"]
                      and prev["tempo"] > 0 and cand["tempo"] > 0)
    key_ok = (have_key and
              key_penalty(prev["camelot"], cand["camelot"]) <= FLOW_SMOOTH_KEY)
    tempo_ok, ratio = False, 1.0
    if have_tempo:
        distance, ratio = bpm_gap(prev["tempo"], cand["tempo"])
        tempo_ok = distance <= FLOW_SMOOTH_BPM
    if have_key and have_tempo and key_ok and tempo_ok:
        return ("smooth (half/double-time)" if ratio != 1.0 else "smooth",
                True, True)
    problems = []
    problems.append("key ?" if not have_key else ("key jump" if not key_ok else ""))
    problems.append("tempo ?" if not have_tempo
                    else ("tempo jump" if not tempo_ok else ""))
    return ", ".join(p for p in problems if p), False, have_key and have_tempo


def rough_transitions(tracks, order):
    """How many joins are bumpy among those we can actually judge."""
    rough = 0
    for before, after in zip(order, order[1:]):
        _, smooth, comparable = join_quality(tracks[before], tracks[after])
        if comparable and not smooth:
            rough += 1
    return rough


def flow_report(tracks, order, arc):
    """Printable lines: the play order, and how each join sounds."""
    lines = ["Flow order (arc: %s):" % arc]
    smooth_total = 0
    for slot, i in enumerate(order):
        track, note = tracks[i], ""
        if slot:
            note, smooth, _ = join_quality(tracks[order[slot - 1]], track)
            smooth_total += 1 if smooth else 0
        label = "%s - %s" % (track["artist"], track["title"])
        if len(label) > 38:
            label = label[:35] + "..."
        lines.append("  %3d. %-38s %5s %-4s %-4s %-3s %s"
                     % (slot + 1, label, _flow_num(track["tempo"]) or "-",
                        track["key"] or "-", track["camelot"] or "-",
                        _flow_num(track["energy"]) or "-", note))
    lines.append("")
    lines.append("Transitions: %d total, %d smooth"
                 % (max(len(order) - 1, 0), smooth_total))
    lines.append("Rough transitions: input order %d -> flow order %d"
                 % (rough_transitions(tracks, list(range(len(tracks)))),
                    rough_transitions(tracks, order)))
    return lines


def write_flow_csv(path, tracks, order):
    """The reordered list, ready to hand back to `import`."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(FLOW_CSV_HEADER)
        for i in order:
            track = tracks[i]
            writer.writerow([track["title"], track["artist"], track["album"],
                             _flow_num(track["tempo"]), track["key"],
                             track["camelot"] or "", _flow_num(track["energy"]),
                             track["vibe"], track["source"]])


def write_tagging_prompt(source, out):
    """The prompt block, then the CSV with Energy/Vibe columns to fill in."""
    with open(source, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    if not rows:
        die("%s is empty" % source)
    if "track name" in [c.strip().lower() for c in rows[0]]:
        head, body = list(rows[0]), [list(r) for r in rows[1:]]
    else:
        head, body = ["Track name", "Artist name"], [list(r) for r in rows]
    present = [c.strip().lower() for c in head]
    for column in ("Energy", "Vibe"):
        if column.lower() not in present:
            head.append(column)
    with open(out, "w", newline="", encoding="utf-8") as f:
        f.write(FLOW_TAG_PROMPT + "\n\n")
        writer = csv.writer(f)
        writer.writerow(head)
        for row in body:
            writer.writerow(row + [""] * (len(head) - len(row)))


def slugify(name):
    """'Garden Party!' -> 'garden-party', for use in a filename."""
    slug = "".join(c for c in name.lower().replace(" ", "-")
                   if c.isalnum() or c in "-_")
    return slug.strip("-") or "playlist"


def add_in_batches(sp, playlist_id, track_ids):
    """Spotify takes 100 items per call."""
    for start in range(0, len(track_ids), 100):
        sp.playlist_add_items(playlist_id, track_ids[start:start + 100])


def flow_write_back(sp, args, name, tracks, order):
    """Push the new order to Spotify - only when explicitly asked."""
    ordered_ids = [tracks[i]["spotify_id"] for i in order
                   if tracks[i].get("spotify_id")]
    dropped = len(order) - len(ordered_ids)
    if args.apply:
        new_name = "%s (flow)" % name
        new_id = create_playlist(sp, new_name, public=False,
            description="kimbo flow: %s arc" % args.arc)
        add_in_batches(sp, new_id, ordered_ids)
        if dropped:
            warn("%d track(s) had no Spotify id and were left out" % dropped)
        print("Created private playlist '%s' with %d tracks."
              % (new_name, len(ordered_ids)))
        print("Playlist: https://open.spotify.com/playlist/" + new_id)
        return

    # --in-place overwrites the playlist and Spotify keeps no undo, so the
    # original order goes to disk before anything is touched.
    backup = slugify(name) + "-before-flow.csv"
    try:
        write_flow_csv(backup, tracks, list(range(len(tracks))))
    except (IOError, OSError) as exc:
        die("could not write the backup %s (%s) - refusing to overwrite "
            "the playlist" % (backup, exc))
    print("Original order saved to %s" % backup)
    if dropped:
        warn("%d track(s) have no Spotify id and will be dropped" % dropped)
    sp.playlist_replace_items(args.playlist_id, ordered_ids[:100])
    add_in_batches(sp, args.playlist_id, ordered_ids[100:])
    print("Reordered '%s' in place (%d tracks)." % (name, len(ordered_ids)))
    print("Playlist: https://open.spotify.com/playlist/" + args.playlist_id)


def flow_from_playlist(args):
    """Pull a playlist, look up tempo and key, return (name, tracks, sp)."""
    sp = spotify_client()
    rows3 = playlist_rows(sp, args.playlist_id)
    ids = playlist_track_ids(sp, args.playlist_id)
    name = sp.playlist(args.playlist_id)["name"]
    if not rows3:
        die("playlist '%s' has no tracks" % name)
    print("Reading tempo and key for %d tracks in '%s'..." % (len(rows3), name))
    found = gather_tempo_key(sp, rows3, ids, analyze=getattr(args, "analyze", False))
    getsongbpm_notice(os.environ.get("GETSONGBPM_KEY"))

    tracks = []
    for i, ((title, artist, album), (tempo, key, source)) in enumerate(zip(rows3, found)):
        tracks.append({"title": title, "artist": artist, "album": album,
                       "tempo": _flow_float(tempo), "key": key or "",
                       "camelot": to_camelot(key), "energy": None, "vibe": "",
                       "source": source,
                       "spotify_id": ids[i] if i < len(ids) else None})
    print("\nNo Energy column here, so tempo stands in for how a track feels.")
    print("For vibe-aware ordering: export, enrich, flow --tag-prompt, then flow.")
    return sp, name, tracks


def cmd_flow(args):
    if args.prefix:
        if args.csv or args.playlist_id or args.tag_prompt or args.out:
            die("--prefix is a batch over playlists you own; combine it only with "
                "--arc, --apply/--in-place, --analyze")
        sp = spotify_client()
        targets = playlists_with_prefix(sp, args.prefix)
        if not targets:
            die("no playlists you own start with %r" % args.prefix)
        print("flow over %d playlists starting with %r (arc: %s):" % (len(targets), args.prefix, args.arc))
        for _, name in targets:
            print("  " + name)
        for pid, name in targets:
            print("\n" + "=" * 60 + "\n%s" % name)
            args.playlist_id = pid
            cmd_flow_one(args)
        args.playlist_id = None
        return
    cmd_flow_one(args)


def cmd_flow_one(args):
    if bool(args.csv) == bool(args.playlist_id):
        die("pass exactly one of --csv or --playlist-id")
    if args.tag_prompt and args.playlist_id:
        die("--tag-prompt needs a CSV - run `export` then `enrich` first")
    if args.apply and args.in_place:
        die("pass --apply or --in-place, not both")
    if args.csv and (args.apply or args.in_place):
        die("--apply and --in-place need --playlist-id")

    if args.tag_prompt:
        out = args.out or (args.csv.rsplit(".csv", 1)[0] + "-tagging.txt")
        write_tagging_prompt(args.csv, out)
        print("Wrote %s" % out)
        print("Paste it to any assistant, save the reply as a CSV, then run:")
        print("  python3 kimbo.py flow --csv <that file>")
        return

    sp = name = None
    if args.playlist_id:
        sp, name, tracks = flow_from_playlist(args)
        default_out = slugify(name) + "-flow.csv"
    else:
        tracks = read_enriched_rows(args.csv)
        if not tracks:
            die("no tracks found in %s" % args.csv)
        if not any(track["tempo"] or track["camelot"] for track in tracks):
            warn("no tempo or key in %s - run enrich first for harmonic "
                 "ordering" % args.csv)
        default_out = args.csv.rsplit(".csv", 1)[0] + "-flow.csv"

    order = order_tracks(tracks, args.arc)
    for line in flow_report(tracks, order, args.arc):
        print(line)
    out = args.out or default_out
    write_flow_csv(out, tracks, order)
    print("\nWrote %s (%d tracks in play order)." % (out, len(order)))

    if args.apply or args.in_place:
        flow_write_back(sp, args, name, tracks, order)


# ------------------------------------------------------------------ setup ---

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

ENV_TEMPLATE = """# kimbo credentials - written by `python3 kimbo.py setup`. Git-ignored.
SPOTIPY_CLIENT_ID=%(SPOTIPY_CLIENT_ID)s
SPOTIPY_CLIENT_SECRET=%(SPOTIPY_CLIENT_SECRET)s
SPOTIPY_REDIRECT_URI=%(SPOTIPY_REDIRECT_URI)s
GENIUS_TOKEN=%(GENIUS_TOKEN)s
GETSONGBPM_KEY=%(GETSONGBPM_KEY)s
"""

ENV_VARS = ["SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET", "SPOTIPY_REDIRECT_URI",
            "GENIUS_TOKEN", "GETSONGBPM_KEY"]


def load_env_file():
    """Load .env sitting next to this script. Never overrides real env."""
    if not os.path.exists(ENV_PATH):
        return
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                if value.strip():
                    os.environ.setdefault(key.strip(), value.strip())


def _masked(value):
    return (value[:4] + "..." + value[-2:]) if value and len(value) > 8 else            ("(set)" if value else "(not set)")


def _ask(label, current):
    got = input("  %s [%s]: " % (label, _masked(current))).strip()
    return got or current


def _check_genius(token):
    try:
        response = requests.get("https://api.genius.com/search",
                                params={"q": "test"}, timeout=15,
                                headers={"Authorization": "Bearer " + token})
        return response.status_code == 200
    except requests.RequestException:
        return False


def _check_getsongbpm(key):
    try:
        response = requests.get("https://api.getsong.co/search/", timeout=15,
                                params={"api_key": key, "type": "song", "lookup": "sixteen tons"})
        data = response.json()
        return response.status_code == 200 and isinstance(data.get("search"), list)
    except (requests.RequestException, ValueError):
        return False


def _validate(values, try_spotify):
    print("\nChecking what's configured...")
    if values["GENIUS_TOKEN"]:
        print("  Genius token:    %s" % ("OK" if _check_genius(values["GENIUS_TOKEN"]) else "FAILED - recheck it"))
    else:
        print("  Genius token:    skipped (only needed for `discover`)")
    if values["GETSONGBPM_KEY"]:
        print("  GetSongBPM key:  %s" % ("OK" if _check_getsongbpm(values["GETSONGBPM_KEY"]) else "FAILED - recheck it"))
    else:
        print("  GetSongBPM key:  skipped (only needed for `enrich`)")
    if not values["SPOTIPY_CLIENT_ID"]:
        print("  Spotify:         NOT CONFIGURED - every command needs a Client ID")
        return
    mode = "client secret" if values["SPOTIPY_CLIENT_SECRET"] else "PKCE (no secret)"
    print("  Spotify auth:    %s" % mode)
    if try_spotify and input("  Test Spotify auth now? Opens a browser once. [y/N]: ").strip().lower() == "y":
        try:
            me = spotify_client().me()
            print("  Spotify:         OK - authenticated as %s" % (me.get("display_name") or me["id"]))
        except Exception as exc:
            print("  Spotify:         FAILED - %s" % exc)
            print("    Most common cause: the redirect URI in your app settings does not")
            print("    EXACTLY match %s" % values["SPOTIPY_REDIRECT_URI"])
    else:
        print("  Spotify:         credentials present (auth tested on first real command)")


def cmd_setup(args):
    values = {var: os.environ.get(var, "") for var in ENV_VARS}
    if not values["SPOTIPY_REDIRECT_URI"]:
        values["SPOTIPY_REDIRECT_URI"] = "http://127.0.0.1:8888/callback"

    if args.check:
        _validate(values, try_spotify=True)
        return

    print("kimbo setup - three credentials, ~5 minutes. Enter keeps the current value.\n")

    print("STEP 1 of 3 - Spotify (required: every command talks to your account)")
    print("  1. Open https://developer.spotify.com/dashboard and log in with your normal Spotify account")
    print("  2. 'Create app' - name and description can be anything (e.g. kimbo)")
    print("  3. Redirect URI: enter EXACTLY  http://127.0.0.1:8888/callback  and click Add.")
    print("     Spotify rejects 'localhost' for new apps - it must be this loopback form or HTTPS.")
    print("  4. Tick 'Web API', save, then open the app's Settings")
    print("  5. Copy the Client ID from the Settings page")
    print("")
    print("  The Client Secret is OPTIONAL - press Enter to skip it and kimbo")
    print("  uses PKCE instead (Spotify's recommended flow for desktop apps;")
    print("  nothing secret to store). If you do want it: on that same")
    print("  Settings page, under the Client ID, click 'View client secret'.\n")
    values["SPOTIPY_CLIENT_ID"] = _ask("Client ID", values["SPOTIPY_CLIENT_ID"])
    values["SPOTIPY_CLIENT_SECRET"] = _ask("Client Secret (optional - Enter to use PKCE)",
                                           values["SPOTIPY_CLIENT_SECRET"])
    values["SPOTIPY_REDIRECT_URI"] = _ask("Redirect URI", values["SPOTIPY_REDIRECT_URI"])

    print("\nSTEP 2 of 3 - Genius (only for `discover`; press Enter to skip)")
    print("  1. Open https://genius.com/api-clients and sign in (free account)")
    print("  2. 'New API Client' - app name anything; app website can be https://example.com")
    print("  3. Copy the CLIENT ACCESS TOKEN (the long one - not the ID or secret)\n")
    values["GENIUS_TOKEN"] = _ask("Genius client access token", values["GENIUS_TOKEN"])

    print("\nSTEP 3 of 3 - GetSongBPM (only for `enrich`; press Enter to skip)")
    print("  1. Open https://getsongbpm.com/api and register - the key arrives by email")
    print("  2. Their terms require a visible link back to getsongbpm.com wherever")
    print("     you publish their tempo/key data\n")
    values["GETSONGBPM_KEY"] = _ask("GetSongBPM API key", values["GETSONGBPM_KEY"])

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write(ENV_TEMPLATE % values)
    os.chmod(ENV_PATH, 0o600)
    for var in ENV_VARS:                      # make new values live for validation
        if values[var]:
            os.environ[var] = values[var]
    print("\nWrote %s (git-ignored; loaded automatically on every run)." % ENV_PATH)
    _validate(values, try_spotify=True)
    print("\nDone. Try: python3 kimbo.py import --csv examples/oil-anti-canon.csv --dry-run")



# ----------------------------------------------------------------- resort ---

def camelot(key_str):
    """(number 1-12, letter A=minor/B=major) for a key name, or None.

    Delegates to the flow section's converter so the two commands cannot
    drift apart - they had separate FLAT_TO_SHARP tables with different
    capitalisation, and whichever loaded second silently broke the other's
    flat keys."""
    code = to_camelot(key_str)
    return camelot_parts(code) if code else None


def sort_key(row, by):
    """row = (title, artist, album, tempo, key). Unknowns sort last, stably."""
    tempo = None
    try:
        tempo = float(row[3])
    except (TypeError, ValueError):
        pass
    cam = camelot(row[4])
    big = (999, "Z")
    if by == "tempo":
        return (tempo is None, tempo or 0)
    if by == "key":
        return (cam is None, cam or big)
    return (cam is None and tempo is None, cam or big, tempo or 0)   # key-tempo


def resort_playlist(sp, playlist_id, by, desc=False, dry_run=False, label="", analyze=False):
    """Look up tempo/key for every track and rewrite the playlist in that
    order. Returns (known, total)."""
    rows = playlist_rows(sp, playlist_id)
    ids = playlist_track_ids(sp, playlist_id)
    if label:
        print("\n" + "=" * 60 + "\n%s" % label)

    print("Looking up tempo/key for %d tracks..." % len(rows))
    api_key = os.environ.get("GETSONGBPM_KEY")
    by_id = spotify_features(sp, ids)   # cheap probe; tells the user if grandfathered
    found = gather_tempo_key(sp, rows, ids, analyze=analyze)
    enriched = [(title, artist, album, tempo, key, ids[i] if i < len(ids) else None)
                for i, ((title, artist, album), (tempo, key, _src)) in enumerate(zip(rows, found))]

    order = sorted(enriched, key=lambda r: sort_key(r, by), reverse=desc)
    print("\nProposed order (--by %s%s):" % (by, ", descending" if desc else ""))
    known = 0
    for title, artist, album, tempo, key, _tid in order:
        cam = camelot(key)
        known += 1 if (tempo or key) else 0
        print("  %-7s %-4s %-4s %s - %s" % (tempo or "-", key or "-",
              ("%d%s" % cam) if cam else "-", artist, title))
    print("\n%d/%d tracks had tempo/key data; unknowns sink to the bottom in "
          "their current order." % (known, len(order)))
    if dry_run:
        print("--dry-run: playlist untouched.")
        return known, len(order)
    if not api_key and not by_id:
        die("no tempo/key data at all - set GETSONGBPM_KEY (kimbo.py setup) "
            "before resorting, or this would just shuffle unknowns")
    new_ids = [t[5] for t in order if t[5]]
    sp.playlist_replace_items(playlist_id, new_ids[:100])
    for start in range(100, len(new_ids), 100):
        sp.playlist_add_items(playlist_id, new_ids[start:start + 100])
    print("Reordered in place: https://open.spotify.com/playlist/" + playlist_id)
    return known, len(order)


def playlists_with_prefix(sp, prefix):
    """[(id, name)] for every playlist the user owns whose name starts with prefix."""
    me = sp.me()["id"]
    found, page = [], sp.current_user_playlists(limit=50)
    while page:
        for pl in page["items"]:
            if pl["owner"]["id"] == me and pl["name"].startswith(prefix):
                found.append((pl["id"], pl["name"]))
        page = sp.next(page) if page.get("next") else None
    return sorted(found, key=lambda t: t[1])


def cmd_resort(args):
    sp = spotify_client()
    if args.prefix:
        targets = playlists_with_prefix(sp, args.prefix)
        if not targets:
            die("no playlists you own start with %r" % args.prefix)
        print("Resorting %d playlists starting with %r by %s:" % (len(targets), args.prefix, args.by))
        for _, name in targets:
            print("  " + name)
        summary = []
        for pid, name in targets:
            known, total = resort_playlist(sp, pid, args.by, args.desc, args.dry_run, label=name,
                                           analyze=args.analyze)
            summary.append((name, known, total))
        print("\n" + "=" * 60 + "\n%s" % ("Dry run - nothing changed." if args.dry_run else "Done."))
        for name, known, total in summary:
            print("  %-34s %2d/%2d with data" % (name, known, total))
        return
    playlist_id = args.playlist_id or playlist_by_title(sp, args.title or "")
    if not playlist_id:
        die("playlist not found - pass --playlist-id, an exact --title you own, "
            "or --prefix to do a batch")
    resort_playlist(sp, playlist_id, args.by, args.desc, args.dry_run, analyze=args.analyze)


# -------------------------------------------------------------------- CLI ---

def main():
    parser = argparse.ArgumentParser(prog="kimbo", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("setup", help="guided walkthrough: get and store all credentials")
    p.add_argument("--check", action="store_true", help="validate existing credentials, no prompts")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("import", help="CSV (or a directory of CSVs) -> Spotify playlists")
    p.add_argument("--csv", required=True, help="a .csv file, or a directory to import all of")
    p.add_argument("--title", help="playlist name (default: derived from the filename)")
    p.add_argument("--prefix", default="", help="prepend to every derived title, e.g. --prefix 'S&T'")
    p.add_argument("--playlist-id", help="add to an existing playlist instead")
    p.add_argument("--public", action="store_true", help="create as public (default private)")
    p.add_argument("--replace", action="store_true", help="clear the playlist first")
    p.add_argument("--analyze", action="store_true",
                   help="for tracks GetSongBPM lacks, fetch a 30s Deezer preview and compute "
                        "tempo/key locally (pip3 install librosa soundfile)")
    p.add_argument("--resort", choices=["tempo", "key", "key-tempo"],
                   help="after importing, reorder each playlist by this (needs GETSONGBPM_KEY)")
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

    p = sub.add_parser("resort", help="reorder playlists in place by tempo/key")
    p.add_argument("--playlist-id")
    p.add_argument("--title", help="exact name of a playlist you own")
    p.add_argument("--prefix", help="batch: every playlist you own whose name starts with this")
    p.add_argument("--by", choices=["tempo", "key", "key-tempo"], default="tempo",
                   help="tempo ramp, Camelot-wheel key order, or key groups with tempo ramps inside")
    p.add_argument("--analyze", action="store_true",
                   help="for tracks GetSongBPM lacks, fetch a 30s Deezer preview and compute "
                        "tempo/key locally (pip3 install librosa soundfile)")
    p.add_argument("--desc", action="store_true", help="high to low")
    p.add_argument("--dry-run", action="store_true", help="print the proposed order, change nothing")
    p.set_defaults(func=cmd_resort)

    p = sub.add_parser("enrich", help="add tempo/key columns")
    p.add_argument("--analyze", action="store_true",
                   help="for tracks GetSongBPM lacks, fetch a 30s Deezer preview and compute "
                        "tempo/key locally (pip3 install librosa soundfile)")
    p.add_argument("--csv")
    p.add_argument("--playlist-id")
    p.add_argument("--out")
    p.set_defaults(func=cmd_enrich)

    p = sub.add_parser("flow", help="reorder so the playlist plays smoothly")
    p.add_argument("--csv", help="an enriched CSV (see `enrich`)")
    p.add_argument("--playlist-id", help="reorder a playlist you own")
    p.add_argument("--prefix", help="batch: every playlist you own whose name starts with this")
    p.add_argument("--analyze", action="store_true",
                   help="for tracks GetSongBPM lacks, fetch a 30s Deezer preview and compute "
                        "tempo/key locally (pip3 install librosa soundfile)")
    p.add_argument("--arc", default="party",
                   choices=["flat", "steady", "party", "chill", "build"],
                   help="energy curve to shape the set around (default party)")
    p.add_argument("--tag-prompt", action="store_true",
                   help="write an LLM prompt for filling in Energy/Vibe")
    p.add_argument("--apply", action="store_true",
                   help="create a new private '<name> (flow)' playlist")
    p.add_argument("--in-place", action="store_true",
                   help="overwrite the playlist itself (saves the old order first)")
    p.add_argument("--out", help="output path (default: <input>-flow.csv)")
    p.set_defaults(func=cmd_flow)

    load_env_file()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
