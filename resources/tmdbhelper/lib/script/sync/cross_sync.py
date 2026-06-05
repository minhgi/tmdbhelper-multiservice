"""Cross-service sync bridge: mirrors context menu actions between Trakt, MDBList, and Simkl.

IMPORTANT: Mirror functions must never instantiate Trakt, MDBList, or Simkl *Item* sync
classes or call their .sync() methods. Doing so would create an infinite echo loop:
  Trakt.sync() -> mirror_to_mdblist() -> ItemMDbListWatched.sync() -> mirror_to_trakt() -> ...

All functions operate directly against each service's HTTP API and local DB cache.
Only on-action mirroring is performed (forward-going actions). Pre-existing library
data that already diverges between services is not reconciled.

Simkl scope:
  Watched/unwatched: movie, show, season (episode deferred — TVDB/TMDb mismatch)
  Watchlist: add-only mirror to Simkl plan-to-watch. Removes are NOT mirrored because
    Simkl's only removal endpoint (history/remove) clears all list status including watched.
  Ratings: no Simkl rating endpoint — Trakt/MDBList only.
  Collection: no Simkl collection endpoint — Trakt/MDBList only.
"""

from tmdbhelper.lib.addon.logger import kodi_log


def _can_mirror_to_mdblist():
    from tmdbhelper.lib.addon.plugin import get_setting
    return bool(get_setting('cross_sync_services') and get_setting('service_mdblist'))


def _can_mirror_to_trakt():
    from tmdbhelper.lib.addon.plugin import get_setting
    return bool(get_setting('cross_sync_services') and get_setting('service_trakt'))


def _can_mirror_to_simkl():
    from tmdbhelper.lib.addon.plugin import get_setting
    return bool(get_setting('cross_sync_services') and get_setting('service_simkl'))


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _mdb_api():
    from tmdbhelper.lib.api.mdblist.api import MDbList
    return MDbList()


def _mdb_is_authorized(mdb):
    return bool(mdb.user_token.value or mdb.api_key)


def _build_mdb_payload(tmdb_type, tmdb_id, season, episode):
    tmdb_id = int(tmdb_id)
    if tmdb_type == 'movie':
        return {'movies': [{'tmdb': tmdb_id}]}
    if season is None:
        return {'shows': [{'tmdb': tmdb_id}]}
    if episode is None:
        return {'shows': [{'tmdb': tmdb_id, 'seasons': [{'number': int(season)}]}]}
    return {'shows': [{'tmdb': tmdb_id, 'seasons': [
        {'number': int(season), 'episodes': [{'number': int(episode)}]}]}]}


def _mdb_post(mdb, payload, *path_parts):
    try:
        url = mdb.get_request_url(*path_parts)
        return mdb.get_api_request(url, postdata=payload, method='json', headers=mdb.auth_headers)
    except Exception as exc:
        kodi_log(f'cross_sync: MDBList API error: {exc}', 2)
        return None


def _update_mdb_watched_db(mdb, tmdb_type, tmdb_id, season, episode, value):
    """Mirror ItemMDbListWatched.update_watched_in_db() exactly.
    Only writes for movie/episode rows (same scope as the original).
    reset_lastactivities() is intentionally omitted: calling it would trigger
    MDbListSyncWatched.sync() on the next Container.Refresh, which calls
    clear_columns() and wipes the plays value we just wrote.
    """
    mdb_type = 'movie' if tmdb_type == 'movie' else ('episode' if episode is not None else 'show')
    if mdb_type == 'show':
        return  # show/season: leave to periodic sync
    try:
        db_type = tmdb_type
        tmdb_id_int = int(tmdb_id)
        season_int = int(season) if episode is not None else None
        episode_int = int(episode) if episode is not None else None
        sd = mdb.mdblist_syncdata
        item_id = sd.get_name(db_type, tmdb_id_int, season_int, episode_int)
        cache = sd.cache
        if value:
            from datetime import datetime, timezone
            ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
            cache.execute_sql(
                cache.statement_insert_or_ignore('simplecache', ('id',)),
                (item_id,))
            cache.execute_sql(
                'UPDATE simplecache SET plays=?, last_watched_at=?,'
                ' item_type=?, tmdb_type=?, tmdb_id=?,'
                ' season_number=?, episode_number=? WHERE id=?',
                (1, ts, mdb_type, db_type, tmdb_id_int, season_int, episode_int, item_id))
        else:
            cache.execute_sql(
                'UPDATE simplecache SET plays=0, last_watched_at=NULL WHERE id=?',
                (item_id,))
    except Exception as exc:
        kodi_log(f'cross_sync: MDBList watched DB update error: {exc}', 2)


