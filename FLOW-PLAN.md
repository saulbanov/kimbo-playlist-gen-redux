# Plan: `flow` — reorder a playlist so it plays smoothly

This document scopes a new kimbo command and then gives a phase-by-phase build
plan written for an autonomous coding model to execute without human help.
Part I is the scope (readable by anyone). Part II is the executor's contract
and build spec (precise, follow it literally).

Background: this grew out of a side exploration into ordering playlists by
vibe, tempo (BPM), and musical key — the way DJs sequence a set. The finding
was that the work splits three ways: BPM/key ordering is mechanical and fully
automatable; the energy arc (warm up → build → peak → wind down) is a template
you pick per occasion; and *vibe* — how a track feels, which tempo and key
can't capture — needs a judgment pass by a person or an LLM. Spotify's own
auto-mix handles beatmatching but not vibe: it will put a delicate 155 BPM
piano piece next to an actual banger at the same tempo. That whiplash is the
thing this feature fixes.

---

## Part I — Scope

### What we're adding

One new subcommand:

```
python3 kimbo.py flow --csv list-enriched.csv --arc party
python3 kimbo.py flow --playlist-id 3cEYpjA9... --apply
python3 kimbo.py flow --csv list-enriched.csv --tag-prompt
```

`flow` takes a playlist (as an enriched CSV, or live from Spotify) and reorders
it so consecutive tracks are compatible in key (Camelot wheel), close in tempo
(treating half/double tempo as a match — 174 BPM into 87 BPM beatmatches
cleanly), and shaped to an energy arc you choose. It writes the reordered CSV,
prints a transition report (which joins are smooth, which are jumps), and can
optionally push the new order back to Spotify.

### Why this fits kimbo, and why now

- The hard data problem is already solved here. The writeup's biggest blocker
  was getting BPM/key out (Spotify deprecated `audio-features` on 2024-11-27;
  djay keeps its analysis locked in its own database). But kimbo's `enrich`
  already fetches tempo and key from GetSongBPM. `flow` is the missing
  consumer of that data.
- The README already names this as the natural next command (it called it
  `resort`). We use the name `flow` since that's what the feature does.
- The pipeline becomes: `export` → `enrich` → (optional vibe tagging) →
  `flow` → `import`/`--apply`. Every piece but the ordering exists.

### The three layers (design principle)

1. **Mechanical (fully automated):** Camelot key compatibility + tempo
   distance with half/double-time awareness. Pure math, unit-testable offline.
2. **Arc (a template you pick):** `--arc flat|steady|party|chill|build`.
   Each is a target energy curve over the playlist.
3. **Vibe (judgment, never faked):** an optional `Energy` (1–5) and `Vibe`
   column in the CSV. `flow --tag-prompt` generates a ready-to-paste prompt so
   any LLM (or a human) fills those columns in; you save the reply as the CSV
   and run `flow` on it. The tool never pretends to know how a track feels.

### Safety posture (matches the rest of kimbo)

- Default output is a **new** CSV / a **new private** playlist named
  `<name> (flow)`. The source playlist is only touched with an explicit
  `--in-place`.
- No new required credentials. GetSongBPM (already configured for `enrich`)
  is the only lookup, and only in `--playlist-id` mode.

### Out of scope (and why)

- **Exporting djay's analysis** — locked database, DRM'd streams; not reachable.
- **Spotify audio-features** — deprecated; `enrich` already tries it for
  grandfathered apps and that's the right amount of effort.
- **Replicating auto-mix's live beatmatching** — that's playback, not ordering.
  For casual/background settings, plain Spotify auto-mix remains the honest
  recommendation; `flow` earns its keep for continuous-attention listening
  (a ride at one tempo, a dance floor, a proper set).
- **Calling an LLM API directly for vibe tagging** — would add a paid
  credential and a failure surface; the `--tag-prompt` loop delivers the same
  result through whatever assistant the user already has. Possible later.
- **Local audio analysis (librosa/Essentia) of preview clips** — heavy
  dependency, and preview clips are being phased out. Possible later.
- **"Scrape all the songs ever"** — no. The lookup cache in Phase 5 is the
  seed of a personal database, grown one real playlist at a time.

---

## Part II — Execution plan (for the autonomous executor)

### Executor contract — read first, obey throughout

