from tmdbhelper.lib.files.ftools import cached_property
from tmdbhelper.lib.addon.plugin import get_localized, executebuiltin
from tmdbhelper.lib.addon.dialog import BusyDialog
from xbmcgui import Dialog


class ItemMDbListSyncBase:
    """Base class for MDBList write sync context menu items.

    Auth:   API key appended to URL via get_request_url (always sent).
            Bearer OAuth token in Authorization header (sent when available).
            Either credential is sufficient — consistent with Phase B behavior.
    Writes: All use method='json' (POST with JSON body).
    Remove: POST to '<path>/remove' suffix — never HTTP DELETE.
    Payload format confirmed from Umbrella + POV reference addons:
        Movie:   {'movies': [{'tmdb': id}]}
        Show:    {'shows':  [{'tmdb': id}]}
        Season:  {'shows':  [{'tmdb': id, 'seasons': [{'number': sn}]}]}
        Episode: {'shows':  [{'tmdb': id, 'seasons': [{'number': sn, 'episodes': [{'number': ep}]}]}]}
    """

    allow_seasons = False
    allow_episodes = False
    preconfigured = False

    def __init__(self, tmdb_type, tmdb_id, season=None, episode=None):
        self.tmdb_type = tmdb_type
        self.tmdb_id = tmdb_id
        self.season = season
        self.episode = episode

    # --- API + sync data ---

    @cached_property
    def mdblist_api(self):
        from tmdbhelper.lib.api.mdblist.api import MDbList
        return MDbList()

    @cached_property
    def mdblist_syncdata(self):
        return self.mdblist_api.mdblist_syncdata

    # --- Authorization gate ---

    @cached_property
    def is_authorized(self):
        """Allow OAuth token OR personal API key (writes work with either)."""
        return bool(self.mdblist_api.user_token.value or self.mdblist_api.api_key)

    # --- Item type helpers ---

    @cached_property
    def mdb_type(self):
        if self.tmdb_type == 'movie':
            return 'movie'
        if self.tmdb_type != 'tv':
            return None
        if self.season is None:
            return 'show'
        if self.episode is None:
            return 'season'
        return 'episode'

    @cached_property
    def mdb_base_type(self):
        return 'movie' if self.tmdb_type == 'movie' else 'show'

    @cached_property
    def is_allowed_type(self):
        if self.season is None:
            return True
        if self.episode is None:
            return self.allow_seasons
        return self.allow_episodes

    # --- Payload ---

    def build_payload(self, extra_fields=None):
        extra_fields = extra_fields or {}
        tmdb_id = int(self.tmdb_id)
        if self.mdb_type == 'movie':
            item = {'tmdb': tmdb_id}
            item.update(extra_fields)
            return {'movies': [item]}
        if self.mdb_type == 'show':
            item = {'tmdb': tmdb_id}
            item.update(extra_fields)
            return {'shows': [item]}
        if self.mdb_type == 'season':
            return {'shows': [{'tmdb': tmdb_id, 'seasons': [{'number': int(self.season)}]}]}
        if self.mdb_type == 'episode':
            return {'shows': [{'tmdb': tmdb_id, 'seasons': [{'number': int(self.season), 'episodes': [{'number': int(self.episode)}]}]}]}
        return {}

    # --- API call ---

    def call_api(self, *path_parts, extra_fields=None):
        """POST to MDBList API. Sends API key (URL param) + Bearer token (header if available)."""
        url = self.mdblist_api.get_request_url(*path_parts)
        payload = self.build_payload(extra_fields)
        with BusyDialog():
            return self.mdblist_api.get_api_request(
                url,
                postdata=payload,
                method='json',
                headers=self.mdblist_api.auth_headers)

    # --- Sync state for add/remove toggle ---

    def get_sync_value(self, key):
        """Read current toggle state directly from DB — no sync triggered.

        Root-cause: get_value() calls sync() before reading, which invokes
        MDbListSyncWatchlist/Collection.sync() → clear_columns() → NULLs ALL
        toggle data for that item_type on every context menu open.  That wipes
        any value update_toggle_in_db just wrote.  Reading from cache directly
        bypasses the clear-and-refetch cycle entirely.
        """
        try:
            db_type = 'movie' if self.tmdb_type == 'movie' else 'tv'
            season = self.season if self.mdb_type in ('season', 'episode') else None
            episode = self.episode if self.mdb_type == 'episode' else None
            item_id = self.mdblist_syncdata.get_name(db_type, self.tmdb_id, season, episode)
            values = self.mdblist_syncdata.cache.get_values(item_id=item_id, keys=(key,))
            return values[0] if values else None
        except Exception:
            return None

    # --- Cached sync response ---

    @cached_property
    def sync_response(self):
        return self.get_sync_response()

    def get_sync_response(self):
        raise NotImplementedError

    @cached_property
    def is_successful_sync(self):
        """get_api_request returns None for 4xx/5xx/connection errors; response object for 2xx."""
        return self.sync_response is not None

    # --- Post-sync actions ---

    def reset_lastactivities(self):
        if self.is_successful_sync:
            self.mdblist_syncdata.reset_lastactivities()

    def update_toggle_in_db(self, key, value):
        """Directly write a toggle column into the local sync DB after a successful write.

        Root-cause analysis:
          • MDbListDataType.sync(forced=False) calls is_expired(), which compares the
            stored DB activity timestamp against last_activities['watchlisted_at'|...].
            Because store_last_activity() stores last_activities['all'] (the maximum of
            all timestamps) as the reference, watchlisted_at <= all always, so
            is_expired() returns False after the first sync.  No re-sync ever runs.
          • force_refresh_sync(forced=True) bypasses is_expired but the try/except
            silently swallows mutex/timerlock errors, so nothing appears in the log.

        Direct SQL fix:
          • add to list  → INSERT OR IGNORE row, then set_many_values (update_if_null) to
                           stamp the column with the current UTC timestamp.
          • remove from list → execute UPDATE … SET col=NULL WHERE id=? directly.
          In both cases get_values() reads the DB AFTER calling sync() — whether or not
          sync actually ran, the DB value we wrote here is what comes back.
        """
        if not self.is_successful_sync:
            return
        try:
            db_type = self.tmdb_type          # 'movie' or 'tv'
            tmdb_id = int(self.tmdb_id)
            item_id = f'{db_type}.{tmdb_id}'
            cache = self.mdblist_syncdata.cache

            if value:
                from datetime import datetime, timezone
                ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
                item_type_str = self.mdb_base_type  # 'movie' or 'show'
                # table_keys mirrors MDbListSyncItem.table_keys for this method
                keys = (key, 'item_type', 'tmdb_type', 'tmdb_id', 'season_number', 'episode_number')
                data = {item_id: [ts, item_type_str, db_type, tmdb_id, None, None]}
                cache.set_many_values(keys=keys, data=data)
            else:
                # set_many_values only writes to NULL columns; use raw SQL to clear
                cache.execute_sql(
                    cache.statement_delete_keys('simplecache', (key,), conditions='id=?'),
                    (item_id,))
        except Exception:
            pass

    def update_watched_in_db(self, value, force=False):
        """Directly write watched state (plays + last_watched_at) to local sync DB.

        Cannot use update_toggle_in_db / set_many_values for 'plays' because the
        column is INTEGER DEFAULT 0 (not NULL).  ifnull(1, 0) = 0 — the UPDATE
        is a no-op.  Plain UPDATE with literal values overwrites unconditionally.

        Scoped to movie and episode only (direct plays column exists at those
        levels).  Show/season watched_episodes aggregation is left to the next
        periodic sync — attempting to reconstruct counts here would be fragile.
        """
        if not force and not self.is_successful_sync:
            return
        if self.mdb_type not in ('movie', 'episode', 'show'):
            return
        try:
            db_type = self.tmdb_type  # 'movie' or 'tv'
            tmdb_id = int(self.tmdb_id)
            season = int(self.season) if self.mdb_type == 'episode' else None
            episode = int(self.episode) if self.mdb_type == 'episode' else None
            item_id = self.mdblist_syncdata.get_name(db_type, tmdb_id, season, episode)
            cache = self.mdblist_syncdata.cache
            if value:
                from datetime import datetime, timezone
                ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
                cache.execute_sql(
                    cache.statement_insert_or_ignore('simplecache', ('id',)),
                    (item_id,))
                if self.mdb_type == 'show':
                    # watched_episodes=9999 sentinel makes badge appear immediately.
                    # get_playcount() returns min(9999, air_count) = air_count.
                    # Next periodic sync overwrites with the real count.
                    cache.execute_sql(
                        'UPDATE simplecache SET last_watched_at=?, watched_episodes=?,'
                        ' item_type=?, tmdb_type=?, tmdb_id=?,'
                        ' season_number=?, episode_number=? WHERE id=?',
                        (ts, 9999, 'show', db_type, tmdb_id, None, None, item_id))
                else:
                    item_type_str = 'movie' if self.mdb_type == 'movie' else 'episode'
                    # Plain UPDATE — ifnull(1, 0) = 0 so set_many_values cannot set plays=1
                    cache.execute_sql(
                        'UPDATE simplecache SET plays=?, last_watched_at=?,'
                        ' item_type=?, tmdb_type=?, tmdb_id=?,'
                        ' season_number=?, episode_number=? WHERE id=?',
                        (1, ts, item_type_str, db_type, tmdb_id, season, episode, item_id))
            else:
                if self.mdb_type == 'show':
                    cache.execute_sql(
                        'UPDATE simplecache SET last_watched_at=NULL, watched_episodes=0 WHERE id=?',
                        (item_id,))
                else:
                    cache.execute_sql(
                        'UPDATE simplecache SET plays=0, last_watched_at=NULL WHERE id=?',
                        (item_id,))
        except Exception:
            pass

    def display_dialog(self):
        if self.is_successful_sync:
            msg = get_localized(32297)
        else:
            msg = get_localized(32296)
        Dialog().ok(self.name, msg.format(self.name, self.tmdb_type, 'TMDb', self.tmdb_id))

    def refresh_containers(self, force=False):
        if not force and not self.is_successful_sync:
            return
        from tmdbhelper.lib.script.method.kodi_utils import reload_widgets
        executebuiltin('Container.Refresh')
        reload_widgets()

    def sync(self):
        self.reset_lastactivities()
        self.display_dialog()
        self.refresh_containers()

    # --- Menu display gate ---

    def get_self(self):
        """Return self to include in menu; None to exclude."""
        if not self.is_authorized:
            return
        if not self.is_allowed_type:
            return
        return self

    @cached_property
    def name(self):
        return self.get_name()

    def get_name(self):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Concrete sync item classes
