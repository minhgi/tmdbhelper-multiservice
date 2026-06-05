from tmdbhelper.lib.files.ftools import cached_property


class SimklPlayData:

    def __init__(self, watchedindicators=False, pauseplayprogress=False):
        self._watchedindicators = watchedindicators
        self._pauseplayprogress = pauseplayprogress

    def is_sync(func):
        def wrapper(self, *args, **kwargs):
            if not self.simkl_syncdata:
                return
            return func(self, *args, **kwargs)
        return wrapper

    @cached_property
    def simkl_api(self):
        from tmdbhelper.lib.api.simkl.api import SimklAPI
        return SimklAPI()

    @cached_property
    def simkl_syncdata(self):
        from tmdbhelper.lib.addon.plugin import get_setting
        if not get_setting('service_simkl'):
            return None
        if not self.simkl_api.user_token.value:
            return None
        return self.simkl_api.simkl_syncdata

    @is_sync
    def pre_sync(self, info=None, tmdb_type=None, tmdb_id=None, season=None, **kwargs):
        if self._watchedindicators:
            if tmdb_type in ('movie', 'both',) or info in ('trakt_userlist',):
                self.simkl_syncdata.sync('movie', ('plays',))
            if tmdb_type in ('tv', 'season', 'both',) or info in ('trakt_userlist', 'trakt_calendar'):
                self.simkl_syncdata.sync('show', ('plays', 'watched_episodes',))
        # Playback progress gated separately — user may want resume points
        # without watched indicators, or vice versa.
        if self._pauseplayprogress:
            if tmdb_type in ('movie', 'both',) or info in ('trakt_userlist',):
                self.simkl_syncdata.sync('movie', ('paused_progress',))

    @is_sync
    def pre_sync_start(self, **kwargs):
        from tmdbhelper.lib.addon.thread import SafeThread
        self._pre_sync = SafeThread(target=self.pre_sync, kwargs=kwargs)
        self._pre_sync.start()

    @is_sync
    def pre_sync_join(self):
        try:
            self._pre_sync.join()
        except AttributeError:
            return

    @is_sync
    def get_playcount(self, li):
        if not self._watchedindicators:
            return

        if li.infolabels.get('mediatype') == 'movie':
            return self.simkl_syncdata.get_movie_playcount(
                tmdb_id=li.unique_ids.get('tmdb')) or 0

        if li.infolabels.get('mediatype') == 'tvshow':
            air_count = int(li.infolabels.get('episode') or 0)
            watched = self.simkl_syncdata.get_episode_watchedcount(
                tmdb_id=li.unique_ids.get('tvshow.tmdb') or li.unique_ids.get('tmdb')) or 0
            return min(watched, air_count) if air_count else watched

        if li.infolabels.get('mediatype') == 'season':
            air_count = int(li.infolabels.get('episode') or 0)
            watched = self.simkl_syncdata.get_episode_watchedcount(
                tmdb_id=li.unique_ids.get('tvshow.tmdb'),
                season=li.infolabels.get('season')) or 0
            return min(watched, air_count) if air_count else watched

        # Episode-level deferred to Phase D (TVDB vs TMDb numbering)

    @is_sync
    def set_playprogress(self, li):
        from tmdbhelper.lib.addon.plugin import get_setting
        if not get_setting('simkl_playprogress'):
            return
        if li.infolabels.get('mediatype') not in ('movie', 'episode'):
            return
        if li.infoproperties.get('ResumeTime'):
            return  # Trakt or MDBList already set a resume point
        duration = li.infolabels.get('duration')
        if not duration:
            return

        if li.infolabels.get('mediatype') == 'movie':
            progress = self.simkl_syncdata.get_value(
                'movie', li.unique_ids.get('tmdb'), key='paused_progress')
        else:
            progress = self.simkl_syncdata.get_value(
                'tv', li.unique_ids.get('tvshow.tmdb'),
                season=li.infolabels.get('season'),
                episode=li.infolabels.get('episode'),
                key='paused_progress')

        if not progress or int(progress) < 4 or int(progress) > 96:
            return

        li.infoproperties['ResumeTime'] = int(duration * int(progress) // 100)
        li.infoproperties['TotalTime'] = int(duration)
