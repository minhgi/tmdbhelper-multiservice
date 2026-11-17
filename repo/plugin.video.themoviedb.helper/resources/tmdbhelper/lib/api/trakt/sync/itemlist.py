#!/usr/bin/python
# -*- coding: utf-8 -*-
from tmdbhelper.lib.files.ftools import cached_property
from collections import namedtuple
from tmdbhelper.lib.addon.thread import ParallelThread


class ItemListSyncDataProperties:
    @cached_property
    def items(self):
        if not self.trakt_syncdata:
            return
        return self.get_items()

    @cached_property
    def additional_keys(self):
        return self.get_additional_keys()

    @cached_property
    def syncdata_getter(self):
        return self.get_syncdata_getter()

    @cached_property
    def presorted_items(self):
        return self.get_presorted_items()

    @cached_property
    def sort_method(self):
        return self.get_sort_method()

    @cached_property
    def sort_key(self):
        return self.get_sort_key()

    @cached_property
    def nonetype(self):
        return self.get_nonetype()

    @cached_property
    def reverse(self):
        return self.get_reverse()

    @property
    def trakt_syncdata(self):
        return self._class_instance_trakt_api.trakt_syncdata

    @cached_property
    def namedtuple_basic(self):
        BasicTuple = namedtuple("BasicTuple", "item mediatype")
        return BasicTuple

    @cached_property
    def namedtuple_episode(self):
        EpisodeTuple = namedtuple("EpisodeTuple", "item mediatype season_number episode_number")
        return EpisodeTuple

    @property
    def detailed_item(self):
        if self.item_type != 'episode':
            return False
        if self.sort_by not in ('airdate', 'todays', 'lastweek', ):
            return False
        return True

    @property
    def item_types(self):
        if self.item_type == 'both':
            return ('movie', 'show', )
        return (self.item_type, )


class ItemListSyncDataMethods:
    def sort_data(self, data):
        if self.sort_by == 'random':
            import random
            random.shuffle(data)
            return data
        if not self.additional_keys:
            return data
        return sorted(data, key=lambda x: x[0][self.sort_key] if x[0][self.sort_key] is not None else self.nonetype, reverse=self.reverse)

    def make_detailed_item(self, i, item_type='show'):
        if not i['id']:
            return i
        trakt_id = self._class_instance_trakt_api.get_id(unique_id=i['id'], id_type='tmdb', trakt_type=item_type, output_type='trakt')
        if not trakt_id:
            return i
        item = self._class_instance_trakt_api.get_details(item_type, trakt_id, season=i.get('season'), episode=i.get('episode'))
        if not item:
            return i
        item.pop('ids', None)
        item.update(i)
        return item

    def make_list(self, sd_func):
        data = []
        for item_type in self.item_types:
            sd = sd_func(item_type)
            sd.additional_keys = self.additional_keys
            data += [self.namedtuple_basic(i, item_type, ) for i in sd.items] if sd.items else []

        if not data:
            return

        data = sorted(data, key=lambda x: x.item[sd.clause_keys[0]], reverse=True)
        return [self.make_item(i) for i in self.sort_data(data) if i]

    def make_item(self, i, detailed_item=False):
        item = {'id': i.item['tmdb_id'], 'mediatype': i.mediatype, 'title': i.item['title']}
        if i.mediatype in ('season', 'episode', ):
            item['season'] = i.season_number if 'season_number' in i._fields else i.item['season_number']
        if i.mediatype == 'episode':
            item['episode'] = i.episode_number if 'episode_number' in i._fields else i.item['episode_number']
        for k in (self.item_keys or ()):
            item.setdefault('infoproperties', {})[k] = i.item[k]
        if detailed_item:
            item = self.make_detailed_item(item)
        return item