# ---------------------------------------------------------------------------

class ItemMDbListWatched(ItemMDbListSyncBase):
    """Mark movie / show / season / episode as watched.

    Endpoint confirmed (Umbrella + POV): POST /sync/watched
    """
    allow_seasons = True
    allow_episodes = True
    preconfigured = True

    def get_self(self):
        # super() handles is_authorized + is_allowed_type
        if not super().get_self():
            return None
        # Toggle: hide "Mark as watched" when already watched.
        # Shows use last_watched_at (plays is always 0 for show-level rows);
        # movies and episodes use plays.
        if self.mdb_type == 'show':
            if self.get_sync_value('last_watched_at'):
                return None
        elif self.get_sync_value('plays'):
            return None
        return self

    def get_name(self):
        return get_localized(16103)  # "Set as watched"

    def get_sync_response(self):
        return self.call_api('sync', 'watched')

    def sync(self):
        # reset_lastactivities() intentionally omitted: prevents MDBList sync from
        # running on Container.Refresh and wiping the plays value we just wrote.
        _ = self.sync_response  # trigger API call first; local write and refresh proceed regardless
        self.update_watched_in_db(True, force=True)
        self.display_dialog()
        self.refresh_containers(force=True)
        if self.is_successful_sync:
            from tmdbhelper.lib.script.sync.cross_sync import mirror_watched_to_trakt, mirror_watched_to_simkl
            mirror_watched_to_trakt(self.tmdb_type, self.tmdb_id, self.season, self.episode, remove=False)
            mirror_watched_to_simkl(self.tmdb_type, self.tmdb_id, self.season, self.episode, remove=False)