def _update_mdb_toggle_db(mdb, tmdb_type, tmdb_id, key, value):
    """Mirror update_toggle_in_db() for watchlist_listed_at / collection_collected_at.
    Uses show/movie-level item_id (f'{db_type}.{tmdb_id}') to match the source method —
    it never uses get_name() with season/episode even for collection at episode level.
    Only called after a confirmed successful API POST.
    """
    try:
        db_type = tmdb_type
        tmdb_id_int = int(tmdb_id)
        item_id = f'{db_type}.{tmdb_id_int}'
        item_type_str = 'movie' if tmdb_type == 'movie' else 'show'
        cache = mdb.mdblist_syncdata.cache
        if value:
            from datetime import datetime, timezone
            ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
            keys = (key, 'item_type', 'tmdb_type', 'tmdb_id', 'season_number', 'episode_number')
            data = {item_id: [ts, item_type_str, db_type, tmdb_id_int, None, None]}
            cache.set_many_values(keys=keys, data=data)
        else:
            cache.execute_sql(
                cache.statement_delete_keys('simplecache', (key,), conditions='id=?'),
                (item_id,))
    except Exception as exc:
        kodi_log(f'cross_sync: MDBList toggle DB update error: {exc}', 2)


def _update_mdb_rating_db(mdb, tmdb_type, tmdb_id, season, episode, rating):
    """Mirror ItemMDbListRating.update_rating_in_db() for cross-posted Trakt ratings.
    Only called after a confirmed successful API POST.
    """
    if rating is None:
        return
    if tmdb_type == 'movie':
        mdb_type = 'movie'
    elif season is None:
        mdb_type = 'show'
    elif episode is None:
        mdb_type = 'season'
    else:
        mdb_type = 'episode'
    try:
        db_type = tmdb_type
        tmdb_id_int = int(tmdb_id)
        season_int = int(season) if mdb_type in ('season', 'episode') else None
        episode_int = int(episode) if mdb_type == 'episode' else None
        sd = mdb.mdblist_syncdata
        item_id = sd.get_name(db_type, tmdb_id_int, season_int, episode_int)
        cache = sd.cache
        if rating:
            from datetime import datetime, timezone
            ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
            cache.execute_sql(
                cache.statement_insert_or_ignore('simplecache', ('id',)),
                (item_id,))
            cache.execute_sql(
                'UPDATE simplecache SET rating=?, rated_at=?,'
                ' item_type=?, tmdb_type=?, tmdb_id=?,'
                ' season_number=?, episode_number=? WHERE id=?',
                (rating, ts, mdb_type, db_type, tmdb_id_int, season_int, episode_int, item_id))
        else:
            cache.execute_sql(
                'UPDATE simplecache SET rating=NULL, rated_at=NULL WHERE id=?',
                (item_id,))
    except Exception as exc:
        kodi_log(f'cross_sync: MDBList rating DB update error: {exc}', 2)


def _trakt_api():
    from tmdbhelper.lib.api.trakt.api import TraktAPI
    return TraktAPI()


def _trakt_is_authorized(trakt):
    return bool(trakt.authorization)


