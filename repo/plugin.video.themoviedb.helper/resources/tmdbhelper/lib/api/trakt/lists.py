from jurialmunkey.parser import try_int
from tmdbhelper.lib.addon.plugin import convert_type, PLUGINPATH, get_plugin_category, get_localized, get_setting, encode_url
from tmdbhelper.lib.items.container import ContainerDirectory


# Trakt lists whose own sort is what defines them (Recently Released, Anticipated, Most Watched): the global sort leaves them alone
GLOBAL_SORT_KEEPS = ('released', 'plays')


class ListBasic(ContainerDirectory):
    def get_items(
            self, info, tmdb_type, page=None, randomise=False, limit=None,
            genres=None, years=None, query=None, languages=None, countries=None, runtimes=None, studio_ids=None,
            **kwargs
    ):

        from tmdbhelper.lib.addon.consts import TRAKT_BASIC_LISTS

        def _get_items_both():
            info_model = TRAKT_BASIC_LISTS.get(info)
            items = self.trakt_api.get_mixed_list(
                path=info_model.get('path', ''),
                trakt_types=['movie', 'show'],
                authorize=info_model.get('authorize', False),
                extended=info_model.get('extended', None),
                genres=genres, years=years, query=query, languages=languages, countries=countries, runtimes=runtimes, studio_ids=studio_ids
            )
            self.tmdb_cache_only = False
            self.library = 'video'
            self.container_content = 'movies'
            self.kodi_db = self.get_kodi_database('both')
            return items

        if tmdb_type == 'both':
            return _get_items_both()
        info_model = TRAKT_BASIC_LISTS.get(info)
        info_tmdb_type = info_model.get('tmdb_type') or tmdb_type
        trakt_type = convert_type(tmdb_type, 'trakt')
        func = self.trakt_api.get_stacked_list if info_model.get('stacked') else self.trakt_api.get_basic_list
        items = func(
            path=info_model.get('path', '').format(trakt_type=trakt_type, **kwargs),
            trakt_type=trakt_type,
            params=info_model.get('params'),
            page=page,
            limit=limit,
            authorize=info_model.get('authorize', False),
            sort_by=info_model.get('sort_by', None),
            sort_how=info_model.get('sort_how', None),
            extended=info_model.get('extended', None),
            randomise=randomise,
            genres=genres, years=years, query=query, languages=languages, countries=countries, runtimes=runtimes, studio_ids=studio_ids,
            always_refresh=False   # Basic lists don't need updating more than once per day
        )
        self.tmdb_cache_only = False
        self.kodi_db = self.get_kodi_database(info_tmdb_type)
        self.library = convert_type(info_tmdb_type, 'library')
        self.container_content = convert_type(info_tmdb_type, 'container')
        self.plugin_category = get_plugin_category(info_model, convert_type(info_tmdb_type, 'plural'))
        return items


