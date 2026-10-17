#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Anime episode metadata enrichment from Seren's anime cache.

Phase 6 of the Seren Anime Features Plan. Reads Seren's animeCache.db directly
via sqlite3 to overlay anime-specific episode titles, plots, and thumbnails onto
TMDb Helper's episode ListItems. Zero API calls — all data comes from Seren's
pre-populated SQLite cache.

Flow:
  1. Given a TMDb show ID, query anime_id_map for anime IDs
  2. Build the episode cache key (mal_{id}, al_{id}, etc.)
  3. Query anime_episodes for the specific season/episode
  4. Return overlay dict with title, plot, thumb (or None if not cached)

The anime_id_map is populated by Seren's anilist_mapping.py when users browse
anime content. If a show has never been browsed in Seren, there will be no
mapping, and this module gracefully returns None (TMDb data is kept as-is).
"""

import os
import sqlite3
import time
import threading

_SEREN_ADDON_ID = 'plugin.video.seren'
_DB_FILENAME = 'animeCache.db'

# Module-level connection cache to avoid reopening the DB on every episode.
# Lock protects creation; check_same_thread=False allows ParallelThread use.
_conn = None
_conn_path = None
_conn_lock = threading.Lock()

# Cached DB path — resolved once per session (survives across threaded calls)
_resolved_path = None
_path_resolved = False

# In-memory cache: tmdb_id → show_id (or _MISS sentinel for negative cache).
# Avoids 23 redundant anime_id_map queries when processing a 24-episode season
# in parallel via configure_listitems_threaded().
_MISS = object()
_show_id_cache = {}


def _get_seren_db_path():
    """Locate Seren's animeCache.db in Kodi's addon_data directory.

    Caches the resolved path after first successful lookup.
    Returns None if Seren is not installed or DB doesn't exist.
    """
    global _resolved_path, _path_resolved

    if _path_resolved:
        # Re-check existence in case DB was deleted, but skip translatePath
        if _resolved_path and os.path.exists(_resolved_path):
            return _resolved_path
        _resolved_path = None
        _path_resolved = False

    # Primary: direct special:// path resolution
    try:
        import xbmcvfs
        path = xbmcvfs.translatePath(
            f'special://profile/addon_data/{_SEREN_ADDON_ID}/{_DB_FILENAME}'
        )
        if os.path.exists(path):
            _resolved_path = path
            _path_resolved = True
            return path
    except Exception:
        pass

    # Fallback: resolve via Seren's addon profile
    try:
        import xbmcvfs
        import xbmcaddon
        seren = xbmcaddon.Addon(_SEREN_ADDON_ID)
        profile = xbmcvfs.translatePath(seren.getAddonInfo('profile'))
        path = os.path.join(profile, _DB_FILENAME)
        if os.path.exists(path):
            _resolved_path = path
            _path_resolved = True
            return path
    except Exception:
        pass

    # Seren not available right now. Don't cache the negative result — the user
    # may install Seren mid-session, so re-check on the next call. (The retry cost
    # is one xbmcvfs.translatePath + one os.path.exists per episode lookup miss.)
    return None


def _get_connection():
    """Get or create a read-only sqlite3 connection to Seren's anime cache.

    Thread-safe: uses a lock for connection creation and check_same_thread=False
    so ParallelThread workers can share the connection.
    """
    global _conn, _conn_path

    db_path = _get_seren_db_path()
    if not db_path:
        return None

    # Fast path: reuse existing connection (no lock needed for read)
    conn = _conn
    if conn is not None and _conn_path == db_path:
        try:
            conn.execute('SELECT 1')
            return conn
        except Exception:
            pass

    # Slow path: create new connection under lock
    with _conn_lock:
        # Double-check after acquiring lock
        if _conn is not None and _conn_path == db_path:
            try:
                _conn.execute('SELECT 1')
                return _conn
            except Exception:
                _conn = None

        try:
            # Open read-only via URI to avoid WAL/lock conflicts with Seren.
            # check_same_thread=False required because ParallelThread workers
            # in configure_listitems_threaded() call from different threads.
            uri = f'file:{db_path}?mode=ro'
            conn = sqlite3.connect(uri, uri=True, timeout=2,
                                   check_same_thread=False)
            conn.row_factory = sqlite3.Row
            _conn = conn
            _conn_path = db_path
            return conn
        except Exception:
            return None


def _build_episode_cache_key(id_row):
    """Build the episode cache key from an anime_id_map row.

    Mirrors Seren's episode_meta._build_cache_key() priority:
    mal_id → anilist_id → anidb_id → simkl_id → kitsu_id
    """
    mal_id = id_row['mal_id']
    if mal_id:
        return f'mal_{mal_id}'

    anilist_id = id_row['anilist_id']
    if anilist_id:
        return f'al_{anilist_id}'

    anidb_id = id_row['anidb_id']
    if anidb_id:
        return f'adb_{anidb_id}'

    simkl_id = id_row['simkl_id']
    if simkl_id:
        return f'smk_{simkl_id}'

    kitsu_id = id_row['kitsu_id']
    if kitsu_id:
        return f'kit_{kitsu_id}'

    return None


def _resolve_show_id(conn, tmdb_id, now):
    """Resolve a TMDb ID to an anime episode cache key, with in-memory caching.

    Eliminates redundant anime_id_map queries when processing multiple episodes
    of the same show in parallel (configure_listitems_threaded).
    """
    cache_key = str(tmdb_id)
    cached = _show_id_cache.get(cache_key)
    if cached is _MISS:
        return None
    if cached is not None:
        return cached

    # Query anime_id_map — Seren stores lookup_key as "tmdb_{id}"
    lookup_key = f'tmdb_{tmdb_id}'
    id_row = conn.execute(
        'SELECT anilist_id, mal_id, anidb_id, kitsu_id, simkl_id '
        'FROM anime_id_map WHERE lookup_key=? AND expires>?',
        (lookup_key, now)
    ).fetchone()

    if not id_row:
        _show_id_cache[cache_key] = _MISS
        return None

    show_id = _build_episode_cache_key(id_row)
    _show_id_cache[cache_key] = show_id or _MISS
    return show_id


def get_anime_episode_overlay(tmdb_id, season, episode):
    """Look up anime episode metadata from Seren's cache.

    Args:
        tmdb_id: TMDb TV show ID (int or str)
        season: Season number (int)
        episode: Episode number (int)

    Returns:
        dict with keys 'title', 'plot', 'thumb' if found, else None.
        Only includes keys that have non-empty values.
    """
    if not tmdb_id or season is None or episode is None:
        return None

    conn = _get_connection()
    if not conn:
        return None

    try:
        now = int(time.time())

        # Step 1: Resolve tmdb_id → show_id (cached in memory)
        show_id = _resolve_show_id(conn, tmdb_id, now)
        if not show_id:
            return None

        # Step 2: Fetch episode metadata
        ep_row = conn.execute(
            'SELECT title, plot, thumb, airdate, duration '
            'FROM anime_episodes WHERE show_id=? AND season=? AND episode=? AND expires>?',
            (show_id, int(season), int(episode), now)
        ).fetchone()

        if not ep_row:
            return None

        # Step 3: Build overlay dict (only non-empty values)
        overlay = {}
        if ep_row['title']:
            overlay['title'] = ep_row['title']
        if ep_row['plot']:
            overlay['plot'] = ep_row['plot']
        if ep_row['thumb']:
            overlay['thumb'] = ep_row['thumb']
        if ep_row['airdate']:
            overlay['airdate'] = ep_row['airdate']
        if ep_row['duration']:
            overlay['duration'] = ep_row['duration']

        return overlay if overlay else None

    except Exception:
        return None


def get_anime_season_overlays(tmdb_id, season):
    """Batch-fetch all cached anime episode metadata for a season.

    More efficient than per-episode lookups when building episode lists.

    Args:
        tmdb_id: TMDb TV show ID
        season: Season number

    Returns:
        dict mapping episode numbers to overlay dicts, or empty dict.
        {1: {'title': '...', 'plot': '...', 'thumb': '...'}, 2: {...}, ...}
    """
    if not tmdb_id or season is None:
        return {}

    conn = _get_connection()
    if not conn:
        return {}

    try:
        now = int(time.time())

        show_id = _resolve_show_id(conn, tmdb_id, now)
        if not show_id:
            return {}

        rows = conn.execute(
            'SELECT episode, title, plot, thumb, airdate, duration '
            'FROM anime_episodes WHERE show_id=? AND season=? AND expires>?',
            (show_id, int(season), now)
        ).fetchall()

        if not rows:
            return {}

        result = {}
        for row in rows:
            overlay = {}
            if row['title']:
                overlay['title'] = row['title']
            if row['plot']:
                overlay['plot'] = row['plot']
            if row['thumb']:
                overlay['thumb'] = row['thumb']
            if row['airdate']:
                overlay['airdate'] = row['airdate']
            if row['duration']:
                overlay['duration'] = row['duration']
            if overlay:
                result[row['episode']] = overlay

        return result

    except Exception:
        return {}


def is_anime_enrichment_enabled():
    """Check if the anime enrichment setting is enabled in TMDb Helper."""
    try:
        from tmdbhelper.lib.addon.plugin import get_setting
        return get_setting('anime_enrichment')
    except Exception:
        return False
