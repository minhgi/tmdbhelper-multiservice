from tmdbhelper.lib.files.ftools import cached_property
from tmdbhelper.lib.addon.tmdate import set_timestamp, get_timestamp
from tmdbhelper.lib.api.wetrakr.sync.activity import WeTrakrLastActivities, NO_ACTIVITY
from tmdbhelper.lib.api.wetrakr.sync.itemdata import WeTrakrSyncItem
from tmdbhelper.lib.files.locker import mutexlock


OVERLAP = 1  # seconds an incremental read starts before the saved mark: an entry read twice is harmless (every write is idempotent), a gap is not
JOURNAL_MARK = 'watched_jmark'  # activity row (item type 'all'): the journal's `visible_until` of the last incremental read, or the activity stamp a full read started from


def shift_iso(value, seconds):
    """ An ISO 8601 stamp of WeTrakr ('2026-11-23T00:54:53.173Z') moved by some seconds, in the same form; None when it cannot be read. (No strptime: Kodi's
    interpreter fails to import its module from a thread.) """
    from datetime import datetime, timedelta
    try:
        text = f'{value}'.strip()
        millis = int((text[20:23] if text[19:20] == '.' else '0').ljust(3, '0'))
        moment = datetime(int(text[0:4]), int(text[5:7]), int(text[8:10]), int(text[11:13]), int(text[14:16]), int(text[17:19]), millis * 1000)
        moment += timedelta(seconds=seconds)
    except (ValueError, TypeError):
        return None
    return f'{moment:%Y-%m-%dT%H:%M:%S}.{moment.microsecond // 1000:03d}Z'


def timerlock(func):
    """ A second sync of the same kind within a few seconds (several widgets building at once) is skipped """
    def wrapper(self, *args, **kwargs):
        interval = 3
        propname = f'wetrakr.syncdecorators.timerlock.{self.method}'
        if get_timestamp(self.window.get_property(propname) or 0, set_int=True):
            return
        self.window.get_property(propname, set_timestamp(interval, set_int=True))
        return func(self, *args, **kwargs)
    return wrapper


def progress_bg(func):
    """ 7.5.25: the same background progress bar the Trakt, MDBList and Simkl syncs show (setting "Sync notifications"; never during video playback) """
    def wrapper(self, *args, **kwargs):
        from tmdbhelper.lib.addon.dialog import DialogProgressSyncBG
        self.dialog_progress_bg = DialogProgressSyncBG()
        self.dialog_progress_bg.heading = f'WeTrakr Syncing {self.item_type} {self.method}'
        self.dialog_progress_bg.create()
        try:
            return func(self, *args, **kwargs)
        finally:
            self.dialog_progress_bg.close()
    return wrapper


class WeTrakrDataType:
    """ One kind of synced data. The stored stamp says which change of the account the stored rows were read at; the data is refetched
    (every page, all or nothing) when WeTrakr reports a newer one. The stamp is read BEFORE the fetch, so a change made while the fetch
    runs triggers another sync instead of being skipped. """

    lock_name = 'sync_wetrakr'
    keys = ()
    method = None
    item_types = ()  # item types whose columns are cleared before a rewrite

    def __init__(self, class_instance_syncdata, item_type):
        self._class_instance_syncdata = class_instance_syncdata
        self._item_type = item_type
        self._forced = False  # a forced sync (the tools list) always reads the lists in full
        self._stamp_cap = None  # an incremental read that could not see the newest changes yet saves no stamp newer than what it saw

    @property
    def mutex_lockname(self):
        return f'{self.cache._db_file}.{self.lock_name}.{self.method}.lockfile'

    @property
    def cache(self):
        return self._class_instance_syncdata.cache

    @property
    def window(self):
        return self._class_instance_syncdata.window

    @property
    def item_type(self):
        return self._item_type

    @property
    def wetrakr_api(self):
        return self._class_instance_syncdata._class_instance_wetrakr_api

    @cached_property
    def last_activities(self):
        return WeTrakrLastActivities(self._class_instance_syncdata)

    def current_stamp(self):
        raise NotImplementedError

    @property
    def is_expired(self):
        stored = self.cache.get_activity(self.item_type, self.method)
        if not stored:
            return True
        current = self.current_stamp()
        if current is None:
            return False  # WeTrakr could not be read: keep what is stored
        return current > stored

    def sync_func(self):
        """ The data read from WeTrakr, or None when any request failed (nothing is then changed) """
        raise NotImplementedError

    def clear_timerlock(self):
        self.window.get_property(f'wetrakr.syncdecorators.timerlock.{self.method}', clear_property=True)

    def sync_data(self):
        from tmdbhelper.lib.api.wetrakr.sync.itemdata import WeTrakrSyncItem
        meta = self.sync_func()
        if meta is None:
            return None
        item = WeTrakrSyncItem(self.method, meta, self.keys)
        data = item.data
        for item_type in self.item_types:
            self.cache.del_column_values(keys=item.base_table_keys, item_type=item_type)
        if data:
            self.cache.set_many_values(keys=item.table_keys, data=data)
        return True

    @mutexlock
    def sync(self, forced=False):
        self._forced = forced
        if forced:
            self.clear_timerlock()
        elif not self.is_expired:
            return
        stamp = self.current_stamp() or NO_ACTIVITY  # before the fetch (see the class docstring)
        if self.timerlocked_sync() is not None:
            if self._stamp_cap and self._stamp_cap < stamp:
                stamp = self._stamp_cap  # the change that moved the stamp is not in the journal yet: the next sync must read it
            self.cache.set_activity(self.item_type, self.method, stamp)

    @timerlock
    @progress_bg
    def timerlocked_sync(self):
        self.dialog_progress_bg.update(20, message='Refreshing Data')
        return self.sync_data()


