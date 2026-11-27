"""Cross-service sync bridge: mirrors context menu actions between Trakt, MDBList, and Simkl (and WeTrakr, see the end of the file).

IMPORTANT: Mirror functions must never instantiate Trakt, MDBList, or Simkl *Item* sync
classes or call their .sync() methods. Doing so would create an infinite echo loop:
  Trakt.sync() -> mirror_to_mdblist() -> ItemMDbListWatched.sync() -> mirror_to_trakt() -> ...
The one exception: mdblist_sync.clear_resume_point() / simkl_sync.clear_resume_point() (a watched mark mirrored in must
not leave a resume point behind) instantiate only the clear-progress items, which never mirror anything.

All functions operate directly against each service's HTTP API and local DB cache.
Only on-action mirroring is performed (forward-going actions). Pre-existing library
data that already diverges between services is not reconciled.

Simkl scope:
  Watched/unwatched: movie, show, season (episode deferred — TVDB/TMDb mismatch)
  Watchlist: add-only mirror to Simkl plan-to-watch. Removes are NOT mirrored because
    Simkl's only removal endpoint (history/remove) clears all list status including watched.
  Ratings: a rating made on Simkl (ItemSimklRating) mirrors out to Trakt and MDBList. INTO Simkl (a rating from
    Trakt, MDBList or WeTrakr, mirror_rating_to_simkl, 7.5.40) only where POST /sync/ratings cannot change a status:
    on any movie that is not 'completed' it makes it 'completed', watched now (clearing the rating does not undo
    that), and an unlisted show becomes 'watching'. A listed show keeps its status. (Live-probed 2026-11-27.)
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
        from tmdbhelper.lib.script.sync.watched_totals import recount_totals
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
            if mdb_type == 'episode':
                recount_totals(cache, tmdb_id_int, season_int, ts)
        else:
            cache.execute_sql(
                'UPDATE simplecache SET plays=0, last_watched_at=NULL WHERE id=?',
                (item_id,))
            if mdb_type == 'episode':
                recount_totals(cache, tmdb_id_int, season_int)
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
            if tmdb_type == 'tv' and season is not None and episode is None:  # a season copy writes no saved rows
                from tmdbhelper.lib.script.sync.mdblist_sync import resync_season_watched
                resync_season_watched(mdb.mdblist_syncdata)
            if not remove:  # a movie / episode now watched on MDBList must not keep its resume point there (issue #13 item 4)
                from tmdbhelper.lib.script.sync.mdblist_sync import clear_resume_point
                clear_resume_point(tmdb_type, tmdb_id, season, episode)
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


def _simkl_not_found(resp):
    """ True when Simkl answered but matched nothing: it replies HTTP 201 with the item under 'not_found' and 'added' at 0 """
    try:
        not_found = resp.json().get('not_found') or {}
    except Exception:
        return False
    return any(not_found.get(k) for k in ('movies', 'shows', 'episodes'))


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
            # completed (watched) and removed (unwatched) both replace a 'watching' status
            cache.execute_sql('UPDATE simplecache SET watching_at=NULL WHERE id=?', (item_id,))
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
        cache.execute_sql('UPDATE simplecache SET watching_at=NULL WHERE id=?', (item_id,))
        if value:
            # Simkl keeps one status per item: setting one clears the other two (as update_status_in_db does)
            others = [k for k in ('plantowatch_at', 'hold_at', 'dropped_at') if k != key]
            cache.execute_sql(
                f'UPDATE simplecache SET {others[0]}=NULL, {others[1]}=NULL WHERE id=?', (item_id,))
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
            if tmdb_type == 'tv' and season is not None:  # Simkl numbers some shows' seasons differently: never remove another season there
                from tmdbhelper.lib.script.sync.unwatch_lists import simkl_numbering_agrees
                if not simkl_numbering_agrees(tmdb_id, [season], simkl):
                    kodi_log(f'cross_sync: season {season} of tv {tmdb_id} not removed on Simkl - Simkl numbers it differently, or it could not be checked', 1)
                    return
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
        if resp is None:
            return
        if remove or not _simkl_not_found(resp):  # a removal of something Simkl does not have leaves it unwatched, as asked
            _update_simkl_watched_db(simkl, tmdb_type, tmdb_id, season, not remove)
            if not remove and tmdb_type == 'movie':  # a movie now watched on Simkl must not keep its resume point there (issue #13 item 4)
                from tmdbhelper.lib.script.sync.simkl_sync import clear_resume_point
                clear_resume_point(tmdb_type, tmdb_id, None, None)
        else:
            kodi_log(f'cross_sync: Simkl did not know {tmdb_type} {tmdb_id} by its TMDb id - not marked watched there or in the local data', 1)
    except Exception as exc:
        kodi_log(f'cross_sync mirror_watched_to_simkl error: {exc}', 2)


def _simkl_movie_is_watched(simkl, tmdb_id):
    """True when Simkl has this movie as watched: GET /sync/all-items/movies/ lists it with last_watched_at
    set or status 'completed' (tmdb ids compared as strings). If that list cannot be read the local play
    count decides. Simkl drops a movie's watched state when it goes to plan-to-watch, hold or dropped.
    """
    try:
        url = f'https://api.simkl.com/sync/all-items/movies/?client_id={simkl.client_id}'
        response = simkl.get_api_request(url, headers=simkl.auth_headers)
        data = response.json() if response is not None else None
        rows = data.get('movies') if isinstance(data, dict) else None
        if isinstance(rows, list):
            for row in rows:
                ids = ((row or {}).get('movie') or {}).get('ids') or {}
                if str(ids.get('tmdb')) == str(tmdb_id):
                    return row.get('last_watched_at') is not None or row.get('status') == 'completed'
            return False
    except Exception as exc:
        kodi_log(f'cross_sync: Simkl library read error: {exc}', 2)
    try:
        row = simkl.simkl_syncdata.cache.execute_sql(
            'SELECT plays FROM simplecache WHERE id=?', (f'movie.{int(tmdb_id)}',), read_only=True).fetchone()
        return bool(row and row[0])
    except Exception:
        return False


def mirror_watchlist_to_simkl(tmdb_type, tmdb_id):
    """Mirror watchlist add to Simkl plan-to-watch. Add-only — removes are NOT mirrored:
    Simkl's history/remove endpoint clears ALL list status including watched/completed.
    A movie Simkl has as watched is left alone (plan-to-watch would un-watch it).
    """
    if not _can_mirror_to_simkl():
        return
    try:
        simkl = _simkl_api()
        if not _simkl_is_authorized(simkl):
            return
        if tmdb_type == 'movie' and _simkl_movie_is_watched(simkl, tmdb_id):
            kodi_log(f'cross_sync: movie {tmdb_id} is watched on Simkl - not moved to plan-to-watch', 1)
            return
        payload = _build_simkl_payload(tmdb_type, tmdb_id, None, to='plantowatch')
        resp = _simkl_post(simkl, payload, 'sync', 'add-to-list')
        if resp is not None and not _simkl_not_found(resp):
            _update_simkl_status_db(simkl, tmdb_type, tmdb_id, 'plantowatch_at', True)
        elif resp is not None:
            kodi_log(f'cross_sync: Simkl did not know {tmdb_type} {tmdb_id} by its TMDb id - not added to plan-to-watch there or in the local data', 1)
    except Exception as exc:
        kodi_log(f'cross_sync mirror_watchlist_to_simkl error: {exc}', 2)


# ── WeTrakr ratings (7.5.38) ───────────────────────────────────────────────────

def _wetrakr_ratings_mode():
    """cross_sync_wetrakr_ratings: 0 off, 1 log only, 2 on. Only while watched marks with WeTrakr are On."""
    from tmdbhelper.lib.addon.plugin import get_setting
    from tmdbhelper.lib.monitor.bridge_sync import _wetrakr_setting_mode
    if _wetrakr_setting_mode() != 2:
        return 0
    try:
        mode = int(get_setting('cross_sync_wetrakr_ratings', 'int'))
    except Exception:
        return 1
    return mode if 0 <= mode <= 2 else 1


def _wetrakr_rating_payload(tmdb_type, tmdb_id, season, episode, rating):
    """Body of POST /sync/ratings (a rating) and /sync/ratings/remove (rating None). A season or episode is nested under its show by number."""
    ids = {'tmdb': int(tmdb_id)}
    extra = {} if rating is None else {'rating': rating}
    if tmdb_type == 'movie':
        return {'movies': [{'ids': ids, **extra}]}
    if season is None:
        return {'shows': [{'ids': ids, **extra}]}
    if episode is None:
        return {'shows': [{'ids': ids, 'seasons': [{'number': int(season), **extra}]}]}
    return {'shows': [{'ids': ids, 'seasons': [{'number': int(season), 'episodes': [{'number': int(episode), **extra}]}]}]}


def _rating_label(tmdb_type, tmdb_id, season, episode):
    numbers = '' if season is None else (f' S{season}' if episode is None else f' S{season}E{episode}')
    return f'{tmdb_type} {tmdb_id}{numbers}'


def mirror_rating_to_wetrakr(tmdb_type, tmdb_id, season, episode, rating):
    """A rating given (or, rating 0, removed) on Trakt, MDBList or Simkl goes to WeTrakr. Direct API call only (no Item* class: no echo)."""
    try:
        mode = _wetrakr_ratings_mode()
        if not mode or rating is None or tmdb_type not in ('movie', 'tv'):
            return
        from tmdbhelper.lib.api.wetrakr.api import WeTrakrAPI
        api = WeTrakrAPI()
        if not api.is_authorized:
            return
        label = _rating_label(tmdb_type, tmdb_id, season, episode)
        what = f'rating {rating}' if rating else 'rating removal'
        if mode == 1:
            kodi_log(f'cross_sync (WeTrakr ratings, log only): On would send {what} for {label} to WeTrakr', 1)
            return
        # 0 is a real rating to WeTrakr, so a removal goes to the remove endpoint without a value
        path = '/sync/ratings' if rating else '/sync/ratings/remove'
        response = api.user_request('POST', path, body=_wetrakr_rating_payload(tmdb_type, tmdb_id, season, episode, rating or None))
        from tmdbhelper.lib.script.sync.wetrakr_sync import read_outcome
        status, detail = read_outcome(response)
        if status == 'failed':
            kodi_log(f'cross_sync: WeTrakr did not take the {what} for {label} - {detail}', 2)
            return
        try:
            api.wetrakr_syncdata.reset_lastactivities()
        except Exception:
            pass
    except Exception as exc:
        kodi_log(f'cross_sync mirror_rating_to_wetrakr error: {type(exc).__name__}: {exc}', 2)


def mirror_rating_from_wetrakr(tmdb_type, tmdb_id, season, episode, rating):
    """A rating given (or, rating 0, removed) on WeTrakr goes to Trakt and MDBList, and to Simkl under that mirror's own setting and checks."""
    try:
        mode = _wetrakr_ratings_mode()
        if not mode or rating is None:
            return
        label = _rating_label(tmdb_type, tmdb_id, season, episode)
        if season is not None and episode is None:
            kodi_log(f'cross_sync: WeTrakr season rating for {label} not sent on (season ratings stay on WeTrakr)', 1)
            return
        if mode == 1:
            what = f'rating {rating}' if rating else 'rating removal'
            kodi_log(f'cross_sync (WeTrakr ratings, log only): On would send {what} for {label} to Trakt and MDBList', 1)
            return
    except Exception as exc:
        kodi_log(f'cross_sync mirror_rating_from_wetrakr error: {type(exc).__name__}: {exc}', 2)
        return
    mirror_rating_to_trakt(tmdb_type, tmdb_id, season, episode, rating)
    mirror_rating_to_mdblist(tmdb_type, tmdb_id, season, episode, rating)
    mirror_rating_to_simkl(tmdb_type, tmdb_id, season, episode, rating)