class ItemMDbListUnwatched(ItemMDbListSyncBase):
    """Remove movie / show / season / episode from watched history.

    Endpoint confirmed (Umbrella + POV): POST /sync/watched/remove
    Also attempts best-effort scrobble bookmark cleanup so item leaves
    In-Progress widgets (POST /scrobble/clear with paused_id from sync DB).
    """
    allow_seasons = True
    allow_episodes = True
    preconfigured = True

    def get_self(self):
        # super() handles is_authorized + is_allowed_type
        if not super().get_self():
            return None
        # Toggle: hide "Mark as unwatched" when not watched.
        # Shows use last_watched_at; movies and episodes use plays.
        if self.mdb_type == 'show':
            if not self.get_sync_value('last_watched_at'):
                return None
        elif not self.get_sync_value('plays'):
            return None
        return self

    def get_name(self):
        return get_localized(16104)  # "Set as unwatched"

    def get_sync_response(self):
        return self.call_api('sync', 'watched', 'remove')

    def _clear_playback_progress(self):
        """Best-effort: clear MDBList scrobble bookmark so item leaves In-Progress.
        Only applies to movie/episode; show/season-level has no single paused_id.
        Deferred gap: If MDBList clears playback server-side on watched/remove, this
        is redundant — but harmless. If not, this keeps local DB consistent.
        """
        if not self.is_successful_sync:
            return
        if self.mdb_type not in ('movie', 'episode'):
            return
        try:
            db_type = 'movie' if self.mdb_type == 'movie' else 'tv'
            paused_id = self.mdblist_syncdata.get_value(
                db_type, self.tmdb_id,
                season=self.season if self.mdb_type == 'episode' else None,
                episode=self.episode if self.mdb_type == 'episode' else None,
                key='paused_id')
            if not paused_id:
                return
            url = self.mdblist_api.get_request_url('scrobble', 'clear')
            with BusyDialog():
                self.mdblist_api.get_api_request(
                    url,
                    postdata={'id': paused_id},
                    method='json',
                    headers=self.mdblist_api.auth_headers)
        except Exception:
            pass

    def sync(self):
        self._clear_playback_progress()  # also triggers sync_response (API call + BusyDialog)
        # reset_lastactivities() intentionally omitted: same reasoning as ItemMDbListWatched.
        self.update_watched_in_db(False, force=True)
        self.display_dialog()
        self.refresh_containers(force=True)
        if self.is_successful_sync:
            from tmdbhelper.lib.script.sync.cross_sync import mirror_watched_to_trakt, mirror_watched_to_simkl
            mirror_watched_to_trakt(self.tmdb_type, self.tmdb_id, self.season, self.episode, remove=True)
            mirror_watched_to_simkl(self.tmdb_type, self.tmdb_id, self.season, self.episode, remove=True)


