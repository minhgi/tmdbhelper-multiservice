from xbmc import Player
from json import loads
from jurialmunkey.parser import boolean
from jurialmunkey.window import get_property
from tmdbhelper.lib.monitor.images import ImageFunctions
from tmdbhelper.lib.monitor.common import CommonMonitorFunctions, SETPROP_RATINGS, SETMAIN_ARTWORK
from tmdbhelper.lib.addon.plugin import get_condvisibility, get_infolabel, get_setting
from tmdbhelper.lib.addon.thread import SafeThread
from tmdbhelper.lib.addon.logger import kodi_log


EXTERNAL_GUARD_DELAY = 12  # seconds the observer waits at the end of playback before checking whether the playing add-on already reported it

# Add-ons that report their own playback, and the services each one reports to: {addon id: {service: reports(setting)}}.
# setting(key) reads one of that add-on's settings ('' when unset). Keep the conditions the same as the add-on's own.
EXTERNAL_OWNERS = {
    'plugin.video.seren': {
        'trakt': lambda s: bool(s('trakt.auth')) and s('trakt.enabled') == 'true' and s('trakt.scrobbling') != 'false',
        'mdblist': lambda s: bool(s('mdblist.auth') or s('mdblist.apikey')) and s('mdblist.enabled') == 'true' and s('mdblist.scrobbling') != 'false',
        'simkl': lambda s: bool(s('simkl.auth')) and s('simkl.enabled') != 'false' and s('simkl.scrobbling') != 'false',
    },
    'plugin.video.elementum': {
        'trakt': lambda s: bool(s('trakt_token')) and (s('trakt_scrobble') == 'true' or s('trakt_sync_watched') != 'false'),
    },
}


# Add-ons that play a plain stream URL (Kodi then shows no plugin:// origin) name themselves in a Home property instead:
# {addon id: property name}. The value is JSON {"mediatype": "movie" | "episode", "tmdb_id", "season", "episode"} and only
# counts when it matches the item that is playing. Read with prefix=-1: get_property would otherwise add "TMDbHelper.".
EXTERNAL_HANDSHAKES = {'plugin.video.seren': 'seren.playback'}


def get_external_handshake_origin(identity):
    """ Id of the add-on that claims the playing item through its Home property, '' if none does """
    for addon_id, name in EXTERNAL_HANDSHAKES.items():
        try:
            marker = loads(get_property(name, prefix=-1) or '{}')
            if marker.get('mediatype') != identity['tmdb_type'] or f'{marker.get("tmdb_id")}' != identity['tmdb_id']:
                continue
            if identity['tmdb_type'] == 'episode' and (
                    int(marker.get('season') or 0) != int(identity['season']) or int(marker.get('episode') or 0) != int(identity['episode'])):
                continue
            return addon_id
        except (ValueError, TypeError, AttributeError):
            continue
    return ''


def get_external_owned_services(addon_id):
    """ Services the add-on that started playback reports to itself (and so must not be reported to again) """
    owners = EXTERNAL_OWNERS.get(addon_id)
    if not owners:
        return set()
    try:
        from xbmcaddon import Addon
        addon = Addon(addon_id)
    except Exception:
        return set()

    def setting(key):
        try:
            return addon.getSetting(key)
        except Exception:
            return ''

    return {service for service, reports in owners.items() if reports(setting)}


