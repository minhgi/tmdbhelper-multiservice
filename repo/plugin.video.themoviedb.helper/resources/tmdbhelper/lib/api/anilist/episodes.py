"""Anime episode titles, plots and stills for any show the Fribb index knows, from AniZip (api.ani.zip).

The Fribb index (api/anilist/fribb.py) says which AniList entry (cour) a TMDb season and episode belong to: the entries that
share a TMDb show and a TMDb season are told apart by their episode offset, and TMDb episode N of an entry that starts after
`offset` episodes is that entry's episode N - offset. AniZip answers by AniList id with every episode of the entry numbered
from 1, so nothing here needs Seren to have been browsed first.

An offset in the file is wrong about one time in seven, so a mapping is only used when the episode's TMDb air date agrees
with AniZip's within DATE_TOLERANCE days (measured on 1,203 episodes: 95 % of correctly mapped episodes differ by at most two
days, while only 5 % of off-by-one mappings do). With no TMDb air date, or none from AniZip, the mapping is only used when it
is the single possible one. An entry whose TMDb season the index does not know is never used: the episode order of such series
often differs between AniZip and TMDb while the dates still agree (Medabots). A wrong overlay is worse than none, so everything
else leaves TMDb's own data alone.

One request returns a whole entry, so the answer is kept in anime_episodes.db: 30 days for an entry whose episodes have all
aired, one day while it is airing, ten minutes after a failure. The fetch is bounded: a short timeout, a few fetches per
plugin process, and a breaker kept in the database (so the other plugin processes of a widget burst see it too) that stops
all fetching for five minutes after two failures in a row.
"""
import re
import sqlite3
import threading
import time
from datetime import date

from tmdbhelper.lib.addon.logger import kodi_log

ANIZIP_URL = 'https://api.ani.zip/mappings'
DB_FILE = 'special://profile/addon_data/plugin.video.themoviedb.helper/anime_episodes.db'
TIMEOUT = 3
DATE_TOLERANCE = 3                    # days between the TMDb and the AniZip air date of the same episode
TTL_SETTLED = 30 * 24 * 3600
TTL_AIRING = 24 * 3600                # an entry with an episode aired in the last AIRING_WINDOW (or still to air)
AIRING_WINDOW = 45 * 24 * 3600
TTL_FAILED = 10 * 60
BREAKER_FAILURES = 2
BREAKER_SECONDS = 5 * 60
MAX_FETCHES = 24                      # new entries fetched by one plugin process (a Next Up list holds a different entry per show; the fetches run in parallel)
MAX_CANDIDATES = 4                    # entries tried for one episode

STATE_SECONDS = 60                    # module state older than this is dropped (a reused interpreter lives for hours)

_PLACEHOLDER = re.compile(r'^(episode|ep\.?)\s*\d+$|^(tba|tbd)$', re.IGNORECASE)   # "Episode 5" is worse than TMDb's own title

_lock = threading.Lock()
_epoch = time.time()
_entry_locks = {}
_memory = {}                          # AniList id -> {episode: dict} for this process
_index_memo = {}                      # TMDb show id -> [(AniList id, season, offset)]
_fetched = 0
_failures = 0


def _translate(path):
    try:
        import xbmcvfs
        return xbmcvfs.translatePath(path)
    except ImportError:
        return None


def _connect():
    path = _translate(DB_FILE)
    if not path:
        return None
    try:
        conn = sqlite3.connect(path, timeout=3)
        conn.execute('CREATE TABLE IF NOT EXISTS entry (anilist_id INTEGER PRIMARY KEY, fetched REAL, ttl REAL, ok INTEGER)')
        conn.execute(
            'CREATE TABLE IF NOT EXISTS episode (anilist_id INTEGER, number INTEGER, title TEXT, plot TEXT, thumb TEXT, '
            'airdate TEXT, duration INTEGER, PRIMARY KEY (anilist_id, number))')
        conn.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value REAL)')
        return conn
    except sqlite3.Error as exc:
        kodi_log(f'Anime episodes: cache unavailable: {type(exc).__name__}', 1)
        return None