class ItemListSyncData(ItemListSyncDataProperties, ItemListSyncDataMethods):

    # Tuple of column_name, reverse_sort, fallback_nonetype
    sort = {
        'year': ('year', True, 0, ),
        'released': ('premiered', True, '', ),
        'title': ('title', True, '', ),
        'watched': ('last_watched_at', True, '', ),
        'votes': ('trakt_votes', True, 0, ),
        'plays': ('plays', True, 0, ),
        'runtime': ('runtime', True, 0, ),
        'collected': ('collection_last_collected_at', True, '', ),
        'airdate': ('last_watched_at', True, '', ),
        'todays': ('last_watched_at', True, '', ),
        'lastweek': ('last_watched_at', True, '', ),
    }

    def __init__(self, class_instance_trakt_api, item_type=None, sort_by=None, sort_how=None, item_keys=None, tmdb_id=None):
        self._class_instance_trakt_api = class_instance_trakt_api
        self.sort_by, self.sort_how = sort_by, sort_how
        self.item_keys = item_keys or ()
        self.item_type = item_type
        self.tmdb_id = tmdb_id

    def get_sort_method(self):
        try:
            return self.sort[self.sort_by]
        except KeyError:
            return

    def get_sort_key(self):
        try:
            return self.sort_method[0]
        except TypeError:  # No sort method
            return

    def get_additional_keys(self):
        try:
            return (self.sort_method[0], *self.item_keys)
        except TypeError:  # No sort method
            return

    def get_reverse(self):
        try:
            reverse = self.sort_method[1]
        except TypeError:  # No sort method
            return
        if self.sort_how != 'asc':
            return reverse
        return not reverse

    def get_nonetype(self):
        try:
            return self.sort_method[2]
        except TypeError:  # No sort method
            return


class ItemListSyncDataCollection(ItemListSyncData):
    """ Items in collection """

    def get_items(self):
        return self.make_list(self.trakt_syncdata.get_all_collected_getter)


class ItemListSyncDataWatchlist(ItemListSyncData):
    """ Items on watchlist """

    sort = {**ItemListSyncData.sort, 'added': ('watchlist_listed_at', True, '', )}

    def get_items(self):
        return self.make_list(self.trakt_syncdata.get_all_watchlist_getter)


class ItemListSyncDataWatched(ItemListSyncData):
    """ Items that have been watched """

    def get_items(self):
        return self.make_list(self.trakt_syncdata.get_all_watched_getter)


class ItemListSyncDataPlayback(ItemListSyncData):
    """ Episodes and movies partially watched with resume points """

    def get_items(self):
        return self.make_list(self.trakt_syncdata.get_all_playback_getter)


class ItemListSyncDataFavorites(ItemListSyncData):
    """ Items in favourites """

    sort = {**ItemListSyncData.sort, 'added': ('favorites_listed_at', True, '', )}

    def get_items(self):
        return self.make_list(self.trakt_syncdata.get_all_favorites_getter)


class ItemListSyncDataUnwatchedPlayback(ItemListSyncData):
    """ Episodes and movies partially watched with resume points """

    def get_items(self):
        return self.make_list(self.trakt_syncdata.get_all_unwatched_playback_getter)


class ItemListSyncDataToWatch(ItemListSyncData):
    """ Mix of watchlist and inprogress items """

    @cached_property
    def presorted_fallback_key(self):
        if self.item_type == 'movie':
            return 'playback_paused_at'
        return 'last_watched_at'

    @cached_property
    def syncdata_getter_func(self):
        if self.item_type == 'movie':
            return self.trakt_syncdata.get_all_unhidden_movies_towatch_getter
        return self.trakt_syncdata.get_all_unhidden_shows_towatch_getter

    def get_syncdata_getter(self):
        sd = self.syncdata_getter_func()
        sd.additional_keys = (self.presorted_fallback_key, *self.item_keys, )
        return sd

    def get_presorted_items(self):
        return sorted(self.syncdata_getter.items, key=lambda x: x['watchlist_listed_at'] or x[self.presorted_fallback_key], reverse=True)

    def get_items(self):
        data = [self.namedtuple_basic(i, self.item_type, ) for i in self.presorted_items]
        return [self.make_item(i) for i in self.sort_data(data) if i]