# ── Ratings into Simkl (7.5.40) ────────────────────────────────────────────────
# POST /sync/ratings is not a rating only: Simkl makes any movie that is not 'completed' completed, watched at that moment, and files an
# unlisted show as 'watching' (live-probed 2026-11-27). So a rating is sent only for a movie Simkl itself lists as completed, or a show
# that is on one of its lists, asked right before sending and written to the entry that was asked about. Anything unclear: nothing is sent.

_SIMKL_LISTS = ('watching', 'plantowatch', 'completed', 'hold', 'dropped', 'notinteresting')   # 'notinteresting' is 'dropped' to some Simkl apps


def _simkl_ratings_mode():
    """cross_sync_simkl_ratings: 0 off, 1 log only, 2 on. Only while the sync is on and Simkl takes part."""
    if not _can_mirror_to_simkl():
        return 0
    from tmdbhelper.lib.addon.plugin import get_setting
    try:
        mode = int(get_setting('cross_sync_simkl_ratings', 'int'))
    except Exception:
        return 1
    return mode if 0 <= mode <= 2 else 1


def _simkl_may_hold(simkl, tmdb_type, tmdb_id):
    """From the saved Simkl data only (no request, no Item* class): a watched movie, or a show in the library. Saves asking Simkl about the rest."""
    keys = ('plays',) if tmdb_type == 'movie' else ('watched_episodes', 'plantowatch_at', 'hold_at', 'dropped_at', 'watching_at')
    try:
        values = simkl.simkl_syncdata.cache.get_values(item_id=f'{tmdb_type}.{int(tmdb_id)}', keys=keys)
    except Exception as exc:
        kodi_log(f'cross_sync (Simkl ratings): saved Simkl data not read - {type(exc).__name__}', 1)
        return False
    if not values:
        return False
    if tmdb_type == 'movie':
        return bool(values[0])
    return values[0] is not None or any(values[1:])


