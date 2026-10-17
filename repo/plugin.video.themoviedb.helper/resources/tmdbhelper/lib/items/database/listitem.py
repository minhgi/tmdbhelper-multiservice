from tmdbhelper.lib.files.ftools import cached_property
from threading import Lock
from tmdbhelper.lib.items.listitem import ListItem
from tmdbhelper.lib.items.database.tmdbdata import ItemDetailsDataBaseCacheFactory


# @cached_property
# def lidc(self):
#     from tmdbhelper.lib.items.database.listitem import ListItemDetailsConfigurator
#     return ListItemDetailsConfigurator(tmdb_api=self.tmdb_api)


class ThreadLocks(dict):
    def __missing__(self, key):
        self[key] = Lock()
        return self[key]


class ListItemDetailsConfigurator:
    pagination = False

    def __init__(self, tmdb_api=None, trakt_api=None):
        self._tmdb_api = tmdb_api
        self._trakt_api = trakt_api

    @cached_property
    def tmdb_api(self):
        from tmdbhelper.lib.api.tmdb.api import TMDb
        return self._tmdb_api or TMDb()

    @cached_property
    def trakt_api(self):
        from tmdbhelper.lib.api.trakt.api import TraktAPI
        return self._trakt_api or TraktAPI()

    @cached_property
    def thread_locks(self):
        return ThreadLocks()

    def get_db_cache(self, mediatype):
        dbc = ItemDetailsDataBaseCacheFactory(mediatype)
        dbc.tmdb_api = self.tmdb_api
        dbc.thread_locks = self.thread_locks
        return dbc

    def get_configured_db_cache(self, li):
        mediatype = li.infolabels.get('mediatype')

        def get_movie():
            dbc.tmdb_id = li.unique_ids.get('tmdb')

        def get_tvshow():
            dbc.tmdb_id = li.unique_ids.get('tmdb') or li.unique_ids.get('tvshow.tmdb')

        def get_season():
            dbc.season = li.infolabels.get('season', 0)
            dbc.tmdb_id = li.unique_ids.get('tvshow.tmdb')

        def get_episode():
            dbc.episode = li.infolabels.get('episode')
            dbc.season = li.infolabels.get('season', 0)
            dbc.tmdb_id = li.unique_ids.get('tvshow.tmdb')

        routes = {
            'movie': get_movie,
            'tvshow': get_tvshow,
            'season': get_season,
            'episode': get_episode
        }

        if mediatype not in routes:
            return

        dbc = self.get_db_cache(mediatype)
        routes[mediatype]()
        return dbc

    def configure_listitem(self, i, cache_refresh=None):
        li = ListItem(**i)

        if 'next_page' in i:
            return li if self.pagination else None

        dbc = self.get_configured_db_cache(li)

        if not dbc:
            return li if cache_refresh != 'never' else None

        db_cache_data = dbc.data

        if not db_cache_data:
            return li if cache_refresh != 'never' else None

        li.set_details(db_cache_data, override=True)

        # Phase 6: Anime episode metadata enrichment from Seren's cache
        if li.infolabels.get('mediatype') == 'episode':
            self._apply_anime_overlay(li)

        return li

    def configure_listitems_threaded(self, items):  # TODO: Retrieve sequentially then pool unavailable items and thread lookups before setting sequentially
        from tmdbhelper.lib.addon.thread import ParallelThread
        with ParallelThread(items, self.configure_listitem, cache_refresh='never') as pt:
            item_queue = pt.queue
        missing_indices = [x for x, i in enumerate(item_queue) if i is None]
        missing_items = [items[x] for x in missing_indices]
        with ParallelThread(missing_items, self.configure_listitem, cache_refresh='force') as pt:
            refresh_item_queue = pt.queue
        refresh_item_queue_iter = iter(refresh_item_queue)
        all_items = [i or next(refresh_item_queue_iter) for i in item_queue]
        all_items = [i for i in all_items if i]

        return all_items

    def configure_listitems(self, items):
        return [j for j in (self.configure_listitem(i) for i in items if i) if j]

    @cached_property
    def _anime_enrichment_enabled(self):
        try:
            from tmdbhelper.lib.items.anime_enrichment import is_anime_enrichment_enabled
            return is_anime_enrichment_enabled()
        except Exception:
            return False

    def _apply_anime_overlay(self, li):
        """Overlay anime-specific episode metadata from Seren's cache."""
        if not self._anime_enrichment_enabled:
            return

        tmdb_id = li.unique_ids.get('tvshow.tmdb') or li.unique_ids.get('tmdb')
        season = li.infolabels.get('season')
        episode = li.infolabels.get('episode')

        if not tmdb_id or season is None or episode is None:
            return

        try:
            from tmdbhelper.lib.items.anime_enrichment import get_anime_episode_overlay
            overlay = get_anime_episode_overlay(tmdb_id, season, episode)
        except Exception:
            return

        if not overlay:
            return

        # Overlay anime-specific data onto existing TMDb data
        if 'title' in overlay:
            li.infolabels['title'] = overlay['title']
            li.label = overlay['title']
        if 'plot' in overlay:
            li.infolabels['plot'] = overlay['plot']
        if 'thumb' in overlay:
            li.art['thumb'] = overlay['thumb']
        if 'airdate' in overlay and not li.infolabels.get('premiered'):
            li.infolabels['premiered'] = overlay['airdate']
        if 'duration' in overlay and overlay['duration']:
            li.infolabels['duration'] = overlay['duration'] * 60  # minutes → seconds