1. Work on branch `claude/playlist-flow-scope-uocufp` only.
2. Run `pip3 install -r requirements.txt` once before starting. Add **no**
   new dependencies. New code uses only the Python standard library
   (`math`, `json`, plus what kimbo already imports).
3. **Never** run commands that need credentials or the network. Do not run
   `setup`, `import`, `export`, `discover`, `enrich`, or `flow
   --playlist-id` for real. All verification is: unit tests + `flow --csv`
   on the fixture + `-h` smoke checks. Unit tests must not open network
   connections.
4. Touch only what each phase lists. Do not modify `cmd_import`,
   `cmd_export`, `cmd_discover`, `cmd_setup`, `read_rows`, `write_rows`,
   `find_track`, or any Spotify helper except where a phase explicitly says
   so. Do not reformat existing code.
5. Match house style: 4-space indent, `%`-style string formatting (no
   f-strings — the file uses `%` throughout), ASCII only in source files,
   section banner comments like
   `# ------------------------------------------------------------- flow ---`.
6. New code lives in `kimbo.py` (new `flow` section placed between the
   `enrich` section and the `setup` section) and in a new `test_flow.py` at
   repo root. Constants go at the top of the flow section.
7. After every phase: run the phase's acceptance checks **and**
   `python3 -m unittest discover -v` (everything green), then commit with the
   given message and tick the phase's box in the Progress checklist at the
   bottom of this file (include that edit in the same commit).
8. If an acceptance check fails: fix and re-run. After 3 failed fix attempts
   on one phase, stop that phase, revert its uncommitted changes, note the
   blocker under Progress, commit the note, push, and stop entirely.
9. Do not add features, flags, or files this plan doesn't list.
10. When all phases are done (or you stopped per rule 8), push the branch:
    `git push -u origin claude/playlist-flow-scope-uocufp`.

### Phase 1 — Camelot + compatibility math (pure functions)

**Goal:** key normalization, Camelot conversion, and the two penalty
functions. No CLI changes yet.

Create the flow section in `kimbo.py` with these constants and functions.
Copy the tables exactly — they encode music theory; do not re-derive them.

```python
# Enharmonic flats -> the sharp names used in PITCH_CLASSES.
FLAT_TO_SHARP = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#",
                 "Bb": "A#", "Cb": "B", "Fb": "E"}

# (root as sharp name, is_minor) -> Camelot code. Complete 24-key wheel.
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

FLOW_W_KEY = 1.0      # weight: harmonic compatibility
FLOW_W_BPM = 1.0      # weight: tempo distance
FLOW_W_ARC = 0.8      # weight: fit to the energy arc
FLOW_UNKNOWN_PEN = 0.3   # neutral penalty when key or tempo is unknown
FLOW_SMOOTH_KEY = 0.25   # max key penalty still called "smooth"
FLOW_SMOOTH_BPM = 0.09   # max tempo log2-distance still called "smooth" (~6%)
```

`normalize_key(raw)` → `(root, is_minor)` or `None`.
- `None`, empty, or `"?"` after stripping → `None`.
- Replace unicode `♯`→`#`, `♭`→`b`; strip whitespace.
- Case-insensitively strip a trailing `minor`/`min`/`m` (try longest first,
  in that order) → `is_minor=True`; else strip trailing `major`/`maj` →
  `is_minor=False`. Strip whitespace again after removing the suffix (so
  `"E minor"` leaves `"E"`).
- Normalize the remaining root: first letter upper, rest lower (so `bb`→`Bb`,
  `f#`→`F#`). Map through `FLAT_TO_SHARP` if present.
- If the result is not in `PITCH_CLASSES` → `None`.

`to_camelot(raw)` → Camelot string like `"8A"`, or `None` (via
`normalize_key`, then the `CAMELOT` table).

`camelot_parts(code)` → `(number:int, letter:str)`, e.g. `"11A"` → `(11, "A")`.

`key_penalty(c1, c2)` → float. If either is `None` → `FLOW_UNKNOWN_PEN`.
Else with `ring = min((n1 - n2) % 12, (n2 - n1) % 12)` and
`letter = 0 if l1 == l2 else 1`:
`return min(1.0, 0.15 * ring + 0.25 * letter)`.

`bpm_gap(a, b)` → `(dist, ratio)`: the minimum of `abs(math.log2(b * r / a))`
over `r in (0.5, 1.0, 2.0)`, and the `r` that achieved it (prefer `1.0` on
ties). Callers guarantee `a`/`b` positive.