def _simkl_listing(simkl, tmdb_type, tmdb_id):
    """(Simkl id, list) as Simkl answers now, list None when the title is on none. (None, None) when it cannot be told."""
    body = [{'tmdb': int(tmdb_id), 'type': 'movie' if tmdb_type == 'movie' else 'tv'}]
    response = _simkl_post(simkl, body, 'sync', 'watched')
    try:
        row = response.json()[0]
        if row.get('result') not in (True, False):
            return None, None
        return int(row['simkl']), row.get('list')
    except Exception:
        return None, None


def _simkl_list_name(value):
    return 'dropped' if value == 'notinteresting' else value


def _update_simkl_rating_db(simkl, tmdb_type, tmdb_id, rating):
    """Write-through as ItemSimklRating.update_rating_in_db(), so the Simkl menu shows the new value at once."""
    try:
        item_id = f'{tmdb_type}.{int(tmdb_id)}'
        cache = simkl.simkl_syncdata.cache
        if rating:
            from datetime import datetime, timezone
            ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
            cache.execute_sql(cache.statement_insert_or_ignore('simplecache', ('id',)), (item_id,))
            cache.execute_sql(
                'UPDATE simplecache SET rating=?, rated_at=?, item_type=?, tmdb_type=?, tmdb_id=? WHERE id=?',
                (rating, ts, 'movie' if tmdb_type == 'movie' else 'show', tmdb_type, int(tmdb_id), item_id))
        else:
            cache.execute_sql('UPDATE simplecache SET rating=NULL, rated_at=NULL WHERE id=?', (item_id,))
        simkl.simkl_syncdata.reset_lastactivities()
    except Exception as exc:
        kodi_log(f'cross_sync: Simkl rating DB update error: {exc}', 2)


