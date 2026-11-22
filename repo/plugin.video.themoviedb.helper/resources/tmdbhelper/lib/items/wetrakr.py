from tmdbhelper.lib.files.ftools import cached_property


class WeTrakrPlayData:
    """ Watched marks and resume points from the local copy of the WeTrakr account (api/wetrakr/sync), for the lists TMDb Helper builds.
    Modelled on SimklPlayData; WeTrakr numbers by TMDb, so its episode rows need no numbering check. """

    def __init__(self, watchedindicators=False, pauseplayprogress=False):
        self._watchedindicators = watchedindicators
        self._pauseplayprogress = pauseplayprogress

    def is_sync(func):
        def wrapper(self, *args, **kwargs):
            if not self.wetrakr_syncdata:
                return
            return func(self, *args, **kwargs)
        return wrapper

    @cached_property
    def wetrakr_api(self):
        from tmdbhelper.lib.api.wetrakr.api import WeTrakrAPI
        return WeTrakrAPI()

    @cached_property
    def wetrakr_syncdata(self):
        from tmdbhelper.lib.addon.plugin import get_setting
        if not (self._watchedindicators or self._pauseplayprogress):
            return None
        if not get_setting('service_wetrakr'):
            return None
        if not self.wetrakr_api.is_authorized:
            return None
        return self.wetrakr_api.wetrakr_syncdata

    @is_sync
    def pre_sync(self, info=None, tmdb_type=None, tmdb_id=None, season=None, **kwargs):
        if self._watchedindicators:
            if tmdb_type in ('movie', 'tv', 'season', 'both',) or info in ('trakt_userlist', 'trakt_calendar'):
                self.wetrakr_syncdata.sync('show', ('plays', 'watched_episodes', 'rating',))
        if self._pauseplayprogress:
            if tmdb_type in ('movie', 'tv', 'season', 'both',) or info in ('trakt_userlist', 'trakt_calendar'):
                self.wetrakr_syncdata.sync('show', ('paused_progress',))

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
    def get_playcount(self, li, own=False):
        """ The watched count of the item from the WeTrakr data. An episode's own WeTrakr mark counts whatever the other services show (7.5.11; before, it was
        skipped while Trakt / MDBList indicators were on): the lists take the highest count of all services, so a mark made from the WeTrakr menu shows a tick.
        `own` is kept for the callers that ask for WeTrakr's own data (Hide watched in a list opened from a WeTrakr In-Progress row); it no longer changes the answer. """
        if not self._watchedindicators:
            return

        mediatype = li.infolabels.get('mediatype')

        if mediatype == 'movie':
            return self.wetrakr_syncdata.get_movie_playcount(tmdb_id=li.unique_ids.get('tmdb')) or 0

        if mediatype == 'tvshow':
            air_count = int(li.infolabels.get('episode') or 0)
            watched = self.wetrakr_syncdata.get_episode_watchedcount(
                tmdb_id=li.unique_ids.get('tvshow.tmdb') or li.unique_ids.get('tmdb')) or 0
            return min(watched, air_count) if air_count else watched

        if mediatype == 'season':
            air_count = int(li.infolabels.get('episode') or 0)
            watched = self.wetrakr_syncdata.get_episode_watchedcount(
                tmdb_id=li.unique_ids.get('tvshow.tmdb'),
                season=li.infolabels.get('season')) or 0
            return min(watched, air_count) if air_count else watched

        if mediatype == 'episode':
            return self.wetrakr_syncdata.get_value(
                'tv', li.unique_ids.get('tvshow.tmdb'),
                season=li.infolabels.get('season'),
                episode=li.infolabels.get('episode'),
                key='plays') or 0

    @is_sync
    def set_playprogress(self, li):
        if not self._pauseplayprogress:
            return
        if li.infolabels.get('mediatype') not in ('movie', 'episode'):
            return
        duration = li.infolabels.get('duration')
        if not duration:
            return
        if li.infoproperties.get('ResumeTime'):
            return  # another service already set a resume point

        if li.infolabels.get('mediatype') == 'movie':
            progress = self.wetrakr_syncdata.get_value('movie', li.unique_ids.get('tmdb'), key='paused_progress')
        else:
            progress = self.wetrakr_syncdata.get_value(
                'tv', li.unique_ids.get('tvshow.tmdb'),
                season=li.infolabels.get('season'),
                episode=li.infolabels.get('episode'),
                key='paused_progress')

        try:
            progress = int(progress)
        except (TypeError, ValueError):
            return
        if progress < 4 or progress > 96:
            return

        li.infoproperties['ResumeTime'] = int(duration * progress // 100)
        li.infoproperties['TotalTime'] = int(duration)