class PlayerScrobbler():
    external = False  # True when another add-on started playback and this scrobbler only observes it (see PlayerMonitor.get_external_playerstrings)
    service_name = 'Trakt'
    _baseline = None
    _baseline_thread = None
    _own_stamps = {}  # {service:media type: watched stamp left by our own last watched post}, shared so back-to-back episodes do not mistake it for another add-on's

    def __init__(self, trakt_api, total_time, playerstring=None):
        if playerstring is not None:  # Identity handed in by the monitor: {} makes the scrobbler inert
            self._playerstring = playerstring
            self.external = bool(playerstring)
        self.trakt_api = trakt_api
        self.current_time = 0
        self.total_time = total_time
        self.tvdb_id = self.playerstring.get('tvdb_id')
        self.imdb_id = self.playerstring.get('imdb_id')
        self.tmdb_id = self.playerstring.get('tmdb_id')
        self.tmdb_type = self.get_playerstring_tmdb_type()
        self.season = int(self.playerstring.get('season') or 0)
        self.episode = int(self.playerstring.get('episode') or 0)
        self.stopped = False
        self.started = False

    def get_playerstring_tmdb_type(self):
        tmdb_type = self.playerstring.get('tmdb_type')
        if tmdb_type in ('movie', ):
            return 'movie'
        if tmdb_type in ('season', 'episode', 'tv'):
            return 'tv'
        return ''

    def is_trakt_authorized(func):
        """ decorator to check that trakt is authorized  """

        def wrapper(self, *args, **kwargs):
            if not boolean(get_property('TraktIsAuth')):
                return
            if not get_setting('trakt_scrobbling'):
                return
            if not self.trakt_api.authorization:
                return
            if not self.trakt_item:
                return
            return func(self, *args, **kwargs)

        return wrapper

    def is_scrobbling(func):
        """ decorator to check if available item should be scrobbled """

        def wrapper(self, *args, **kwargs):
            if self.stopped:
                return
            if not self.tmdb_type:
                return
            if not self.tmdb_id:
                return
            if not self.total_time:
                return
            return func(self, *args, **kwargs)

        return wrapper

    @property
    def progress(self):
        return ((self.current_time / self.total_time) * 100)

    @property
    def playerstring(self):
        try:
            return self._playerstring
        except AttributeError:
            self._playerstring = self.get_playerstring()
            return self._playerstring

    @staticmethod
    def get_playerstring():
        playerstring = get_property('PlayerInfoString')
        if not playerstring:
            return {}
        return loads(playerstring)

    def is_match(self, tmdb_type, tmdb_id):
        if self.external:
            # The identity was fixed from the info tag when this item started and the scrobblers are rebuilt for every new
            # item; the monitor's own tmdb_id can differ (it reads a show's id under 'tmdb' as an episode id), so compare the type only
            return f'{self.tmdb_type}' == f'{tmdb_type}'
        if f'{self.tmdb_id}' != f'{tmdb_id}':
            return False
        if f'{self.tmdb_type}' != f'{tmdb_type}':
            return False
        return True

    @is_scrobbling
    def update_time(self, tmdb_type, tmdb_id, current_time):
        if not self.is_match(tmdb_type, tmdb_id):
            return
        self.current_time = current_time

    @property
    def trakt_item(self):
        try:
            return self._trakt_item
        except AttributeError:
            self._trakt_item = self.get_trakt_item() or {}
            return self._trakt_item

    @is_scrobbling
    def get_trakt_item(self):
        if self.tmdb_type == 'tv':
            if not self.season or not self.episode:
                return
            return {
                'show': {'ids': {'tmdb': self.tmdb_id}},
                'episode': {'season': self.season, 'number': self.episode},
                'progress': self.progress
            }

        if self.tmdb_type == 'movie':
            return {
                'movie': {'ids': {'tmdb': self.tmdb_id}},
                'progress': self.progress
            }

    @is_trakt_authorized
    def _trakt_scrobble_post(self, method):
        self.trakt_item['progress'] = self.progress
        self.trakt_api.get_api_request(
            f'https://api.trakt.tv/scrobble/{method}',
            postdata=self.trakt_item,
            headers=self.trakt_api.headers,
            method='json'
        )

    @is_scrobbling
    def trakt_scrobbling(self, method):
        if method not in ('start', 'pause', 'stop'):
            return
        self._trakt_scrobble_post(method)

    @property
    def _watched_ready(self):
        """ True when a watched mark would actually be sent: service signed in and its scrobbling setting on """
        return bool(boolean(get_property('TraktIsAuth')) and self.trakt_api.authorization and get_setting('trakt_scrobbling'))

    @property
    def _resume_ready(self):
        return self._watched_ready

    def external_report(self):
        """ What external playback will send to this service, for the log """
        sends = [name for name, ready in (('resume points', self._resume_ready), ('watched', self._watched_ready)) if ready]
        return 'reports ' + ' + '.join(sends) if sends else 'skipped (not signed in or its scrobbling settings are off)'

    def _watched_stamp(self):
        """ Newest watched-activity stamp of the service for this media type, None if it cannot be read """
        try:
            data = self.trakt_api.get_api_request('https://api.trakt.tv/sync/last_activities', headers=self.trakt_api.headers).json()
            return data['movies' if self.tmdb_type == 'movie' else 'episodes']['watched_at'] or ''
        except Exception:
            return None

    def _start_baseline(self):
        """ External playback only: note the service's watched stamp as playback starts (read off the monitor thread) """
        if not self.external or self._baseline_thread or not self._watched_ready:
            return

        def _read():
            self._baseline = self._watched_stamp()

        self._baseline_thread = SafeThread(target=_read)
        self._baseline_thread.start()

    def _report_watched(self, action):
        """ Run the post that marks the item watched.
        For playback started by another add-on that add-on may already have reported the same viewing, so wait a moment
        and post only if the service's watched activity has not moved since playback started.
        """
        if not self.external:
            return action()
        if not self._watched_ready:
            return
        SafeThread(target=self._report_watched_guarded, args=(action, )).start()

    def _report_watched_guarded(self, action):
        from xbmc import Monitor
        label = f'External playback {self.tmdb_type} {self.tmdb_id} S{self.season}E{self.episode} ({self.service_name})'
        own_key = f'{self.service_name}:{self.tmdb_type}'
        if self._baseline_thread:
            self._baseline_thread.join()
        if Monitor().waitForAbort(EXTERNAL_GUARD_DELAY):
            return
        after = self._watched_stamp()
        if self._baseline is None or after is None:
            return kodi_log(f'{label}: not marked watched - the service\'s watched activity could not be read', 1)
        if after not in (self._baseline, PlayerScrobbler._own_stamps.get(own_key)):
            return kodi_log(f'{label}: not marked watched - the playing add-on already reported it', 1)
        action()
        kodi_log(f'{label}: watched mark sent', 1)
        PlayerScrobbler._own_stamps[own_key] = self._watched_stamp()

    @is_scrobbling
    def start(self, tmdb_type, tmdb_id):
        if not self.is_match(tmdb_type, tmdb_id):
            return self.stop(tmdb_type, tmdb_id)
        self._start_baseline()
        self.trakt_scrobbling('start')
        self.started = True

    @is_scrobbling
    def pause(self, tmdb_type, tmdb_id):
        if not self.is_match(tmdb_type, tmdb_id):
            return self.stop(tmdb_type, tmdb_id)
        self.trakt_scrobbling('pause')

    @is_scrobbling
    def stop(self, tmdb_type, tmdb_id):
        if not self.started:
            return
        if self.external:
            # Kodi marks its own library and refreshes the widgets itself; only Trakt's stop (which logs the play from 80%) is guarded
            if self.progress >= 75:
                self._report_watched(lambda: self._trakt_scrobble_post('stop'))
            else:
                self.trakt_scrobbling('stop')
            self.stopped = True
            return
        self.trakt_scrobbling('stop')
        self.set_kodi_watched()
        self.update_stats()
        # TODO: Decide if we allow further scrobbling of item if restarted after stopped
        # if self.is_match(tmdb_type, tmdb_id):
        #     return
        self.stopped = True

    @is_scrobbling
    @is_trakt_authorized
    def update_stats(self):
        from tmdbhelper.lib.script.method.trakt import get_stats
        from tmdbhelper.lib.addon.consts import LASTACTIVITIES_DATA
        from tmdbhelper.lib.script.method.kodi_utils import reload_widgets
        from xbmc import executebuiltin
        get_property(LASTACTIVITIES_DATA, clear_property=True)
        get_stats()
        executebuiltin('Container.Refresh')
        reload_widgets()

    @is_scrobbling
    def set_kodi_watched(self):
        if not self.current_time:
            return

        # Only update if progress is 75% or more
        if self.progress < 75:
            return

        import tmdbhelper.lib.api.kodi.rpc as rpc

        if self.tmdb_type == 'tv':
            tvshowid = rpc.KodiLibrary('tvshow').get_info(
                info='dbid',
                imdb_id=self.imdb_id,
                tmdb_id=self.tmdb_id,
                tvdb_id=self.tvdb_id)
            if not tvshowid:
                return
            dbid = rpc.KodiLibrary('episode', tvshowid).get_info(
                info='dbid',
                season=self.season,
                episode=self.episode)
            if not dbid:
                return
            rpc.set_watched(dbid=dbid, dbtype='episode')
            return

        if self.tmdb_type == 'movie':
            dbid = rpc.KodiLibrary('movie').get_info(
                info='dbid',
                imdb_id=self.imdb_id,
                tmdb_id=self.tmdb_id,
                tvdb_id=self.tvdb_id)
            if not dbid:
                return
            rpc.set_watched(dbid=dbid, dbtype='movie')
            return