class ListSync(ContainerDirectory):
    def _keep_scope(self, items, scope):
        """Keep the rows of an anime / Asian Progress list that belong to that scope (TMDb details, cached)."""
        from tmdbhelper.lib.api.mdblist.lists import _in_scope
        kept = []
        for i in items:
            tmdb_type = 'movie' if i.get('mediatype') == 'movie' else 'tv'
            if i.get('id') and _in_scope(self.tmdb_api.get_request_sc(tmdb_type, i['id']) or {}, scope, tmdb_type):
                kept.append(i)
        return kept

    @staticmethod
    def _global_sort(sync_type, item_type, sort_by, sort_how):
        """The global sort for a list that has no sort of its own (opt-in: other sync lists keep the order they build)."""
        if sort_by:
            return sort_by, sort_how
        from tmdbhelper.lib.items.globalsort import trakt_sort_args
        return trakt_sort_args(sync_type, item_type)

    def get_list_items(self, sync_type, item_type, page=1, limit=None, sort_by=None, sort_how=None, params=None, next_page=True, filters=None, tmdb_id=None, scope=None, **kwargs):
        from tmdbhelper.lib.api.trakt.sync.itemlist import ItemListSyncDataFactory
        from tmdbhelper.lib.items.itemlist import ItemListPagination

        item_keys = tuple(set([v for k, v in filters.items() if k.startswith('filter_key') or k.startswith('exclude_key')])) if filters else None
        sync_list = ItemListSyncDataFactory(sync_type, self.trakt_api, sort_by=sort_by, sort_how=sort_how, item_type=item_type, item_keys=item_keys, tmdb_id=tmdb_id)

        if not sync_list.items:
            return

        items = self._keep_scope(sync_list.items, scope) if scope else sync_list.items
        if not items:
            return

        limit = limit or self.trakt_api.sync_item_limit
        params_def = {item_type: params} if params else None
        response = ItemListPagination({item_type: items}, page=page or 1, limit=limit, params_def=params_def, filters=filters)

        return response.items if not next_page or sort_by == 'random' else response.items + response.next_page

    def get_items(self, info, tmdb_type, page=None, **kwargs):
        from tmdbhelper.lib.addon.consts import TRAKT_SYNC_LISTS
        info_model = TRAKT_SYNC_LISTS.get(info)
        info_tmdb_type = info_model.get('tmdb_type') or tmdb_type
        sort_by, sort_how = kwargs.get('sort_by', None), kwargs.get('sort_how', None)
        if not sort_by and info_model.get('sort_by') not in GLOBAL_SORT_KEEPS:  # a path or menu sort wins, then the global sort, then the list's own
            from tmdbhelper.lib.items.globalsort import trakt_sort_args
            sort_by, sort_how = trakt_sort_args(info_model.get('sync_type'), convert_type(tmdb_type, 'trakt'))
        items = self.get_list_items(
            sync_type=info_model.get('sync_type', ''),
            item_type=convert_type(tmdb_type, 'trakt'),
            page=page,
            sort_by=sort_by or info_model.get('sort_by', None),
            sort_how=sort_how or info_model.get('sort_how', None),
            params=info_model.get('params'),
            filters=info_model.get('filters', None))
        self.tmdb_cache_only = False
        self.kodi_db = self.get_kodi_database(info_tmdb_type)
        self.library = convert_type(info_tmdb_type, 'library')
        self.container_content = convert_type(info_tmdb_type, 'container', items=items)
        self.plugin_category = get_plugin_category(info_model, convert_type(info_tmdb_type, 'plural'))
        return items


class ListWatchlist(ListSync):
    """Trakt watchlist; TV page 1 (not a widget) starts with a 'Drop Shows from Watchlist' action."""
    def get_items(self, info, tmdb_type, page=None, **kwargs):
        items = super().get_items(info, tmdb_type, page=page, **kwargs)
        if tmdb_type != 'tv':
            return items
        from tmdbhelper.lib.script.sync.dropped import with_header, HEADER_TRAKT_DROP
        return with_header(items, HEADER_TRAKT_DROP, self.is_widget, page)


class ListDropped(ContainerDirectory):
    """Trakt dropped shows - GET /users/hidden/dropped?type=show. Read live, never through the cached basic list, so a show
    that was just dropped or restored is reflected at once. Page 1 (not a widget) starts with 'Restore Dropped Shows'."""
    def get_items(self, info, tmdb_type=None, page=None, **kwargs):
        from tmdbhelper.lib.api.trakt.api import is_authorized
        from tmdbhelper.lib.api.trakt.items import TraktItems
        from tmdbhelper.lib.items.pages import get_next_page
        from tmdbhelper.lib.script.sync.dropped import with_header, HEADER_TRAKT_RESTORE

        @is_authorized
        def _get_response(api):
            return api.get_response('users', 'hidden', 'dropped', type='show', page=try_int(page, fallback=1) or 1, limit=self.trakt_api.item_limit)

        response = _get_response(self.trakt_api)
        if response is None:
            return
        try:
            data = response.json()
        except (ValueError, AttributeError):
            return
        items = TraktItems(data, headers=response.headers, trakt_type='show').configure_items()['items']
        self.tmdb_cache_only = False
        self.kodi_db = self.get_kodi_database('tv')
        self.library = 'video'
        self.container_content = 'tvshows'
        self.plugin_category = get_localized(32568)
        return with_header(items, HEADER_TRAKT_RESTORE, self.is_widget, page) + (get_next_page(response.headers) if items else [])


