from time import time
from tmdbhelper.lib.files.ftools import cached_property
from tmdbhelper.lib.addon.tmdate import set_timestamp, get_timestamp
from tmdbhelper.lib.api.trakt.sync.activity import SyncLastActivities
from tmdbhelper.lib.files.locker import mutexlock


def timerlock(func):
    def wrapper(self, *args, **kwargs):
        interval = 3
        propname = f'syncdecorators.timerlock.sync_data'
        propname = f'{propname}.{self.item_type}.{self.method}'
        if get_timestamp(self.window.get_property(propname) or 0, set_int=True):
            return
        self.window.get_property(propname, set_timestamp(interval, set_int=True))
        data = func(self, *args, **kwargs)
        return data
    return wrapper


def progress_bg(func):
    def wrapper(self, *args, **kwargs):
        from tmdbhelper.lib.addon.dialog import DialogProgressSyncBG
        self.dialog_progress_bg = DialogProgressSyncBG()
        self.dialog_progress_bg.heading = f'Trakt Syncing {self.item_type} {self.method}'
        self.dialog_progress_bg.create()
        data = func(self, *args, **kwargs)
        self.dialog_progress_bg.close()
        return data
    return wrapper


class DataType:
    sync_kwgs = {}
    lock_name = 'sync_trakt'
    key_prefix = None

    def __init__(self, class_instance_syncdata, item_type):
        self._class_instance_syncdata = class_instance_syncdata
        self._item_type = item_type

    @property
    def mutex_lockname(self):
        return f'{self.cache._db_file}.{self.lock_name}.{self.item_type}.{self.method}.lockfile'

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
    def get_response_json(self):
        return self._class_instance_syncdata.get_response_json

    max_pages = 100  # safety cap on a paginated read

    def get_response_sync(self, *args, **kwargs):
        """GET a Trakt sync endpoint. A JSON list that Trakt paginates (X-Pagination-Page-Count > 1)
        is read to completion and merged; before 6.0.94 only page 1 was read, so e.g. a 417-movie
        watched list arrived as 100 movies. Returns None when any page fails (or the page cap is hit)
        so sync_data() skips the clear-and-rewrite instead of storing a partial list. Objects (dicts)
        and single-page lists come back unchanged."""
        path = self.trakt_api.get_request_url(*args, **kwargs)
        data = self.trakt_api.get_api_request(path, headers=self.trakt_api.headers)
        if data is None:
            return
        try:
            result = data.json()
        except ValueError:
            return
        except AttributeError:
            return
        if not isinstance(result, list):
            return result
        try:
            page_count = int(data.headers.get('X-Pagination-Page-Count') or 1)
        except (AttributeError, TypeError, ValueError):
            page_count = 1
        if page_count > self.max_pages:
            return
        for page in range(2, page_count + 1):
            more = self.trakt_api.get_api_request(
                self.trakt_api.get_request_url(*args, page=page, **kwargs), headers=self.trakt_api.headers)
            if more is None:
                return
            try:
                more = more.json()
            except (ValueError, AttributeError):
                return
            if not isinstance(more, list):
                return
            result.extend(more)
        return result

    @property
    def trakt_api(self):
        return self._class_instance_syncdata._class_instance_trakt_api

    @cached_property
    def last_activities(self):
        return self.get_last_activities()

    def get_last_activities(self):
        return SyncLastActivities(self._class_instance_syncdata)

    paged_marker = 'paged_v1'  # lactivities entry per item_type/method: missing forces one full refetch (sync endpoints were read as page 1 only before 6.0.94)

    @property
    def paged_marker_key(self):
        return f'{self.method}.{self.paged_marker}'

    def store_last_activity(self):
        self.cache.set_activity(self.item_type, self.method, self.last_activities.json.get('all') or '2000-01-01T00:00:00.000Z')
        self.cache.set_activity(self.item_type, self.paged_marker_key, 'ok')

    def clear_timerlock(self):
        propname = f'syncdecorators.timerlock.sync_data.{self.item_type}.{self.method}'
        self.window.get_property(propname, clear_property=True)

    @property
    def last_activities_item_type(self):
        return f'{self.item_type}s'

    @property
    def last_activities_keys(self):
        return (self.last_activities_item_type, self.last_activities_key, )

    def clear_columns(self, keys):
        self.cache.del_column_values(keys=keys, item_type=self.item_type)
        self.clear_child_columns(keys)

    def clear_child_columns(self, keys):
        pass

    @property
    def is_expired(self):
        if not self.cache.get_activity(self.item_type, self.paged_marker_key):
            return True
        timestamp = self.cache.get_activity(self.item_type, self.method)
        return self.last_activities.is_expired(timestamp, keys=self.last_activities_keys)

    @timerlock
    def sync_func(self):
        from tmdbhelper.lib.addon.logger import TimerFunc
        with TimerFunc(f'Sync: {self.method} {self.item_type}', inline=True, log_threshold=0.001):
            return self.get_response_sync('sync', self.method, f'{self.item_type}s', **self.sync_kwgs)

    @progress_bg
    def sync_data(self, **kwargs):
        self.dialog_progress_bg.update(20, message='Refreshing Data')
        meta = self.sync_func()
        if meta is None:
            return

        from tmdbhelper.lib.api.trakt.sync.itemdata import SyncItem
        item = SyncItem(self.item_type, meta, self.keys, key_prefix=self.key_prefix)

        self.dialog_progress_bg.update(40, message='Cleaning Data')
        self.clear_columns(item.base_table_keys)

        if not meta:
            return True

        self.dialog_progress_bg.update(60, message='Configuring Data')
        data = item.data

        self.dialog_progress_bg.update(80, message='Updating Data')
        self.cache.set_many_values(keys=item.table_keys, data=data)

        return (item.table_keys, data, )

    @mutexlock
    def sync(self, forced=False):
        if not forced and not self.is_expired:
            return
        if forced:
            self.clear_timerlock()
        result = self.sync_data()
        if result is not None:
            self.store_last_activity()