def mirror_rating_to_simkl(tmdb_type, tmdb_id, season, episode, rating):
    """A movie or show rating given (or, rating 0, removed) on Trakt, MDBList or WeTrakr goes to Simkl. Never a season or an episode
    (a season rating overwrites the show's on Simkl). Direct API call only (no Item* class: no echo)."""
    try:
        mode = _simkl_ratings_mode()
        if not mode or rating is None or tmdb_type not in ('movie', 'tv') or season is not None or episode is not None:
            return
        rating = int(rating)
        if not 0 <= rating <= 10:
            return
        simkl = _simkl_api()
        if not _simkl_is_authorized(simkl):
            return
        label = _rating_label(tmdb_type, tmdb_id, None, None)
        what = f'rating {rating}' if rating else 'rating removal'
        key = 'movies' if tmdb_type == 'movie' else 'shows'
        listed = None
        if rating:
            if not _simkl_may_hold(simkl, tmdb_type, tmdb_id):
                kodi_log(f'cross_sync (Simkl ratings): {what} for {label} not sent - the saved Simkl data does not have it as a watched movie or a library show', 1)
                return
            simkl_id, listed = _simkl_listing(simkl, tmdb_type, tmdb_id)
            if simkl_id is None:
                kodi_log(f'cross_sync (Simkl ratings): {what} for {label} not sent - Simkl could not be asked what list it is on', 1)
                return
            if not (listed == 'completed' if tmdb_type == 'movie' else listed in _SIMKL_LISTS):
                kodi_log(f'cross_sync (Simkl ratings): {what} for {label} not sent - Simkl list: {listed or "none"} (a rating would change it)', 1)
                return
            payload = {key: [{'ids': {'simkl': simkl_id}, 'rating': rating}]}
            path = ('sync', 'ratings')
        else:
            # removing a rating changes no list, also for a title on none (live-probed 2026-11-27): nothing to ask first
            payload = {key: [{'ids': {'tmdb': int(tmdb_id)}}]}
            path = ('sync', 'ratings', 'remove')
        if mode == 1:
            where = f' (Simkl list: {listed})' if listed else ''
            kodi_log(f'cross_sync (Simkl ratings, log only): On would send {what} for {label} to Simkl{where}', 1)
            return
        response = _simkl_post(simkl, payload, *path)
        if response is None or _simkl_not_found(response):
            kodi_log(f'cross_sync: Simkl did not take the {what} for {label}', 2)
            return
        if rating:
            try:
                now = response.json()['added']['statuses'][0]['response']['status']
            except Exception:
                now = None
            if now and _simkl_list_name(now) != _simkl_list_name(listed):
                kodi_log(f'cross_sync: Simkl moved {label} from {listed} to {now} when it took the rating', 2)
        kodi_log(f'cross_sync (Simkl ratings): sent {what} for {label} to Simkl', 1)
        _update_simkl_rating_db(simkl, tmdb_type, tmdb_id, rating)
    except Exception as exc:
        kodi_log(f'cross_sync mirror_rating_to_simkl error: {type(exc).__name__}: {exc}', 2)


