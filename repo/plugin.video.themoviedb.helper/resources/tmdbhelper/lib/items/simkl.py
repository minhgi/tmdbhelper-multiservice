from tmdbhelper.lib.files.ftools import cached_property


class SimklPlayData:

    def __init__(self, watchedindicators=False, pauseplayprogress=False):
        self._watchedindicators = watchedindicators
        self._pauseplayprogress = pauseplayprogress
        # {(show tmdb id, season, episode): percent} that Simkl itself holds, set by the Simkl In-Progress Episodes list for its own rows
        self.server_progress = {}

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
            if tmdb_type in ('tv', 'season', 'both',) or info in ('trakt_userlist', 'trakt_calendar'):
                # the episode resume points come with the same read (7.0.109); started here, in the list's background thread, so the
                # first episode of the list does not wait for it
                self.simkl_syncdata.sync('show', ('paused_progress',))

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

        if li.infolabels.get('mediatype') == 'episode':
            # The episode mark is a local row in TMDb numbering: written when an episode is finished (scrobble) or set as
            # watched here, and (7.0.97) by the watched sync for the episodes Simkl lists for a regular show in progress.
            # Anime carries only a count on Simkl, so its episodes are marked by the first two only; the aired episodes of a regular show Simkl lists as completed with all aired episodes watched count as watched (no mark of their own needed).
            # It is only used when Trakt and MDBList show no watched marks: the lists take the highest count of all services and an episode unwatched
            # from the Trakt or MDBList menu is not mirrored to Simkl, so this mark could not be cleared from there.
            from tmdbhelper.lib.addon.plugin import get_setting
            if any(get_setting(s) and get_setting(i) for s, i in (
                    ('service_trakt', 'trakt_watchedindicators'), ('service_mdblist', 'mdblist_watchedindicators'))):
                return
            mark = self.simkl_syncdata.get_value(
                'tv', li.unique_ids.get('tvshow.tmdb'),
                season=li.infolabels.get('season'),
                episode=li.infolabels.get('episode'),
                key='plays')
            if mark is not None:
                return mark or 0  # a mark of its own, also an explicit 0 (set as unwatched here), always wins
            return self._episode_of_completed_show(li)

    def _episode_of_completed_show(self, li):
        """1 for an aired, regular (not season 0) episode with no mark of its own whose show Simkl lists as completed with every aired
        episode watched (the show row's plays flag, see SimklSyncWatched._is_completed_show): Simkl sends such a show as a count only.
        An episode with no air date counts as not watched."""
        try:
            if int(li.infolabels.get('season')) < 1:
                return 0
        except (TypeError, ValueError):
            return 0
        aired = str(li.infolabels.get('premiered') or '')[:10]
        if not aired:
            return 0
        from datetime import date
        if aired > date.today().isoformat():
            return 0
        return 1 if self.simkl_syncdata.get_value('tv', li.unique_ids.get('tvshow.tmdb'), key='plays') else 0

    @is_sync
    def set_playprogress(self, li):
        from tmdbhelper.lib.addon.plugin import get_setting
        if not get_setting('simkl_playprogress'):
            return
        if li.infolabels.get('mediatype') not in ('movie', 'episode'):
            return
        duration = li.infolabels.get('duration')
        if not duration:
            return

        if self.server_progress and li.infolabels.get('mediatype') == 'episode':
            # a row of Simkl's own In-Progress Episodes list: Simkl's percentage for that episode wins over the local copy and over
            # the resume point Trakt / MDBList set, so the list shows what Simkl holds (GitHub issue #13)
            try:
                key = (int(li.unique_ids.get('tvshow.tmdb')), int(li.infolabels.get('season')), int(li.infolabels.get('episode')))
                percent = int(round(float(self.server_progress.get(key))))
            except (TypeError, ValueError):
                percent = None
            if percent is not None and 4 <= percent <= 96:
                li.infoproperties['ResumeTime'] = int(duration * percent // 100)
                li.infoproperties['TotalTime'] = int(duration)
                return

        if li.infoproperties.get('ResumeTime'):
            return  # Trakt or MDBList already set a resume point

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