def _get_trakt_sync_item(trakt, tmdb_type, tmdb_id, season, episode):
    """Return (items_list, type_key) for Trakt postdata, or (None, None) on failure.
    Mirrors ItemSync.get_post_response_item() + get_post_response_type() behavior.
    For seasons, Trakt expects a list of episode objects under key 'episodes'.
    """
    base_type = 'movie' if tmdb_type == 'movie' else 'show'
    trakt_id = trakt.get_id(tmdb_id, 'tmdb', base_type, output_type='trakt')
    if not trakt_id:
        return None, None
    if season is None:
        item = trakt.get_request_lc(f'{base_type}s', trakt_id)
        return ([item] if item else None), f'{base_type}s'
    if episode is None:
        items = trakt.get_request_lc(f'{base_type}s', trakt_id, 'seasons', season)
        if not items:
            return None, None
        return (items if isinstance(items, list) else [items]), 'episodes'
    item = trakt.get_request_lc(f'{base_type}s', trakt_id, 'seasons', season, 'episodes', episode)
    return ([item] if item else None), 'episodes'


def _trakt_post(trakt, *method_parts, postdata):
    try:
        return trakt.post_response('sync', *method_parts, postdata=postdata)
    except Exception as exc:
        kodi_log(f'cross_sync: Trakt API error: {exc}', 2)
        return None


# ── Trakt → MDBList mirrors ────────────────────────────────────────────────────

def mirror_watched_to_mdblist(tmdb_type, tmdb_id, season, episode, remove):
    if not _can_mirror_to_mdblist():
        return
    try:
        mdb = _mdb_api()
        if not _mdb_is_authorized(mdb):
            return
        payload = _build_mdb_payload(tmdb_type, tmdb_id, season, episode)
        path = ('sync', 'watched', 'remove') if remove else ('sync', 'watched')
        if _mdb_post(mdb, payload, *path) is not None:
            _update_mdb_watched_db(mdb, tmdb_type, tmdb_id, season, episode, not remove)
    except Exception as exc:
        kodi_log(f'cross_sync mirror_watched_to_mdblist error: {exc}', 2)


def mirror_rating_to_mdblist(tmdb_type, tmdb_id, season, episode, rating):
    """Mirror Trakt rating to MDBList.
    Trakt ItemRating has allow_seasons=False, so only movie/show/episode arrive here.
    Local DB write mirrors ItemMDbListRating.update_rating_in_db() — no read-sync
    exists for rating/rated_at columns (sync=None), so without this the MDBList
    context menu would never show the correct label after a Trakt-side rating change.
    """
    if not _can_mirror_to_mdblist():
        return
    try:
        mdb = _mdb_api()
        if not _mdb_is_authorized(mdb):
            return
        tmdb_id_int = int(tmdb_id)
        if tmdb_type == 'movie':
            item = {'tmdb': tmdb_id_int}
            if rating:
                item['rating'] = rating
            payload = {'movies': [item]}
        elif season is None:  # show
            item = {'tmdb': tmdb_id_int}
            if rating:
                item['rating'] = rating
            payload = {'shows': [item]}
        else:  # episode
            ep = {'number': int(episode)}
            if rating:
                ep['rating'] = rating
            payload = {'shows': [{'tmdb': tmdb_id_int, 'seasons': [
                {'number': int(season), 'episodes': [ep]}]}]}
        path = ('sync', 'ratings', 'remove') if rating == 0 else ('sync', 'ratings')
        if _mdb_post(mdb, payload, *path) is not None:
            _update_mdb_rating_db(mdb, tmdb_type, tmdb_id, season, episode, rating)
    except Exception as exc:
        kodi_log(f'cross_sync mirror_rating_to_mdblist error: {exc}', 2)