class ListToWatch(ListSync):
    def get_items(self, info, tmdb_type, page=None, **kwargs):
        """ Get a mix of watchlisted and inprogress """
        if tmdb_type not in ['movie', 'tv']:
            return
        self.tmdb_cache_only = False
        self.kodi_db = self.get_kodi_database(tmdb_type)
        self.library = convert_type(tmdb_type, 'library')
        self.container_content = convert_type(tmdb_type, 'container')
        self.plugin_category = f'{convert_type(tmdb_type, "plural")} {get_localized(32078)}'
        return self.get_list_items(
            sync_type='towatch',
            item_type='movie' if tmdb_type == 'movie' else 'show',
            page=page)


class ListHistoryStats(ContainerDirectory):
    """ Placeholder for the watch-history stats route that newer skins (Arctic Fuse 3) request.
    This fork keeps no per-day watch history, so the list is empty: it opens blank instead of raising 'not valid route'.
    """
    def get_items(self, **kwargs):
        return []


class ListComments(ContainerDirectory):
    def get_items(self, info, tmdb_type, tmdb_id, sort=None, **kwargs):
        """ Get a mix of watchlisted and inprogress """
        from tmdbhelper.lib.api.mapping import get_empty_item

        if tmdb_type not in ['movie', 'tv']:
            return
        trakt_type = convert_type(tmdb_type, 'trakt')
        slug = self.trakt_api.get_id(tmdb_id, 'tmdb', trakt_type, 'slug')
        items = self.trakt_api.get_request_sc(f'{trakt_type}s', slug, 'comments', sort, limit=50)

        def _map_item(i):
            item = get_empty_item()
            plot = i.get('comment') or ''
            rate = i.get('user_stats', {}).get('rating')
            date = (i.get('created_at') or '')[:10]
            plot = f'{plot}\n{get_localized(563)} {rate}/10' if rate else plot
            plot = f'{plot}\n{date}'
            item['infolabels']['plot'] = plot
            item['label'] = item['infolabels']['title'] = i.get('user', {}).get('name') or i.get('user', {}).get('username')
            item['infolabels']['premiered'] = date
            item['infolabels']['rating'] = rate
            item['params'] = {'comment_id': i.get('id'), 'parent_id': i.get('parent_id'), 'user_slug': i.get('user', {}).get('ids', {}).get('slug')}
            return item

        items = [_map_item(i) for i in items if i]

        return items


class ListBecauseYouWatched(ListSync):
    def get_items(self, info, tmdb_type, page=None, **kwargs):
        import random
        watched_items = self.get_list_items(
            sync_type='watched',
            item_type=convert_type(tmdb_type, 'trakt'),
            page=1,
            limit=get_setting('trakt_becausewatchedseed', 'int') or 5,
            next_page=False,
            params=None,
            sort_by='plays' if info == 'trakt_becausemostwatched' else 'watched',
            sort_how='desc')
        if not watched_items:
            return
        item = watched_items[random.randint(0, len(watched_items) - 1)]
        try:
            item_tmdb_type = item['params']['tmdb_type']
            item_tmdb_id = item['params']['tmdb_id']
        except (AttributeError, KeyError):
            return
        self.parent_params = {
            'info': 'recommendations',
            'tmdb_type': item_tmdb_type,
            'tmdb_id': item_tmdb_id}
        from tmdbhelper.lib.api.tmdb.lists import ListBasic as TMDbListBasic
        items = TMDbListBasic.get_items(
            self,
            info='recommendations',
            tmdb_type=item_tmdb_type,
            tmdb_id=item_tmdb_id,
            page=1)
        self.plugin_category = self.parent_params['plugin_category'] = f'{get_localized(32288)} {item.get("label")}'
        return items


