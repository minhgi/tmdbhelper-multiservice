from xbmc import Player
from json import loads
from jurialmunkey.parser import boolean
from jurialmunkey.window import get_property
from tmdbhelper.lib.monitor.images import ImageFunctions
from tmdbhelper.lib.monitor.common import CommonMonitorFunctions, SETPROP_RATINGS, SETMAIN_ARTWORK
from tmdbhelper.lib.addon.plugin import get_condvisibility, get_infolabel, get_setting
from tmdbhelper.lib.addon.thread import SafeThread


class PlayerScrobbler():
    def __init__(self, trakt_api, total_time):
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

    @is_scrobbling
    @is_trakt_authorized
    def trakt_scrobbling(self, method):
        if method not in ('start', 'pause', 'stop'):
            return
        self.trakt_item['progress'] = self.progress
        self.trakt_api.get_api_request(
            f'https://api.trakt.tv/scrobble/{method}',
            postdata=self.trakt_item,
            headers=self.trakt_api.headers,
            method='json'
        )

    @is_scrobbling
    def start(self, tmdb_type, tmdb_id):
        if not self.is_match(tmdb_type, tmdb_id):
            return self.stop(tmdb_type, tmdb_id)
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

    def __init__(self, mdblist_api, total_time):
        # Mirrors PlayerScrobbler.__init__ but stores mdblist_api instead of trakt_api.
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

    def _pause_payload(self):
        """Build /scrobble/pause payload (POV-confirmed format with 'ids' wrapper)."""
        if self.tmdb_type == 'movie':
            return {'movie': {'ids': {'tmdb': self.tmdb_id}}, 'progress': self.progress}
        if self.tmdb_type == 'tv' and self.season and self.episode:
            return {
                'show': {'ids': {'tmdb': self.tmdb_id},
                         'season': {'number': self.season, 'episode': {'number': self.episode}}},
                'progress': self.progress
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
        self.mdblist_api.get_api_request(url, postdata=payload, method='json', headers=self.mdblist_api.auth_headers)
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

    @PlayerScrobbler.is_scrobbling
    def start(self, tmdb_type, tmdb_id):
        if not self.is_match(tmdb_type, tmdb_id):
            return self.stop(tmdb_type, tmdb_id)
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
            self._mark_watched()
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

    def __init__(self, simkl_api, total_time):
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

    @PlayerScrobbler.is_scrobbling
    def start(self, tmdb_type, tmdb_id):
        if not self.is_match(tmdb_type, tmdb_id):
            return self.stop(tmdb_type, tmdb_id)
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
            self._mark_watched()
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
        self.scrobbler = PlayerScrobbler(trakt_api=self.trakt_api, total_time=self.getTotalTime())
        self.mdb_scrobbler = MDbListScrobbler(mdblist_api=self.mdblist_api, total_time=self.getTotalTime())
        self.simkl_scrobbler = SimklScrobbler(simkl_api=self.simkl_api, total_time=self.getTotalTime())

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