def mirror_watchlist_to_mdblist(tmdb_type, tmdb_id, remove):
    """Mirror Trakt watchlist add/remove to MDBList.
    Local DB write mirrors update_toggle_in_db() — watchlist_listed_at has no
    read-sync in pre_sync(), so without this the MDBList toggle would stay stale.
    """
    if not _can_mirror_to_mdblist():
        return
    try:
        mdb = _mdb_api()
        if not _mdb_is_authorized(mdb):
            return
        payload = _build_mdb_payload(tmdb_type, tmdb_id, None, None)
        path = ('watchlist', 'items', 'remove') if remove else ('watchlist', 'items', 'add')
        if _mdb_post(mdb, payload, *path) is not None:
            _update_mdb_toggle_db(mdb, tmdb_type, tmdb_id, 'watchlist_listed_at', not remove)
    except Exception as exc:
        kodi_log(f'cross_sync mirror_watchlist_to_mdblist error: {exc}', 2)


def mirror_collection_to_mdblist(tmdb_type, tmdb_id, season, episode, remove):
    """Mirror Trakt collection add/remove to MDBList.
    Local DB write mirrors update_toggle_in_db() — same reasoning as watchlist.
    """
    if not _can_mirror_to_mdblist():
        return
    try:
        mdb = _mdb_api()
        if not _mdb_is_authorized(mdb):
            return
        payload = _build_mdb_payload(tmdb_type, tmdb_id, season, episode)
        path = ('sync', 'collection', 'remove') if remove else ('sync', 'collection')
        if _mdb_post(mdb, payload, *path) is not None:
            _update_mdb_toggle_db(mdb, tmdb_type, tmdb_id, 'collection_collected_at', not remove)
    except Exception as exc:
        kodi_log(f'cross_sync mirror_collection_to_mdblist error: {exc}', 2)


# ── MDBList → Trakt mirrors ────────────────────────────────────────────────────

def mirror_watched_to_trakt(tmdb_type, tmdb_id, season, episode, remove):
    if not _can_mirror_to_trakt():
        return
    try:
        trakt = _trakt_api()
        if not _trakt_is_authorized(trakt):
            return
        items, type_key = _get_trakt_sync_item(trakt, tmdb_type, tmdb_id, season, episode)
        if not items or not type_key:
            return
        method = 'history/remove' if remove else 'history'
        _trakt_post(trakt, method, postdata={type_key: items})
        try:
            trakt.trakt_syncdata.reset_lastactivities()
        except Exception:
            pass
    except Exception as exc:
        kodi_log(f'cross_sync mirror_watched_to_trakt error: {exc}', 2)


def mirror_rating_to_trakt(tmdb_type, tmdb_id, season, episode, rating):
    """Mirror MDBList rating to Trakt.
    Season-level ratings are skipped: Trakt uses a different nested payload format
    for seasons, and ItemRating.allow_seasons=False means seasons never originate
    from the Trakt side either.
    """
    if not _can_mirror_to_trakt():
        return
    if season is not None and episode is None:
        return  # season-level: no matching Trakt rating format
    try:
        trakt = _trakt_api()
        if not _trakt_is_authorized(trakt):
            return
        items, type_key = _get_trakt_sync_item(trakt, tmdb_type, tmdb_id, season, episode)
        if not items or not type_key:
            return
        for item in items:
            if isinstance(item, dict):
                item['rating'] = rating
        method = 'ratings/remove' if rating == 0 else 'ratings'
        _trakt_post(trakt, method, postdata={type_key: items})
        try:
            trakt.trakt_syncdata.reset_lastactivities()
        except Exception:
            pass
    except Exception as exc:
        kodi_log(f'cross_sync mirror_rating_to_trakt error: {exc}', 2)


def mirror_watchlist_to_trakt(tmdb_type, tmdb_id, remove):
    if not _can_mirror_to_trakt():
        return
    try:
        trakt = _trakt_api()
        if not _trakt_is_authorized(trakt):
            return
        items, type_key = _get_trakt_sync_item(trakt, tmdb_type, tmdb_id, None, None)
        if not items or not type_key:
            return
        method = 'watchlist/remove' if remove else 'watchlist'
        _trakt_post(trakt, method, postdata={type_key: items})
        try:
            trakt.trakt_syncdata.reset_lastactivities()
        except Exception:
            pass
    except Exception as exc:
        kodi_log(f'cross_sync mirror_watchlist_to_trakt error: {exc}', 2)