def count_aired_by_date(episodes, trakt_aired, now=None):
    """ Number of regular episodes that have aired by their own Trakt air date (UTC), or None when the list cannot be trusted.

    episodes is [(season_number, episode_number, first_aired)] from the local Trakt episode cache. Trakt's show-level
    aired_episodes can run ahead of the episode air dates (A Tale of the Secret Saint 2026-11-16: aired_episodes 8, but episode 8
    airs 11/21), so In Progress cross-checks it. The cache holds single episodes too, so a gappy list would undercount and hide a
    show that still has unwatched aired episodes: it is used only when it has at least trakt_aired episodes, every season runs
    1..N without a gap and no episode lacks an air date while a later one has one. The cache is written once and never
    refreshed, so an episode stored before its date was announced keeps reading "no date" (tmdb 286171 on the Shield test
    account: 19 undated rows that have since aired). """
    import datetime
    if not episodes or not trakt_aired or len(episodes) < trakt_aired:
        return None
    seasons = {}
    for season, episode, _ in episodes:
        seasons.setdefault(season, set()).add(episode)
    if any(numbers != set(range(1, max(numbers) + 1)) for numbers in seasons.values()):
        return None
    ordered = [bool(first_aired) for _, _, first_aired in sorted(episodes, key=lambda e: (e[0], e[1]))]
    if any(not dated and any(ordered[i + 1:]) for i, dated in enumerate(ordered)):
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    aired = 0
    for _, _, first_aired in episodes:
        try:
            if datetime.datetime.strptime(first_aired[:19], '%Y-%m-%dT%H:%M:%S') <= now:
                aired += 1
        except (TypeError, ValueError):  # no air date yet (to be announced) counts as not aired
            continue
    return aired


def count_future_dated_in_aired(episodes, trakt_aired, now=None):
    """ How many of the first trakt_aired regular episodes (season, episode order) carry an air date still in the future: the ones
    Trakt's show-level aired_episodes counts as aired too early (A Tale of the Secret Saint 2026-11-16: aired_episodes 8, episode 8
    airs 11/21). Only a known future date counts, so an episode with no date (a cache written before the date was announced) never
    lowers the count. 0 when the list cannot be trusted: fewer rows than trakt_aired, or a season with a gap. """
    import datetime
    if not episodes or not trakt_aired or len(episodes) < trakt_aired:
        return 0
    seasons = {}
    for season, episode, _ in episodes:
        seasons.setdefault(season, set()).add(episode)
    if any(numbers != set(range(1, max(numbers) + 1)) for numbers in seasons.values()):
        return 0
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    future = 0
    for _, _, first_aired in sorted(episodes, key=lambda e: (e[0], e[1]))[:trakt_aired]:
        try:
            if datetime.datetime.strptime(first_aired[:19], '%Y-%m-%dT%H:%M:%S') > now:
                future += 1
        except (TypeError, ValueError):  # no air date: unknown, not counted
            continue
    return future


class ItemListSyncDataInProgress(ItemListSyncData):
    """ Partially watched shows that are inprogress """

    def get_syncdata_getter(self):
        sd = self.trakt_syncdata.get_all_unhidden_shows_inprogress_getter()
        sd.additional_keys = self.additional_keys
        return sd

    def get_episode_air_rows(self, tmdb_id):
        """ Cached Trakt episode air dates of a show, read only: no request is made for episodes that are not cached """
        episodedata = self._class_instance_trakt_api.trakt_episodedata
        if not episodedata:
            return
        rows = episodedata.cache.get_list_values(
            keys=('season_number', 'episode_number', 'first_aired', ), values=(tmdb_id, ), conditions='tmdb_id=? AND season_number>0')
        return [(r[0], r[1], r[2]) for r in rows or []]

    def is_all_aired_watched(self, item):
        """ True for a show Trakt's aired count keeps in progress but whose episodes that have really aired are all watched.
        Only removes shows (the date count is never higher than Trakt's); the shared in-progress getter feeds the Next Up
        syncs too, so this filter lives on the widget list only. """
        try:
            aired, watched = item['aired_episodes'], item['watched_episodes']
            if aired is None or watched is None or aired <= watched:
                return False  # unknown, or is_inprogress_show already decided it with the calendar check
            by_date = count_aired_by_date(self.get_episode_air_rows(item['tmdb_id']), aired)
            if by_date is None or by_date != watched:
                return False  # more watched than the dates say aired (or still unwatched aired ones): the cached dates are stale, keep the show
            if not self.syncdata_getter.is_calendar_watched(item['tmdb_id']):
                return False
            from tmdbhelper.lib.addon.logger import kodi_log
            kodi_log(f'Trakt In Progress: tmdb {item["tmdb_id"]} left out, Trakt aired {aired} but {by_date} aired by date, {watched} watched', 1)
            return True
        except Exception:
            return False

    def get_presorted_items(self):
        items = sorted(self.syncdata_getter.items, key=lambda x: x['last_watched_at'], reverse=True)
        return [i for i in items if not self.is_all_aired_watched(i)]

    def get_items(self):
        data = [self.namedtuple_basic(i, 'show', ) for i in self.presorted_items]
        return [self.make_item(i) for i in self.sort_data(data) if i]