def _to_date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _parse(data):
    """{episode number: {title, plot, thumb, airdate, duration}} from an AniZip answer; titles only in English."""
    out = {}
    episodes = data.get('episodes') if isinstance(data, dict) else None
    for key, ep in (episodes.items() if isinstance(episodes, dict) else ()):   # a 200 with another shape is an empty answer, not a crash
        if not str(key).isdigit() or int(key) < 1 or not isinstance(ep, dict):
            continue
        title = ((ep.get('title') or {}).get('en') or '').replace('`', "'")   # AniDB's apostrophe
        try:
            duration = int(ep.get('runtime') or ep.get('length') or 0)
        except (ValueError, TypeError):
            duration = 0
        out[int(key)] = {
            'title': '' if _PLACEHOLDER.match(title.strip()) else title.strip(),
            'plot': (ep.get('overview') or ep.get('summary') or '').replace('`', "'").strip(),
            'thumb': ep.get('image') or '',
            'airdate': str(ep.get('airDate') or ep.get('airdate') or '')[:10],
            'duration': duration,
        }
    return out


def _ttl(episodes, now):
    """One day while the entry is airing (an episode aired lately or still to air), else 30 days."""
    for ep in episodes.values():
        aired = _to_date(ep['airdate'])
        if aired and time.mktime(aired.timetuple()) > now - AIRING_WINDOW:
            return TTL_AIRING
    return TTL_SETTLED if episodes else TTL_AIRING


def _request(anilist_id):
    """{} for an id AniZip does not know, the episodes otherwise, None when the request failed."""
    import requests
    try:
        response = requests.get(ANIZIP_URL, params={'anilist_id': str(anilist_id)}, timeout=TIMEOUT, headers={'User-Agent': 'TMDbHelper'})
    except Exception as exc:
        kodi_log(f'Anime episodes: AniZip request failed for {anilist_id}: {type(exc).__name__}', 1)
        return None
    if response.status_code == 404:
        return {}
    if response.status_code != 200:
        kodi_log(f'Anime episodes: AniZip answered HTTP {response.status_code} for {anilist_id}', 1)
        return None
    try:
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError('not an object')
        return _parse(data)
    except (ValueError, AttributeError, TypeError):   # not JSON, or JSON of a shape _parse cannot read: a failure, so the breaker counts it
        kodi_log(f'Anime episodes: AniZip sent an unreadable body for {anilist_id}', 1)
        return None


def _rows(conn, anilist_id):
    rows = conn.execute('SELECT number, title, plot, thumb, airdate, duration FROM episode WHERE anilist_id=?', (anilist_id,)).fetchall()
    return {r[0]: {'title': r[1], 'plot': r[2], 'thumb': r[3], 'airdate': r[4], 'duration': r[5]} for r in rows}


def _stored(conn, anilist_id, now):
    """(found, episodes): the still-valid saved answer for an entry; an empty dict when the saved answer is a failure."""
    row = conn.execute('SELECT fetched, ttl, ok FROM entry WHERE anilist_id=?', (anilist_id,)).fetchone()
    if not row or now - row[0] >= row[1]:
        return False, {}
    return True, (_rows(conn, anilist_id) if row[2] else {})


def _breaker_open(conn, now):
    row = conn.execute("SELECT value FROM state WHERE key='breaker_until'").fetchone()
    return bool(row and row[0] > now)


def _save(conn, anilist_id, episodes, now):
    with conn:
        conn.execute('DELETE FROM episode WHERE anilist_id=?', (anilist_id,))
        conn.executemany(
            'INSERT INTO episode VALUES (?,?,?,?,?,?,?)',
            [(anilist_id, n, e['title'], e['plot'], e['thumb'], e['airdate'], e['duration']) for n, e in episodes.items()])
        conn.execute('INSERT OR REPLACE INTO entry VALUES (?,?,?,1)', (anilist_id, now, _ttl(episodes, now)))