class WeTrakrSyncWatched(WeTrakrDataType):
    """ Watched marks: GET /sync/tracking/watched/movies and /episodes (compact, every page). The show and season counts are worked out from the
    episode rows, so they agree with the marks shown on the episodes.

    Incremental read (7.5.20): once a full read has saved a mark, a change moves only what changed. The journal (GET /sync/journal, category watched)
    says WHICH titles changed since the mark and the lists' own from_date returns their NEW state: a title still watched comes back with its new play
    count, one that lost its last play does not come back, so a title the journal reports as removed and the list does not return is cleared. The journal
    shows only what is a few seconds old, so its `visible_until` (never the clock or the activity stamp) is the next mark, and the activity stamp saved is
    never newer than it. Anything not sure (no mark, mark too old, a long journal, a request that failed, an episode of a show the database does not know)
    falls back to the full read, which also saves the mark. """

    keys = ('plays', 'last_watched_at', 'watched_episodes',)
    method = 'watched'
    item_types = ('movie', 'show', 'season', 'episode',)

    def __init__(self, class_instance_syncdata, item_type):
        super().__init__(class_instance_syncdata, 'all')

    def current_stamp(self):
        return self.last_activities.watched_stamp()

    def sync_data(self):
        if not self._forced and self._sync_incremental():
            return True
        stamp = self.current_stamp() or NO_ACTIVITY  # the stamp this read started from (read before it): the journal mark of a full read
        done = super().sync_data()
        if done:
            self.cache.set_activity(self.item_type, JOURNAL_MARK, stamp)
        return done

    @staticmethod
    def _journal_plan(entries):
        """ What the journal entries say: {'movies': bool, 'episodes': bool, 'removed_movies': {tmdb}, 'removed_episodes': [(show wt id, season, number)]},
        or None when something cannot be placed (a removal with no ids, a removed season / show no episode entry explains): the lists are then read in full """
        plan = {'movies': False, 'episodes': False, 'removed_movies': set(), 'removed_episodes': []}
        bulk = []
        for entry in entries:
            kind, removed = entry.get('type'), entry.get('status') == 'removed'
            if kind == 'movie':
                plan['movies'] = True
                if removed:
                    tmdb_id = WeTrakrSyncItem._int((entry.get('ids') or {}).get('tmdb'))
                    if not tmdb_id:
                        return None
                    plan['removed_movies'].add(tmdb_id)
            elif kind == 'episode':
                plan['episodes'] = True
                if removed:
                    media, season, number = (WeTrakrSyncItem._int(entry.get(k)) for k in ('media_id', 'season_number', 'number'))
                    if media is None or season is None or number is None:
                        return None
                    plan['removed_episodes'].append((media, season, number))
            elif kind in ('season', 'show'):
                plan['episodes'] = True
                if removed:
                    bulk.append(WeTrakrSyncItem._int(entry.get('media_id') if kind == 'season' else entry.get('id')))
        covered = {media for media, _, _ in plan['removed_episodes']}
        if any(media is None or media not in covered for media in bulk):
            return None  # a season / show was unwatched and its episodes are not in the journal: do not guess
        return plan

    def _sync_incremental(self):
        """ True when the stored data was brought up to date from the journal; False when the lists must be read in full (nothing is changed then) """
        from tmdbhelper.lib.addon.logger import kodi_log
        mark = self.cache.get_activity(self.item_type, JOURNAL_MARK)
        since = shift_iso(mark, -OVERLAP) if mark else None
        if not since:
            kodi_log('WeTrakr Sync: watched - no journal mark yet, reading the lists in full', 1)
            return False
        # one lower bound for the journal and both lists, the journal FIRST: its upper bound (until) is then never newer than what the lists show
        state, entries, until = self.wetrakr_api.get_journal(since, 'watched')
        if state != 'ok':
            kodi_log(f'WeTrakr Sync: watched - journal not usable ({state}), reading the lists in full', 1)
            return False
        plan = self._journal_plan(entries)
        if plan is None:
            kodi_log('WeTrakr Sync: watched - the journal has a change that cannot be placed, reading the lists in full', 1)
            return False
        movie_rows, episode_rows = [], []
        if plan['movies']:
            ok, movie_rows = self.wetrakr_api.get_list('/sync/tracking/watched/movies', {'from_date': since})
            if not ok:
                return False
        if plan['episodes']:
            ok, episode_rows = self.wetrakr_api.get_list('/sync/tracking/watched/episodes', {'from_date': since})
            if not ok:
                return False
        if not self._apply_delta(plan, movie_rows, episode_rows):
            kodi_log('WeTrakr Sync: watched - the changes could not be applied, reading the lists in full', 1)
            return False
        self.cache.set_activity(self.item_type, JOURNAL_MARK, until)
        self._stamp_cap = until
        kodi_log(f'WeTrakr Sync: watched - incremental, {len(entries)} journal entries, {len(movie_rows)} movie rows, {len(episode_rows)} episode rows', 1)
        return True

    def _apply_delta(self, plan, movie_rows, episode_rows):
        """ Write the changed rows, clear the titles that lost their last play, recount the seasons and shows they belong to. False (and the caller reads
        everything again, which repairs any half-done write) when a removed episode cannot be placed or a write failed. """
        item = WeTrakrSyncItem('watched', {'movies': movie_rows, 'episodes': episode_rows}, self.keys)
        rows, _ = item.delta_watched()
        shows = {}  # WeTrakr show id -> TMDb id, from the rows just read, then from the stored ones
        for row in episode_rows:
            wt_id, tmdb_id = item._show_wt(row), item._show_tmdb(row)
            if wt_id and tmdb_id:
                shows[wt_id] = tmdb_id
        clear, seasons_seen = [], set()
        for tmdb_id in plan['removed_movies']:
            if f'movie.{tmdb_id}' not in rows:
                clear.append(f'movie.{tmdb_id}')
        for media, season, number in plan['removed_episodes']:
            tmdb_id = shows.get(media) or self.cache.show_tmdb_of(media)
            if not tmdb_id:
                return False
            shows[media] = tmdb_id
            seasons_seen.add((tmdb_id, season))
            if f'tv.{tmdb_id}.{season}.{number}' not in rows:
                clear.append(f'tv.{tmdb_id}.{season}.{number}')
        for key in rows:
            if key.startswith('tv.') and key.count('.') == 3:
                _, tmdb_id, season, _ = key.split('.')
                seasons_seen.add((int(tmdb_id), int(season)))
        wt_of = {tmdb_id: media for media, tmdb_id in shows.items()}
        if rows and self.cache.set_many_values(keys=item.table_keys, data=rows) is None:
            return False
        if not self.cache.clear_values(clear, self.keys):
            return False
        counts = self.cache.watched_counts({tmdb_id for tmdb_id, _ in seasons_seen})
        if counts is None:
            return False
        season_counts, show_counts = counts
        data, empty = {}, []
        for tmdb_id, season in sorted(seasons_seen):
            count = season_counts.get((tmdb_id, season), 0)
            if count:
                data[f'tv.{tmdb_id}.{season}'] = item._row([None, None, count], 'season', tmdb_id, season, wt_id=wt_of.get(tmdb_id))
            else:
                empty.append(f'tv.{tmdb_id}.{season}')
        for tmdb_id in sorted({tmdb_id for tmdb_id, _ in seasons_seen}):
            count = show_counts.get(tmdb_id, 0)
            if count:
                data[f'tv.{tmdb_id}'] = item._row([None, None, count], 'show', tmdb_id, wt_id=wt_of.get(tmdb_id))
            else:
                empty.append(f'tv.{tmdb_id}')
        if data and self.cache.set_many_values(keys=item.table_keys, data=data) is None:
            return False
        return bool(self.cache.clear_values(empty, self.keys))

    def sync_func(self):
        from tmdbhelper.lib.addon.logger import kodi_log
        movies_ok, movies = self.wetrakr_api.get_list('/sync/tracking/watched/movies')
        episodes_ok, episodes = self.wetrakr_api.get_list('/sync/tracking/watched/episodes') if movies_ok else (False, [])
        if not (movies_ok and episodes_ok):
            kodi_log('WeTrakr Sync: watched - a request failed, keeping local data', 1)
            return None
        kodi_log(f'WeTrakr Sync: watched - {len(movies)} movies, {len(episodes)} episodes', 1)
        return {'movies': movies, 'episodes': episodes}


