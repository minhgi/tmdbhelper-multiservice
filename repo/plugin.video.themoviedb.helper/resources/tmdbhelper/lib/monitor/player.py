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

    @property
    def _item_label(self):
        return f'{self.tmdb_type} {self.tmdb_id}' + (f' S{self.season}E{self.episode}' if self.tmdb_type == 'tv' else '')

    def _log_scrobble(self, action, response):
        """ One log line per post to a service: what was sent and whether it was accepted (a refused or dropped post is otherwise silent) """
        result = f'HTTP {response.status_code}' if response is not None else 'not accepted - no usable response (see the log lines before this)'
        kodi_log(f'Scrobble {self.service_name} {action} {self._item_label} at {self.progress:.1f}%: {result}', 1)

    def _nothing_played(self, action):
        """ True (and logged) while no playing time has been measured. Kodi reports a pause as playback starts, and a pause or stop
        sent then would put 0% over the progress the service already holds for the item (MDBList and Simkl accept it) """
        if self.current_time:
            return False
        kodi_log(f'Scrobble {self.service_name} {action} {self._item_label}: skipped - no playing time measured yet', 1)
        return True

    @is_trakt_authorized
    def _trakt_scrobble_post(self, method):
        if method != 'start' and self._nothing_played(method):
            return
        self.trakt_item['progress'] = self.progress
        response = self.trakt_api.get_api_request(
            f'https://api.trakt.tv/scrobble/{method}',
            postdata=self.trakt_item,
            headers=self.trakt_api.headers,
            method='json'
        )
        self._log_scrobble(method, response)

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
            kodi_log(f'{label}: not marked watched - the playing add-on already reported it', 1)
            return self._refresh_after_stop()  # the service has the viewing now, so lists and widgets still need to catch up
        action()
        kodi_log(f'{label}: watched mark sent', 1)
        PlayerScrobbler._own_stamps[own_key] = self._watched_stamp()
        if not self._refreshes_on_report:
            self._refresh_after_stop()

    _refreshes_on_report = False  # True when the watched post already reloads the lists itself (MDBList, Simkl)

    def refresh_after_stop(self):
        """ Bring lists and widgets up to date once the posts for a stop are done (in the background: Trakt's sync can download the whole watched list) """
        SafeThread(target=self._refresh_after_stop).start()

    def _refresh_after_stop(self):
        """ Trakt: drop the cached last activities and sync the item's watched and resume data now, then reload the visible list and the widgets.
        Syncing first matters: a list that renders before the sync reads the old data, and the widgets are only rebuilt on a reload """
        synced = 'skipped - not signed in'
        try:
            if boolean(get_property('TraktIsAuth')) and self.trakt_api.authorization:
                from tmdbhelper.lib.api.trakt.sync.datasync import SyncData
                syncdata = SyncData(self.trakt_api)
                syncdata.reset_lastactivities()
                syncdata.sync('movie' if self.tmdb_type == 'movie' else 'show', ('plays', 'last_watched_at', 'playback_progress'))
                synced = 'done'
        except Exception as exc:
            synced = f'failed - {type(exc).__name__}: {exc}'
        self._reload_lists(f'Trakt sync {synced}')

    def _reload_lists(self, note=''):
        from tmdbhelper.lib.script.method.kodi_utils import reload_widgets
        from xbmc import executebuiltin
        executebuiltin('Container.Refresh')
        reload_widgets()
        kodi_log(f'Post-stop refresh {self._item_label}: {note + ", " if note else ""}list and widgets reloaded', 1)

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
                if self._watched_ready:
                    self.refresh_after_stop()
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
        get_property(LASTACTIVITIES_DATA, clear_property=True)
        get_stats()  # the list and widget reload follows once every service has posted (PlayerMonitor.scrobbler_stop)

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
    _refreshes_on_report = True  # _mark_watched reloads the lists itself

    def _refresh_after_stop(self):
        self._reload_lists()  # MDBList's lists read the server live and the resume row was written locally by the pause post

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
        paused_id = None
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
        except Exception:
            pass
        # Clear the server-side entries so the item leaves /sync/playback (In-Progress list); runs even if the local clear failed
        try:
            self._clear_server_progress(paused_id)
        except Exception as exc:
            kodi_log(f'Scrobble MDBList clear {self._item_label}: failed - {type(exc).__name__}: {exc}', 1)

    def _clear_server_progress(self, paused_id=None):
        """ MDBList keeps an item's /sync/playback entry when the item is marked watched, and its pause reply carries no id.
        The local paused_id only arrives with a playback sync, so an entry saved by a pause in this session has none: the entries
        are looked up live by TMDb ids (as Reset resume position does) and cleared together with the stored id """
        from tmdbhelper.lib.script.sync.mdblist_sync import ItemMDbListClearProgress
        found = ItemMDbListClearProgress(self.tmdb_type, self.tmdb_id, self.season or None, self.episode or None).get_playback_ids()
        ids = list(dict.fromkeys([*(found or []), *([paused_id] if paused_id else [])]))
        url = self.mdblist_api.get_request_url('scrobble', 'clear')
        cleared = sum(
            self.mdblist_api.get_api_request(url, postdata={'id': i}, method='json', headers=self.mdblist_api.auth_headers) is not None
            for i in ids)
        kodi_log(f'Scrobble MDBList clear {self._item_label}: {cleared} of {len(ids)} resume entries cleared'
                 + ('' if found is not None else ' (the server list could not be read)'), 1)

    def _post_pause(self):
        if not get_setting('service_mdblist'):
            return
        if not get_setting('mdblist_playprogress'):
            return
        if not self._is_authorized:
            return
        payload = self._pause_payload()
        if not payload or self._nothing_played('pause'):
            return
        url = self.mdblist_api.get_request_url('scrobble', 'pause')
        response = self.mdblist_api.get_api_request(url, postdata=payload, method='json', headers=self.mdblist_api.auth_headers)
        if response is None:
            kodi_log(f'MDBList scrobble/pause was not accepted: {payload}', 1)
        self._log_scrobble('pause', response)
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
        response = self.mdblist_api.get_api_request(url, postdata=payload, method='json', headers=self.mdblist_api.auth_headers)
        self._log_scrobble('watched', response)
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
            if self.external and self._resume_ready:
                self.refresh_after_stop()
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
    Episode numbers use TMDb numbering, which Simkl places by TVDB season — wrong or refused for many anime. An anime episode that
    resolves to its entry (cour) is therefore sent as that entry first: AniList / MAL ids and the number inside the entry
    (_anime_entry); the TMDb numbers are the fallback when Simkl does not take it, and the only request for other titles.
    """

    BASE_URL = 'https://api.simkl.com'

    service_name = 'Simkl'
    _anime_target = None  # (ids, number inside the entry) / False once resolved; see _anime_entry
    _refreshes_on_report = True  # _mark_watched reloads the lists itself

    def _refresh_after_stop(self):
        self._reload_lists()  # Simkl's lists read the server live and the resume row was written locally by the pause post

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
        if not payload or self._nothing_played('pause'):
            return
        url = f'{self.BASE_URL}/scrobble/pause?client_id={self.simkl_api.client_id}'
        response = None
        target = self._anime_entry()
        if target:  # an anime episode that resolves goes out as its entry first (the TMDb numbers can land in the wrong cour with a success answer)
            response = self.simkl_api.get_api_request(
                url, postdata=self._anime_pause_payload(target), method='json', headers=self.simkl_api.auth_headers)
            kodi_log(f'Scrobble Simkl pause {self._item_label}: sent as anime {target[0]} episode {target[1]}: '
                     f'{"accepted" if response is not None else "not accepted, sending the TMDb numbers"}', 1)
        if response is None:
            response = self.simkl_api.get_api_request(
                url, postdata=payload, method='json', headers=self.simkl_api.auth_headers)
        paused_id = None
        if response is not None:
            try:
                paused_id = response.json().get('id')
            except Exception:
                pass
        self._log_scrobble('pause', response)
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
        target = self._anime_entry()
        response, matched = None, False
        if target:  # an anime episode that resolves goes out as its entry first (the TMDb numbers can land in the wrong cour with a success answer)
            response = self.simkl_api.get_api_request(
                url, postdata=self._anime_watched_payload(target), method='json', headers=self.simkl_api.auth_headers)
            matched = response is not None and not self._not_found(response)
            kodi_log(f'Scrobble Simkl watched {self._item_label}: sent as anime {target[0]} episode {target[1]}: '
                     f'{"accepted" if matched else "not accepted, sending the TMDb numbers"} - {self._response_text(response)}', 1)
        if not matched:  # not an anime that resolves, or Simkl did not take the entry: the TMDb numbers
            response = self.simkl_api.get_api_request(
                url, postdata=payload, method='json', headers=self.simkl_api.auth_headers)
            self._log_scrobble('watched', response)
            # a title that resolved to an entry does not count a refused mark (None) as marked; other titles keep the old rule
            matched = (response is not None and not self._not_found(response)) if target else not self._not_found(response)
        if not matched:
            response = self._retry_watched_by_other_ids(url, payload)
            matched = response is not None and not self._not_found(response)
        if matched:  # a mark Simkl did not take must not show as watched in the lists
            item.update_watched_in_db(True, force=True)
        self._clear_progress_in_db()
        item.refresh_containers(force=True)

    def _anime_entry(self):
        """({'anilist': id, 'mal': id}, episode number inside the entry) of the anime entry (cour) a TMDb episode belongs to, or
        None. Simkl keeps each cour as its own entry numbered from 1 and places TMDb-style season / episode numbers by TVDB
        coordinates, so a merged TMDb season (Apothecary Diaries S01E49 = episode 1 of its third cour) is not found that way;
        the AniList / MAL ids of the entry with the number inside it are the form Simkl documents for anime. The entry comes
        from the Fribb index with its offset confirmed by the episode's air date (api/anilist/episodes.entry_for_episode); the
        entry's Simkl id is not sent (Fribb's is wrong for some entries and Simkl tries it first). Resolved once per playback."""
        if self._anime_target is None:
            self._anime_target = False
            try:
                self._anime_target = self._resolve_anime_entry() or False
            except Exception as exc:
                kodi_log(f'Scrobble Simkl {self._item_label}: anime entry lookup failed - {type(exc).__name__}: {exc}', 1)
        return self._anime_target or None

    def _resolve_anime_entry(self):
        if self.tmdb_type != 'tv' or not (self.tmdb_id and self.season and self.episode):
            return None
        from tmdbhelper.lib.api.anilist import episodes, fribb
        if not fribb.tv_entries(self.tmdb_id):  # not an anime the offline index knows: no network call
            return None
        from tmdbhelper.lib.api.tmdb.api import TMDb
        season = TMDb().get_request_sc('tv', self.tmdb_id, 'season', self.season) or {}
        airdate = next((e.get('air_date') for e in season.get('episodes') or [] if e.get('episode_number') == self.episode), None)
        found = episodes.entry_for_episode(self.tmdb_id, self.season, self.episode, airdate)
        if not found:
            return None
        anilist_id, number = found
        ids = {'anilist': anilist_id}
        mal_id = next((r.get('m') for r in fribb.score_records(self.tmdb_id) or [] if r.get('a') == anilist_id and r.get('m')), None)
        if mal_id:
            ids['mal'] = mal_id
        return ids, number

    def _anime_pause_payload(self, target):
        ids, number = target
        return {'progress': self.progress, 'anime': {'ids': ids}, 'episode': {'number': number}}

    @staticmethod
    def _anime_watched_payload(target):
        ids, number = target
        return {'anime': [{'ids': ids, 'episodes': [{'number': number}]}]}

    @staticmethod
    def _response_text(response):
        """ Short text of a Simkl answer for the log (the shape of an anime answer is not documented in full) """
        if response is None:
            return 'no answer'
        try:
            from json import dumps
            return dumps(response.json())[:300]
        except Exception:
            return f'HTTP {getattr(response, "status_code", "?")}'

    @staticmethod
    def _not_found(response):
        """ True when Simkl answered but matched nothing: it replies HTTP 201 with 'added' at 0 and the item under 'not_found' """
        try:
            not_found = response.json().get('not_found') or {}
        except Exception:
            return False
        return any(not_found.get(k) for k in ('movies', 'shows', 'episodes', 'anime'))

    def _retry_watched_by_other_ids(self, url, payload):
        """ Simkl did not know the TMDb id: send the watched mark again with the item's IMDb and TVDB ids from TMDb """
        key = 'movies' if self.tmdb_type == 'movie' else 'shows'
        try:
            from tmdbhelper.lib.api.tmdb.api import TMDb
            external = (TMDb().get_request_sc(self.tmdb_type, self.tmdb_id, append_to_response='external_ids') or {}).get('external_ids') or {}
        except Exception as exc:
            external = {}
            kodi_log(f'Scrobble Simkl watched {self._item_label}: external ids could not be read - {exc}', 1)
        ids = {k: v for k, v in (('imdb', external.get('imdb_id')), ('tvdb', external.get('tvdb_id'))) if v}
        if not ids:
            kodi_log(f'Scrobble Simkl watched {self._item_label}: Simkl did not know the TMDb id and TMDb has no IMDb or TVDB id to try - not marked on Simkl', 1)
            return None
        retry = {key: [{**payload[key][0], 'ids': ids}]}
        response = self.simkl_api.get_api_request(url, postdata=retry, method='json', headers=self.simkl_api.auth_headers)
        found = response is not None and not self._not_found(response)
        kodi_log(f'Scrobble Simkl watched {self._item_label}: Simkl did not know the TMDb id, retried with {"/".join(ids)}: '
                 f'{"Simkl recognised it" if found else "still not recognised by Simkl"}', 1)
        return response

    def _watched_stamp(self):
        try:
            url = f'{self.BASE_URL}/sync/activities?client_id={self.simkl_api.client_id}'
            data = self.simkl_api.get_api_request(url, headers=self.simkl_api.auth_headers).json()
            if self.tmdb_type == 'movie':
                return data['movies']['completed'] or ''
            shows, anime = data['tv_shows'], data.get('anime') or {}   # an anime mark moves Simkl's anime section, not tv_shows
            return f"{shows['watching'] or ''}|{shows['completed'] or ''}|{anime.get('watching') or ''}|{anime.get('completed') or ''}"
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
            if self.external and self._resume_ready:
                self.refresh_after_stop()
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
            meta['UniqueID.tvshow.imdb'] = info_tag.getUniqueID('tvshow.imdb')
            meta['UniqueID.tmdb'] = info_tag.getUniqueID('tmdb')
            meta['UniqueID.imdb'] = info_tag.getUniqueID('imdb')

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
        scrobblers = [s for s in (self.scrobbler, self.mdb_scrobbler, self.simkl_scrobbler) if s]
        was_stopped = [s.stopped for s in scrobblers]
        self.scrobbler.stop(self.tmdb_type, self.tmdb_id)
        if self.mdb_scrobbler:
            self.mdb_scrobbler.stop(self.tmdb_type, self.tmdb_id)
        if self.simkl_scrobbler:
            self.simkl_scrobbler.stop(self.tmdb_type, self.tmdb_id)
        # One reload after all three posts, not one per service: a reload that runs before the last post renders the old data.
        # External playback reloads from its own scrobblers (they post later, in a background thread).
        if any(s.external for s in scrobblers) or not any(s.stopped and not before for s, before in zip(scrobblers, was_stopped)):
            return  # an inert scrobbler (no identity for its service) is not external, so every scrobbler is asked
        if any(s._resume_ready or s._watched_ready for s in scrobblers):  # nothing was posted to any service: nothing changed to show
            self.scrobbler.refresh_after_stop()

    def update_time(self):
        if not self.scrobbler:
            return
        self.scrobbler.update_time(self.tmdb_type, self.tmdb_id, self.getTime())
        if self.mdb_scrobbler:
            self.mdb_scrobbler.update_time(self.tmdb_type, self.tmdb_id, self.getTime())
        if self.simkl_scrobbler:
            self.simkl_scrobbler.update_time(self.tmdb_type, self.tmdb_id, self.getTime())

    def get_external_show_tmdb_id(self, meta):
        """ TMDb id of a playing episode's show from the show's IMDb id in the info tag, for add-ons that tag an episode with
        the IMDb id only (Elementum). The lookup is TMDb's cached /find by id, never a title search; an episode's own tt id has
        no tv result, so it yields nothing rather than a wrong show """
        for key in ('UniqueID.tvshow.imdb', 'UniqueID.imdb', 'IMDBNumber'):
            imdb_id = f'{meta.get(key) or ""}'
            if not imdb_id.startswith('tt'):
                continue
            try:
                tmdb_id = self.get_tmdb_id('tv', imdb_id=imdb_id)
            except Exception as exc:
                kodi_log(f'External playback: TMDb lookup for {imdb_id} failed - {exc}', 1)
                continue
            if f'{tmdb_id or ""}'.isdigit():
                return tmdb_id

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
            if not f'{tmdb_id or ""}'.isdigit() and (season or 0) > 0 and (episode or 0) > 0:
                tmdb_id = self.get_external_show_tmdb_id(meta)
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

    def log_scrobble_plan(self):
        """ Own playback (started by TMDb Helper's player): what each service will be sent. External playback logs its own line """
        scrobblers = (self.scrobbler, self.mdb_scrobbler, self.simkl_scrobbler)
        if self.scrobbler.external or not self.scrobbler.tmdb_id:
            return
        item = f'{self.scrobbler.tmdb_type} {self.scrobbler.tmdb_id}' + (
            f' S{self.scrobbler.season}E{self.scrobbler.episode}' if self.scrobbler.tmdb_type == 'tv' else '')
        if not self.scrobbler.total_time:
            return kodi_log(f'Scrobbling {item}: Kodi gave no duration for this video, so nothing will be reported to Trakt, MDBList or Simkl', 1)
        kodi_log(f'Scrobbling {item} ({self.scrobbler.total_time:.0f}s): ' + ', '.join(
            f'{s.service_name} {s.external_report()}' for s in scrobblers), 1)

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
        self.log_scrobble_plan()

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