def _save_failure(conn, anilist_id, now):
    """A failed refresh keeps the episodes already saved (stale beats nothing) and only moves the retry time."""
    with conn:
        if conn.execute('SELECT 1 FROM episode WHERE anilist_id=? LIMIT 1', (anilist_id,)).fetchone():
            conn.execute('INSERT OR REPLACE INTO entry VALUES (?,?,?,1)', (anilist_id, now, TTL_FAILED))
        else:
            conn.execute('INSERT OR REPLACE INTO entry VALUES (?,?,?,0)', (anilist_id, now, TTL_FAILED))


def _expire_state():
    """Drop what this process remembers once it is old: under a reused interpreter the module lives for hours."""
    global _epoch, _fetched, _failures
    now = time.time()
    with _lock:
        if now - _epoch > STATE_SECONDS:
            _memory.clear()
            _index_memo.clear()
            _entry_locks.clear()
            _fetched = _failures = 0
            _epoch = now


def _entry_episodes(anilist_id):
    """{episode: dict} of one AniList entry, or None when it could not be had right now (breaker, fetch cap or failure)."""
    global _fetched, _failures
    with _lock:
        if anilist_id in _memory:
            return _memory[anilist_id]
        entry_lock = _entry_locks.setdefault(anilist_id, threading.Lock())
    with entry_lock:   # the episodes of one season are built in parallel: one of them fetches, the others wait for it
        if anilist_id in _memory:
            return _memory[anilist_id]
        now = time.time()
        conn = _connect()
        stale = {}
        try:
            if conn is not None:
                try:
                    found, episodes = _stored(conn, anilist_id, now)
                    if found:
                        _memory[anilist_id] = episodes
                        return episodes
                    stale = _rows(conn, anilist_id)   # expired, but better than nothing when no refresh can happen now
                    if _breaker_open(conn, now):
                        return stale or None
                except sqlite3.Error:
                    pass
            with _lock:
                if _fetched >= MAX_FETCHES:
                    return stale or None
                _fetched += 1
            episodes = _request(anilist_id)
            with _lock:
                _failures = 0 if episodes is not None else _failures + 1
                tripped = _failures >= BREAKER_FAILURES
            if conn is not None:
                try:
                    if episodes is None:
                        _save_failure(conn, anilist_id, now)
                    else:
                        _save(conn, anilist_id, episodes, now)
                    if tripped:
                        with conn:
                            conn.execute("INSERT OR REPLACE INTO state VALUES ('breaker_until', ?)", (now + BREAKER_SECONDS,))
                        kodi_log(f'Anime episodes: AniZip failed {BREAKER_FAILURES} times in a row, no fetching for {BREAKER_SECONDS // 60} minutes', 1)
                except sqlite3.Error as exc:
                    kodi_log(f'Anime episodes: cache write failed: {type(exc).__name__}', 1)
            if episodes is None:
                return stale or None
            _memory[anilist_id] = episodes
            kodi_log(f'Anime episodes: fetched {len(episodes)} episodes for AniList {anilist_id}', 1)
            return episodes
        finally:
            if conn is not None:
                conn.close()


def _index_entries(tmdb_id):
    key = str(tmdb_id)
    if key not in _index_memo:
        from tmdbhelper.lib.api.anilist import fribb
        _index_memo[key] = fribb.tv_entries(tmdb_id)
    return _index_memo[key]


def entry_for_episode(tmdb_id, season, episode, tmdb_airdate=None):
    """(AniList id, number inside the entry) or None - see entry_status, which also says WHY there is none."""
    return entry_status(tmdb_id, season, episode, tmdb_airdate)[1]


