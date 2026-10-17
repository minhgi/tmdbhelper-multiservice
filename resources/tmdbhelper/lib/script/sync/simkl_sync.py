from tmdbhelper.lib.files.ftools import cached_property
from tmdbhelper.lib.addon.plugin import get_localized, executebuiltin
from tmdbhelper.lib.addon.dialog import BusyDialog
from tmdbhelper.lib.addon.logger import kodi_log
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
    cancelled = False       # the user declined a confirmation: nothing was sent, nothing to report
    kept_history = False    # a show left its list as 'watching' instead of being wiped (see remove_status)

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
                # completed (watched) and removed (unwatched) both replace a 'watching' status
                cache.execute_sql('UPDATE simplecache SET watching_at=NULL WHERE id=?', (item_id,))
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
        """Write-through for plantowatch_at / hold_at / dropped_at columns.

        Simkl keeps one status per item, so any change here also ends a local 'watching' mark
        (a removal goes through sync/history/remove, which clears the whole status). Writing one status
        therefore also clears the other two, or a movie moved plan -> hold keeps both flags until a resync
        (and Simkl's movie activities carry no hold stamp, so none fires).
        """
        if not self.is_successful_sync:
            return
        try:
            db_type = 'movie' if self.tmdb_type == 'movie' else 'tv'
            tmdb_id = int(self.tmdb_id)
            item_id = f'{db_type}.{tmdb_id}'
            cache = self.simkl_syncdata.cache
            cache.execute_sql('UPDATE simplecache SET watching_at=NULL WHERE id=?', (item_id,))
            if value:
                others = [k for k in ('plantowatch_at', 'hold_at', 'dropped_at') if k != key]
                cache.execute_sql(
                    f'UPDATE simplecache SET {others[0]}=NULL, {others[1]}=NULL WHERE id=?', (item_id,))
                from datetime import datetime, timezone
                ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
                item_type_str = 'movie' if self.simkl_type == 'movie' else 'show'
                keys = (key, 'item_type','tmdb_type', 'tmdb_id', 'season_number', 'episode_number')
                data = {item_id: [ts, item_type_str, db_type, tmdb_id, None, None]}
                cache.set_many_values(keys=keys, data=data)
            else:
                cache.execute_sql(
                    cache.statement_delete_keys('simplecache', (key,), conditions='id=?'),
                    (item_id,))
        except Exception:
            pass

    # --- Taking an item off a plan-to-watch / hold / dropped list ---

    def read_library_entry(self):
        """(readable, entry): this item's row of GET /sync/all-items/{movies|shows|anime}/ (every status).

        readable is False when the list could not be read; entry is None when it was read and does not
        list the item (tmdb ids compared as strings). Read only when the action runs, never to build a menu.
        Simkl keeps anime in a bucket of its own (live-probed 2026-10-14: an anime added to plan-to-watch shows
        up in 'anime', never in 'shows'), so a show that is not in 'shows' is looked up in 'anime' as well -
        and if that second list cannot be read the answer is "unreadable", not "not listed".
        """
        if self.simkl_type == 'movie':
            return self.read_library_bucket('movies', 'movie')
        readable, entry = self.read_library_bucket('shows', 'show')
        if not readable or entry is not None:
            return readable, entry
        return self.read_library_bucket('anime', 'show')

    def read_library_bucket(self, group, key):
        url = f'{BASE_URL}/sync/all-items/{group}/?client_id={self.simkl_api.client_id}'
        with BusyDialog():
            response = self.simkl_api.get_api_request(url, headers=self.simkl_api.auth_headers)
        try:
            data = response.json() if response is not None else None
        except (ValueError, AttributeError):
            return False, None
        rows = data.get(group) if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return False, None
        for row in rows:
            ids = ((row or {}).get(key) or {}).get('ids') or {}
            if str(ids.get('tmdb')) == str(self.tmdb_id):
                return True, row
        return True, None

    def confirm_unwatching_movie(self):
        """True = go ahead with putting this item on a plan-to-watch / hold / dropped list.

        Simkl drops a movie's watched state the moment it goes to any of those lists (live-probed 2026-10-14:
        last_watched_at becomes null), so a watched movie asks first; declining sets self.cancelled and
        nothing is sent. Shows keep their history through a status change and never ask. Watched = the full
        all-items list says so (last_watched_at set or status 'completed'); if that list cannot be read the
        local play count decides.
        """
        if self.simkl_type != 'movie':
            return True
        readable, entry = self.read_library_entry()
        if readable:
            watched = bool(entry) and (entry.get('last_watched_at') is not None or entry.get('status') == 'completed')
        else:
            watched = bool(self.get_sync_value('plays'))
        if not watched or Dialog().yesno(self.name, get_localized(32589)):
            return True
        self.cancelled = True
        return False

    def remove_status(self):
        """Take a movie / show off its plan-to-watch, hold or dropped list. Returns the response (None = failed).

        Simkl has no call that only clears a status: POST /sync/history/remove also deletes the watch history
        and the rating. Live-probed 2026-10-14:
          * a show keeps its real episode history through every status change, so a show with any history or
            a rating leaves the list by becoming 'watching' (add-to-list), never through history/remove;
          * a movie loses its watched state the moment it goes to plantowatch / hold / dropped, so history/remove
            takes nothing more from it - except its rating (a rated movie asks first).
        history/remove is only sent when the full all-items list was read and shows nothing to lose. If the list
        could not be read nothing is sent; if it does not list the item there is nothing to remove on Simkl.
        """
        readable, entry = self.read_library_entry()
        if not readable:
            kodi_log(f'Simkl: could not read the library to check {self.tmdb_type} {self.tmdb_id} before taking it off a list - nothing sent', 1)
            return None
        if entry is None:
            return {'removed': 0}
        if self.simkl_type == 'show':
            if not (entry.get('watched_episodes_count') == 0 and entry.get('last_watched_at') is None
                    and entry.get('user_rating') is None):
                self.kept_history = True
                kodi_log(f'Simkl: show {self.tmdb_id} has watch history or a rating - it leaves its list as "watching" (history/remove would wipe it)', 1)
                return self.call_api('sync', 'add-to-list', payload=self.build_payload(to='watching'))
        elif entry.get('user_rating') is not None:
            if not Dialog().yesno(self.name, get_localized(32588).format(entry.get('user_rating'))):
                self.cancelled = True
                return None
        return self.call_api('sync', 'history', 'remove', payload=self.build_payload())

    def mark_watching_in_db(self):
        """Local write-through after remove_status left a show as 'watching' (same marks as ItemSimklWatching)."""
        if not (self.kept_history and self.is_successful_sync):
            return
        try:
            from datetime import datetime, timezone
            ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
            tmdb_id = int(self.tmdb_id)
            item_id = f'tv.{tmdb_id}'
            cache = self.simkl_syncdata.cache
            keys = ('watching_at', 'item_type', 'tmdb_type', 'tmdb_id', 'season_number', 'episode_number')
            cache.set_many_values(keys=keys, data={item_id: [ts, 'show', 'tv', tmdb_id, None, None]})
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


