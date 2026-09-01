#!/usr/bin/env python3
"""kimbo - playlist plumbing for Spotify.

Redux of the old playlist-generator script. Five subcommands:

  setup     guided walkthrough: get every credential, store and test them
  import    CSV -> Spotify playlist (ordered, deduped, reports misses)
  export    Spotify playlist -> CSV in the same format
  discover  Genius lyric-density search -> a candidates playlist or CSV
  enrich    add tempo/key columns via GetSongBPM (and Spotify
            audio-features, when your app still has access)
  resort    reorder a playlist in place by tempo, key (Camelot wheel),
            or key-then-tempo

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
            die("%s is not set - run `python3 kimbo.py setup` for a guided "
                "walkthrough (or copy .env.example to .env and fill it in; "
                ".env is loaded automatically)" % var)
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
    if not (values["SPOTIPY_CLIENT_ID"] and values["SPOTIPY_CLIENT_SECRET"]):
        print("  Spotify:         NOT CONFIGURED - every command needs it")
        return
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
    print("  5. Copy the Client ID, then 'View client secret' and copy that too\n")
    values["SPOTIPY_CLIENT_ID"] = _ask("Client ID", values["SPOTIPY_CLIENT_ID"])
    values["SPOTIPY_CLIENT_SECRET"] = _ask("Client Secret", values["SPOTIPY_CLIENT_SECRET"])
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

FLAT_TO_SHARP = {"DB": "C#", "EB": "D#", "GB": "F#", "AB": "G#", "BB": "A#",
                 "CB": "B", "FB": "E"}


def camelot(key_str):
    """Map a key name ('Em', 'F#', 'Bb', 'C#m') to its Camelot wheel slot
    (number 1-12, letter A=minor/B=major). Returns None if unparseable.
    Adjacent numbers and same-number A/B pairs mix harmonically."""
    if not key_str:
        return None
    k = key_str.strip().replace("\u266f", "#").replace("\u266d", "b")
    minor = k.lower().endswith("min") or (k.endswith("m") and not k.lower().endswith("maj"))
    root = re.sub(r"(?i)(maj|min|m)$", "", k).strip().upper()
    root = FLAT_TO_SHARP.get(root, root)
    if root not in PITCH_CLASSES:
        return None
    pc = PITCH_CLASSES.index(root)
    if minor:
        pc = (pc + 3) % 12          # relative major shares the slot number
    number = ((pc * 7) % 12 + 7) % 12 + 1
    return (number, "A" if minor else "B")


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


def cmd_resort(args):
    sp = spotify_client()
    playlist_id = args.playlist_id or playlist_by_title(sp, args.title or "")
    if not playlist_id:
        die("playlist not found - pass --playlist-id or an exact --title you own")
    rows = playlist_rows(sp, playlist_id)
    ids = playlist_track_ids(sp, playlist_id)

    print("Looking up tempo/key for %d tracks..." % len(rows))
    by_id = spotify_features(sp, ids)
    api_key = os.environ.get("GETSONGBPM_KEY")
    session = requests.Session()
    enriched = []
    for i, (title, artist, album) in enumerate(rows):
        tempo, key = "", ""
        if i < len(ids) and ids[i] in by_id:
            tempo, key, _ = by_id[ids[i]]
        elif api_key:
            hit = getsongbpm_lookup(session, api_key, title, artist)
            if hit:
                tempo, key = hit[0], hit[1]
            time.sleep(0.6)
        enriched.append((title, artist, album, tempo, key, ids[i] if i < len(ids) else None))

    order = sorted(enriched, key=lambda r: sort_key(r, args.by), reverse=args.desc)
    print("\nProposed order (--by %s%s):" % (args.by, ", descending" if args.desc else ""))
    known = 0
    for title, artist, album, tempo, key, _tid in order:
        cam = camelot(key)
        known += 1 if (tempo or key) else 0
        print("  %-7s %-4s %-4s %s - %s" % (tempo or "-", key or "-",
              ("%d%s" % cam) if cam else "-", artist, title))
    print("\n%d/%d tracks had tempo/key data; unknowns sink to the bottom in "
          "their current order." % (known, len(order)))
    if args.dry_run:
        print("--dry-run: playlist untouched.")
        return
    if not api_key and not by_id:
        die("no tempo/key data at all - set GETSONGBPM_KEY (kimbo.py setup) "
            "before resorting, or this would just shuffle unknowns")
    new_ids = [t[5] for t in order if t[5]]
    sp.playlist_replace_items(playlist_id, new_ids[:100])
    for start in range(100, len(new_ids), 100):
        sp.playlist_add_items(playlist_id, new_ids[start:start + 100])
    print("Reordered in place: https://open.spotify.com/playlist/" + playlist_id)


# -------------------------------------------------------------------- CLI ---

def main():
    parser = argparse.ArgumentParser(prog="kimbo", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("setup", help="guided walkthrough: get and store all credentials")
    p.add_argument("--check", action="store_true", help="validate existing credentials, no prompts")
    p.set_defaults(func=cmd_setup)

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

    p = sub.add_parser("resort", help="reorder a playlist in place by tempo/key")
    p.add_argument("--playlist-id")
    p.add_argument("--title", help="exact name of a playlist you own")
    p.add_argument("--by", choices=["tempo", "key", "key-tempo"], default="tempo",
                   help="tempo ramp, Camelot-wheel key order, or key groups with tempo ramps inside")
    p.add_argument("--desc", action="store_true", help="high to low")
    p.add_argument("--dry-run", action="store_true", help="print the proposed order, change nothing")
    p.set_defaults(func=cmd_resort)

    p = sub.add_parser("enrich", help="add tempo/key columns")
    p.add_argument("--csv")
    p.add_argument("--playlist-id")
    p.add_argument("--out")
    p.set_defaults(func=cmd_enrich)

    load_env_file()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