# ── WeTrakr menu actions (7.5.39) ──────────────────────────────────────────────
# Into WeTrakr: watched / unwatched, watchlist (the planning list) and collection (the library). Out of WeTrakr: watched / unwatched only
# (WeTrakr's menu has no watchlist or collection action). Direct API calls only. When anything cannot be checked the change is left to the
# background sync. The sync's own files (snapshot, journal) are never touched here.

def _wetrakr_menu_mode():
    """cross_sync_wetrakr_menu: 0 off, 1 log only, 2 on. Only while watched marks with WeTrakr are On."""
    from tmdbhelper.lib.addon.plugin import get_setting
    from tmdbhelper.lib.monitor.bridge_sync import _wetrakr_setting_mode
    if _wetrakr_setting_mode() != 2:
        return 0
    try:
        mode = int(get_setting('cross_sync_wetrakr_menu', 'int'))
    except Exception:
        return 1
    return mode if 0 <= mode <= 2 else 1


def _wetrakr_menu_list_mode():
    """Watchlist / collection into WeTrakr: the weaker of the menu setting and the WeTrakr watchlist and collection setting."""
    mode = _wetrakr_menu_mode()
    if not mode:
        return 0
    from tmdbhelper.lib.monitor.bridge_sync import _wetrakr_lists_mode
    return min(mode, _wetrakr_lists_mode())