class DataTypeEpisodes(DataType):
    def clear_child_columns(self, keys):
        if self.item_type == 'show':
            self.cache.del_column_values(keys=keys, item_type='season')
            self.cache.del_column_values(keys=keys, item_type='episode')

    @property
    def last_activities_item_type(self):
        if self.item_type == 'show':
            return 'episodes'
        return f'{self.item_type}s'


class SyncHidden(DataType):
    keys = ('hidden_at', )
    last_activities_key = 'hidden_at'
    method = 'hidden'

    @timerlock
    def sync_func(self):
        """ Get items that are hidden on Trakt """
        from tmdbhelper.lib.addon.logger import TimerFunc
        with TimerFunc(f'Sync: {self.method} {self.item_type}', inline=True, log_threshold=0.001):
            response = []
            response += self.get_response_sync('users', self.method, 'progress_watched', type=f'{self.item_type}s', limit=4095) or []
            response += self.get_response_sync('users', self.method, 'progress_collected', type=f'{self.item_type}s', limit=4095) or []
            response += self.get_response_sync('users', self.method, 'calendar', type=f'{self.item_type}s', limit=4095) or []
            if self.item_type == 'show':
                # Dropped shows are hidden from the progress/next-up lists like Trakt's other hidden sections. Trakt does NOT move the
                # hidden_at activity stamp when a show is dropped, so the script/sync/dropped.py actions force this sync after a change.
                # A failed read skips the whole rewrite (returning None) rather than un-hiding every dropped show.
                dropped = self.get_response_sync('users', self.method, 'dropped', type='shows', limit=4095)
                if dropped is None:
                    return
                response += dropped
            return response