class ItemMDbListWatchlist(ItemMDbListSyncBase):
    """Add / remove movie or show from MDBList watchlist.

    Write endpoints confirmed (Umbrella + POV):
        Add:    POST /watchlist/items/add
        Remove: POST /watchlist/items/remove
    Note: These differ from the read endpoint GET /sync/watchlist used in Phase B.
    Season/episode watchlist not supported by MDBList (show-level only).
    """

    @cached_property
    def remove(self):
        return bool(self.get_sync_value('watchlist_listed_at'))

    def get_name(self):
        return get_localized(32292 if self.remove else 32291)
        # 32291 = "Add to watchlist" | 32292 = "Remove from watchlist"

    def get_sync_response(self):
        if self.remove:
            return self.call_api('watchlist', 'items', 'remove')
        return self.call_api('watchlist', 'items', 'add')

    def sync(self):
        self.reset_lastactivities()
        # not self.remove: True=adding→stamp timestamp, False=removing→set NULL
        self.update_toggle_in_db('watchlist_listed_at', not self.remove)
        self.display_dialog()
        self.refresh_containers()
        if self.is_successful_sync:
            from tmdbhelper.lib.script.sync.cross_sync import mirror_watchlist_to_trakt, mirror_watchlist_to_simkl
            mirror_watchlist_to_trakt(self.tmdb_type, self.tmdb_id, self.remove)
            if not self.remove:
                mirror_watchlist_to_simkl(self.tmdb_type, self.tmdb_id)


class ItemMDbListCollection(ItemMDbListSyncBase):
    """Add / remove movie / show / episode from MDBList collection.

    Write endpoints confirmed (POV):
        Add:    POST /sync/collection
        Remove: POST /sync/collection/remove
    """
    allow_episodes = True

    @cached_property
    def remove(self):
        return bool(self.get_sync_value('collection_collected_at'))

    def get_name(self):
        return get_localized(32290 if self.remove else 32289)
        # 32289 = "Add to collection" | 32290 = "Remove from collection"

    def get_sync_response(self):
        if self.remove:
            return self.call_api('sync', 'collection', 'remove')
        return self.call_api('sync', 'collection')

    def sync(self):
        self.reset_lastactivities()
        self.update_toggle_in_db('collection_collected_at', not self.remove)
        self.display_dialog()
        self.refresh_containers()
        if self.is_successful_sync:
            from tmdbhelper.lib.script.sync.cross_sync import mirror_collection_to_trakt
            mirror_collection_to_trakt(self.tmdb_type, self.tmdb_id, self.season, self.episode, self.remove)