class ItemListSyncDataNextUp(ItemListSyncData):
    """ Episodes next up to watch for all inprogress shows """
    sort_method = ('last_watched_at', True, '', )

    def get_syncdata_getter(self):
        sd = self.trakt_syncdata.get_all_unhidden_shows_nextepisode_getter()
        sd.additional_keys = self.additional_keys
        return sd

    def get_presorted_items(self):
        # configure items
        data = [self.namedtuple_episode(i, 'episode', i['next_episode_id'].split('.')[2], i['next_episode_id'].split('.')[3], ) for i in self.syncdata_getter.items]
        data = [i for i in self.sort_data(data) if i]
        with ParallelThread(data, self.make_item, detailed_item=self.detailed_item) as pt:
            item_queue = pt.queue
        return [i for i in item_queue if i]

    def get_special_sort(self):
        from tmdbhelper.lib.addon.tmdate import is_future_timestamp
        days = -1 if self.sort_by == 'todays' else -7
        recent, remainder = [], []
        for i in self.presorted_items:
            first_aired = i.get('first_aired')
            if first_aired and is_future_timestamp(first_aired, "%Y-%m-%d", 10, use_today=True, days=days):
                recent.append(i)
                continue
            remainder.append(i)
        return sorted(recent, key=lambda x: x['first_aired'], reverse=True) + remainder

    def get_items(self):
        if self.sort_by == 'airdate':
            return sorted(self.presorted_items, key=lambda x: x.get('first_aired') or '0', reverse=True)
        if self.sort_by in ('todays', 'lastweek', ):
            return self.get_special_sort()
        return self.presorted_items


class ItemListSyncDataUpNext(ItemListSyncData):
    """ Episodes Up Next for specific tmdb_id show """

    def get_syncdata_getter(self):
        sd = self.trakt_syncdata.get_unhidden_show_episodes_upnext(self.tmdb_id)
        sd.additional_keys = self.additional_keys
        return sd

    def get_presorted_items(self):
        return [
            self.namedtuple_episode(i, 'episode', i['upnext_episode_id'].split('.')[2], i['upnext_episode_id'].split('.')[3], )
            for i in self.syncdata_getter.items]

    def get_items(self):
        return [self.make_item(i) for i in self.sort_data(self.presorted_items) if i]


def ItemListSyncDataFactory(sync_type, *args, **kwargs):

    routes = {
        'collection': ItemListSyncDataCollection,
        'watchlist': ItemListSyncDataWatchlist,
        'watched': ItemListSyncDataWatched,
        'playback': ItemListSyncDataPlayback,
        'favorites': ItemListSyncDataFavorites,
        'nextup': ItemListSyncDataNextUp,
        'upnext': ItemListSyncDataUpNext,
        'inprogress': ItemListSyncDataInProgress,
        'towatch': ItemListSyncDataToWatch,
        'unwatchedplayback': ItemListSyncDataUnwatchedPlayback,
    }

    return routes[sync_type](*args, **kwargs)