class SyncWatched(DataTypeEpisodes):
    keys = ('plays', 'last_watched_at', 'last_updated_at', 'aired_episodes', 'watched_episodes', 'reset_at', )
    last_activities_key = 'watched_at'
    sync_kwgs = {'extended': 'full'}
    method = 'watched'

    """
    Trakt's /sync/watched/shows no longer includes the seasons/episodes lists, so shows never got a watched_episodes count
    and no season/episode rows. For shows we rebuild that list from /shows/{id}/progress/watched in the old response shape
    so SyncItem parses it as before. Shows whose last_watched_at/reset_at/plays/last_updated_at are unchanged are rebuilt from the rows already stored.
    """
    progress_marker = 'watched_progress_v1'  # lactivities entry (show only): 'ok' or 'partial:<retry timestamp>'; missing forces a full rebuild
    progress_retry_after = 1800  # seconds before retrying shows whose progress request failed
    progress_threads = 2  # get_setting('max_threads') is 0 (unlimited) by default - never fire one request per show at once; a 429 shows a toast and blocks all Trakt requests for 15s (reqapi.py)
    progress_state = 'ok'

    @property
    def is_progress_due(self):
        state = self.cache.get_activity(self.item_type, self.progress_marker)
        if not state:
            return True
        if state.startswith('partial:'):
            try:
                return float(state.split(':', 1)[1]) <= time()
            except ValueError:
                return True
        return False

    @property
    def is_expired(self):
        if self.item_type == 'show' and self.is_progress_due:
            return True
        return super().is_expired

    @staticmethod
    def get_progress_trakt_id(item):
        return ((item.get('show') or {}).get('ids') or {}).get('trakt')

    @staticmethod
    def get_progress_key(trakt_id, last_watched_at, reset_at, plays, last_updated_at):
        """ Key a show's stored episodes are reused under: plays/last_updated_at change when an older episode is unwatched (last_watched_at does not) """
        return tuple(str(i) for i in (trakt_id, last_watched_at, reset_at, plays, last_updated_at))

    def get_progress_stored_seasons(self):
        """ Returns {get_progress_key(): seasons list} built from the show/episode rows of the last sync """
        shows = self.cache.get_list_values(
            keys=('tmdb_id', 'trakt_id', 'last_watched_at', 'reset_at', 'plays', 'last_updated_at'), values=('show', ), conditions='item_type=?') or []
        episodes = self.cache.get_list_values(
            keys=('tmdb_id', 'season_number', 'episode_number', 'last_watched_at', 'plays'),
            values=('episode', ), conditions='item_type=? AND last_watched_at IS NOT NULL') or []
        by_tmdb = {}
        for tmdb_id, season_number, episode_number, last_watched_at, plays in episodes:
            by_tmdb.setdefault(str(tmdb_id), {}).setdefault(season_number, []).append(
                {'number': episode_number, 'plays': plays or 1, 'last_watched_at': last_watched_at})
        stored = {}
        for tmdb_id, trakt_id, last_watched_at, reset_at, plays, last_updated_at in shows:
            seasons = by_tmdb.get(str(tmdb_id))
            if seasons:
                stored[self.get_progress_key(trakt_id, last_watched_at, reset_at, plays, last_updated_at)] = [
                    {'number': k, 'episodes': v} for k, v in sorted(seasons.items())]
        return stored

    @staticmethod
    def get_progress_seasons(progress):
        """ Converts a /progress/watched response to the seasons list of /sync/watched (watched episodes only, specials left out as before) """
        seasons = []
        for season in progress.get('seasons') or []:
            if not season.get('number'):
                continue
            episodes = [
                {'number': i['number'], 'plays': (i.get('stats') or {}).get('play_count') or 1, 'last_watched_at': i['last_watched_at']}
                for i in season.get('episodes') or []
                if i.get('completed') and i.get('last_watched_at') and i.get('number') is not None]
            if episodes:
                seasons.append({'number': season['number'], 'episodes': episodes})
        return seasons

    def get_show_progress(self, item):
        try:
            progress = self.get_response_sync('shows', self.get_progress_trakt_id(item), 'progress', 'watched')
            if isinstance(progress, dict) and isinstance(progress.get('seasons'), list):
                return progress
        except Exception:
            pass

    def sync_show_progress(self, meta):
        from tmdbhelper.lib.addon.thread import ParallelThread
        from tmdbhelper.lib.addon.logger import kodi_log

        threads = min(self.progress_threads, ParallelThread.thread_max or self.progress_threads)

        class ProgressThread(ParallelThread):
            thread_max = threads

        timer_a = time()
        stored = self.get_progress_stored_seasons() if self.cache.get_activity(self.item_type, self.progress_marker) else {}
        reused, fetch = 0, []
        for item in meta:
            if item.get('seasons') or not self.get_progress_trakt_id(item):
                continue
            seasons = stored.get(self.get_progress_key(
                self.get_progress_trakt_id(item), item.get('last_watched_at'), item.get('reset_at'),
                item.get('plays'), item.get('last_updated_at') or item.get('updated_at')))
            if seasons:
                item['seasons'] = seasons
                reused += 1
                continue
            fetch.append(item)

        queue = []
        if fetch:
            with ProgressThread(fetch, self.get_show_progress) as pt:
                pass
            queue = pt.queue

        failed = 0
        for item, progress in zip(fetch, queue):
            if progress is None:  # Leave seasons unset: watched_episodes stays empty for this show and it is treated as in progress
                failed += 1
                continue
            item['seasons'] = self.get_progress_seasons(progress)

        if failed:
            self.progress_state = f'partial:{time() + self.progress_retry_after}'
        kodi_log(f'Sync: watched show progress - {len(fetch) - failed} fetched, {reused} reused, {failed} failed {time() - timer_a:.1f} sec', 1)

    def sync_func(self):
        meta = super().sync_func()
        self.progress_state = 'ok'
        if self.item_type == 'show' and meta:
            self.sync_show_progress(meta)
        return meta

    def store_last_activity(self):
        super().store_last_activity()
        if self.item_type == 'show':
            self.cache.set_activity(self.item_type, self.progress_marker, self.progress_state)