def entry_status(tmdb_id, season, episode, tmdb_airdate=None):
    """('ok', (AniList id, number inside the entry)), ('later', None) when an AniZip fetch failed or was held back (cap / breaker): ask
    again later, or ('none', None) when no entry fits. (AniList id, the episode's number inside that entry) for TMDb episode `season`x`episode` of a show the Fribb index knows,
    or None. The same candidates and the same air-date check as episode_overlay (a Fribb offset is wrong about one time in
    seven, so a mapping only counts when AniZip's air date agrees with TMDb's; with no TMDb air date only a single candidate
    counts), but it returns the entry itself, for a service that numbers anime by entry (Simkl)."""
    _expire_state()
    entries = _index_entries(tmdb_id)
    if not entries:
        return 'none', None
    try:
        season, episode = int(season), int(episode)
    except (ValueError, TypeError):
        return 'none', None
    if season < 1 or episode < 1:
        return 'none', None
    candidates = [(anilist_id, offset or 0) for anilist_id, entry_season, offset in entries
                  if entry_season == season and (offset or 0) < episode]
    candidates.sort(key=lambda c: (-c[1], c[0]))
    candidates = candidates[:MAX_CANDIDATES]
    tmdb_date = _to_date(tmdb_airdate) if tmdb_airdate else None
    later = False
    for anilist_id, offset in candidates:
        episodes = _entry_episodes(anilist_id)
        if episodes is None:           # could not be had right now (breaker, fetch cap or failure): not the same as 'no such episode'
            later = True
            continue
        data = episodes.get(episode - offset)
        if not data:
            continue
        aired = _to_date(data['airdate'])
        if tmdb_date and aired:
            if abs((aired - tmdb_date).days) > DATE_TOLERANCE:
                continue
        elif len(candidates) > 1:
            continue
        return 'ok', (anilist_id, episode - offset)
    return ('later' if later else 'none'), None


def episode_overlay(tmdb_id, season, episode, tmdb_airdate=None):
    """(overlay or None, indexed): the AniZip data for TMDb episode `season`x`episode` of a show, and whether the Fribb index
    knows the show at all (when it does, the caller must not fall back to another source for an episode this rejected)."""
    _expire_state()
    entries = _index_entries(tmdb_id)
    if not entries:
        return None, False
    try:
        season, episode = int(season), int(episode)
    except (ValueError, TypeError):
        return None, True
    if season < 1 or episode < 1:   # specials are not numbered the same way in TMDb and AniZip
        return None, True

    candidates = []
    for anilist_id, entry_season, offset in entries:
        if entry_season == season and (offset or 0) < episode:   # an entry whose season the index does not know is never guessed
            candidates.append((anilist_id, offset or 0))
    candidates.sort(key=lambda c: (-c[1], c[0]))   # the entry that started last before this episode first
    candidates = candidates[:MAX_CANDIDATES]
    tmdb_date = _to_date(tmdb_airdate) if tmdb_airdate else None

    why = 'no entry for this season'
    for anilist_id, offset in candidates:
        episodes = _entry_episodes(anilist_id)
        if episodes is None:
            why = 'AniZip not available right now'
            continue
        data = episodes.get(episode - offset)
        if not data:
            why = f'AniList {anilist_id} has no episode {episode - offset}'
            continue
        aired = _to_date(data['airdate'])
        if tmdb_date and aired:
            gap = abs((aired - tmdb_date).days)
            if gap > DATE_TOLERANCE:
                why = f'AniList {anilist_id} episode {episode - offset} aired {aired}, TMDb says {tmdb_date}'
                continue
            checked = f'air date checked, {gap} d apart'
        elif len(candidates) > 1:   # nothing to check the mapping against, so only a single sure mapping counts
            why = f'AniList {anilist_id} episode {episode - offset} cannot be checked against an air date'
            continue
        else:
            checked = 'no air date to check, single entry'
        overlay = {k: v for k, v in data.items() if v}
        kodi_log(f'Anime episodes: tmdb {tmdb_id} S{season}E{episode} -> AniList {anilist_id} episode {episode - offset} ({checked})', 0)
        return (overlay or None), True
    kodi_log(f'Anime episodes: tmdb {tmdb_id} S{season}E{episode}: no overlay ({why})', 0)
    return None, True