def _wetrakr_client():
    from tmdbhelper.lib.api.wetrakr.api import WeTrakrAPI
    api = WeTrakrAPI()
    return api if api.is_authorized else None


def _wetrakr_menu_log(message, level=1):
    kodi_log(f'cross_sync (WeTrakr menu): {message}', level)


def _wetrakr_write(api, path, body, what):
    """One write. Returns 'ok' (something changed), 'unchanged' or 'failed'. A 2xx answer is not enough, see read_outcome."""
    from tmdbhelper.lib.script.sync.wetrakr_sync import read_outcome
    response = api.user_request('POST', path, body=body)
    if getattr(response, 'status_code', None) == 420:
        _wetrakr_menu_log(f'{what}: the WeTrakr library is full for this account plan', 2)
        return 'failed'
    status, detail = read_outcome(response)
    if status == 'ok':
        return 'ok'
    if status == 'already':
        return 'unchanged'
    try:
        errored = f'{response.json().get("errored") or ""}'.lower()
    except Exception:
        errored = ''
    if 'nothing to remove' in errored:
        return 'unchanged'
    _wetrakr_menu_log(f'WeTrakr did not take the {what} - {detail}. Left to the sync', 2)
    return 'failed'


def _wetrakr_refresh_local(api):
    try:
        api.wetrakr_syncdata.reset_lastactivities()
    except Exception:
        pass


def _wetrakr_resync_watched(api):
    """After a watched / unwatched copy to WeTrakr: read the watched marks again so the WeTrakr menu is not built from the old ones."""
    try:
        from tmdbhelper.lib.script.sync.wetrakr_sync import resync_watched
        resync_watched(api.wetrakr_syncdata)
    except Exception:
        pass


def _wetrakr_watched_payload(tmdb_type, tmdb_id, season, episode):
    """Body of POST /sync/tracking and /sync/tracking/remove/all. season None = the whole title, so an episode without a season is refused."""
    if tmdb_type not in ('movie', 'tv') or (season is None and episode is not None):
        return None
    ids = {'tmdb': int(tmdb_id)}
    if tmdb_type == 'movie':
        return {'movies': [{'ids': ids, 'status': 'watched'}]}
    item = {'ids': ids, 'status': 'watched'}
    if season is not None:
        entry = {'number': int(season)}
        if episode is not None:
            entry['episodes'] = [{'number': int(episode)}]
        item['seasons'] = [entry]
    return {'shows': [item]}


def mirror_watched_to_wetrakr(tmdb_type, tmdb_id, season, episode, remove):
    """Mark as watched / unwatched made on Trakt, MDBList or Simkl. Unwatched removes every play (remove/all), as the other services do."""
    try:
        mode = _wetrakr_menu_mode()
        if not mode:
            return
        payload = _wetrakr_watched_payload(tmdb_type, tmdb_id, season, episode)
        api = _wetrakr_client() if payload else None
        if not api:
            return
        what = f'{"unwatched" if remove else "watched"} mark for {_rating_label(tmdb_type, tmdb_id, season, episode)}'
        if mode == 1:
            _wetrakr_menu_log(f'log only: On would send the {what} to WeTrakr')
            return
        if not remove:
            payload['allow_rewatch'] = False  # never a second play for a title WeTrakr already has
        if _wetrakr_write(api, '/sync/tracking/remove/all' if remove else '/sync/tracking', payload, what) == 'failed':
            return
        _wetrakr_refresh_local(api)
        _wetrakr_resync_watched(api)
        if not remove and (tmdb_type == 'movie' or episode is not None):  # WeTrakr drops its resume point itself; the local row goes too
            from tmdbhelper.lib.script.sync.wetrakr_sync import clear_local_resume_point
            try:
                syncdata = api.wetrakr_syncdata
                item_id = syncdata.get_name('movie' if tmdb_type == 'movie' else 'tv', tmdb_id, season, episode)
                clear_local_resume_point(syncdata, item_id)
            except Exception:
                pass
    except Exception as exc:
        kodi_log(f'cross_sync mirror_watched_to_wetrakr error: {type(exc).__name__}: {exc}', 2)


