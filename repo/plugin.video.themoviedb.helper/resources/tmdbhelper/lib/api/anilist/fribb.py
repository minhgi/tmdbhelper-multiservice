"""Offline anime id index from the Fribb anime-lists file (github.com/Fribb/anime-lists), kept on disk.

The ARM service (api/anilist/idmap.py, api/anilist/scores.py) is built from this same file, so the index answers what ARM
would, but with no network call. Two tables:
  tmdb    one row per AniList id that has a TMDb id: which kind of TMDb id it is (the file's own `tv` or `movie` key - TMDb
          numbers movies and shows in one number space, so the kind is the only thing that says which endpoint the number
          belongs to), the id(s), the TMDb season and the episode offset (AniList id -> TMDb, for the lists and the episodes)
  scores  one row per (TMDb id, entry) with the MyAnimeList id, the AniList id, the TMDb season and the media type (TMDb id ->
          MyAnimeList / AniList, for the ranks and scores; entries that have only a MyAnimeList id are in it too)

Nothing here downloads while a list is being built: records() only reads. The download runs from the service
(monitor/cronjob.py) once a week, and only once the AniList id table exists (the user has opened an anime list). The old
index is kept when a download fails or the new file looks broken (the file has lost data in one update before).
"""
import json
import os
import sqlite3
import time

from tmdbhelper.lib.addon.logger import kodi_log

FRIBB_URL = 'https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json'
DB_FILE = 'special://profile/addon_data/plugin.video.themoviedb.helper/anime_fribb.db'
REFRESH_SECONDS = 7 * 24 * 3600
RETRY_SECONDS = 6 * 3600         # after a failed download, no new attempt from this process for this long
MIN_ENTRIES = 30000              # the file holds about 39,600 entries; a much smaller one is a broken download
MIN_MAPPED = 5000                # about 8,300 entries have an AniList id and a TMDb id
SHRINK_FLOOR = 0.9               # a new file with fewer entries than this share of the current index is not trusted
MAX_IDS = 3                      # movie ids tried per entry (the file lists up to 4, usually 1)
SCHEMA = '2'                     # 2 added the scores table; an index without it is rebuilt at the next due check
SCORES_FRESH = 2 * REFRESH_SECONDS   # a TMDb id the index does not list counts as "not anime" only while the index is this young
CHUNK = 500

_last_try = 0.0


def _translate(path):
    try:
        import xbmcvfs
        return xbmcvfs.translatePath(path)
    except ImportError:
        return None


def _db_path():
    return _translate(DB_FILE)