class SyncPlayback(DataTypeEpisodes):
    keys = ('progress', 'paused_at', 'id', )
    last_activities_key = 'paused_at'
    sync_kwgs = {'extended': 'full'}
    method = 'playback'
    key_prefix = 'playback'


class SyncRatings(DataType):
    keys = ('rating', 'rated_at', )
    last_activities_key = 'rated_at'
    method = 'ratings'


class SyncFavorites(DataType):
    keys = ('rank', 'listed_at', 'notes', )
    last_activities_key = 'favorited_at'
    sync_kwgs = {'extended': 'full'}
    method = 'favorites'
    key_prefix = 'favorites'


class SyncWatchlist(DataType):
    keys = ('rank', 'listed_at', 'notes', )
    last_activities_key = 'watchlisted_at'
    sync_kwgs = {'extended': 'full'}
    method = 'watchlist'
    key_prefix = 'watchlist'


class SyncCollection(DataTypeEpisodes):
    keys = ('last_collected_at', 'last_updated_at', )
    last_activities_key = 'collected_at'
    method = 'collection'
    key_prefix = 'collection'


class SyncAllNextEpisodes(DataTypeEpisodes):
    keys = ('upnext_episode_id', )
    last_activities_key = 'watched_at'
    method = 'all_next_episodes'

    def get_all_next_episodes(self, response, tmdb_id):
        # For list of episodes we need to build them by comparing against the reset_at date
        from tmdbhelper.lib.addon.tmdate import convert_timestamp
        reset_at = convert_timestamp(response['reset_at']) if response.get('reset_at') else None
        return (
            f'tv.{tmdb_id}.{season["number"]}.{episode["number"]}'
            for season in response.get('seasons', []) for episode in season.get('episodes', [])
            if not episode.get('completed') or (reset_at and convert_timestamp(episode.get('last_watched_at')) < reset_at))

    def get_next_episodes_response(self, trakt_id):
        if not trakt_id:
            return
        return self.get_response_sync('shows', trakt_id, 'progress/watched')

    def get_next_episodes(self, tmdb_id, trakt_id):
        response = self.get_next_episodes_response(trakt_id)
        if not response:
            return
        return self.get_all_next_episodes(response, tmdb_id)

    @timerlock
    def sync_func(self):
        """ Get next episodes on Trakt """
        from tmdbhelper.lib.addon.thread import ParallelThread
        from tmdbhelper.lib.addon.logger import TimerFunc

        dpro = self.dialog_progress_bg

        def get_item(i, item_id):
            tmdb_type, tmdb_id, season_number, episode_number = item_id.split('.')
            item = {"show": {"ids": {"tmdb": i["tmdb_id"], "trakt": i["trakt_id"]}}}
            item['upnext_episode_id'] = item_id
            item['type'] = 'episode'
            item['episode'] = {'season': season_number, 'number': episode_number}
            return item

        def get_items(i):
            next_episodes = self.get_next_episodes(i["tmdb_id"], i["trakt_id"])
            dpro.increment()
            if not next_episodes:
                dpro.set_message(f'Skip: {i["tmdb_id"]} {i["trakt_id"]}')
                return
            dpro.set_message(f'Sync: {i["tmdb_id"]} {i["trakt_id"]}')
            return [get_item(i, item_id) for item_id in next_episodes]

        with TimerFunc(f'Sync: {self.method} {self.item_type}', inline=True, log_threshold=0.001):
            sd = self._class_instance_syncdata.get_all_unhidden_shows_inprogress_getter()
            sd.additional_keys = ('trakt_id', )
            dpro.max_value = len(sd.items)
            with ParallelThread(sd.items, get_items) as pt:
                item_queue = pt.queue
            meta = [i for items in item_queue for i in items if i]
        return meta