class ListCalendar(ContainerDirectory):
    def _get_calendar_movies(self, startdate, days, page=None, endpoint=None, user=True, countries=None, calendar_filter=None, genres=None):
        # Movies: only a calendar that names its own countries or genres (the Asian and Anime calendars) is filtered. The "All" calendar is
        # not filtered by the region setting: Trakt's country filter is the title's country of origin, not where it is released, so it
        # dropped UK/Canadian/Japanese films that do release in the user's country.
        items = self.trakt_api.get_calendar_movies_list(
            try_int(startdate),
            try_int(days),
            user=user,
            page=page,
            endpoint=endpoint,
            countries=countries,
            **{k: v for k, v in (('calendar_filter', calendar_filter), ('genres', genres)) if v})

        self.kodi_db = self.get_kodi_database('movie')
        self.tmdb_cache_only = False
        self.library = 'video'
        self.container_content = 'movies'
        return items

    def _get_calendar_items(self, info, startdate, days, page=None, kodi_db=None, endpoint=None, user=True, tmdb_type=None, countries=None, calendar_filter=None, genres=None, unstacked=None, **kwargs):
        from tmdbhelper.lib.addon.tmdate import get_calendar_name

        if tmdb_type == 'movie':
            items = self._get_calendar_movies(startdate, days, page=page, endpoint=endpoint, user=user, countries=countries, calendar_filter=calendar_filter, genres=genres)
            self.plugin_category = get_calendar_name(startdate=try_int(startdate), days=try_int(days))
            self.thumb_override = get_setting('calendar_art', 'int')
            return items

        items = self.trakt_api.get_calendar_episodes_list(
            try_int(startdate),
            try_int(days),
            kodi_db=kodi_db,
            user=user,
            page=page,
            endpoint=endpoint,
            **{k: v for k, v in (('countries', countries), ('calendar_filter', calendar_filter), ('genres', genres)) if v},
            **({'stack': False} if unstacked == 'true' else {}))

        self.kodi_db = self.get_kodi_database('tv')
        self.tmdb_cache_only = False
        self.library = 'video'
        self.container_content = 'episodes'
        self.plugin_category = get_calendar_name(startdate=try_int(startdate), days=try_int(days))
        self.thumb_override = get_setting('calendar_art', 'int')
        return items

    def get_items(self, **kwargs):
        kwargs['user'] = kwargs.pop('user', '').lower() != 'false'
        return self._get_calendar_items(**kwargs)


class ListLibraryCalendar(ListCalendar):
    def get_items(self, **kwargs):
        from tmdbhelper.lib.api.kodi.rpc import get_kodi_library
        kodi_db = get_kodi_library('tv')
        if not kodi_db:
            return
        return self._get_calendar_items(kodi_db=kodi_db, user=False, **kwargs)


class ListOnDeck(ListSync):
    def get_ondeck_movies(self, page=None, sort_by=None, sort_how=None, unwatched=False, anime_only=None, asian_only=None, other_only=None, **kwargs):
        from tmdbhelper.lib.api.mdblist.lists import _get_scope
        self.container_content = 'movies'
        self.plugin_category = f'{get_localized(32196)} {convert_type("movie", "plural")}'
        self.kodi_db = self.get_kodi_database('movie')
        sync_type = 'unwatchedplayback' if unwatched else 'playback'
        sort_by, sort_how = self._global_sort(sync_type, 'movie', sort_by, sort_how)
        return self.get_list_items(sync_type=sync_type, item_type='movie', page=page, sort_by=sort_by, sort_how=sort_how, scope=_get_scope(anime_only, asian_only, other_only))

    def get_ondeck_episodes(self, page=None, sort_by=None, sort_how=None, unwatched=False, anime_only=None, asian_only=None, other_only=None, **kwargs):
        from tmdbhelper.lib.api.mdblist.lists import _get_scope
        self.container_content = 'episodes'
        self.plugin_category = get_localized(32406)
        self.kodi_db = self.get_kodi_database('tv')
        sync_type = 'unwatchedplayback' if unwatched else 'playback'
        return self.get_list_items(sync_type=sync_type, item_type='episode', page=page, sort_by=sort_by, sort_how=sort_how, scope=_get_scope(anime_only, asian_only, other_only))

    def get_items(self, **kwargs):
        self.tmdb_cache_only = False
        self.library = 'video'
        return self.get_ondeck_episodes(**kwargs)