def mirror_bulk_unwatched_to_wetrakr(tmdb_id, seasons, episodes):
    """The ticked seasons / episodes of the Unwatched seasons / episodes pickers, in one request. Never a whole show: without a nested
    season the request would wipe every play of the show, so nothing is sent then. Returns (ok, text) for the result dialog, or None."""
    try:
        mode = _wetrakr_menu_mode()
        if not mode:
            return None
        whole_seasons = sorted({int(s) for s in seasons})
        nested = [{'number': s} for s in whole_seasons]
        for season, numbers in sorted(episodes.items(), key=lambda pair: int(pair[0])):
            if int(season) not in whole_seasons and numbers:
                nested.append({'number': int(season), 'episodes': [{'number': int(n)} for n in sorted(numbers, key=int)]})
        api = _wetrakr_client() if nested else None
        if not api:
            return None
        what = f'unwatched marks for picked seasons / episodes of tv {tmdb_id}'
        if mode == 1:
            _wetrakr_menu_log(f'log only: On would send the {what} to WeTrakr: {nested}')
            return None
        body = {'shows': [{'ids': {'tmdb': int(tmdb_id)}, 'status': 'watched', 'seasons': nested}]}
        status = _wetrakr_write(api, '/sync/tracking/remove/all', body, what)
        if status == 'failed':
            return False, 'failed'
        _wetrakr_refresh_local(api)
        _wetrakr_resync_watched(api)
        return True, 'done' if status == 'ok' else 'nothing to remove there'
    except Exception as exc:
        kodi_log(f'cross_sync mirror_bulk_unwatched_to_wetrakr error: {type(exc).__name__}: {exc}', 2)
        return False, 'failed'


def _wetrakr_list_holding(api, kind, tmdb_id):
    """(checked, list name or None): which WeTrakr tracking list has this movie / show, read fresh. checked False = a list could not be read."""
    from tmdbhelper.lib.monitor.bridge_sync import _WETRAKR_LISTS
    for status in _WETRAKR_LISTS[kind]:
        ok, rows = api.get_list(f'/sync/tracking/{status}/{kind}s')
        if not ok:
            return False, None
        if any(str(((row or {}).get('ids') or {}).get('tmdb')) == str(tmdb_id) for row in rows):
            return True, status
    return True, None


def mirror_watchlist_to_wetrakr(tmdb_type, tmdb_id, remove):
    """Watchlist add / remove = WeTrakr's planning list. Adding moves a title off any other list there, so it is sent only when a fresh
    read finds the title on no list. A removal names the planning list, so it can touch nothing else."""
    try:
        mode = _wetrakr_menu_list_mode()
        if not mode or tmdb_type not in ('movie', 'tv'):
            return
        api = _wetrakr_client()
        if not api:
            return
        kind = 'movie' if tmdb_type == 'movie' else 'show'
        what = f'watchlist {"removal" if remove else "add"} for {tmdb_type} {tmdb_id}'
        if not remove:
            checked, holding = _wetrakr_list_holding(api, kind, tmdb_id)
            if not checked:
                _wetrakr_menu_log(f'{what}: the WeTrakr lists could not be read. Left to the sync')
                return
            if holding:
                _wetrakr_menu_log(f'{what}: already on the WeTrakr {holding} list - left alone')
                return
        if mode == 1:
            _wetrakr_menu_log(f'log only: On would send the {what} to WeTrakr')
            return
        body = {f'{kind}s': [{'ids': {'tmdb': int(tmdb_id)}, 'status': 'planning'}]}
        if _wetrakr_write(api, '/sync/tracking/remove' if remove else '/sync/tracking', body, what) != 'failed':
            _wetrakr_refresh_local(api)
    except Exception as exc:
        kodi_log(f'cross_sync mirror_watchlist_to_wetrakr error: {type(exc).__name__}: {exc}', 2)