class ItemSimklWatching(ItemSimklSyncBase):
    """Set a show's Simkl status to 'watching'.

    Add:    POST /sync/add-to-list  with "to": "watching"
    Shows only: Simkl rewrites 'watching' on a movie to 'completed' (live-checked 2026-10-14).
    Add-only: the only endpoint that clears a status is /sync/history/remove, which also wipes the
    show's watch history, so there is no 'remove from watching' (Plan to watch / Hold / Drop already
    cover the other statuses). Shown while the local watching_at write-through column is empty.
    """

    @cached_property
    def is_allowed_type(self):
        return self.simkl_type == 'show'

    def get_self(self):
        if not super().get_self():
            return None
        if self.get_sync_value('watching_at'):
            return None
        return self

    def get_name(self):
        return get_localized(32587)  # "Set as watching"

    def get_sync_response(self):
        return self.call_api('sync', 'add-to-list', payload=self.build_payload(to='watching'))

    def update_watching_in_db(self):
        """Statuses are mutually exclusive on Simkl: record watching and drop the other status marks."""
        if not self.is_successful_sync:
            return
        try:
            from datetime import datetime, timezone
            ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
            tmdb_id = int(self.tmdb_id)
            item_id = f'tv.{tmdb_id}'
            cache = self.simkl_syncdata.cache
            keys = ('watching_at', 'item_type', 'tmdb_type', 'tmdb_id', 'season_number', 'episode_number')
            cache.set_many_values(keys=keys, data={item_id: [ts, 'show', 'tv', tmdb_id, None, None]})
            cache.execute_sql(
                'UPDATE simplecache SET plantowatch_at=NULL, hold_at=NULL, dropped_at=NULL WHERE id=?',
                (item_id,))
        except Exception:
            pass

    def sync(self):
        self.reset_lastactivities()
        self.update_watching_in_db()
        self.display_dialog()
        self.refresh_containers()