def mirror_collection_to_trakt(tmdb_type, tmdb_id, season, episode, remove):
    if not _can_mirror_to_trakt():
        return
    try:
        trakt = _trakt_api()
        if not _trakt_is_authorized(trakt):
            return
        items, type_key = _get_trakt_sync_item(trakt, tmdb_type, tmdb_id, season, episode)
        if not items or not type_key:
            return
        method = 'collection/remove' if remove else 'collection'
        _trakt_post(trakt, method, postdata={type_key: items})
        try:
            trakt.trakt_syncdata.reset_lastactivities()
        except Exception:
            pass
    except Exception as exc:
        kodi_log(f'cross_sync mirror_collection_to_trakt error: {exc}', 2)


# ── Simkl helpers ──────────────────────────────────────────────────────────────

def _simkl_api():
    from tmdbhelper.lib.api.simkl.api import SimklAPI
    return SimklAPI()


def _simkl_is_authorized(simkl):
    return bool(simkl.user_token.value)


def _build_simkl_payload(tmdb_type, tmdb_id, season, to=None):
    """Build Simkl write payload using TMDb IDs.
    tmdb_type is 'movie' or 'tv'. Episode-level not supported (TVDB/TMDb mismatch).
    """
    tmdb_id = int(tmdb_id)
    ids = {'ids': {'tmdb': tmdb_id}}
    if to:
        ids['to'] = to
    if tmdb_type == 'movie':
        return {'movies': [ids]}
    if season is None:
        return {'shows': [ids]}
    item = {'ids': {'tmdb': tmdb_id}, 'seasons': [{'number': int(season)}]}
    if to:
        item['to'] = to
    return {'shows': [item]}


def _simkl_post(simkl, payload, *path_parts):
    try:
        base_url = 'https://api.simkl.com'
        url = f"{base_url}/{'/'.join(path_parts)}?client_id={simkl.client_id}"
        return simkl.get_api_request(url, postdata=payload, method='json', headers=simkl.auth_headers)
    except Exception as exc:
        kodi_log(f'cross_sync: Simkl API error: {exc}', 2)
        return None


def _update_simkl_watched_db(simkl, tmdb_type, tmdb_id, season, value):
    """Mirror ItemSimklSyncBase.update_watched_in_db() for cross-posted watched state.
    Movie: writes plays + last_watched_at.
    Show: writes last_watched_at + watched_episodes=9999 sentinel (badge appears immediately).
    Season-level DB write skipped — left to next periodic sync.
    """
    try:
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        cache = simkl.simkl_syncdata.cache

        if tmdb_type == 'movie':
            item_id = f'movie.{int(tmdb_id)}'
            cache.execute_sql(
                cache.statement_insert_or_ignore('simplecache', ('id',)), (item_id,))
            if value:
                cache.execute_sql(
                    'UPDATE simplecache SET plays=?, last_watched_at=?,'
                    ' item_type=?, tmdb_type=?, tmdb_id=?,'
                    ' season_number=?, episode_number=? WHERE id=?',
                    (1, ts, 'movie', 'movie', int(tmdb_id), None, None, item_id))
            else:
                cache.execute_sql(
                    'UPDATE simplecache SET plays=0, last_watched_at=NULL WHERE id=?',
                    (item_id,))

        elif tmdb_type == 'tv' and season is None:
            item_id = f'tv.{int(tmdb_id)}'
            cache.execute_sql(
                cache.statement_insert_or_ignore('simplecache', ('id',)), (item_id,))
            if value:
                cache.execute_sql(
                    'UPDATE simplecache SET last_watched_at=?, watched_episodes=?,'
                    ' item_type=?, tmdb_type=?, tmdb_id=? WHERE id=?',
                    (ts, 9999, 'show', 'tv', int(tmdb_id), item_id))
            else:
                cache.execute_sql(
                    'UPDATE simplecache SET last_watched_at=NULL, watched_episodes=0 WHERE id=?',
                    (item_id,))

        # season-level: DB write deferred to next periodic sync
    except Exception as exc:
        kodi_log(f'cross_sync: Simkl watched DB update error: {exc}', 2)


