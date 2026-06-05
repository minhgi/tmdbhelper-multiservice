from tmdbhelper.lib.files.ftools import cached_property


class MDbListPlayData:

    def __init__(self, watchedindicators=False, pauseplayprogress=False):
        self._watchedindicators = watchedindicators
        self._pauseplayprogress = pauseplayprogress

    def is_sync(func):
        def wrapper(self, *args, **kwargs):
            if not self.mdblist_syncdata:
                return
            return func(self, *args, **kwargs)
        return wrapper

    @cached_property
    def mdblist_api(self):
        from tmdbhelper.lib.api.mdblist.api import MDbList
        api = MDbList()
        return api

    @cached_property
    def mdblist_syncdata(self):
        from tmdbhelper.lib.addon.plugin import get_setting
        if not get_setting('service_mdblist'):
            return None
        # Allow sync via OAuth token OR personal API key (both authenticate /sync/* endpoints)
        if not self.mdblist_api.user_token.value and not self.mdblist_api.api_key:
            return None
        return self.mdblist_api.mdblist_syncdata

    @is_sync
    def pre_sync(self, info=None, tmdb_type=None, tmdb_id=None, season=None, **kwargs):
        if tmdb_type in ('movie', 'both',) or info in ('trakt_userlist',):
            if self._watchedindicators:
                self.mdblist_syncdata.sync('movie', ('plays',))
            if self._pauseplayprogress:
                self.mdblist_syncdata.sync('movie', ('paused_progress',))

        if tmdb_type in ('tv', 'season', 'both',) or info in ('trakt_userlist', 'trakt_calendar'):
            if self._watchedindicators:
                self.mdblist_syncdata.sync('show', ('plays', 'watched_episodes',))
            if self._pauseplayprogress and tmdb_id is not None and season is not None:
                self.mdblist_syncdata.sync('show', ('paused_progress',))

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
            return self.mdblist_syncdata.get_movie_playcount(
                tmdb_id=li.unique_ids.get('tmdb')) or 0

        if li.infolabels.get('mediatype') == 'episode':
            return self.mdblist_syncdata.get_episode_playcount(
                tmdb_id=li.unique_ids.get('tvshow.tmdb'),
                season=li.infolabels.get('season'),
                episode=li.infolabels.get('episode')) or 0

        if li.infolabels.get('mediatype') == 'tvshow':
            air_count = int(li.infolabels.get('episode') or 0)
            watched = self.mdblist_syncdata.get_episode_watchedcount(
                tmdb_id=li.unique_ids.get('tvshow.tmdb') or li.unique_ids.get('tmdb')) or 0
            return min(watched, air_count) if air_count else watched

        if li.infolabels.get('mediatype') == 'season':
            air_count = int(li.infolabels.get('episode') or 0)
            watched = self.mdblist_syncdata.get_episode_watchedcount(
                tmdb_id=li.unique_ids.get('tvshow.tmdb'),
                season=li.infolabels.get('season')) or 0
            return min(watched, air_count) if air_count else watched

    @is_sync
    def set_playprogress(self, li):
        if not self._pauseplayprogress:
            return
        if li.infolabels.get('mediatype') not in ('movie', 'episode'):
            return
        # Only apply if Trakt has not already set a resume point
        if li.infoproperties.get('ResumeTime'):
            return
        duration = li.infolabels.get('duration')
        if not duration:
            return

        if li.infolabels.get('mediatype') == 'movie':
            progress = self.mdblist_syncdata.get_movie_playprogress(
                tmdb_id=li.unique_ids.get('tmdb'))
        else:
            progress = self.mdblist_syncdata.get_episode_playprogress(
                tmdb_id=li.unique_ids.get('tvshow.tmdb'),
                season=li.infolabels.get('season'),
                episode=li.infolabels.get('episode'))

        if not progress or progress < 4 or progress > 96:
            return

        li.infoproperties['ResumeTime'] = int(duration * progress // 100)
        li.infoproperties['TotalTime'] = int(duration)