class ListOnDeckUnWatched(ListOnDeck):
    def get_items(self, tmdb_type, **kwargs):
        self.tmdb_cache_only = False
        self.library = 'video'
        func = self.get_ondeck_episodes if tmdb_type == 'tv' else self.get_ondeck_movies
        return func(unwatched=True, **kwargs)


class ListInProgress(ListOnDeck):
    def get_items(self, info, tmdb_type, page=None, sort_by=None, sort_how=None, anime_only=None, asian_only=None, other_only=None, **kwargs):
        from tmdbhelper.lib.api.mdblist.lists import _get_scope
        self.tmdb_cache_only = False
        self.library = convert_type(tmdb_type, 'library')

        if tmdb_type != 'tv':
            return self.get_ondeck_movies(page=page, sort_by=sort_by, sort_how=sort_how, anime_only=anime_only, asian_only=asian_only, other_only=other_only, **kwargs)

        params = {
            'info': 'trakt_upnext',
            'tmdb_type': 'tv',
            'tmdb_id': '{tmdb_id}'}

        self.kodi_db = self.get_kodi_database(tmdb_type)
        self.container_content = convert_type(tmdb_type, 'container')
        self.plugin_category = f'{get_localized(32196)} {convert_type(tmdb_type, "plural")}'
        sort_by, sort_how = self._global_sort('inprogress', 'show', sort_by, sort_how)
        return self.get_list_items(
            sync_type='inprogress', item_type='show', page=page, sort_by=sort_by, sort_how=sort_how, params=params,
            scope=_get_scope(anime_only, asian_only, other_only))


class ListNextEpisodes(ListSync):
    def get_items(self, info, tmdb_type, page=None, anime_only=None, asian_only=None, other_only=None, **kwargs):
        from tmdbhelper.lib.api.mdblist.lists import _get_scope
        if tmdb_type != 'tv':
            return
        self.tmdb_cache_only = False

        sort_by = get_setting('trakt_nextepisodesort', 'str')
        sort_how = 'desc'

        self.library = 'video'
        self.container_content = 'episodes'
        self.thumb_override = get_setting('calendar_art', 'int')
        self.plugin_category = get_localized(32197)
        return self.get_list_items(
            sync_type='nextup', item_type='episode', page=page, sort_by=sort_by, sort_how=sort_how, scope=_get_scope(anime_only, asian_only, other_only))


class ListUpNext(ListSync):
    def get_items(self, info, tmdb_type, tmdb_id, page=None, fallback_season=None, **kwargs):
        if tmdb_type != 'tv':
            return
        self.ib.cache_only = self.tmdb_cache_only = False
        self.precache_parent(tmdb_id)
        items = self.get_list_items(
            sync_type='upnext',
            item_type='episode',
            tmdb_id=tmdb_id,
            page=page)
        if not items:  # nothing synced for the show (not followed): list the season the caller names (the show calendar passes the airing one), else season 1
            items = self.tmdb_api.get_episode_list(tmdb_id, try_int(fallback_season) or 1, get_detailed=True)
            items = list(items) if items else []
        self.kodi_db = self.get_kodi_database(tmdb_type)
        self.library = 'video'
        self.container_content = 'episodes'
        self.plugin_category = get_localized(32043)
        return items


class ListGenres(ContainerDirectory):
    def get_items(self, info, tmdb_type, **kwargs):
        items = self.trakt_api.get_list_of_genres(convert_type(tmdb_type, 'trakt'))
        self.library = 'video'
        self.plugin_category = get_localized(135)
        return items