class ItemMDbListRating(ItemMDbListSyncBase):
    """Rate a movie, show, season, or episode on MDBList.

    Endpoint confirmed from MDBList API docs (apiary.apib):
        Add/change: POST /sync/ratings
        Remove:     POST /sync/ratings/remove   (rating=0)

    Payload nests the rating at the correct hierarchy level:
        Movie:   {"movies": [{"tmdb": id, "rating": X}]}
        Show:    {"shows":  [{"tmdb": id, "rating": X}]}
        Season:  {"shows":  [{"tmdb": id, "seasons": [{"number": sn, "rating": X}]}]}
        Episode: {"shows":  [{"tmdb": id, "seasons": [{"number": sn, "episodes": [{"number": ep, "rating": X}]}]}]}
    """
    allow_seasons = True
    allow_episodes = True

    @cached_property
    def current_rating(self):
        return self.get_sync_value('rating') or 0

    def get_name(self):
        if self.current_rating:
            return f'{get_localized(32489)} ({self.current_rating})'  # "Change Rating (X)"
        return get_localized(32485)  # "Add rating"

    def build_rating_payload(self, rating):
        """Build /sync/ratings payload with rating nested at the correct level.
        rating=0 omits the rating field (used for the remove endpoint).
        """
        tmdb_id = int(self.tmdb_id)
        if self.mdb_type == 'movie':
            item = {'tmdb': tmdb_id}
            if rating:
                item['rating'] = rating
            return {'movies': [item]}
        if self.mdb_type == 'show':
            item = {'tmdb': tmdb_id}
            if rating:
                item['rating'] = rating
            return {'shows': [item]}
        if self.mdb_type == 'season':
            season = {'number': int(self.season)}
            if rating:
                season['rating'] = rating
            return {'shows': [{'tmdb': tmdb_id, 'seasons': [season]}]}
        if self.mdb_type == 'episode':
            episode = {'number': int(self.episode)}
            if rating:
                episode['rating'] = rating
            return {'shows': [{'tmdb': tmdb_id, 'seasons': [
                {'number': int(self.season), 'episodes': [episode]}]}]}
        return {}

    def call_rating_api(self, x):
        payload = self.build_rating_payload(x)
        if not payload:
            return None
        path_parts = ('sync', 'ratings', 'remove') if x == 0 else ('sync', 'ratings')
        url = self.mdblist_api.get_request_url(*path_parts)
        with BusyDialog():
            return self.mdblist_api.get_api_request(
                url, postdata=payload, method='json',
                headers=self.mdblist_api.auth_headers)

    def get_sync_response(self):
        try:
            x = int(Dialog().numeric(0, f'{get_localized(32485)} (0-10)'))
        except ValueError:
            return None  # cancelled — _rating_value intentionally not set
        if x < 0 or x > 10:
            return None
        self._rating_value = x
        return self.call_rating_api(x)

    def update_rating_in_db(self, force=False):
        """Override to support season/episode item IDs and level-specific item_type."""
        if not force and not self.is_successful_sync:
            return
        rating = getattr(self, '_rating_value', None)
        if rating is None:
            return
        try:
            db_type = self.tmdb_type
            tmdb_id = int(self.tmdb_id)
            season = int(self.season) if self.mdb_type in ('season', 'episode') else None
            episode = int(self.episode) if self.mdb_type == 'episode' else None
            item_id = self.mdblist_syncdata.get_name(db_type, tmdb_id, season, episode)
            item_type_str = self.mdb_type  # 'movie', 'show', 'season', 'episode'
            cache = self.mdblist_syncdata.cache
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
                    (rating, ts, item_type_str, db_type, tmdb_id, season, episode, item_id))
            else:
                cache.execute_sql(
                    'UPDATE simplecache SET rating=NULL, rated_at=NULL WHERE id=?',
                    (item_id,))
        except Exception:
            pass

    def sync(self):
        _ = self.sync_response  # triggers dialog + API call; sets self._rating_value if valid input
        if not hasattr(self, '_rating_value'):
            return  # user cancelled dialog
        self.update_rating_in_db(force=True)
        self.display_dialog()
        self.refresh_containers(force=True)
        if self.is_successful_sync:
            from tmdbhelper.lib.script.sync.cross_sync import mirror_rating_to_trakt
            mirror_rating_to_trakt(self.tmdb_type, self.tmdb_id, self.season, self.episode, self._rating_value)