`bpm_penalty(a, b)` → float. If either is `None` or `<= 0` →
`FLOW_UNKNOWN_PEN`. Else `min(1.0, bpm_gap(a, b)[0] / 0.5)`.

Create `test_flow.py` (stdlib `unittest`, `import kimbo`) asserting exactly:

| call | expected |
|---|---|
| `to_camelot("Am")` | `"8A"` |
| `to_camelot("C")` | `"8B"` |
| `to_camelot("Ebm")` | `"2A"` |
| `to_camelot("F#")` | `"2B"` |
| `to_camelot("Db")` | `"3B"` |
| `to_camelot("bb")` | `"6B"` |
| `to_camelot("A♯m")` | `"3A"` |
| `to_camelot("E minor")` | `"9A"` |
| `to_camelot("")`, `to_camelot("?")`, `to_camelot(None)`, `to_camelot("H")` | `None` |
| `key_penalty("8A", "8A")` | `0.0` |
| `key_penalty("8A", "8B")` | `0.25` |
| `key_penalty("8A", "9A")` | `0.15` (approx) |
| `key_penalty("8A", "9B")` | `0.4` (approx) |
| `key_penalty("8A", "2A")` | `0.9` (approx) |
| `key_penalty("1A", "12A")` | `0.15` (approx, wheel wraps) |
| `key_penalty(None, "8A")` | `0.3` |
| `bpm_penalty(120, 120)` | `0.0` |
| `bpm_penalty(174, 87)` and `bpm_penalty(87, 174)` | `0.0` |
| `round(bpm_penalty(100, 106), 3)` | `0.168` |
| `bpm_penalty(None, 120)` | `0.3` |
| `bpm_gap(174, 87)[1]` | `0.5` |

Use `assertAlmostEqual(..., places=6)` for the float comparisons marked
approx.

**Accept:** `python3 -m unittest discover -v` green;
`python3 -c "import kimbo"` exits 0.
**Commit:** `flow: camelot conversion + key/tempo compatibility math`

### Phase 2 — Ordering engine (greedy, arc-aware)

**Goal:** turn a track list into a play order. Still no CLI changes.

Track dicts have keys: `title`, `artist`, `album`, `tempo` (float or None),
`key` (str), `camelot` (str or None), `energy` (float or None), `vibe` (str),
`source` (str).

`ARC_BREAKPOINTS` — per arc, `(position, energy)` breakpoints, linear
interpolation between them; positions span 0..1:

```python
ARC_BREAKPOINTS = {
    "flat":   None,                                   # no arc: pure key/tempo chaining
    "steady": [(0.0, 3.0), (1.0, 3.0)],               # hold one level (bike ride)
    "party":  [(0.0, 2.0), (0.15, 3.0), (0.7, 5.0),   # warm up, build, peak,
               (0.85, 5.0), (1.0, 2.5)],              # wind down
    "chill":  [(0.0, 3.5), (1.0, 1.5)],               # gentle descent
    "build":  [(0.0, 1.5), (1.0, 5.0)],               # straight climb (workout)
}
```

`arc_targets(name, n)` → list of `n` floats (or `n` `None`s for `"flat"`).
Slot `i` has position `p = i / (n - 1)` (`p = 0.0` when `n == 1`); interpolate
linearly between the surrounding breakpoints.

`effective_energies(tracks)` → list of `n` floats:
- If any track has `energy` set: use it (clamped to 1..5); missing → `3.0`.
- Else if any track has `tempo`: proxy from tempo — rank tracks with known
  tempo ascending (ties broken by input index); energy
  `= 1.0 + 4.0 * rank / (k - 1)` over the `k` known-tempo tracks (`3.0` if
  `k == 1`); unknown-tempo tracks → `3.0`. (Crude on purpose; a real
  `Energy` column always beats it.)
- Else: all `3.0`.

`transition_cost(prev, cand, cand_energy, slot_target)` →
`FLOW_W_KEY * key_penalty(prev["camelot"], cand["camelot"])
+ FLOW_W_BPM * bpm_penalty(prev["tempo"], cand["tempo"])
+ (FLOW_W_ARC * abs(cand_energy - slot_target) / 4.0 if slot_target is not
None else 0.0)`.