def _wetrakr_in_library(api, kind, tmdb_id):
    """True / False whether the WeTrakr library holds the title; None when that cannot be told (a page failed or rows are missing)."""
    page, pages, rows_read, total = 1, 1, 0, None
    while page <= pages:
        if page > 100:
            return None
        response = api.user_request('GET', '/sync/library/all', params={'page': page, 'limit': 100})
        if response is None or response.status_code != 200:
            return None
        try:
            data = response.json()
            pages = int(response.headers.get('X-Pagination-Page-Count') or 0)
            total = int(response.headers.get('X-Pagination-Item-Count'))
        except (ValueError, TypeError):
            return None
        if not isinstance(data, list):
            return None
        for row in data:
            if row.get('type') not in ('movie', 'show'):
                return None
            if row.get('type') == kind and str((row.get('ids') or {}).get('tmdb')) == str(tmdb_id):
                return True
        rows_read += len(data)
        page += 1
    return False if rows_read == total else None


def mirror_collection_to_wetrakr(tmdb_type, tmdb_id, season, episode, remove):
    """Collection add / remove = WeTrakr's library, whole titles only (as the background sync). The add makes a copy with the sync's own
    copy_key, and only when the library does not hold the title yet. A removal takes only copies carrying that prefix: a copy the user made
    on WeTrakr stays."""
    try:
        mode = _wetrakr_menu_list_mode()
        if not mode or tmdb_type not in ('movie', 'tv') or season is not None or episode is not None:
            return
        api = _wetrakr_client()
        if not api:
            return
        from tmdbhelper.lib.monitor.bridge_sync import _WETRAKR_COPY_PREFIX
        kind = 'movie' if tmdb_type == 'movie' else 'show'
        what = f'collection {"removal" if remove else "add"} for {tmdb_type} {tmdb_id}'
        if not remove:
            held = _wetrakr_in_library(api, kind, tmdb_id)
            if held is None:
                _wetrakr_menu_log(f'{what}: the WeTrakr library could not be read. Left to the sync')
                return
            if held:
                _wetrakr_menu_log(f'{what}: already in the WeTrakr library - left alone')
                return
        if mode == 1:
            _wetrakr_menu_log(f'log only: On would send the {what} to WeTrakr')
            return
        entry = {'ids': {'tmdb': int(tmdb_id)}}
        if remove:
            path, body = '/sync/library/remove', {f'{kind}s': [entry], 'copy_key_prefix': _WETRAKR_COPY_PREFIX}
        else:
            from datetime import datetime, timezone
            entry['copy_key'] = f'{_WETRAKR_COPY_PREFIX}{kind}:{int(tmdb_id)}'
            entry['collected_at'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
            path, body = '/sync/library', {f'{kind}s': [entry]}
        if _wetrakr_write(api, path, body, what) != 'failed':
            _wetrakr_refresh_local(api)
    except Exception as exc:
        kodi_log(f'cross_sync mirror_collection_to_wetrakr error: {type(exc).__name__}: {exc}', 2)


def mirror_watched_from_wetrakr(tmdb_type, tmdb_id, season, episode, remove):
    """Set as watched / unwatched made in WeTrakr's menu goes to Trakt, MDBList and Simkl. None of the three calls below sends anything
    back to WeTrakr (no echo)."""
    try:
        mode = _wetrakr_menu_mode()
        if not mode or tmdb_type not in ('movie', 'tv') or (season is None and episode is not None):
            return
        if mode == 1:
            what = f'{"unwatched" if remove else "watched"} mark for {_rating_label(tmdb_type, tmdb_id, season, episode)}'
            _wetrakr_menu_log(f'log only: On would send the {what} to Trakt, MDBList and Simkl')
            return
    except Exception as exc:
        kodi_log(f'cross_sync mirror_watched_from_wetrakr error: {type(exc).__name__}: {exc}', 2)
        return
    mirror_watched_to_trakt(tmdb_type, tmdb_id, season, episode, remove)
    mirror_watched_to_mdblist(tmdb_type, tmdb_id, season, episode, remove)
    mirror_watched_to_simkl(tmdb_type, tmdb_id, season, episode, remove)
