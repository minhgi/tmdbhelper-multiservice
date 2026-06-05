from tmdbhelper.lib.files.ftools import cached_property
from tmdbhelper.lib.addon.plugin import get_localized, executebuiltin
from tmdbhelper.lib.addon.dialog import BusyDialog
from xbmcgui import Dialog

BASE_URL = 'https://api.simkl.com'


class ItemSimklSyncBase:
    """Base class for Simkl write-sync context menu items.

    Writes use TMDb IDs — confirmed via live probe that Simkl resolves them
    for both /sync/history and /sync/add-to-list endpoints.
    Bearer token required for all user-data writes.
    Episode-level writes deferred to Phase D (TVDB vs TMDb numbering).
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
    def simkl_api(self):
        from tmdbhelper.lib.api.simkl.api import SimklAPI
        return SimklAPI()

    @cached_property
    def simkl_syncdata(self):
        return self.simkl_api.simkl_syncdata

    # --- Auth gate ---

    @cached_property
    def is_authorized(self):
        return bool(self.simkl_api.user_token.value)

    # --- Item type helpers ---

    @cached_property
    def simkl_type(self):
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
    def is_allowed_type(self):
        if self.season is None:
            return True
        if self.episode is None:
            return self.allow_seasons
        return False  # Episode-level deferred to Phase D

    # --- Payload builder ---

    def build_payload(self, to=None):
        """Build write payload using TMDb IDs.
        `to` is only used for /sync/add-to-list (e.g. 'plantowatch', 'hold', 'dropped', 'completed').
        """
        tmdb_id = int(self.tmdb_id)
        ids = {'tmdb': tmdb_id}

        if self.simkl_type == 'movie':
            item = {'ids': ids}
            if to:
                item['to'] = to
            return {'movies': [item]}

        if self.simkl_type == 'show':
            item = {'ids': ids}
            if to:
                item['to'] = to
            return {'shows': [item]}

        if self.simkl_type == 'season':
            item = {'ids': ids, 'seasons': [{'number': int(self.season)}]}
            if to:
                item['to'] = to
            return {'shows': [item]}

        return {}

    # --- API call ---

    def call_api(self, *path_parts, payload=None):
        url = f"{BASE_URL}/{'/'.join(path_parts)}?client_id={self.simkl_api.client_id}"
        with BusyDialog():
            return self.simkl_api.get_api_request(
                url,
                postdata=payload,
                method='json',
                headers=self.simkl_api.auth_headers)

    # --- Toggle state helpers ---

    def get_sync_value(self, key):
        """Read current state directly from DB — does not trigger a sync.
        Avoids the clear_columns() wipe that would erase any value just written.
        """
        try:
            db_type = 'movie' if self.tmdb_type == 'movie' else 'tv'
            season = self.season if self.simkl_type in ('season', 'episode') else None
            episode = self.episode if self.simkl_type == 'episode' else None
            item_id = self.simkl_syncdata.get_name(db_type, self.tmdb_id, season, episode)
            values = self.simkl_syncdata.cache.get_values(item_id=item_id, keys=(key,))
            return values[0] if values else None
        except Exception:
            return None

    def update_watched_in_db(self, value, force=False):
        """Write watched state directly via raw UPDATE.

        Movies: write plays + last_watched_at (plays is INTEGER DEFAULT 0, ifnull no-op).
        Shows:  write last_watched_at only (used as toggle indicator; episode counts
                come from periodic sync). Season-level deferred to Phase D.
        """
        if not force and not self.is_successful_sync:
            return
        try:
            from datetime import datetime, timezone
            ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')

            if self.simkl_type == 'movie':
                item_id = f'movie.{int(self.tmdb_id)}'
                cache = self.simkl_syncdata.cache
                cache.execute_sql(
                    cache.statement_insert_or_ignore('simplecache', ('id',)), (item_id,))
                if value:
                    cache.execute_sql(
                        'UPDATE simplecache SET plays=?, last_watched_at=?,'
                        ' item_type=?, tmdb_type=?, tmdb_id=?,'
                        ' season_number=?, episode_number=? WHERE id=?',
                        (1, ts, 'movie', 'movie', int(self.tmdb_id), None, None, item_id))
                else:
                    cache.execute_sql(
                        'UPDATE simplecache SET plays=0, last_watched_at=NULL WHERE id=?',
                        (item_id,))

            elif self.simkl_type == 'episode':
                tmdb_id = int(self.tmdb_id)
                season = int(self.season)
                episode = int(self.episode)
                item_id = f'tv.{tmdb_id}.{season}.{episode}'
                cache = self.simkl_syncdata.cache
                cache.execute_sql(
                    cache.statement_insert_or_ignore('simplecache', ('id',)), (item_id,))
                if value:
                    cache.execute_sql(
                        'UPDATE simplecache SET plays=1, last_watched_at=?,'
                        ' item_type=?, tmdb_type=?, tmdb_id=?,'
                        ' season_number=?, episode_number=? WHERE id=?',
                        (ts, 'episode', 'tv', tmdb_id, season, episode, item_id))
                else:
                    cache.execute_sql(
                        'UPDATE simplecache SET plays=0, last_watched_at=NULL WHERE id=?',
                        (item_id,))

            elif self.simkl_type == 'show':
                item_id = f'tv.{int(self.tmdb_id)}'
                cache = self.simkl_syncdata.cache
                cache.execute_sql(
                    cache.statement_insert_or_ignore('simplecache', ('id',)), (item_id,))
                if value:
                    # watched_episodes=9999 sentinel makes badge appear immediately;
                    # get_playcount() returns min(9999, air_count) = air_count.
                    # Next periodic sync overwrites with the real count.
                    cache.execute_sql(
                        'UPDATE simplecache SET last_watched_at=?, watched_episodes=?,'
                        ' item_type=?, tmdb_type=?, tmdb_id=? WHERE id=?',
                        (ts, 9999, 'show', 'tv', int(self.tmdb_id), item_id))
                else:
                    cache.execute_sql(
                        'UPDATE simplecache SET last_watched_at=NULL, watched_episodes=0 WHERE id=?',
                        (item_id,))
        except Exception:
            pass

    def update_status_in_db(self, key, value):
        """Write-through for plantowatch_at / hold_at / dropped_at columns."""
        if not self.is_successful_sync:
            return
        try:
            db_type = 'movie' if self.tmdb_type == 'movie' else 'tv'
            tmdb_id = int(self.tmdb_id)
            item_id = f'{db_type}.{tmdb_id}'
            cache = self.simkl_syncdata.cache
            if value:
                from datetime import datetime, timezone
                ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
                item_type_str = 'movie' if self.simkl_type == 'movie' else 'show'
                keys = (key, 'item_type', 'tmdb_type', 'tmdb_id', 'season_number', 'episode_number')
                data = {item_id: [ts, item_type_str, db_type, tmdb_id, None, None]}
                cache.set_many_values(keys=keys, data=data)
            else:
                cache.execute_sql(
                    cache.statement_delete_keys('simplecache', (key,), conditions='id=?'),
                    (item_id,))
        except Exception:
            pass

    # --- Response + sync result ---

    @cached_property
    def sync_response(self):
        return self.get_sync_response()

    def get_sync_response(self):
        raise NotImplementedError

    @cached_property
    def is_successful_sync(self):
        """Success = 2xx response AND item not in not_found."""
        if self.sync_response is None:
            return False
        try:
            resp_json = self.sync_response.json()
            not_found = resp_json.get('not_found', {})
            for key in ('movies', 'shows', 'episodes'):
                if not_found.get(key):
                    return False
        except Exception:
            pass
        return True

    # --- Cross-service mirror ---

    def _cross_sync(self):
        """No-op hook. Subclasses override to mirror the action to Trakt/MDBList."""
        pass

    # --- Post-sync actions ---

    def reset_lastactivities(self):
        if self.is_successful_sync:
            self.simkl_syncdata.reset_lastactivities()

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

    # --- Menu gate ---

    def get_self(self):
        if not self.is_authorized:
            return None
        from tmdbhelper.lib.addon.plugin import get_setting
        if not get_setting('service_simkl'):
            return None
        if not self.is_allowed_type:
            return None
        return self

    @cached_property
    def name(self):
        return self.get_name()

    def get_name(self):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Concrete sync item classes
# ---------------------------------------------------------------------------

class ItemSimklWatched(ItemSimklSyncBase):
    """Mark movie / show / season as watched on Simkl.

    Movie:  POST /sync/history     {"movies":[{"ids":{"tmdb":N}}]}
    Show:   POST /sync/add-to-list {"shows":[{"to":"completed","ids":{"tmdb":N}}]}
    Season: POST /sync/history     {"shows":[{"seasons":[{"number":S}],"ids":{"tmdb":N}}]}
    """
    allow_seasons = True
    preconfigured = True

    def get_self(self):
        if not super().get_self():
            return None
        if self.simkl_type == 'movie' and self.get_sync_value('plays'):
            return None
        if self.simkl_type == 'show' and self.get_sync_value('last_watched_at'):
            return None
        return self

    def get_name(self):
        return get_localized(16103)  # "Set as watched"

    def get_sync_response(self):
        if self.simkl_type == 'movie':
            from datetime import datetime, timezone
            ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            payload = {'movies': [{'watched_at': ts, 'ids': {'tmdb': int(self.tmdb_id)}}]}
            return self.call_api('sync', 'history', payload=payload)
        if self.simkl_type == 'show':
            return self.call_api('sync', 'add-to-list', payload=self.build_payload(to='completed'))
        if self.simkl_type == 'season':
            return self.call_api('sync', 'history', payload=self.build_payload())
        return None

    def _cross_sync(self):
        from tmdbhelper.lib.script.sync.cross_sync import mirror_watched_to_trakt, mirror_watched_to_mdblist
        mirror_watched_to_trakt(self.tmdb_type, self.tmdb_id, self.season, self.episode, remove=False)
        mirror_watched_to_mdblist(self.tmdb_type, self.tmdb_id, self.season, self.episode, remove=False)

    def sync(self):
        _ = self.sync_response
        self.update_watched_in_db(True, force=True)
        self.display_dialog()
        self.refresh_containers(force=True)
        if self.is_successful_sync:
            self._cross_sync()


class ItemSimklUnwatched(ItemSimklSyncBase):
    """Remove movie / show / season from Simkl watched history.

    POST /sync/history/remove for all types.
    """
    allow_seasons = True
    preconfigured = True

    def get_self(self):
        if not super().get_self():
            return None
        if self.simkl_type == 'movie' and not self.get_sync_value('plays'):
            return None
        if self.simkl_type == 'show' and not self.get_sync_value('last_watched_at'):
            return None
        return self

    def get_name(self):
        return get_localized(16104)  # "Set as unwatched"

    def get_sync_response(self):
        return self.call_api('sync', 'history', 'remove', payload=self.build_payload())

    def _cross_sync(self):
        from tmdbhelper.lib.script.sync.cross_sync import mirror_watched_to_trakt, mirror_watched_to_mdblist
        mirror_watched_to_trakt(self.tmdb_type, self.tmdb_id, self.season, self.episode, remove=True)
        mirror_watched_to_mdblist(self.tmdb_type, self.tmdb_id, self.season, self.episode, remove=True)

    def sync(self):
        _ = self.sync_response
        self.update_watched_in_db(False, force=True)
        self.display_dialog()
        self.refresh_containers(force=True)
        if self.is_successful_sync:
            self._cross_sync()


class ItemSimklPlanToWatch(ItemSimklSyncBase):
    """Add / remove movie or show from Simkl plan-to-watch list.

    Add:    POST /sync/add-to-list  with "to": "plantowatch"
    Remove: POST /sync/history/remove  (clears all Simkl list status — confirmed via live probe)
    Toggle state read from local DB write-through column plantowatch_at.
    """

    @cached_property
    def remove(self):
        return bool(self.get_sync_value('plantowatch_at'))

    def get_name(self):
        return get_localized(32292 if self.remove else 32291)
        # 32291 = "Add to watchlist" | 32292 = "Remove from watchlist"

    def get_sync_response(self):
        if self.remove:
            return self.call_api('sync', 'history', 'remove', payload=self.build_payload())
        return self.call_api('sync', 'add-to-list', payload=self.build_payload(to='plantowatch'))

    def _cross_sync(self):
        from tmdbhelper.lib.script.sync.cross_sync import mirror_watchlist_to_trakt, mirror_watchlist_to_mdblist
        mirror_watchlist_to_trakt(self.tmdb_type, self.tmdb_id, self.remove)
        mirror_watchlist_to_mdblist(self.tmdb_type, self.tmdb_id, self.remove)

    def sync(self):
        self.reset_lastactivities()
        self.update_status_in_db('plantowatch_at', not self.remove)
        self.display_dialog()
        self.refresh_containers()
        if self.is_successful_sync:
            self._cross_sync()


class ItemSimklHold(ItemSimklSyncBase):
    """Add / remove movie or show from Simkl hold list.

    Add:    POST /sync/add-to-list  with "to": "hold"
    Remove: POST /sync/history/remove
    """

    @cached_property
    def remove(self):
        return bool(self.get_sync_value('hold_at'))

    def get_name(self):
        return get_localized(32559 if self.remove else 32558)
        # 32558 = "Add to hold" | 32559 = "Remove from hold"

    def get_sync_response(self):
        if self.remove:
            return self.call_api('sync', 'history', 'remove', payload=self.build_payload())
        return self.call_api('sync', 'add-to-list', payload=self.build_payload(to='hold'))

    def sync(self):
        self.reset_lastactivities()
        self.update_status_in_db('hold_at', not self.remove)
        self.display_dialog()
        self.refresh_containers()


class ItemSimklDrop(ItemSimklSyncBase):
    """Add / remove movie or show from Simkl 'Not Interested' list.

    Simkl's API uses "notinteresting" as the to= value — "dropped" returns 400.
    Works for both movies and shows (live-confirmed via probe).
    Remove: POST /sync/history/remove clears all list status.
    """

    @cached_property
    def remove(self):
        return bool(self.get_sync_value('dropped_at'))

    def get_name(self):
        return get_localized(32561 if self.remove else 32560)
        # 32560 = "Drop" | 32561 = "Remove from dropped"

    def get_sync_response(self):
        if self.remove:
            return self.call_api('sync', 'history', 'remove', payload=self.build_payload())
        return self.call_api('sync', 'add-to-list', payload=self.build_payload(to='notinteresting'))

    def sync(self):
        self.reset_lastactivities()
        self.update_status_in_db('dropped_at', not self.remove)
        self.display_dialog()
        self.refresh_containers()