`order_tracks(tracks, arc)` → list of input indices in play order:
- `n == 0` → `[]`; `n == 1` → `[0]`.
- `targets = arc_targets(arc, n)`, `energies = effective_energies(tracks)`.
- Start: for `"flat"`, index 0 (keep the user's opener). Otherwise the index
  minimizing `abs(energies[i] - targets[0])`, ties → lowest index.
- Then greedily: for each next slot, pick the unused index minimizing
  `transition_cost(tracks[prev], tracks[cand], energies[cand],
  targets[slot])`; ties (within 1e-9) → lowest index. Deterministic.

Add to `test_flow.py`:
- **Interpolation:** `arc_targets("party", 3)` → `[2.0, ...midpoint..., 2.5]`
  where the midpoint (p=0.5) interpolates (0.15,3)-(0.7,5):
  `assertAlmostEqual(t[1], 3 + 2 * (0.5 - 0.15) / 0.55)`. Also
  `arc_targets("flat", 4) == [None] * 4`, and for `n = 41`,
  `arc_targets("party", 41)[28]` (p exactly 0.7) `== 5.0` (approx).
- **Provable 3-track case:** tracks (title/artist/album filler, energy None,
  vibe/source ""): `[("C", 120), ("Em", 176), ("G", 122)]` as (key, tempo),
  camelot filled via `to_camelot`. `order_tracks(tracks, "flat") == [0, 2, 1]`
  — from C/120, G/122 costs ~0.198 (0.15 key + 0.048 tempo) vs Em/176 ~1.295
  (0.4 key + 0.895 tempo).
- **Properties** on an inline 8-track scrambled list (mix of keys around the
  wheel, tempos 80–175 including one exact half/double pair, energies 1–5):
  result is a permutation of `range(8)`; calling twice gives the same list;
  and the mean `transition_cost` over consecutive pairs (with `slot_target
  None`) is strictly lower for the flow order than for input order `0..7`.

**Accept:** all tests green.
**Commit:** `flow: greedy arc-aware ordering engine`

### Phase 3 — `flow` CSV mode + transition report

**Goal:** the command works end to end on a CSV, offline.

1. `read_enriched_rows(path)` → list of track dicts. Reuse `read_rows`'s
   conventions: `utf-8-sig`, TuneMyMusic-style header row detected
   case-insensitively; columns `Track name`/`Artist name` required, `Album`,
   `Tempo`, `Key`, `Source`, `Energy`, `Vibe` optional (absent column →
   None/"" for every row). Headerless 2-column files are accepted (title,
   artist only). Skip rows with an empty title. `Tempo`/`Energy` parse via
   `float()`, blank or unparsable → None. Set `camelot` via `to_camelot(key)`.
2. `flow_report(tracks, order, arc)` → list of printable lines. First line:
   `Flow order (arc: <arc>):`. Then one line per
   track in play order — position, `artist - title`, tempo, key, camelot,
   energy — and from the second track on, an annotation for the join from the
   previous track: `smooth` when `key_penalty <= FLOW_SMOOTH_KEY` and
   `bpm_gap dist <= FLOW_SMOOTH_BPM` (append `(half/double-time)` when the
   best ratio isn't 1.0); otherwise the applicable labels `key jump` /
   `tempo jump`; `?` when data is missing on either side. End with two
   summary lines: `Transitions: <n> total, <s> smooth` and
   `Rough transitions: input order <a> -> flow order <b>` (a rough
   transition is any non-smooth one between tracks that both have data).
3. `cmd_flow(args)` — CSV branch only for now: read, warn (existing `warn`)
   if no row has tempo or key (`run enrich first for harmonic ordering`),
   order with `args.arc`, print the report, write the reordered CSV to
   `--out` or `<input>-flow.csv` (mirror `enrich`'s naming), with header
   `Track name, Artist name, Album, Tempo, Key, Camelot, Energy, Vibe,
   Source` and one row per track in play order.
4. Wire the parser in `main()` after `enrich`: `flow` with `--csv`,
   `--playlist-id` (mutually exclusive, exactly one required — `die`
   otherwise), `--arc` (choices `flat steady party chill build`, default
   `party`), `--out`. Add help strings in the file's voice. Playlist mode:
   `die("playlist mode arrives in a later phase")` for now.
5. Update the module docstring: five subcommands → six, adding
   `flow      reorder by key/tempo/energy so the playlist plays smoothly`.
6. Create the fixture `examples/garden-party-enriched.csv` **exactly**
   (illustrative values; one half/double pair 174/87, one blank tempo, one
   blank key):

```csv
Track name,Artist name,Album,Tempo,Key,Source,Energy,Vibe
Golden Hour,June & the Latches,Porch Light,98,C,getsongbpm,2,sunshine
Diesel Heart,The Flare Stacks,Boomtown,174,Em,getsongbpm,5,barreling
Sleepy Marigold,Ada Plum,Seedlings,87,G,getsongbpm,1,drowsy
Company Store,Val Hollows,Ledger Lines,112,Am,getsongbpm,3,brooding
Tin Roof Rain,June & the Latches,Porch Light,104,F,getsongbpm,2,patter
Peak Bloom,DJ Kimbo,Garden Cuts,124,A,getsongbpm,5,floor-filler
Sixteen Wheels,The Flare Stacks,Boomtown,148,Bm,getsongbpm,4,swagger
Cold Frame,Ada Plum,Seedlings,,Dm,getsongbpm,2,hushed
Sprinkler Season,DJ Kimbo,Garden Cuts,120,D,getsongbpm,4,bounce
Root Cellar,Val Hollows,Ledger Lines,96,,getsongbpm,3,dusty
Hummingbird Feint,Marla Q,Nectar,118,F#m,getsongbpm,3,flutter
Last Slice,Marla Q,Nectar,101,Ebm,getsongbpm,2,waltzy
```

Add tests: `read_enriched_rows` on the fixture returns 12 dicts, with
`tracks[1]["tempo"] == 174.0`, `tracks[7]["tempo"] is None`,
`tracks[9]["camelot"] is None`, `tracks[11]["camelot"] == "2A"`; and a
round-trip check that the written flow CSV (run `cmd_flow` via a small
`argparse.Namespace` in the test, writing into a temp directory) contains the
same 12 title/artist pairs as the input.

**Accept:** tests green, plus:
```
python3 kimbo.py flow --csv examples/garden-party-enriched.csv
```
exits 0, prints lines containing `arc: party`, `smooth`, `Transitions:`, and
writes `examples/garden-party-enriched-flow.csv` with 13 lines (delete that
generated file before committing — commit only the fixture);
`python3 kimbo.py flow -h` and `python3 kimbo.py import -h` both exit 0.
**Commit:** `flow: CSV mode with transition report + example fixture`

### Phase 4 — `--tag-prompt` (the vibe layer)

**Goal:** generate the paste-ready LLM tagging prompt.

Add `--tag-prompt` to the `flow` parser (CSV mode only; with
`--playlist-id`, `die` with a hint to `export` + `enrich` first). When set,
`cmd_flow` writes `<input>-tagging.txt` (or `--out` if given) and exits
before ordering. File contents: the instruction block below, a blank line,
then the input CSV's text verbatim (header included, all existing columns
preserved; if the file had no `Energy`/`Vibe` columns, extend the echoed
header and rows with empty ones).

```
You are tagging songs for playlist ordering. For each track in the CSV
below, fill in the Energy column with a rating from 1 (sleepy, ambient)
to 5 (peak, floor-filling), judging how the track FEELS, not how fast it
is - a delicate piano piece can be fast and still low energy. Fill in the
Vibe column with one word (e.g. dreamy, swagger, sunshine, brooding).
Reply with ONLY the completed CSV: same columns, same row order, nothing
else. If you don't know a track, rate it 3 and guess the vibe from the
title.
```

Then the user pastes any model's reply into a file and runs `flow --csv` on
it — no reply-parsing code needed.

Add a test: run the tag-prompt path on the fixture (temp dir); the output
file contains `Energy column`, the CSV header, and `Last Slice`.

**Accept:** tests green;
`python3 kimbo.py flow --csv examples/garden-party-enriched.csv --tag-prompt --out /tmp/tagging.txt`
exits 0 and the file exists.
**Commit:** `flow: --tag-prompt generates the LLM vibe-tagging handoff`

### Phase 5 — Playlist mode + lookup cache

**Goal:** `flow --playlist-id` pulls, enriches, orders, and (opt-in) writes
back. This phase cannot be run for real without credentials — verify by unit
tests on the pure parts, `-h` smoke checks, and a careful re-read of the
diff against this spec.

1. Refactor the enrichment core out of `cmd_enrich` into
   `gather_tempo_key(sp, rows3, ids)` → list of `(tempo, key, source)`
   aligned with `rows3`: the Spotify-features attempt (when `ids` and `sp`),
   the GetSongBPM fallback with the same `0.6`s sleep, the same per-row
   progress printing, and the same missing-key warning behavior.
   `cmd_enrich` keeps identical observable behavior (same printed lines, same
   CSV columns) but now calls the helper. This is the only permitted change
   to existing code besides `main()` and the docstring.
2. In `cmd_flow`, playlist branch: `spotify_client()`, fetch
   `playlist_rows` + `playlist_track_ids`, get the playlist's name via
   `sp.playlist(playlist_id)["name"]`, call `gather_tempo_key`, build track
   dicts (energy None — print a note that vibe-aware ordering wants the CSV
   route), order, print the report, write the CSV to `--out` or
   `<slugified-name>-flow.csv` (slug: lowercase, spaces→`-`, keep only
   alphanumerics and `-_`).
3. Write-back flags (playlist mode only; `die` if both given, or if either
   is used with `--csv`):
   - `--apply`: create a **new private** playlist named `<name> (flow)`
     (description `kimbo flow: <arc> arc`), add the known track IDs in play
     order, 100 per `playlist_add_items` call, print the
     `https://open.spotify.com/playlist/` link.
   - `--in-place`: `playlist_replace_items(playlist_id, first_100)` then
     `playlist_add_items` for the rest in 100s. Print the link.
   - Neither flag → report + CSV only (the safe default).
4. GetSongBPM cache (small win; skip this item if it causes any trouble):
   `load_bpm_cache()` / `save_bpm_cache(cache)` reading/writing JSON at
   `~/.cache/kimbo/getsongbpm-cache.json`, keys `"title|artist"` lowercased,
   values `[tempo, key]`. `gather_tempo_key` loads it, checks before each
   GetSongBPM call (cache hits skip the sleep, source `"cache"`), records
   successful lookups, saves once at the end. Unit-test the load/save
   round-trip against a temp path (add an optional `path=` parameter to both
   functions for this).
5. New flags mean the Phase 3/4 tests that build an `argparse.Namespace` by
   hand need the new attributes too — update those Namespaces with
   `apply=False, in_place=False` (and `tag_prompt=False` where missing) so
   every earlier test still runs.

**Accept:** all tests green (including Phase 1–4 regressions);
`python3 kimbo.py flow -h` shows `--apply` and `--in-place`;
`python3 kimbo.py enrich -h` unchanged; `python3 -c "import kimbo"` exits 0.
**Commit:** `flow: playlist mode, opt-in write-back, getsongbpm cache`

### Phase 6 — Documentation

**Goal:** README tells the story; nothing else changes.

1. README `Commands` block: add two `flow` examples (CSV + `--playlist-id
   ... --apply`).
2. New README section `### Flow: making a playlist play smoothly`, in the
   file's plain voice, covering: what `flow` does (key/tempo/arc), the
   half/double-time point (174→87 beatmatches; the engine knows), the arc
   choices and when to pick each, the `--tag-prompt` loop (and that Energy
   beats the tempo proxy), the safe-by-default write-back, and the honest
   line: for casual background listening Spotify's auto-mix is good enough —
   `flow` is for when the flow gets felt (one-tempo rides, dance floors,
   proper sets). Keep the existing GetSongBPM attribution note in force.
3. Update the `Tempo and key: the honest state` paragraph's last sentence:
   the `resort` idea is now real and named `flow`.
4. Fix any stale statements this work introduced elsewhere in the README.

**Accept:** `python3 -m unittest discover -v` green; a final read of
`git diff main` confirms only planned files changed
(`kimbo.py`, `test_flow.py`, `examples/garden-party-enriched.csv`,
`README.md`, `FLOW-PLAN.md`).
**Commit:** `flow: document the new command`

### Progress

- [ ] Phase 1 — camelot + compatibility math
- [ ] Phase 2 — ordering engine
- [ ] Phase 3 — CSV mode + report + fixture
- [ ] Phase 4 — tag-prompt
- [ ] Phase 5 — playlist mode + cache
- [ ] Phase 6 — docs

Blockers (executor: note anything that stopped you, with the failing
command and its output):

*(none yet)*