class ListLists(ContainerDirectory):
    def get_items(self, info, page=None, **kwargs):
        from xbmcplugin import SORT_METHOD_UNSORTED
        from tmdbhelper.lib.addon.consts import TRAKT_LIST_OF_LISTS

        info_model = TRAKT_LIST_OF_LISTS.get(info)

        if info_model.get('get_trakt_id'):
            kwargs['trakt_type'] = {'movie': 'movie', 'tv': 'show'}[kwargs['tmdb_type']]
            kwargs['trakt_id'] = self.trakt_api.get_id(kwargs['tmdb_id'], 'tmdb', trakt_type=kwargs['trakt_type'], output_type='trakt')

        items = self.trakt_api.get_list_of_lists(
            path=info_model.get('path', '').format(**kwargs),
            page=page,
            authorize=info_model.get('authorize', False))

        self.library = 'video'
        self.plugin_category = get_plugin_category(info_model)
        self.sort_methods = [{'sortMethod': SORT_METHOD_UNSORTED, 'label2Mask': '%U'}]  # Label2 Mask by Studio (i.e. User Name)
        return items


class ListCustom(ContainerDirectory):
    def get_items(
            self, list_slug, user_slug=None, page=None,
            **kwargs
    ):
        from jurialmunkey.parser import boolean
        sort_by, sort_how, extended = kwargs.get('sort_by', None), kwargs.get('sort_how', None), kwargs.get('extended', None)
        if not sort_by:  # no sort in the path: the global sort, if one is chosen
            from tmdbhelper.lib.items.globalsort import get_global_sort
            sort = get_global_sort()
            if sort:
                sort_by, sort_how = sort
                extended = extended or ('full' if sort_by == 'released' else None)  # release dates come with extended=full
        response = self.trakt_api.get_custom_list(
            page=page or 1,
            list_slug=list_slug,
            user_slug=user_slug,
            sort_by=sort_by,
            sort_how=sort_how,
            extended=extended,
            authorize=False if user_slug and not boolean(kwargs.get('owner', False)) else True,
            always_refresh=True if not get_setting('trakt_cacheownlists') and boolean(kwargs.get('owner', False)) else False)
        if not response:
            return []
        self.tmdb_cache_only = False
        self.set_mixed_content(response)
        return response.get('items', []) + response.get('next_page', [])


class ListCustomSearch(ContainerDirectory):
    def get_items(self, query=None, **kwargs):
        from xbmcgui import Dialog
        if not query:
            kwargs['query'] = query = Dialog().input(get_localized(32044))
            if not kwargs['query']:
                return
            self.container_update = f'{encode_url(PLUGINPATH, **kwargs)},replace'
        items = self.trakt_api.get_list_of_lists(path=f'search/list?query={query}&fields=name', sort_likes=True, authorize=False)
        self.library = 'video'
        return items


class ListUserLists(ContainerDirectory):
    """Browse another Trakt user's public lists (GET users/{username}/lists)."""

    def get_items(self, username=None, page=None, **kwargs):
        if not username:
            from xbmcgui import Dialog
            kwargs['username'] = username = Dialog().input(get_localized(32573))
            if not username:
                return
            self.container_update = f'{encode_url(PLUGINPATH, **kwargs)},replace'
        from urllib.parse import quote
        from xbmcplugin import SORT_METHOD_UNSORTED
        items = self.trakt_api.get_list_of_lists(path=f"users/{quote(username, safe='')}/lists", page=page or 1, authorize=False)
        self.library = 'video'
        self.plugin_category = f'{get_localized(32573)}: {username}'
        self.sort_methods = [{'sortMethod': SORT_METHOD_UNSORTED, 'label2Mask': '%U'}]  # Label2 Mask by Studio (i.e. User Name)
        return items


class ListSortBy(ContainerDirectory):
    def get_items(self, info, **kwargs):
        from tmdbhelper.lib.api.trakt.sorting import get_sort_methods
        from tmdbhelper.lib.api.mapping import get_empty_item

        def _listsortby_item(i, **params):
            item = get_empty_item()
            item['label'] = item['infolabels']['title'] = f'{params.get("list_name")} - {i["name"]}'
            item['params'] = params
            for k, v in i['params'].items():
                item['params'][k] = v
            return item

        kwargs['info'] = kwargs.pop('parent_info', None)
        items = get_sort_methods(kwargs['info'])
        items = [_listsortby_item(i, **kwargs) for i in items]
        self.library = 'video'
        return items
