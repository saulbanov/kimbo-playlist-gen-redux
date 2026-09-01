#!/usr/bin/env python3
"""kimbo - playlist plumbing for Spotify.

Redux of the old playlist-generator script. Six subcommands:

  setup     guided walkthrough: get every credential, store and test them
  import    CSV -> Spotify playlist (ordered, deduped, reports misses)
  export    Spotify playlist -> CSV in the same format
  discover  Genius lyric-density search -> a candidates playlist or CSV
  enrich    add tempo/key columns via GetSongBPM (and Spotify
            audio-features, when your app still has access)
  flow      reorder by key/tempo/energy so the playlist plays smoothly

Run `python3 kimbo.py <command> -h` for options. Credentials come from
environment variables; see .env.example.
"""

import argparse
import csv
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


def cmd_flow(args):
    if bool(args.csv) == bool(args.playlist_id):
        die("pass exactly one of --csv or --playlist-id")
    if args.playlist_id:
        die("playlist mode arrives in a later phase")

    tracks = read_enriched_rows(args.csv)
    if not tracks:
        die("no tracks found in %s" % args.csv)
    if not any(track["tempo"] or track["camelot"] for track in tracks):
        warn("no tempo or key in %s - run enrich first for harmonic ordering"
             % args.csv)

    order = order_tracks(tracks, args.arc)
    for line in flow_report(tracks, order, args.arc):
        print(line)
    out = args.out or (args.csv.rsplit(".csv", 1)[0] + "-flow.csv")
    write_flow_csv(out, tracks, order)
    print("\nWrote %s (%d tracks in play order)." % (out, len(order)))


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

    p = sub.add_parser("enrich", help="add tempo/key columns")
    p.add_argument("--csv")
    p.add_argument("--playlist-id")
    p.add_argument("--out")
    p.set_defaults(func=cmd_enrich)

    p = sub.add_parser("flow", help="reorder so the playlist plays smoothly")
    p.add_argument("--csv", help="an enriched CSV (see `enrich`)")
    p.add_argument("--playlist-id", help="reorder a playlist you own")
    p.add_argument("--arc", default="party",
                   choices=["flat", "steady", "party", "chill", "build"],
                   help="energy curve to shape the set around (default party)")
    p.add_argument("--out", help="output path (default: <input>-flow.csv)")
    p.set_defaults(func=cmd_flow)

    load_env_file()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