class MDbListScrobbler(PlayerScrobbler):
    """MDBList scrobbler — runs alongside PlayerScrobbler (Trakt) during playback.

    Endpoint refs (POV mdblist_api.py confirmed):
        pause:   POST /scrobble/pause  {'movie': {'ids': {'tmdb': id}}, 'progress': float}
                                        episode: {'show': {'ids': {'tmdb': id}, 'season': {'number': sn, 'episode': {'number': ep}}}, 'progress': float}
        watched: POST /sync/watched    {'movies': [{'tmdb': id}]} / {'shows': [{'tmdb': id, 'seasons': [...]}]}

    Lifecycle:
        start  — sets started=True; no API call (MDBList has no /scrobble/start endpoint).
        pause  — POST /scrobble/pause (gated on service_mdblist + mdblist_playprogress).
        stop ≥75% — POST /sync/watched + direct DB write via ItemMDbListWatched + reload_widgets.
        stop <75% — POST /scrobble/pause (server-only resume save; no local DB write in v1).
    """

    service_name = 'MDBList'

    def __init__(self, mdblist_api, total_time, playerstring=None):
        # Mirrors PlayerScrobbler.__init__ but stores mdblist_api instead of trakt_api.
        if playerstring is not None:
            self._playerstring = playerstring
            self.external = bool(playerstring)
        self.mdblist_api = mdblist_api
        self.current_time = 0
        self.total_time = total_time
        ps = self.playerstring
        self.tvdb_id = ps.get('tvdb_id')
        self.imdb_id = ps.get('imdb_id')
        self.tmdb_id = ps.get('tmdb_id')
        self.tmdb_type = self.get_playerstring_tmdb_type()
        self.season = int(ps.get('season') or 0)
        self.episode = int(ps.get('episode') or 0)
        self.stopped = False
        self.started = False

    @property
    def _is_authorized(self):
        return bool(self.mdblist_api.user_token.value or self.mdblist_api.api_key)

    @property
    def _resume_ready(self):
        return bool(get_setting('service_mdblist') and get_setting('mdblist_playprogress') and self._is_authorized)

    @property
    def _watched_ready(self):
        return bool(get_setting('service_mdblist') and get_setting('mdblist_scrobbling') and self._is_authorized)

    @property
    def _api_progress(self):
        """MDBList rejects (HTTP 400, which the request layer drops silently) any progress with more than 2 decimals."""
        return round(min(max(self.progress, 0), 100), 2)

    def _pause_payload(self):
        """Build /scrobble/pause payload (POV-confirmed format with 'ids' wrapper)."""
        if self.tmdb_type == 'movie':
            return {'movie': {'ids': {'tmdb': self.tmdb_id}}, 'progress': self._api_progress}
        if self.tmdb_type == 'tv' and self.season and self.episode:
            return {
                'show': {'ids': {'tmdb': self.tmdb_id},
                         'season': {'number': self.season, 'episode': {'number': self.episode}}},
                'progress': self._api_progress
            }

    def _update_progress_in_db(self, progress):
        """Write pause progress directly to MDbListSync.db.
        Mirrors update_watched_in_db() pattern — direct SQL, bypasses sync pipeline.
        """
        try:
            from datetime import datetime, timezone
            db_type = self.tmdb_type
            tmdb_id = int(self.tmdb_id)
            season = self.season or None
            episode = self.episode or None
            item_type_str = 'episode' if (self.tmdb_type == 'tv' and season and episode) else \
                            'show' if self.tmdb_type == 'tv' else 'movie'
            syncdata = self.mdblist_api.mdblist_syncdata
            item_id = syncdata.get_name(db_type, tmdb_id, season, episode)
            ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
            cache = syncdata.cache
            cache.execute_sql(
                cache.statement_insert_or_ignore('simplecache', ('id',)),
                (item_id,))
            cache.execute_sql(
                'UPDATE simplecache SET paused_progress=?, paused_at=?,'
                ' item_type=?, tmdb_type=?, tmdb_id=?,'
                ' season_number=?, episode_number=? WHERE id=?',
                (int(progress), ts, item_type_str, db_type, tmdb_id, season, episode, item_id))
        except Exception:
            pass

    def _clear_progress_in_db(self):
        """Clear pause progress after marking item as watched.
        Also calls /scrobble/clear with the stored paused_id to remove the entry
        from the server's /sync/playback — otherwise the item stays in In-Progress.
        """
        try:
            db_type = self.tmdb_type
            tmdb_id = int(self.tmdb_id)
            season = self.season or None
            episode = self.episode or None
            syncdata = self.mdblist_api.mdblist_syncdata
            item_id = syncdata.get_name(db_type, tmdb_id, season, episode)
            cache = syncdata.cache
            # Read paused_id before clearing — needed for server-side clear
            rows = cache.get_values(item_id=item_id, keys=('paused_id',))
            paused_id = rows[0] if rows else None
            # Clear local DB
            cache.execute_sql(
                'UPDATE simplecache SET paused_progress=0, paused_at=NULL, paused_id=NULL WHERE id=?',
                (item_id,))
            # Clear server-side entry so item leaves /sync/playback (In-Progress list)
            if paused_id:
                url = self.mdblist_api.get_request_url('scrobble', 'clear')
                self.mdblist_api.get_api_request(
                    url, postdata={'id': paused_id}, method='json',
                    headers=self.mdblist_api.auth_headers)
        except Exception:
            pass

    def _post_pause(self):
        if not get_setting('service_mdblist'):
            return
        if not get_setting('mdblist_playprogress'):
            return
        if not self._is_authorized:
            return
        payload = self._pause_payload()
        if not payload:
            return
        url = self.mdblist_api.get_request_url('scrobble', 'pause')
        response = self.mdblist_api.get_api_request(url, postdata=payload, method='json', headers=self.mdblist_api.auth_headers)
        if response is None:
            kodi_log(f'MDBList scrobble/pause was not accepted: {payload}', 1)
        self._update_progress_in_db(self.progress)

    def _mark_watched(self):
        if not get_setting('service_mdblist'):
            return
        if not get_setting('mdblist_scrobbling'):
            return
        if not self._is_authorized:
            return
        from tmdbhelper.lib.script.sync.mdblist_sync import ItemMDbListWatched
        season = self.season or None
        episode = self.episode or None
        item = ItemMDbListWatched(self.tmdb_type, self.tmdb_id, season, episode)
        # Make the API call directly — avoid BusyDialog (scrobbler operates silently)
        payload = item.build_payload()
        if not payload:
            return
        url = self.mdblist_api.get_request_url('sync', 'watched')
        self.mdblist_api.get_api_request(url, postdata=payload, method='json', headers=self.mdblist_api.auth_headers)
        # Proven direct-write path for badge + hub refresh (same as v6.0.42 context menu fix)
        item.update_watched_in_db(True, force=True)
        self._clear_progress_in_db()  # item no longer in-progress
        item.refresh_containers(force=True)

    def _watched_stamp(self):
        try:
            url = self.mdblist_api.get_request_url('sync', 'last_activities')
            data = self.mdblist_api.get_api_request(url, headers=self.mdblist_api.auth_headers).json()
            return data['watched_at' if self.tmdb_type == 'movie' else 'episode_watched_at'] or ''
        except Exception:
            return None

    @PlayerScrobbler.is_scrobbling
    def start(self, tmdb_type, tmdb_id):
        if not self.is_match(tmdb_type, tmdb_id):
            return self.stop(tmdb_type, tmdb_id)
        self._start_baseline()
        self.started = True  # No API call — MDBList has no /scrobble/start endpoint

    @PlayerScrobbler.is_scrobbling
    def pause(self, tmdb_type, tmdb_id):
        if not self.is_match(tmdb_type, tmdb_id):
            return self.stop(tmdb_type, tmdb_id)
        self._post_pause()

    @PlayerScrobbler.is_scrobbling
    def stop(self, tmdb_type, tmdb_id):
        if not self.started:
            return
        if self.progress >= 75:
            self._report_watched(self._mark_watched)
        else:
            self._post_pause()
        self.stopped = True