def _int(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _connect():
    """Read connection to the index, or None when there is none (or it is not a Fribb index)."""
    path = _db_path()
    if not path or not os.path.exists(path):
        return None
    conn = None
    try:
        conn = sqlite3.connect(path, timeout=3)
        row = conn.execute("SELECT value FROM meta WHERE key='source'").fetchone()
        if not row or row[0] != 'fribb':
            conn.close()
            return None
        return conn
    except sqlite3.Error:
        if conn is not None:   # a file that is not a database: do not keep it open (Windows cannot replace an open file)
            conn.close()
        return None


def _state():
    """(built_at, source entries, mapped rows) of the index on disk, zeros when there is none."""
    conn = _connect()
    if conn is None:
        return 0, 0, 0
    try:
        meta = dict(conn.execute('SELECT key, value FROM meta').fetchall())
        return _int(meta.get('built_at')) or 0, _int(meta.get('entries')) or 0, _int(meta.get('mapped')) or 0
    except sqlite3.Error:
        return 0, 0, 0
    finally:
        conn.close()


def records(anilist_ids):
    """{AniList id: {'kind': 'tv' or 'movie', 'ids': [TMDb ids], 'season': int or None, 'offset': int or None}}.

    Only AniList ids the index knows a TMDb id for; empty when there is no index. Reads only."""
    ids = [i for i in dict.fromkeys(_int(i) for i in anilist_ids) if i]
    conn = _connect() if ids else None
    if conn is None:
        return {}
    out = {}
    try:
        for start in range(0, len(ids), CHUNK):
            chunk = ids[start:start + CHUNK]
            query = f'SELECT anilist_id, kind, ids, season, ep_offset FROM tmdb WHERE anilist_id IN ({",".join("?" * len(chunk))})'
            for anilist_id, kind, tmdb_ids, season, offset in conn.execute(query, chunk):
                found = [i for i in (_int(x) for x in str(tmdb_ids).split(',')) if i][:MAX_IDS]
                if found and kind in ('tv', 'movie'):
                    out[anilist_id] = {'kind': kind, 'ids': found, 'season': season, 'offset': offset}
    except sqlite3.Error as exc:
        kodi_log(f'Anime Fribb index: read failed: {type(exc).__name__}', 1)
        return {}
    finally:
        conn.close()
    return out


def tv_entries(tmdb_id):
    """[(AniList id, TMDb season or None, episode offset or None)] of the index's tv rows for a TMDb show id, oldest AniList
    id first; empty for a show the index does not know and when there is no index. Reads only."""
    tmdb_id = _int(tmdb_id)
    conn = _connect() if tmdb_id else None
    if conn is None:
        return []
    try:
        return [tuple(row) for row in conn.execute(
            "SELECT anilist_id, season, ep_offset FROM tmdb WHERE kind='tv' AND ids=? ORDER BY anilist_id", (str(tmdb_id),))]
    except sqlite3.Error as exc:
        kodi_log(f'Anime Fribb index: read failed: {type(exc).__name__}', 1)
        return []
    finally:
        conn.close()


def score_records(tmdb_id):
    """[{'m': MyAnimeList id, 'a': AniList id, 's': TMDb season, 'f': media}] of the entries listed for a TMDb id (movie or show:
    one number space), the same records the ARM service answers (api/anilist/scores.py compact()).

    An empty list is an answer ("not anime") only while the index is fresh. None when the index cannot answer: there is none,
    it predates the scores table, it is older than SCORES_FRESH and does not list the id, or it could not be read. Reads only."""
    tmdb_id = _int(tmdb_id)
    conn = _connect() if tmdb_id else None
    if conn is None:
        return None
    try:
        meta = dict(conn.execute('SELECT key, value FROM meta').fetchall())
        if meta.get('schema') != SCHEMA:
            return None
        found = [{'m': m, 'a': a, 's': s, 'f': f} for m, a, s, f in conn.execute(
            'SELECT mal_id, anilist_id, season, media FROM scores WHERE tmdb_id=?', (tmdb_id,))]
        if found:
            return found
        built_at = _int(meta.get('built_at')) or 0
        return [] if 0 <= time.time() - built_at < SCORES_FRESH else None
    except sqlite3.Error as exc:
        kodi_log(f'Anime Fribb index: read failed: {type(exc).__name__}', 1)
        return None
    finally:
        conn.close()


def _schema():
    conn = _connect()
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def update_due():
    """True when the service should (re)build the index now."""
    if time.time() - _last_try < RETRY_SECONDS:
        return False
    try:
        from tmdbhelper.lib.api.anilist.idmap import CACHE_FILE
        table = _translate(CACHE_FILE)
        if not table or not os.path.exists(table):   # the AniList id table is created by the first anime list that resolves ids
            return False
    except ImportError:
        return False
    built_at = _state()[0]
    if built_at and _schema() != SCHEMA:   # an index from before the scores table: rebuild at once, not in up to a week
        return True
    return not built_at or not 0 <= time.time() - built_at < REFRESH_SECONDS


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _build(entries, path):
    """Write the index for the entries to path; returns (mapped rows: entries with an AniList id and a TMDb id)."""
    _remove(path)
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)')
            conn.execute('CREATE TABLE tmdb (anilist_id INTEGER PRIMARY KEY, kind TEXT NOT NULL, ids TEXT NOT NULL, season INTEGER, ep_offset INTEGER)')
            conn.execute('CREATE TABLE scores (tmdb_id INTEGER NOT NULL, mal_id INTEGER, anilist_id INTEGER, season INTEGER, media TEXT)')
            mapped = 0
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get('themoviedb_id'), dict):
                    continue
                anilist_id, mal_id = _int(entry.get('anilist_id')), _int(entry.get('mal_id'))
                tmdb = entry['themoviedb_id']
                tv_id = _int(tmdb.get('tv'))
                movie_ids = [i for i in (_int(x) for x in tmdb['movie']) if i] if isinstance(tmdb.get('movie'), list) else []
                if not (tv_id or movie_ids):
                    continue
                season = entry.get('season') if isinstance(entry.get('season'), dict) else {}
                offset = entry.get('episode_offset') if isinstance(entry.get('episode_offset'), dict) else {}
                if anilist_id or mal_id:   # what ARM's themoviedb lookup answers: every TMDb id of the entry, entries with only a MyAnimeList id too
                    media = entry.get('type') if isinstance(entry.get('type'), str) else None
                    conn.executemany(
                        'INSERT INTO scores VALUES (?,?,?,?,?)',
                        [(i, mal_id, anilist_id, _int(season.get('tmdb')), media) for i in dict.fromkeys(([tv_id] if tv_id else []) + movie_ids)])
                if not anilist_id:
                    continue
                kind, ids = ('tv', [tv_id]) if tv_id else ('movie', movie_ids)
                conn.execute(
                    'INSERT OR REPLACE INTO tmdb VALUES (?,?,?,?,?)',
                    (anilist_id, kind, ','.join(str(i) for i in ids), _int(season.get('tmdb')), _int(offset.get('tmdb'))))
                mapped += 1
            conn.execute('CREATE INDEX scores_tmdb ON scores (tmdb_id)')
            conn.executemany('INSERT INTO meta VALUES (?,?)', [
                ('source', 'fribb'), ('schema', SCHEMA), ('built_at', str(int(time.time()))), ('entries', str(len(entries))), ('mapped', str(mapped))])
        return mapped
    finally:
        conn.close()