def _update_simkl_status_db(simkl, tmdb_type, tmdb_id, key, value):
    """Write-through for plantowatch_at in SimklSync.db after a confirmed API POST.
    Mirrors ItemSimklSyncBase.update_status_in_db() — sync=None for status columns means
    without this the Simkl context menu toggle stays stale after a mirror action.
    """
    try:
        db_type = 'movie' if tmdb_type == 'movie' else 'tv'
        tmdb_id_int = int(tmdb_id)
        item_id = f'{db_type}.{tmdb_id_int}'
        item_type_str = 'movie' if tmdb_type == 'movie' else 'show'
        cache = simkl.simkl_syncdata.cache
        if value:
            from datetime import datetime, timezone
            ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
            keys = (key, 'item_type', 'tmdb_type', 'tmdb_id', 'season_number', 'episode_number')
            data = {item_id: [ts, item_type_str, db_type, tmdb_id_int, None, None]}
            cache.set_many_values(keys=keys, data=data)
        else:
            cache.execute_sql(
                cache.statement_delete_keys('simplecache', (key,), conditions='id=?'),
                (item_id,))
    except Exception as exc:
        kodi_log(f'cross_sync: Simkl status DB update error: {exc}', 2)


# ── → Simkl mirrors ────────────────────────────────────────────────────────────

def mirror_watched_to_simkl(tmdb_type, tmdb_id, season, episode, remove):
    """Mirror watched/unwatched to Simkl. Episodes skipped (TVDB/TMDb mismatch).
    Show mark-watched uses /sync/add-to-list (to=completed) per Simkl's own pattern;
    all other writes and removals use /sync/history[/remove].
    """
    if not _can_mirror_to_simkl():
        return
    if episode is not None:
        return
    try:
        simkl = _simkl_api()
        if not _simkl_is_authorized(simkl):
            return
        if remove:
            payload = _build_simkl_payload(tmdb_type, tmdb_id, season)
            resp = _simkl_post(simkl, payload, 'sync', 'history', 'remove')
        elif tmdb_type == 'movie':
            from datetime import datetime, timezone
            ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            payload = {'movies': [{'watched_at': ts, 'ids': {'tmdb': int(tmdb_id)}}]}
            resp = _simkl_post(simkl, payload, 'sync', 'history')
        elif season is None:
            payload = _build_simkl_payload(tmdb_type, tmdb_id, None, to='completed')
            resp = _simkl_post(simkl, payload, 'sync', 'add-to-list')
        else:
            payload = _build_simkl_payload(tmdb_type, tmdb_id, season)
            resp = _simkl_post(simkl, payload, 'sync', 'history')
        if resp is not None:
            _update_simkl_watched_db(simkl, tmdb_type, tmdb_id, season, not remove)
    except Exception as exc:
        kodi_log(f'cross_sync mirror_watched_to_simkl error: {exc}', 2)


def mirror_watchlist_to_simkl(tmdb_type, tmdb_id):
    """Mirror watchlist add to Simkl plan-to-watch. Add-only — removes are NOT mirrored:
    Simkl's history/remove endpoint clears ALL list status including watched/completed.
    """
    if not _can_mirror_to_simkl():
        return
    try:
        simkl = _simkl_api()
        if not _simkl_is_authorized(simkl):
            return
        payload = _build_simkl_payload(tmdb_type, tmdb_id, None, to='plantowatch')
        if _simkl_post(simkl, payload, 'sync', 'add-to-list') is not None:
            _update_simkl_status_db(simkl, tmdb_type, tmdb_id, 'plantowatch_at', True)
    except Exception as exc:
        kodi_log(f'cross_sync mirror_watchlist_to_simkl error: {exc}', 2)