class SimklScrobbler(PlayerScrobbler):
    """Simkl scrobbler — runs alongside Trakt and MDBList scrobblers during playback.

    Pause:       POST /scrobble/pause  with TMDb IDs (201, returns resume id)
    Mark watched (≥75%): POST /sync/history (movie/episode) or /sync/add-to-list
                         with "to":"completed" (show)
    Clear resume: DELETE /sync/playback/{id}  (204) — /sync/history does NOT auto-clear.
    No /scrobble/start endpoint used (matches MDBList pattern).

    Resume reading: local-only (SimklSync.db written on pause). Progress from
    simkl.com or other devices not synced in this phase.
    Episode numbers use TMDb numbering — may mismatch TVDB for anime.
    """

    BASE_URL = 'https://api.simkl.com'

    service_name = 'Simkl'

    def __init__(self, simkl_api, total_time, playerstring=None):
        if playerstring is not None:
            self._playerstring = playerstring
            self.external = bool(playerstring)
        self.simkl_api = simkl_api
        self.current_time = 0
        self.total_time = total_time
        ps = self.playerstring
        self.tvdb_id = ps.get('tvdb_id')
        self.imdb_id = ps.get('imdb_id')
        self.tmdb_id = ps.get('tmdb_id')
        self.tmdb_type = self.get_playerstring_tmdb_type()
        self.season = int(ps.get('season') or 0)
        self.episode = int(ps.get('episode') or 0)
        self.stopped = False
        self.started = False

    @property
    def _is_authorized(self):
        return bool(self.simkl_api and self.simkl_api.user_token.value)

    @property
    def _resume_ready(self):
        return bool(get_setting('service_simkl') and get_setting('simkl_playprogress') and self._is_authorized)

    @property
    def _watched_ready(self):
        return bool(get_setting('service_simkl') and get_setting('simkl_scrobbling') and self._is_authorized)

    def _pause_payload(self):
        if self.tmdb_type == 'movie':
            return {'progress': self.progress, 'movie': {'ids': {'tmdb': self.tmdb_id}}}
        if self.tmdb_type == 'tv' and self.season and self.episode:
            return {
                'progress': self.progress,
                'show': {'ids': {'tmdb': self.tmdb_id}},
                'episode': {'season': self.season, 'number': self.episode}
            }

    def _update_progress_in_db(self, progress, paused_id):
        try:
            from datetime import datetime, timezone
            db_type = self.tmdb_type
            tmdb_id = int(self.tmdb_id)
            season = self.season or None
            episode = self.episode or None
            item_type_str = ('episode' if (self.tmdb_type == 'tv' and season and episode)
                             else 'show' if self.tmdb_type == 'tv' else 'movie')
            syncdata = self.simkl_api.simkl_syncdata
            item_id = syncdata.get_name(db_type, tmdb_id, season, episode)
            ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
            cache = syncdata.cache
            cache.execute_sql(
                cache.statement_insert_or_ignore('simplecache', ('id',)), (item_id,))
            cache.execute_sql(
                'UPDATE simplecache SET paused_progress=?, paused_at=?, paused_id=?,'
                ' item_type=?, tmdb_type=?, tmdb_id=?,'
                ' season_number=?, episode_number=? WHERE id=?',
                (int(progress), ts, paused_id,
                 item_type_str, db_type, tmdb_id, season, episode, item_id))
        except Exception:
            pass

    def _clear_progress_in_db(self):
        try:
            db_type = self.tmdb_type
            tmdb_id = int(self.tmdb_id)
            season = self.season or None
            episode = self.episode or None
            syncdata = self.simkl_api.simkl_syncdata
            item_id = syncdata.get_name(db_type, tmdb_id, season, episode)
            cache = syncdata.cache
            rows = cache.get_values(item_id=item_id, keys=('paused_id',))
            paused_id = rows[0] if rows else None
            cache.execute_sql(
                'UPDATE simplecache SET paused_progress=0, paused_at=NULL, paused_id=NULL WHERE id=?',
                (item_id,))
            if paused_id:
                url = (f'{self.BASE_URL}/sync/playback/{paused_id}'
                       f'?client_id={self.simkl_api.client_id}')
                self.simkl_api.get_api_request(
                    url, method='delete', headers=self.simkl_api.auth_headers)
        except Exception:
            pass

    def _post_pause(self):
        if not get_setting('service_simkl'):
            return
        if not get_setting('simkl_playprogress'):
            return
        if not self._is_authorized:
            return
        payload = self._pause_payload()
        if not payload:
            return
        url = f'{self.BASE_URL}/scrobble/pause?client_id={self.simkl_api.client_id}'
        response = self.simkl_api.get_api_request(
            url, postdata=payload, method='json', headers=self.simkl_api.auth_headers)
        paused_id = None
        if response is not None:
            try:
                paused_id = response.json().get('id')
            except Exception:
                pass
        self._update_progress_in_db(self.progress, paused_id)

    def _mark_watched(self):
        if not get_setting('service_simkl'):
            return
        if not get_setting('simkl_scrobbling'):
            return
        if not self._is_authorized:
            return
        from tmdbhelper.lib.script.sync.simkl_sync import ItemSimklWatched
        from datetime import datetime, timezone
        season = self.season or None
        episode = self.episode or None
        item = ItemSimklWatched(self.tmdb_type, self.tmdb_id, season, episode)
        # Build payload and call API directly — no BusyDialog in scrobbler
        ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        tmdb_id = int(self.tmdb_id)
        if self.tmdb_type == 'movie':
            payload = {'movies': [{'watched_at': ts, 'ids': {'tmdb': tmdb_id}}]}
            url = f'{self.BASE_URL}/sync/history?client_id={self.simkl_api.client_id}'
        elif self.tmdb_type == 'tv' and season and episode:
            payload = {'shows': [{'seasons': [{'episodes': [{'number': episode}], 'number': season}],
                                  'ids': {'tmdb': tmdb_id}}]}
            url = f'{self.BASE_URL}/sync/history?client_id={self.simkl_api.client_id}'
        else:
            return
        self.simkl_api.get_api_request(
            url, postdata=payload, method='json', headers=self.simkl_api.auth_headers)
        item.update_watched_in_db(True, force=True)
        self._clear_progress_in_db()
        item.refresh_containers(force=True)

    def _watched_stamp(self):
        try:
            url = f'{self.BASE_URL}/sync/activities?client_id={self.simkl_api.client_id}'
            data = self.simkl_api.get_api_request(url, headers=self.simkl_api.auth_headers).json()
            if self.tmdb_type == 'movie':
                return data['movies']['completed'] or ''
            shows = data['tv_shows']
            return f"{shows['watching'] or ''}|{shows['completed'] or ''}"
        except Exception:
            return None

    @PlayerScrobbler.is_scrobbling
    def start(self, tmdb_type, tmdb_id):
        if not self.is_match(tmdb_type, tmdb_id):
            return self.stop(tmdb_type, tmdb_id)
        self._start_baseline()
        self.started = True  # No /scrobble/start endpoint

    @PlayerScrobbler.is_scrobbling
    def pause(self, tmdb_type, tmdb_id):
        if not self.is_match(tmdb_type, tmdb_id):
            return self.stop(tmdb_type, tmdb_id)
        self._post_pause()

    @PlayerScrobbler.is_scrobbling
    def stop(self, tmdb_type, tmdb_id):
        if not self.started:
            return
        if self.progress >= 75:
            self._report_watched(self._mark_watched)
        else:
            self._post_pause()
        self.stopped = True