class WeTrakrSyncPaused(WeTrakrDataType):
    """ Resume points: the paused rows of GET /sync/tracking/playing/movies and /episodes. Every movie and episode row is rewritten from the read, so a
    pause that was finished (or deleted) on WeTrakr disappears here. """

    keys = ('paused_progress', 'paused_at',)
    method = 'playback'
    item_types = ('movie', 'episode',)

    def __init__(self, class_instance_syncdata, item_type):
        super().__init__(class_instance_syncdata, 'all')

    def current_stamp(self):
        return self.last_activities.playback_stamp()

    def sync_func(self):
        from tmdbhelper.lib.addon.logger import kodi_log
        movies_ok, movies = self.wetrakr_api.get_list('/sync/tracking/playing/movies')
        episodes_ok, episodes = self.wetrakr_api.get_list('/sync/tracking/playing/episodes') if movies_ok else (False, [])
        if not (movies_ok and episodes_ok):
            kodi_log('WeTrakr Sync: playback - a request failed, keeping local data', 1)
            return None
        return {'movies': movies, 'episodes': episodes}


class WeTrakrSyncRatings(WeTrakrDataType):
    """ The account's own ratings: GET /sync/ratings/all (compact, every page) holds movies, shows, seasons and episodes in one list. Every rating
    row is rewritten from the read, so a rating removed on WeTrakr disappears here. The ratings stamp moves on a rating added, changed or removed
    and on nothing else, so a play or a pause never costs a read of the ratings. """

    keys = ('rating', 'rated_at',)
    method = 'ratings'
    item_types = ('movie', 'show', 'season', 'episode',)

    def __init__(self, class_instance_syncdata, item_type):
        super().__init__(class_instance_syncdata, 'all')

    def current_stamp(self):
        return self.last_activities.ratings_stamp()

    def sync_data(self):
        """ Incremental (7.5.20): GET /sync/ratings/all?from_date= answers the ratings changed since the stored stamp, a removed one with status
        'removed' and no rating, so no journal is needed. The first read (no stored stamp) and a forced sync read the whole list. """
        from tmdbhelper.lib.addon.logger import kodi_log
        stored = None if self._forced else self.cache.get_activity(self.item_type, self.method)
        since = shift_iso(stored, -OVERLAP) if stored else None
        if not since:
            return super().sync_data()
        ok, rows = self.wetrakr_api.get_list('/sync/ratings/all', {'from_date': since})
        if not ok:
            kodi_log('WeTrakr Sync: ratings - a request failed, keeping local data', 1)
            return None
        item = WeTrakrSyncItem(self.method, {'ratings': rows}, self.keys)
        data, removed = item.delta_ratings()
        if data and self.cache.set_many_values(keys=item.table_keys, data=data) is None:
            return None
        if not self.cache.clear_values(removed, self.keys):
            return None
        kodi_log(f'WeTrakr Sync: ratings - incremental, {len(data)} changed, {len(removed)} removed', 1)
        return True

    def sync_func(self):
        from tmdbhelper.lib.addon.logger import kodi_log
        ok, rows = self.wetrakr_api.get_list('/sync/ratings/all')
        if not ok:
            kodi_log('WeTrakr Sync: ratings - a request failed, keeping local data', 1)
            return None
        kodi_log(f'WeTrakr Sync: ratings - {len(rows)} rated', 1)
        return {'ratings': rows}