def update_db(abort=None):
    """Download the Fribb file and rebuild the index. abort() -> True stops it (Kodi is closing). Returns True when a usable index is on disk.

    Called from the service thread only, never from a list build."""
    global _last_try
    import requests
    _last_try = time.time()
    path = _db_path()
    if not path:
        return False
    _, old_entries, old_mapped = _state()
    had_index = bool(old_entries)
    suffix = f'.{os.getpid()}.tmp'
    json_tmp, db_tmp = path + '.json' + suffix, path + suffix
    kodi_log('Anime Fribb index: updating', 1)
    try:
        response = requests.get(FRIBB_URL, stream=True, timeout=(10, 60), headers={'User-Agent': 'TMDbHelper'})
        response.raise_for_status()
        with open(json_tmp, 'wb') as handle:
            for chunk in response.iter_content(65536):
                if abort and abort():
                    return had_index
                handle.write(chunk)
        with open(json_tmp, 'rb') as handle:
            entries = json.load(handle)
        _remove(json_tmp)
        if not isinstance(entries, list) or len(entries) < MIN_ENTRIES or (old_entries and len(entries) < old_entries * SHRINK_FLOOR):
            kodi_log(f'Anime Fribb index: file rejected ({len(entries) if isinstance(entries, list) else type(entries).__name__} entries, current index {old_entries})', 1)
            return had_index
        if abort and abort():
            return had_index
        total = len(entries)
        mapped = _build(entries, db_tmp)
        if mapped < MIN_MAPPED or (old_mapped and mapped < old_mapped * SHRINK_FLOOR):
            kodi_log(f'Anime Fribb index: new index rejected ({mapped} mapped rows, current index {old_mapped})', 1)
            return had_index
        os.replace(db_tmp, path)
        kodi_log(f'Anime Fribb index: updated, {total} entries, {mapped} with a TMDb id', 1)
        return True
    except Exception as exc:
        kodi_log(f'Anime Fribb index: update failed: {type(exc).__name__}', 1)
        return had_index
    finally:
        _remove(json_tmp)
        _remove(db_tmp)