class PlayerItem():
    def __init__(self, parent):
        self._parent = parent

    @property
    def baseitem(self):
        try:
            return self._baseitem
        except AttributeError:
            self._baseitem = self.get_baseitem()
            return self._baseitem

    def get_baseitem(self):
        if not self._parent.isPlayingVideo():
            return
        return self._parent.ib.get_item(
            self._parent.tmdb_type,
            self._parent.tmdb_id,
            self._parent.season,
            self._parent.episode)

    @property
    def details(self):
        try:
            return self._details
        except AttributeError:
            self._details = self.get_baseitem_details()
            return self._details

    def get_baseitem_details(self):
        if not self.baseitem:
            return {}
        return self.baseitem['listitem']

    @property
    def artwork(self):
        try:
            return self._artwork
        except AttributeError:
            self._artwork = self.get_baseitem_artwork()
            return self._artwork

    def get_baseitem_artwork(self):
        if not self.baseitem:
            return {}
        return self.baseitem['artwork']

    @property
    def meta(self):
        try:
            return self._meta
        except AttributeError:
            self._meta = self.get_meta()
            return self._meta

    def get_meta(self):
        info_tag = self.get_player_info_tag()

        if not info_tag:
            return {}

        meta = {}
        meta['DbId'] = info_tag.getDbId()
        meta['MediaType'] = info_tag.getMediaType()
        meta['Title'] = info_tag.getTitle()
        meta['Year'] = info_tag.getYear()

        if self._parent.isPlayingVideo():
            meta['IMDBNumber'] = info_tag.getIMDBNumber()
            meta['TVShowTitle'] = info_tag.getTVShowTitle()
            meta['Season'] = info_tag.getSeason()
            meta['Episode'] = info_tag.getEpisode()
            meta['UniqueID.tvshow.tmdb'] = info_tag.getUniqueID('tvshow.tmdb')
            meta['UniqueID.tmdb'] = info_tag.getUniqueID('tmdb')

        return meta

    def get_player_info_tag(self):
        if self._parent.isPlayingVideo():
            return self._parent.getVideoInfoTag()
        if self._parent.isPlayingAudio():
            return self._parent.getMusicInfoTag()

    @property
    def dbtype(self):
        return self.meta.get('MediaType')

    @property
    def dbid(self):
        return self.meta.get('DbId')

    @property
    def imdb_id(self):
        if self.dbtype != 'movie':
            return
        return self.meta.get('IMDBNumber')

    @property
    def query(self):
        if self.dbtype == 'episode':
            return self.meta.get('TVShowTitle')
        return self.meta.get('Title')

    @property
    def year(self):
        if self.dbtype == 'episode':
            return
        return self.meta.get('Year')

    @property
    def epyear(self):
        if self.dbtype != 'episode':
            return
        return self.meta.get('Year')

    @property
    def season(self):
        if self.dbtype != 'episode':
            return
        return self.meta.get('Season')

    @property
    def episode(self):
        if self.dbtype != 'episode':
            return
        return self.meta.get('Episode')

    @property
    def tmdb_type(self):
        if self.dbtype == 'movie':
            return 'movie'
        if self.dbtype == 'episode':
            return 'tv'
        return ''

    @property
    def tmdb_id(self):
        try:
            return self._tmdb_id
        except AttributeError:
            self._tmdb_id = self.get_tmdb_id()
            return self._tmdb_id

    def get_tmdb_id_parent(self):
        if self.dbtype != 'episode':
            return self.meta.get('UniqueID.tmdb')

        tmdb_id = self.meta.get('UniqueID.tvshow.tmdb')

        return tmdb_id or self._parent.get_tmdb_id_parent(
            tmdb_id=self.meta.get('UniqueID.tmdb'),
            trakt_type='episode',
            season_episode_check=(
                self.season,
                self.episode,
            )
        )

    def get_tmdb_id(self):
        if self.dbtype not in ('episode', 'movie'):
            return

        tmdb_id = self.get_tmdb_id_parent()

        return tmdb_id or self._parent.get_tmdb_id(
            self.tmdb_type,
            self.imdb_id,
            self.query,
            self.year,
            self.epyear
        )

    def get_ratings(self):
        if not self.details:
            return {}
        if get_condvisibility("Skin.HasSetting(TMDbHelper.DisableRatings)"):
            return {}
        try:
            trakt_type = {'movie': 'movie', 'tv': 'show'}[self.tmdb_type]
        except KeyError:
            trakt_type = None
        if not trakt_type:
            return {}
        details = self.details

        # All rating functions write to non-overlapping keys in details['infoproperties']
        # so they are safe to run in parallel under CPython's GIL.
        threads = [
            SafeThread(target=self._parent.get_omdb_ratings, args=(details,)),
            SafeThread(target=self._parent.get_imdb_top250_rank, args=(details,), kwargs={'trakt_type': trakt_type}),
            SafeThread(target=self._parent.get_tvdb_awards, args=(details, self.tmdb_type, self.tmdb_id)),
            SafeThread(target=self._parent.get_trakt_ratings, args=(details, trakt_type), kwargs={'season': self.season, 'episode': self.episode}),
            SafeThread(target=self._parent.get_mdblist_ratings, args=(details, trakt_type), kwargs={'tmdb_id': self.tmdb_id}),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        return details.get('infoproperties', {})

    def get_artwork(self):
        if get_condvisibility("Skin.HasSetting(TMDbHelper.DisableArtwork)"):
            return {}
        if not self.artwork:
            return {}
        self.details['art'] = self._parent.ib.get_item_artwork(self.artwork, is_season=True if self.season else False)
        return self.details['art']


class PlayerMonitor(Player, CommonMonitorFunctions):
    def __init__(self):
        Player.__init__(self)
        CommonMonitorFunctions.__init__(self)
        self.property_prefix = 'Player'
        self.reset_properties()

    def onAVStarted(self):
        self.get_playingitem()

    def onAVChange(self):
        self.get_playingitem()

    def onPlayBackEnded(self):
        self.scrobbler_stop()
        self.reset_properties()

    def onPlayBackStopped(self):
        self.scrobbler_stop()
        self.reset_properties()

    def onPlayBackPaused(self):
        self.scrobbler_pause()

    def onPlayBackResumed(self):
        self.scrobbler_start()

    def reset_player_item(self):
        self.player_item = PlayerItem(self)

    def reset_properties(self):
        self.clear_properties()
        self.clear_artwork()
        self.previous_item = None
        self.current_item = None
        self.playerstring = None
        self.scrobbler = None
        self.mdb_scrobbler = None
        self.simkl_scrobbler = None

    def scrobbler_start(self):
        if not self.scrobbler:
            return
        self.scrobbler.start(self.tmdb_type, self.tmdb_id)
        if self.mdb_scrobbler:
            self.mdb_scrobbler.start(self.tmdb_type, self.tmdb_id)
        if self.simkl_scrobbler:
            self.simkl_scrobbler.start(self.tmdb_type, self.tmdb_id)

    def scrobbler_pause(self):
        if not self.scrobbler:
            return
        self.scrobbler.pause(self.tmdb_type, self.tmdb_id)
        if self.mdb_scrobbler:
            self.mdb_scrobbler.pause(self.tmdb_type, self.tmdb_id)
        if self.simkl_scrobbler:
            self.simkl_scrobbler.pause(self.tmdb_type, self.tmdb_id)

    def scrobbler_stop(self):
        if not self.scrobbler:
            return
        self.scrobbler.stop(self.tmdb_type, self.tmdb_id)
        if self.mdb_scrobbler:
            self.mdb_scrobbler.stop(self.tmdb_type, self.tmdb_id)
        if self.simkl_scrobbler:
            self.simkl_scrobbler.stop(self.tmdb_type, self.tmdb_id)

    def update_time(self):
        if not self.scrobbler:
            return
        self.scrobbler.update_time(self.tmdb_type, self.tmdb_id, self.getTime())
        if self.mdb_scrobbler:
            self.mdb_scrobbler.update_time(self.tmdb_type, self.tmdb_id, self.getTime())
        if self.simkl_scrobbler:
            self.simkl_scrobbler.update_time(self.tmdb_type, self.tmdb_id, self.getTime())

    def get_external_identity(self):
        """ Identity of the playing item from the explicit ids in Kodi's info tag - never a title search.
        {} unless the tag has a TMDb id (plus season and episode for an episode) """
        meta = self.player_item.meta
        if self.dbtype == 'movie':
            tmdb_id, season, episode = meta.get('UniqueID.tmdb'), 0, 0
        else:
            tmdb_id, season, episode = meta.get('UniqueID.tvshow.tmdb'), meta.get('Season'), meta.get('Episode')
            if not f'{tmdb_id or ""}'.isdigit():
                # Seren stores the SHOW's id under plain 'tmdb' on an episode, which is indistinguishable from an episode id
                # (Elementum): accept it only when Seren's handshake names this very show, season and episode
                shows_id = meta.get('UniqueID.tmdb')
                if f'{shows_id or ""}'.isdigit() and get_external_handshake_origin(
                        {'tmdb_type': 'episode', 'tmdb_id': f'{shows_id}', 'season': season or 0, 'episode': episode or 0}):
                    tmdb_id = shows_id
        if not f'{tmdb_id or ""}'.isdigit() or not int(tmdb_id):
            return {}
        if self.dbtype == 'episode' and not ((season or 0) > 0 and (episode or 0) > 0):
            return {}
        identity = {'tmdb_type': self.dbtype, 'tmdb_id': f'{tmdb_id}'}
        if self.dbtype == 'movie':
            imdb_id = meta.get('IMDBNumber')
            if imdb_id and f'{imdb_id}'.startswith('tt'):
                identity['imdb_id'] = imdb_id
        else:
            identity['season'], identity['episode'] = season, episode
        return identity

    def get_external_origin(self, identity=None):
        """ Id of the add-on that started the playing item, '' if it is not visible: the plugin:// path Kodi shows for a
        resolved item, else a Home-property handshake from an add-on that plays a stream URL (needs the item's identity) """
        candidates = [get_infolabel('Player.Filenameandpath'), get_infolabel('Player.Folderpath')]
        try:
            candidates.insert(0, self.getVideoInfoTag().getFilenameAndPath())
        except Exception:
            pass
        for path in candidates:
            if path and f'{path}'.startswith('plugin://'):
                return f'{path}'[len('plugin://'):].split('/', 1)[0]
        return get_external_handshake_origin(identity) if identity else ''

    def get_external_playerstrings(self):
        """ Opt-in observer (setting scrobble_external_playback, off by default) for playback started by another add-on.
        Returns the identity each scrobbler is built with, {} per service to leave it inert, or {} overall (nothing to do) when
        the setting is off, TMDb Helper's own player started the item, or it is not a movie or episode.
        - Identity comes from the info tag only. No TMDb id there means nothing is reported.
        - A service the playing add-on reports to itself is left alone. Trakt is also off unless scrobble_external_trakt is on.
        """
        self._external_log = None
        if not get_setting('scrobble_external_playback') or self.dbtype not in ('movie', 'episode'):
            return {}

        own = PlayerScrobbler.get_playerstring()
        own_type = 'movie' if own.get('tmdb_type') == 'movie' else 'tv' if own.get('tmdb_type') in ('season', 'episode', 'tv') else ''
        if own.get('tmdb_id') and f'{own["tmdb_id"]}' == f'{self.tmdb_id}' and own_type == self.tmdb_type:
            return {}  # Started by TMDb Helper's own player: the normal scrobblers cover it

        identity = self.get_external_identity()
        origin = self.get_external_origin(identity)
        if not identity:
            kodi_log(f'External playback ({origin or "origin unknown"}): no TMDb id in the info tag - nothing reported', 1)
            return {'trakt': {}, 'mdblist': {}, 'simkl': {}}

        owned = get_external_owned_services(origin)
        plan = {
            'trakt': identity if get_setting('scrobble_external_trakt') and 'trakt' not in owned else {},
            'mdblist': {} if 'mdblist' in owned else identity,
            'simkl': {} if 'simkl' in owned else identity,
        }
        skipped = {key: 'skipped (reported by the add-on)' if key in owned else 'skipped (off)' for key in plan if not plan[key]}
        self._external_log = (origin, identity, skipped)  # Logged by log_external_plan once the scrobblers say what they will send
        return plan

    def log_external_plan(self):
        if not self._external_log:
            return
        origin, identity, skipped = self._external_log
        self._external_log = None
        scrobblers = {'trakt': self.scrobbler, 'mdblist': self.mdb_scrobbler, 'simkl': self.simkl_scrobbler}
        summary = ', '.join(
            f'{name} {skipped.get(key) or scrobblers[key].external_report()}'
            for key, name in (('trakt', 'Trakt'), ('mdblist', 'MDBList'), ('simkl', 'Simkl')))
        kodi_log(f'External playback ({origin or "origin unknown"}) {identity}: {summary}', 1)

    @property
    def details(self):
        return self.player_item.details

    @property
    def artwork(self):
        return self.player_item.artwork

    @property
    def dbtype(self):
        return self.player_item.dbtype

    @property
    def dbid(self):
        return self.player_item.dbid

    @property
    def imdb_id(self):
        return self.player_item.imdb_id

    @property
    def query(self):
        return self.player_item.query

    @property
    def year(self):
        return self.player_item.year

    @property
    def epyear(self):
        return self.player_item.epyear

    @property
    def season(self):
        return self.player_item.season

    @property
    def episode(self):
        return self.player_item.episode

    @property
    def tmdb_type(self):
        return self.player_item.tmdb_type

    @property
    def tmdb_id(self):
        return self.player_item.tmdb_id

    def update_crop(self):
        if get_condvisibility("!Skin.HasSetting(TMDbHelper.EnableCrop)"):
            return

        art = self.details.get('art') or {}

        clearlogo = (
            get_infolabel('Player.Art(clearlogo)')
            or get_infolabel('Player.Art(artist.clearlogo)')
            or get_infolabel('Player.Art(tvshow.clearlogo)')
            or art.get('clearlogo')
            or art.get('tvshow.clearlogo'))

        if clearlogo != self.previous_clearlogo:
            ImageFunctions(method='crop', is_thread=False, prefix='Player', artwork=clearlogo).run()
            self.previous_clearlogo = clearlogo

    def update_blur(self):
        if get_condvisibility("!Skin.HasSetting(TMDbHelper.EnableBlur)"):
            return

        art = self.details.get('art') or {}

        fanart = (
            get_infolabel('Player.Art(fanart)')
            or get_infolabel('Player.Art(artist.fanart)')
            or get_infolabel('Player.Art(tvshow.fanart)')
            or art.get('fanart')
            or art.get('tvshow.fanart'))

        poster = (
            get_infolabel('Player.Art(poster)')
            or get_infolabel('Player.Art(artist.poster)')
            or get_infolabel('Player.Art(tvshow.poster)')
            or get_infolabel('Player.Icon')
            or art.get('poster')
            or art.get('tvshow.poster'))

        if poster != self.previous_poster:
            ImageFunctions(method='blur', is_thread=False, prefix='Player.Poster', artwork=poster).run()
            self.previous_poster = poster

        if fanart != self.previous_fanart:
            ImageFunctions(method='blur', is_thread=False, prefix='Player.Fanart', artwork=fanart).run()
            self.previous_fanart = fanart

    def update_artwork(self):
        self.update_crop()
        self.update_blur()

    def clear_artwork(self):
        self.clear_property('CropImage')
        self.clear_property('CropImage.Original')
        self.clear_property('Poster.BlurImage')
        self.clear_property('Poster.BlurImage.Original')
        self.clear_property('Fanart.BlurImage')
        self.clear_property('Fanart.BlurImage.Original')
        self.previous_clearlogo = None
        self.previous_poster = None
        self.previous_fanart = None

    def get_playingitem(self):
        # Check that video other than dummy splash video is playing
        if self.getPlayingFile() and self.getPlayingFile().endswith('dummy.mp4'):
            self.reset_properties()
            return

        # Get fresh info tags etc.
        self.reset_player_item()
        self.current_item = (
            self.dbtype,
            self.dbid,
            self.imdb_id,
            self.query,
            self.year,
            self.epyear,
            self.season,
            self.episode,
        )

        # Avoid resetting the same item
        if self.previous_item and self.current_item == self.previous_item:
            return

        # Update scrobbler
        self.scrobbler_stop()
        external = self.get_external_playerstrings()
        self.scrobbler = PlayerScrobbler(trakt_api=self.trakt_api, total_time=self.getTotalTime(), playerstring=external.get('trakt'))
        self.mdb_scrobbler = MDbListScrobbler(mdblist_api=self.mdblist_api, total_time=self.getTotalTime(), playerstring=external.get('mdblist'))
        self.simkl_scrobbler = SimklScrobbler(simkl_api=self.simkl_api, total_time=self.getTotalTime(), playerstring=external.get('simkl'))
        self.log_external_plan()

        # Clear properties and store the last cleared item
        self.previous_item = self.current_item
        self.clear_properties()

        # Extra info only for Movies and Episodes. Exit early and only update monitor art for other types. TODO: Maybe get extra PVR info?
        if self.dbtype not in ('movie', 'episode', ):
            self.update_artwork()
            return

        # Update artwork details
        self.set_iter_properties(self.player_item.get_artwork(), SETMAIN_ARTWORK)

        # Update our artwork manipulation cropped logo
        self.update_artwork()

        # Update ratings _details
        self.set_iter_properties(self.player_item.get_ratings(), SETPROP_RATINGS)

        # Update our properties
        self.set_properties(self.details)

        # Start Trakt trakt_scrobbling
        self.scrobbler_start()