class SyncNextEpisodes(SyncAllNextEpisodes):
    keys = ('next_episode_id', )
    last_activities_key = 'watched_at'
    method = 'nextup'

    def get_next_episode(self, tmdb_id, trakt_id):
        response = self.get_next_episodes_response(trakt_id)
        if not response:
            return
        if not response.get('reset_at') and response.get('next_episode'):
            return f'tv.{tmdb_id}.{response["next_episode"]["season"]}.{response["next_episode"]["number"]}'

        # Get a next item generator to only check for one next episode not all
        items = self.get_all_next_episodes(response, tmdb_id)
        try:
            return next(items)
        except StopIteration:
            return

    @timerlock
    def sync_func(self):
        """ Get next episodes on Trakt """
        from tmdbhelper.lib.addon.thread import ParallelThread
        from tmdbhelper.lib.addon.logger import TimerFunc

        dpro = self.dialog_progress_bg

        def get_item(i):
            next_episode_id = self.get_next_episode(i["tmdb_id"], i["trakt_id"])
            dpro.increment()
            if not next_episode_id:
                dpro.set_message(f'Skip: {next_episode_id}')
                return
            dpro.set_message(f'Sync: {next_episode_id}')
            return {"next_episode_id": next_episode_id, "show": {"ids": {"tmdb": i["tmdb_id"], "trakt": i["trakt_id"]}}}

        with TimerFunc(f'Sync: {self.method} {self.item_type}', inline=True, log_threshold=0.001):
            sd = self._class_instance_syncdata.get_all_unhidden_shows_inprogress_getter()
            sd.additional_keys = ('trakt_id', )
            dpro.max_value = len(sd.items)
            with ParallelThread(sd.items, get_item) as pt:
                item_queue = pt.queue
            meta = [i for i in item_queue if i]
        return meta