class ItemSimklPlanToWatch(ItemSimklSyncBase):
    """Add / remove movie or show from Simkl plan-to-watch list.

    Add:    POST /sync/add-to-list  with "to": "plantowatch"
    Remove: remove_status() - POST /sync/history/remove clears the list status but also the watch history and
            rating, so a show with history leaves the list as 'watching' instead (see ItemSimklSyncBase).
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
            return self.remove_status()
        if not self.confirm_unwatching_movie():
            return None
        return self.call_api('sync', 'add-to-list', payload=self.build_payload(to='plantowatch'))

    def _cross_sync(self):
        from tmdbhelper.lib.script.sync.cross_sync import mirror_watchlist_to_trakt, mirror_watchlist_to_mdblist
        mirror_watchlist_to_trakt(self.tmdb_type, self.tmdb_id, self.remove)
        mirror_watchlist_to_mdblist(self.tmdb_type, self.tmdb_id, self.remove)

    def sync(self):
        _ = self.sync_response
        if self.cancelled:
            return
        self.reset_lastactivities()
        self.update_status_in_db('plantowatch_at', not self.remove)
        self.mark_watching_in_db()
        self.display_dialog()
        self.refresh_containers()
        if self.is_successful_sync:
            self._cross_sync()


class ItemSimklHold(ItemSimklSyncBase):
    """Add / remove movie or show from Simkl hold list.

    Add:    POST /sync/add-to-list  with "to": "hold"
    Remove: remove_status() (a show with history leaves the list as 'watching'; see ItemSimklSyncBase)
    """

    @cached_property
    def remove(self):
        return bool(self.get_sync_value('hold_at'))

    def get_name(self):
        return get_localized(32559 if self.remove else 32558)
        # 32558 = "Add to hold" | 32559 = "Remove from hold"

    def get_sync_response(self):
        if self.remove:
            return self.remove_status()
        if not self.confirm_unwatching_movie():
            return None
        return self.call_api('sync', 'add-to-list', payload=self.build_payload(to='hold'))

    def sync(self):
        _ = self.sync_response
        if self.cancelled:
            return
        self.reset_lastactivities()
        self.update_status_in_db('hold_at', not self.remove)
        self.mark_watching_in_db()
        self.display_dialog()
        self.refresh_containers()


class ItemSimklDrop(ItemSimklSyncBase):
    """Add / remove movie or show from the Simkl 'Dropped' list.

    Add:    POST /sync/add-to-list. The accepted "to" value depends on the Simkl app the token belongs to
            (live-probed 2026-10-14): this addon's own app accepts "notinteresting" (201; 'dropped' = 400
            'Wrong "to" parameter'), Seren's app accepts "dropped" (201; 'notinteresting' = 400). "notinteresting"
            goes first; a rejected request (400 comes back as None) is retried once with "dropped".
    Remove: remove_status() (a show with history leaves the list as 'watching'; see ItemSimklSyncBase).
    """

    @cached_property
    def remove(self):
        return bool(self.get_sync_value('dropped_at'))

    def get_name(self):
        return get_localized(32561 if self.remove else 32560)
        # 32560 = "Drop" | 32561 = "Remove from dropped"

    def get_sync_response(self):
        if self.remove:
            return self.remove_status()
        if not self.confirm_unwatching_movie():
            return None
        response = self.call_api('sync', 'add-to-list', payload=self.build_payload(to='notinteresting'))
        if response is None:
            response = self.call_api('sync', 'add-to-list', payload=self.build_payload(to='dropped'))
        return response

    def sync(self):
        _ = self.sync_response
        if self.cancelled:
            return
        self.reset_lastactivities()
        self.update_status_in_db('dropped_at', not self.remove)
        self.mark_watching_in_db()
        self.display_dialog()
        self.refresh_containers()


class ItemSimklClearProgress(ItemSimklSyncBase):
    """Remove the Simkl playback session (resume point) of a movie / episode without marking it unwatched.

    Endpoint: DELETE /sync/playback/{id}  (204).
    Shown only while a local row holds the session id (paused_id, written by the scrobbler and, for
    movies, by the playback sync): without the id there is nothing to delete.
    Episodes are allowed here although the other Simkl items skip them: the row the scrobbler wrote
    is TMDb-numbered, and the delete is by id, so the TVDB numbering mismatch does not matter.
    If the DELETE fails the id is looked up in GET /sync/playback; an id the server no longer lists
    is already gone, so the stale local row is cleared and the action counts as done (episode rows
    are never rewritten by the playback sync, so a stale id would otherwise stay forever).
    """
    preconfigured = True

    @cached_property
    def is_allowed_type(self):
        return self.simkl_type in ('movie', 'episode')

    def get_self(self):
        if not super().get_self():
            return None
        if not self.get_sync_value('paused_id'):
            return None
        return self

    def get_name(self):
        return get_localized(38209)  # "Reset resume position" (Kodi core string, same label as Trakt's item)

    @cached_property
    def paused_id(self):
        return self.get_sync_value('paused_id')

    def is_playback_gone(self):
        """True only when GET /sync/playback was read successfully and no longer lists self.paused_id."""
        url = f'{BASE_URL}/sync/playback?client_id={self.simkl_api.client_id}'
        response = self.simkl_api.get_api_request(url, headers=self.simkl_api.auth_headers)
        if response is None:
            return False
        try:
            entries = response.json()
        except (ValueError, AttributeError):
            return False
        if not isinstance(entries, list):
            return False
        return str(self.paused_id) not in {str(e.get('id')) for e in entries if isinstance(e, dict)}

    def get_sync_response(self):
        """A dict (truthy for is_successful_sync) when the session is deleted or already gone; None on failure."""
        if not self.paused_id:
            return None
        with BusyDialog():
            url = f'{BASE_URL}/sync/playback/{self.paused_id}?client_id={self.simkl_api.client_id}'
            response = self.simkl_api.get_api_request(url, method='delete', headers=self.simkl_api.auth_headers)
            if response is not None:
                return {'cleared': 1}
            if self.is_playback_gone():
                return {'cleared': 0}
        return None

    def update_progress_in_db(self):
        if not self.is_successful_sync:
            return
        try:
            db_type = 'movie' if self.simkl_type == 'movie' else 'tv'
            season = int(self.season) if self.simkl_type == 'episode' else None
            episode = int(self.episode) if self.simkl_type == 'episode' else None
            item_id = self.simkl_syncdata.get_name(db_type, int(self.tmdb_id), season, episode)
            self.simkl_syncdata.cache.execute_sql(
                'UPDATE simplecache SET paused_progress=0, paused_at=NULL, paused_id=NULL WHERE id=?',
                (item_id,))
        except Exception:
            pass

    def sync(self):
        _ = self.sync_response
        self.update_progress_in_db()
        self.display_dialog()
        self.refresh_containers()


class ItemSimklRating(ItemSimklSyncBase):
    """Rate a movie or show on Simkl (1-10; 0 removes the rating, as the Trakt / MDBList items do).

    Add / change: POST /sync/ratings         {"movies": [{"ids": {"tmdb": N}, "rating": X}]}  (or "shows")
    Remove:       POST /sync/ratings/remove  {"movies": [{"ids": {"tmdb": N}}]}

    Movies and shows only (live-checked 2026-10-14): a season rating overwrites the show's rating and an
    episode rating comes back not_found. An out-of-range rating is answered with HTTP 201 and the item in
    not_found, which is_successful_sync already treats as a failure; 0-10 is also checked here.

    Simkl puts an item that is not in the library there when it is rated: an unwatched movie (even one on
    plan-to-watch) becomes 'completed', a show that is in no list becomes 'watching' (a show already on
    plan-to-watch / hold / dropped keeps its status). Since a mirrored 'completed' would then reach Trakt
    and MDBList as a watched movie, the item is only offered for a movie that is already watched and a
    show that is already in the library (a synced watched row or a local status mark).
    """

    @cached_property
    def current_rating(self):
        return self.get_sync_value('rating') or 0

    def get_self(self):
        if not super().get_self():
            return None
        if self.simkl_type == 'movie':
            if not self.get_sync_value('plays'):
                return None
        elif not self.is_in_library:
            return None
        return self

    @cached_property
    def is_in_library(self):
        """A show is in the Simkl library when the watched sync gave it a row (watched_episodes is 0 or more
        for every library show; a show that left the library has it cleared) or a status was set here."""
        if self.get_sync_value('watched_episodes') is not None:
            return True
        return any(self.get_sync_value(k) for k in ('plantowatch_at', 'hold_at', 'dropped_at', 'watching_at'))

    def get_name(self):
        if self.current_rating:
            return f'{get_localized(32489)} ({self.current_rating})'  # "Change rating (X)"
        return get_localized(32485)  # "Add rating"

    def build_rating_payload(self, rating):
        item = {'ids': {'tmdb': int(self.tmdb_id)}}
        if rating:
            item['rating'] = rating
        return {'movies' if self.simkl_type == 'movie' else 'shows': [item]}

    def get_sync_response(self):
        try:
            x = int(Dialog().numeric(0, f'{get_localized(32485)} (0-10)'))
        except ValueError:
            return None  # cancelled - _rating_value intentionally not set
        if x < 0 or x > 10:
            return None
        self._rating_value = x
        path = ('sync', 'ratings', 'remove') if x == 0 else ('sync', 'ratings')
        return self.call_api(*path, payload=self.build_rating_payload(x))

    def update_rating_in_db(self):
        rating = getattr(self, '_rating_value', None)
        if rating is None or not self.is_successful_sync:
            return
        try:
            db_type = 'movie' if self.simkl_type == 'movie' else 'tv'
            tmdb_id = int(self.tmdb_id)
            item_id = f'{db_type}.{tmdb_id}'
            cache = self.simkl_syncdata.cache
            if rating:
                from datetime import datetime, timezone
                ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
                cache.execute_sql(
                    cache.statement_insert_or_ignore('simplecache', ('id',)), (item_id,))
                cache.execute_sql(
                    'UPDATE simplecache SET rating=?, rated_at=?, item_type=?, tmdb_type=?, tmdb_id=?'
                    ' WHERE id=?',
                    (rating, ts, self.simkl_type, db_type, tmdb_id, item_id))
            else:
                cache.execute_sql(
                    'UPDATE simplecache SET rating=NULL, rated_at=NULL WHERE id=?', (item_id,))
        except Exception:
            pass

    def _cross_sync(self):
        from tmdbhelper.lib.script.sync.cross_sync import mirror_rating_to_trakt, mirror_rating_to_mdblist
        mirror_rating_to_trakt(self.tmdb_type, self.tmdb_id, None, None, self._rating_value)
        mirror_rating_to_mdblist(self.tmdb_type, self.tmdb_id, None, None, self._rating_value)

    def sync(self):
        _ = self.sync_response  # triggers the dialog + API call; sets self._rating_value when the input is valid
        if not hasattr(self, '_rating_value'):
            return  # cancelled or out of range
        self.update_rating_in_db()
        self.reset_lastactivities()
        self.display_dialog()
        self.refresh_containers()
        if self.is_successful_sync:
            self._cross_sync()
