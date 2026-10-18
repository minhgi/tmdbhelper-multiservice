from tmdbhelper.lib.addon.tmdate import get_timedelta, get_datetime_today
from tmdbhelper.lib.addon.plugin import ADDONPATH, PLUGINPATH, convert_type, get_localized, ADDON, get_flatseasons_info_param, get_setting
from jurialmunkey.parser import merge_two_items
from tmdbhelper.lib.items.builder import ItemBuilder
from tmdbhelper.lib.items.container import ContainerDirectory
from tmdbhelper.lib.items.basedir_plots import add_descriptions
from tmdbhelper.lib.addon.consts import TVDB_DISCLAIMER, NODE_BASEDIR


def _build_basedir_item(i, t, space):
    item = i.copy()
    item['label'] = item['label'].format(space=space, item_type=convert_type(t, 'plural') if space else '')
    item['params'] = i.get('params', {}).copy()
    item['params']['tmdb_type'] = t
    if item.pop('sorting', False):
        item.setdefault('infoproperties', {})['is_sortable'] = 'True'
        item['context_menu'] = [(
            get_localized(32309),
            u'Runscript(plugin.video.themoviedb.helper,sort_list,{})'.format(
                u','.join(f'{k}={v}' for k, v in item['params'].items())))]
        item['params']['list_name'] = item['label']
    item.pop('types', None)
    item.pop('filters', None)
    return item


def _build_basedir(item_type=None, basedir=None):
    if not basedir:
        return []
    space = '' if item_type else ' '  # Type not added to label when single type so dont need spaces
    return [
        _build_basedir_item(i, t, space)
        for i in basedir for t in i.get('types', [])
        if not item_type or item_type == t]


def _get_basedir_progress(basedir, infos, progress_filter=None):
    """Anime / Asian Progress: the four progress rows of a service menu (In-Progress Movies / TV shows / Combined, Next Up)
    limited to anime or to Asian titles."""
    if progress_filter not in ('anime', 'asian', 'other'):
        return []
    rows = []
    for i in basedir:
        if i['params'].get('info') in infos:
            i = i.copy()
            i['params'] = dict(i['params'], **{f'{progress_filter}_only': 'true'})
            rows.append(i)
    return _build_basedir(None, rows)


def _get_basedir_mdblist_progress(progress_filter=None):
    return _get_basedir_progress(_get_basedir_mdblist(all_labels=False), ('mdblist_upnext', 'mdblist_nextup'), progress_filter)


def _get_basedir_trakt_progress(progress_filter=None):
    return _get_basedir_progress(_get_basedir_trakt(all_labels=False), ('trakt_inprogress', 'trakt_ondeck', 'trakt_ondeck_unwatched', 'trakt_nextepisodes'), progress_filter)


def _get_basedir_simkl_progress(progress_filter=None):
    return _get_basedir_progress(_get_basedir_simkl(all_labels=False), ('simkl_upnext', 'simkl_nextup'), progress_filter)


# A main-menu row shows only while its switch is on: a watch service in Settings > Accounts, Movies / TV Shows / Anime / Asian in Settings > Navigation > Main menu
SERVICE_MENUS = {'dir_trakt': 'service_trakt', 'dir_mdblist': 'service_mdblist', 'dir_simkl': 'service_simkl',
                 'dir_movie': 'menu_movies', 'dir_tv': 'menu_tv', 'dir_anime_hub': 'menu_anime', 'dir_asian_hub': 'menu_asian'}   # main-menu rows and the switch (Settings) that shows each


def _get_basedir_list(item_type=None, trakt=False, tmdb=False, mdblist=False, tvdb=False, simkl=False):
    basedir = []
    trakt, mdblist, simkl = (trakt and get_setting('service_trakt')), (mdblist and get_setting('service_mdblist')), (simkl and get_setting('service_simkl'))
    if tmdb:
        tmdb_items = _get_basedir_tmdb()
        if item_type in ('movie', 'tv'):
            # Movies / TV Shows menus: Search sits after Most Voted instead of first (People and the combined menus keep it first)
            search = next((i for i in tmdb_items if i['params'].get('info') == 'dir_search'), None)
            voted = next((n for n, i in enumerate(tmdb_items) if i['params'].get('info') == 'most_voted'), None)
            if search is not None and voted is not None:
                tmdb_items.insert(voted + 1, search)   # inserted after Most Voted first, so the index is still valid
                tmdb_items.remove(search)              # ...then drop the original (first) entry; remove() takes the first match
        basedir += tmdb_items
    if trakt:
        basedir += _get_basedir_trakt()
    if mdblist:
        basedir += _get_basedir_mdblist()
    if tvdb:
        basedir += _get_basedir_tvdb()
    if simkl:
        basedir += _get_basedir_simkl()
    return _build_basedir(item_type, basedir)


def _get_play_item():
    return [
        {
            'label': get_localized(208),
            'params': {'info': 'play'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/default.png'},
            'types': ['movie', 'episode']}]


def _get_basedir_details():
    return [
        {
            'label': get_localized(33054),
            'params': {'info': get_flatseasons_info_param()},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/episodes.png'},
            'types': ['tv', 'episode']},
        {
            'label': get_localized(32192),
            'params': {'info': 'collection'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/movies.png'},
            'types': ['movie']},
        {
            'label': get_localized(20360),
            'params': {'info': 'episodes'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/episodes.png'},
            'types': ['episode']},
        {
            'label': get_localized(206),
            'params': {'info': 'cast'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/cast.png'},
            'types': ['movie', 'tv', 'episode']},
        {
            'label': get_localized(32223),
            'params': {'info': 'recommendations'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/recommended.png'},
            'types': ['movie', 'tv', 'episode']},
        {
            'label': 'Anime Recommendations',   # shown only for anime (get_basedir_details)
            'params': {'info': 'anime_recommendations'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/recommended.png'},
            'types': ['movie', 'tv', 'episode']},
        {
            'label': get_localized(32224),
            'params': {'info': 'similar'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/similar.png'},
            'types': ['movie', 'tv', 'episode']},
        {
            'label': get_localized(32225),
            'params': {'info': 'crew'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/cast.png'},
            'types': ['movie', 'tv', 'episode']},
        {
            'label': get_localized(32226),
            'params': {'info': 'posters'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/images.png'},
            'types': ['movie', 'tv', 'episode']},
        {
            'label': get_localized(20445),
            'params': {'info': 'fanart'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/images.png'},
            'types': ['movie', 'tv', 'episode']},
        {
            'label': get_localized(21861),
            'params': {'info': 'movie_keywords'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/tags.png'},
            'types': ['movie']},
        {
            'label': get_localized(32188),
            'params': {'info': 'reviews'},
            'path': PLUGINPATH,
            'art': {
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/reviews.png',
                'landscape': f'{ADDONPATH}/fanart.jpg'},
            'types': ['movie', 'tv', 'episode']},
        {
            'label': get_localized(32227),
            'params': {'info': 'stars_in_movies'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/movies.png'},
            'types': ['person']},
        {
            'label': get_localized(32228),
            'params': {'info': 'stars_in_tvshows'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/tv.png'},
            'types': ['person']},
        {
            'label': get_localized(32229),
            'params': {'info': 'crew_in_movies'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/movies.png'},
            'types': ['person']},
        {
            'label': get_localized(32230),
            'params': {'info': 'crew_in_tvshows'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/tv.png'},
            'types': ['person']},
        {
            'label': get_localized(32191),
            'params': {'info': 'images'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/images.png'},
            'types': ['person']},
        {
            'label': get_localized(32231),
            'params': {'info': 'episode_thumbs'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/images.png'},
            'types': ['episode']},
        {
            'label': get_localized(10025),
            'params': {'info': 'videos'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/movies.png'},
            'types': ['movie', 'tv', 'episode']},
        {
            'label': get_localized(32345),
            'params': {'info': 'episode_groups'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/episodes.png'},
            'types': ['tv']},
        {
            'label': get_localized(32232),
            'params': {'info': 'trakt_inlists'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/trakt.png'},
            'types': ['movie', 'tv', 'episode']}]


def _get_basedir_random():
    return [
        {
            'label': u'{} {{item_type}}{{space}}{}'.format(
                get_localized(590), get_localized(515)),
            'types': ['movie', 'tv'],
            'params': {'info': 'random_genres'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/genre.png'}},
        {
            'label': u'{} {{item_type}}{{space}}{}'.format(
                get_localized(590), get_localized(32117)),
            'types': ['movie'],
            'params': {'info': 'random_keyword'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/default.png'}},
        {
            'label': u'{}{{space}}{{item_type}}'.format(get_localized(32199)),
            'types': ['movie', 'tv'],
            'params': {'info': 'trakt_becauseyouwatched'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/recommended.png'}},
        {
            'label': u'{}{{space}}{{item_type}}'.format(get_localized(32200)),
            'types': ['movie', 'tv'],
            'params': {'info': 'trakt_becausemostwatched'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/recommended.png'}},
        {
            'label': u'{} {}{{space}}{{item_type}}'.format(
                get_localized(590), get_localized(32204)),
            'types': ['movie', 'tv', 'both'],
            'params': {'info': 'random_trending'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/trend.png'}},
        {
            'label': u'{} {}{{space}}{{item_type}}'.format(
                get_localized(590), get_localized(32175)),
            'types': ['movie', 'tv', 'both'],
            'params': {'info': 'random_popular'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/popular.png'}},
        {
            'label': u'{} {}{{space}}{{item_type}}'.format(
                get_localized(590), get_localized(32205)),
            'types': ['movie', 'tv', 'both'],
            'params': {'info': 'random_mostplayed'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/mostplayed.png'}},
        {
            'label': u'{} {}{{space}}{{item_type}}'.format(
                get_localized(590), get_localized(32414)),
            'types': ['movie', 'tv', 'both'],
            'params': {'info': 'random_mostviewers'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/mostplayed.png'}},
        {
            'label': u'{} {}{{space}}{{item_type}}'.format(
                get_localized(590), get_localized(32206)),
            'types': ['movie', 'tv', 'both'],
            'params': {'info': 'random_anticipated'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/anticipated.png'}},
        {
            'label': u'{} {}{{space}}{{item_type}}'.format(
                get_localized(590), get_localized(32300)),
            'types': ['list'],
            'params': {'info': 'random_trendinglists'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/trendinglist.png'}},
        {
            'label': u'{} {}{{space}}{{item_type}}'.format(
                get_localized(590), get_localized(32301)),
            'types': ['list'],
            'params': {'info': 'random_popularlists'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/popularlist.png'}},
        {
            'label': u'{} {}{{space}}{{item_type}}'.format(
                get_localized(590), get_localized(32302)),
            'types': ['list'],
            'params': {'info': 'random_likedlists'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/likedlist.png'}},
        {
            'label': u'{} {}{{space}}{{item_type}}'.format(
                get_localized(590), get_localized(32303)),
            'types': ['list'],
            'params': {'info': 'random_mylists'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/mylists.png'}}
    ]


def _get_basedir_trakt_genre_types(genre, tmdb_type):
    items = []

    endpoints = [d for d in _get_basedir_trakt() if d.get('filters')]

    for i in endpoints:
        item = {}
        item['label'] = i['label'].format(space=' ', item_type=f'{genre.capitalize()} {convert_type(tmdb_type, "plural")}')
        item['infolabels'] = {}
        item['infoproperties'] = {}
        item['params'] = i.get('params', {}).copy()
        item['params']['tmdb_type'] = tmdb_type
        item['params']['genres'] = genre
        item['art'] = i.get('art', {}).copy()
        item['unique_ids'] = {'slug': i.get('slug')}
        items.append(item)

    return items


def _get_basedir_nodes(filename=None, basedir=None):
    from json import loads
    from tmdbhelper.lib.files.futils import get_files_in_folder, read_file

    basedir = basedir or NODE_BASEDIR
    files = get_files_in_folder(basedir, r'.*\.json')

    if not files:
        return []

    def _get_node(file):
        data = read_file(basedir + file)
        meta = loads(data) or {}
        if not meta:
            return
        return {
            'label': meta.get('name') or '',
            'types': ['both'],
            'params': {'info': 'dir_custom_node', 'filename': file, 'basedir': basedir},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': meta.get('icon') or ''}}

    if not filename:
        return [i for i in (_get_node(file) for file in files) if i]

    if filename not in files:
        return []

    def _get_item(item):
        if not item:
            return
        return {
            'label': item.get('name') or '',
            'path': item.get('path') or PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': item.get('icon') or ''}}
    try:
        data = read_file(basedir + filename)
        meta = loads(data) or {}
        return [i for i in (_get_item(item) for item in meta['list']) if i]
    except (KeyError, TypeError):
        return []


def _get_basedir_tvdb():
    return [
        {
            'label': get_localized(32460),
            'types': ['both'],
            'params': {'info': 'dir_tvdb_awards'},
            'path': PLUGINPATH,
            'infolabels': {'plot': TVDB_DISCLAIMER},
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/tvdb/tvdb.png'}},
        {
            'label': u'{{item_type}}{{space}}{}'.format(get_localized(135)),
            'types': ['movie', 'tv'],
            'params': {'info': 'dir_tvdb_genres'},
            'path': PLUGINPATH,
            'infolabels': {'plot': TVDB_DISCLAIMER},
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/tvdb/tvdb.png'}},
    ]


def _get_basedir_mdblist(all_labels=True):
    # The MDBList menu calls its unfiltered progress rows "All ..." (they sit above the Movies and TV Shows / Anime / Asian menus);
    # the submenus of those (all_labels=False) keep the plain progress names.
    return [
        {
            'label': u'All In-Progress{space}{item_type}' if all_labels else u'{}{{space}}{{item_type}}'.format(get_localized(32196)),
            'types': ['movie', 'tv', 'both'],
            'params': {'info': 'mdblist_upnext'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/mdblist/inprogress.png'}},
        {
            'label': u'All {}'.format(get_localized(32572)) if all_labels else get_localized(32572),
            'types': ['tv'],
            'params': {'info': 'mdblist_nextup'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/mdblist/nextup.png'}},
        {
            'label': 'Movies and TV Shows Progress',
            'types': ['tv'],
            'params': {'info': 'dir_mdblist_progress', 'progress_filter': 'other'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/mdblist/nextup.png'}},
        {
            'label': 'Anime Progress',
            'types': ['tv'],
            'params': {'info': 'dir_mdblist_progress', 'progress_filter': 'anime'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/mdblist/nextup.png'}},
        {
            'label': 'Asian Progress',
            'types': ['tv'],
            'params': {'info': 'dir_mdblist_progress', 'progress_filter': 'asian'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/mdblist/nextup.png'}},
        {
            'label': u'{}{{space}}{{item_type}}'.format(get_localized(32193)),
            'types': ['movie', 'tv', 'both'],
            'params': {'info': 'mdblist_watchlist'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/mdblist/watchlist.png'}},
        {
            'label': u'{}{{space}}{{item_type}}'.format(get_localized(32194)),
            'types': ['movie', 'tv', 'both'],
            'params': {'info': 'mdblist_history'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/mdblist/recentlywatched.png'}},
        {
            'label': u'{}{{space}}{{item_type}}'.format(get_localized(32567)),
            'types': ['movie', 'tv', 'both'],
            'params': {'info': 'mdblist_collection'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/mdblist/collection.png'}},
        {
            'label': get_localized(32568),
            'types': ['tv'],
            'params': {'info': 'mdblist_dropped'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/mdblist/dropped.png'}},
        {
            'label': get_localized(32211),
            'types': ['both'],
            'params': {'info': 'mdblist_yourlists'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/mdblist/mdblist.png'}},
        {
            'label': get_localized(32210),
            'types': ['both'],
            'params': {'info': 'mdblist_likedlists'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/mdblist/likedlists.png'}},
        {
            'label': get_localized(32421),
            'types': ['both'],
            'params': {'info': 'mdblist_toplists'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/mdblist/mdblist.png'}},
        {
            'label': get_localized(32361),
            'types': ['both'],
            'params': {'info': 'mdblist_searchlists'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/mdblist/mdblist.png'}},
        {
            'label': get_localized(32573),
            'types': ['both'],
            'params': {'info': 'mdblist_userlists'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/mdblist/userlists.png'}}]


def _get_basedir_simkl(all_labels=True):
    # As in the MDBList menu: the unfiltered progress rows are "All ..."; the submenus (all_labels=False) keep the plain names.
    return [
        {
            'label': u'All In-Progress{space}{item_type}' if all_labels else u'In-Progress{space}{item_type}',
            'types': ['movie', 'tv', 'both'],
            'params': {'info': 'simkl_upnext'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/simkl/inprogress.png'}},
        {
            'label': u'All {}'.format(get_localized(32572)) if all_labels else get_localized(32572),
            'types': ['tv'],
            'params': {'info': 'simkl_nextup'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/simkl/nextup.png'}},
        {
            'label': 'Movies and TV Shows Progress',
            'types': ['tv'],
            'params': {'info': 'dir_simkl_progress', 'progress_filter': 'other'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/simkl/nextup.png'}},
        {
            'label': 'Anime Progress',
            'types': ['tv'],
            'params': {'info': 'dir_simkl_progress', 'progress_filter': 'anime'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/simkl/nextup.png'}},
        {
            'label': 'Asian Progress',
            'types': ['tv'],
            'params': {'info': 'dir_simkl_progress', 'progress_filter': 'asian'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/simkl/nextup.png'}},
        {
            'label': u'Watching',
            'types': ['tv'],
            'params': {'info': 'simkl_watching'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/simkl/watching.png'}},
        {
            'label': u'Plan to Watch{space}{item_type}',
            'types': ['movie', 'tv', 'both'],
            'params': {'info': 'simkl_plantowatch'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/simkl/plantowatch.png'}},
        {
            'label': u'Recently Watched{space}{item_type}',
            'types': ['movie', 'tv', 'both'],
            'params': {'info': 'simkl_history'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/simkl/recentlywatched.png'}},
        {
            'label': u'On Hold',
            'types': ['tv'],
            'params': {'info': 'simkl_hold'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/simkl/onhold.png'}},
        {
            'label': u'Dropped{space}{item_type}',
            'types': ['movie', 'tv', 'both'],
            'params': {'info': 'simkl_dropped'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/simkl/dropped.png'}}]


def _get_basedir_trakt(all_labels=True):
    """The Trakt menu. all_labels: the unfiltered progress rows read "All ..." (Your In-Progress Movies -> All In-Progress Movies,
    Your Next Episodes -> All Next Episodes), as in the MDBList / Simkl menus; the Anime / Asian / Movies and TV Shows submenus
    (all_labels=False) keep the plain names."""
    rows = _get_basedir_trakt_rows()
    if all_labels:
        for i in rows:
            if i['params'].get('info') in ('trakt_inprogress', 'trakt_ondeck', 'trakt_ondeck_unwatched', 'trakt_nextepisodes') and i['label'].startswith('Your '):
                i['label'] = 'All ' + i['label'][len('Your '):]
    return rows


def _get_basedir_trakt_rows():
    return [
        {
            'label': u'{{item_type}}{{space}}{}'.format(get_localized(32192)),
            'types': ['movie', 'tv', 'both'],
            'params': {'info': 'trakt_collection'},
            'path': PLUGINPATH,
            'sorting': True,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/watchlist.png'}},
        {
            'label': u'{{item_type}}{{space}}{}'.format(get_localized(1036)),
            'types': ['movie', 'tv', 'both'],
            'params': {'info': 'trakt_favorites'},
            'path': PLUGINPATH,
            'sorting': True,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/watchlist.png'}},
        {
            'label': u'{{item_type}}{{space}}{}'.format(get_localized(32193)),
            'types': ['movie', 'tv', 'season', 'episode', 'both'],
            'params': {'info': 'trakt_watchlist'},
            'path': PLUGINPATH,
            'sorting': True,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/watchlist.png'}},
        {
            'label': u'{{item_type}}{{space}}{}'.format(get_localized(32456)),
            'types': ['movie', 'tv', 'season', 'episode', 'both'],
            'params': {'info': 'trakt_watchlist_released'},
            'path': PLUGINPATH,
            'sorting': True,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/watchlist.png'}},
        {
            'label': u'{{item_type}}{{space}}{}'.format(get_localized(32457)),
            'types': ['movie', 'tv', 'season', 'episode', 'both'],
            'params': {'info': 'trakt_watchlist_anticipated'},
            'path': PLUGINPATH,
            'sorting': True,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/watchlist.png'}},
        {
            'label': u'{}{{space}}{{item_type}}'.format(get_localized(32194)),
            'types': ['movie', 'tv', 'both'],
            'params': {'info': 'trakt_history'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/recentlywatched.png'}},
        {
            'label': get_localized(32568),
            'types': ['tv'],
            'params': {'info': 'trakt_dropped'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/watchlist.png'}},
        {
            'label': u'{}{{space}}{{item_type}}'.format(get_localized(32195)),
            'types': ['movie', 'tv'],
            'params': {'info': 'trakt_mostwatched'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/mostwatched.png'}},
        {
            'label': u'{}{{space}}{{item_type}}'.format(get_localized(32196)),
            'types': ['movie', 'tv'],
            'params': {'info': 'trakt_inprogress'},
            'path': PLUGINPATH,
            'sorting': True,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/inprogress.png'}},
        {
            'label': get_localized(32406),
            'types': ['tv'],
            'params': {'info': 'trakt_ondeck'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/inprogress.png'}},
        {
            'label': u'{}{{space}}{{item_type}} ({})'.format(get_localized(32196), get_localized(16101)),
            'types': ['movie'],
            'params': {'info': 'trakt_ondeck_unwatched'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/inprogress.png'}},
        {
            'label': f'{get_localized(32406)} ({get_localized(16101)})',
            'types': ['tv'],
            'params': {'info': 'trakt_ondeck_unwatched'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/inprogress.png'}},
        {
            'label': 'Movies and TV Shows Progress',
            'types': ['tv'],
            'params': {'info': 'dir_trakt_progress', 'progress_filter': 'other'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/inprogress.png'}},
        {
            'label': 'Anime Progress',
            'types': ['tv'],
            'params': {'info': 'dir_trakt_progress', 'progress_filter': 'anime'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/inprogress.png'}},
        {
            'label': 'Asian Progress',
            'types': ['tv'],
            'params': {'info': 'dir_trakt_progress', 'progress_filter': 'asian'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/inprogress.png'}},
        {
            'label': u'{{item_type}}{{space}}{}'.format(get_localized(32078)),
            'types': ['movie', 'tv'],
            'params': {'info': 'trakt_towatch'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/watchlist.png'}},
        {
            'label': get_localized(32197),
            'types': ['tv'],
            'params': {'info': 'trakt_nextepisodes'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/inprogress.png'}},
        {
            'label': u'{{item_type}}{{space}}{}'.format(get_localized(32198)),
            'types': ['movie', 'tv'],
            'params': {'info': 'trakt_recommendations'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/recommended.png'}},
        {
            'label': u'{}{{space}}{{item_type}}'.format(get_localized(32199)),
            'types': ['movie', 'tv'],
            'params': {'info': 'trakt_becauseyouwatched'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/recommended.png'}},
        {
            'label': u'{}{{space}}{{item_type}}'.format(get_localized(32200)),
            'types': ['movie', 'tv'],
            'params': {'info': 'trakt_becausemostwatched'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/recommended.png'}},
        {
            'label': u'{} {{item_type}}{{space}}{}'.format(get_localized(32201), get_localized(32202)),
            'types': ['tv'],
            'params': {'info': 'trakt_myairing'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/airing.png'}},
        {
            'label': get_localized(32459),
            'types': ['tv'],
            'params': {'info': 'trakt_airingnext'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/calendar.png'}},
        {
            'label': u'{} {{item_type}}{{space}}{}'.format(get_localized(32201), get_localized(32203)),
            # One scrollable page of the shows you follow (a window) instead of the day-bucket list; the list stays reachable by its path (widgets)
            'types': ['tv'],
            'is_folder': False,
            'params': {'info': 'dir_calendar_shows_window', 'window_title': 'Your TV Calendar'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/calendar.png'}},
        {
            'label': u'{} {{item_type}}{{space}}{} {}'.format(get_localized(32201), get_localized(32203), get_localized(32416)),
            'types': ['tv'],
            'is_folder': False,
            'params': {'info': 'dir_calendar_shows_window', 'endpoint': 'premieres', 'window_title': 'Your TV Premieres'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/calendar.png'}},
        {
            'label': u'{} {{item_type}}{{space}}{} {}'.format(get_localized(32201), get_localized(32203), get_localized(842)),
            'types': ['tv'],
            'is_folder': False,
            'params': {'info': 'dir_calendar_shows_window', 'endpoint': 'new', 'window_title': 'Your New TV Shows'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/calendar.png'}},
        {
            'label': u'{} {{item_type}}{{space}}{}'.format(get_localized(32186), get_localized(32203)),
            'types': ['tv'],
            'is_folder': False,
            'params': {'info': 'dir_calendar_shows_window', 'user': 'false', 'window_title': 'All TV Calendar'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/calendar.png'}},
        {
            'label': u'{} {{item_type}}{{space}}{} {}'.format(get_localized(32186), get_localized(32203), get_localized(32416)),
            'types': ['tv'],
            'is_folder': False,
            'params': {'info': 'dir_calendar_shows_window', 'endpoint': 'premieres', 'user': 'false', 'window_title': 'All TV Premieres'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/calendar.png'}},
        {
            'label': u'{} {{item_type}}{{space}}{} {}'.format(get_localized(32186), get_localized(32203), get_localized(842)),
            'types': ['tv'],
            'is_folder': False,
            'params': {'info': 'dir_calendar_shows_window', 'endpoint': 'new', 'user': 'false', 'window_title': 'All New TV Shows'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/calendar.png'}},
        {
            'label': u'{} {{item_type}}{{space}}{}'.format(get_localized(32201), get_localized(32203)),
            # Month poster-grid windows (Seren style) instead of the day-bucket lists; the lists stay reachable by their paths (widgets)
            'types': ['movie'],
            'is_folder': False,
            'params': {'info': 'dir_calendar_window', 'window_title': 'Your Movies Calendar'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/calendar.png'}},
        {
            'label': u'{} {{item_type}}{{space}}{} {}'.format(get_localized(32201), get_localized(32203), 'DVD'),
            'types': ['movie'],
            'is_folder': False,
            'params': {'info': 'dir_calendar_window', 'endpoint': 'dvd', 'window_title': 'Your Movies Calendar DVD'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/calendar.png'}},
        {
            'label': u'{} {{item_type}}{{space}}{}'.format(get_localized(32186), get_localized(32203)),
            'types': ['movie'],
            'is_folder': False,
            'params': {'info': 'dir_calendar_window', 'user': 'false', 'window_title': 'All Movies Calendar'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/calendar.png'}},
        {
            'label': u'{} {{item_type}}{{space}}{} {}'.format(get_localized(32186), get_localized(32203), 'DVD'),
            'types': ['movie'],
            'is_folder': False,
            'params': {'info': 'dir_calendar_window', 'endpoint': 'dvd', 'user': 'false', 'window_title': 'All Movies Calendar DVD'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/calendar.png'}},
        {
            'label': u'{{item_type}}{{space}}{}'.format(get_localized(135)),
            'types': ['movie', 'tv'],
            'params': {'info': 'trakt_genres'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/genres.png'}},
        {
            'label': u'{}{{space}}{{item_type}}'.format(get_localized(32204)),
            'types': ['movie', 'tv'],
            'params': {'info': 'trakt_trending'},
            'filters': True,
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/trend.png'}},
        {
            'label': u'{}{{space}}{{item_type}}'.format(get_localized(32175)),
            'types': ['movie', 'tv'],
            'params': {'info': 'trakt_popular'},
            'filters': True,
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/popular.png'}},
        {
            'label': u'{}{{space}}{{item_type}}'.format(get_localized(32205)),
            'types': ['movie', 'tv'],
            'params': {'info': 'trakt_mostplayed'},
            'filters': True,
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/mostplayed.png'}},
        {
            'label': u'{}{{space}}{{item_type}}'.format(get_localized(32414)),
            'types': ['movie', 'tv'],
            'params': {'info': 'trakt_mostviewers'},
            'filters': True,
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/mostplayed.png'}},
        {
            'label': u'{}{{space}}{{item_type}}'.format(get_localized(32206)),
            'types': ['movie', 'tv'],
            'params': {'info': 'trakt_anticipated'},
            'filters': True,
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/anticipated.png'}},
        {
            'label': u'{}{{space}}{{item_type}}'.format(get_localized(32207)),
            'types': ['movie'],
            'params': {'info': 'trakt_boxoffice'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/boxoffice.png'}},
        {
            'label': get_localized(32208),
            'types': ['both'],
            'params': {'info': 'trakt_trendinglists'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/trendinglist.png'}},
        {
            'label': get_localized(32209),
            'types': ['both'],
            'params': {'info': 'trakt_popularlists'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/popularlist.png'}},
        {
            'label': get_localized(32210),
            'types': ['both'],
            'params': {'info': 'trakt_likedlists'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/likedlist.png'}},
        {
            'label': get_localized(32211),
            'types': ['both'],
            'params': {'info': 'trakt_mylists'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/mylists.png'}},
        {
            'label': get_localized(32361),
            'types': ['both'],
            'params': {'info': 'trakt_searchlists'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/mylist.png'}},
        {
            'label': get_localized(32573),
            'types': ['both'],
            'params': {'info': 'trakt_userlists'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/trakt/mylists.png'}}]


def _get_basedir_tmdb():
    return [
        {
            'label': u'{}{{space}}{{item_type}}'.format(get_localized(137)),
            'types': ['movie', 'tv', 'person'],
            'params': {'info': 'dir_search'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/search.png'}},
        {
            'label': u'{}{{space}}{{item_type}}'.format(get_localized(32175)),
            'types': ['movie', 'tv', 'person'],
            'params': {'info': 'popular'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/popular.png'}},
        {
            'label': u'{}{{space}}{{item_type}}'.format(get_localized(32176)),
            'types': ['movie', 'tv'],
            'params': {'info': 'top_rated'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/toprated.png'}},
        {
            'label': u'{}{{space}}{{item_type}}'.format(get_localized(32177)),
            'types': ['movie'],
            'params': {'info': 'upcoming'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/upcoming.png'}},
        {
            'label': u'{{item_type}}{{space}}{}'.format(get_localized(32178)),
            'types': ['movie', 'tv', 'person'],
            'params': {'info': 'trending_day'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/upcoming.png'}},
        {
            'label': u'{{item_type}}{{space}}{}'.format(get_localized(32179)),
            'types': ['movie', 'tv', 'person'],
            'params': {'info': 'trending_week'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/upcoming.png'}},
        {
            'label': get_localized(32180),
            'types': ['movie'],
            'params': {'info': 'now_playing'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/intheatres.png'}},
        {
            'label': get_localized(32181),
            'types': ['tv'],
            'params': {'info': 'airing_today'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/airing.png'}},
        {
            'label': get_localized(32182),
            'types': ['tv'],
            'params': {'info': 'on_the_air'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/airing.png'}},
        {
            'label': get_localized(32183),
            'types': ['tv'],
            'params': {'info': 'dir_calendar_library'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/airing.png'}},
        {
            'label': get_localized(32458),
            'types': ['tv'],
            'params': {'info': 'library_airingnext'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/airing.png'}},
        {
            'label': u'{{item_type}}{{space}}{}'.format(get_localized(135)),
            'types': ['movie', 'tv'],
            'params': {'info': 'genres'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/genre.png'}},
        {
            'label': u'{{item_type}}{{space}}{}'.format(get_localized(32411)),
            'types': ['movie', 'tv'],
            'params': {'info': 'watch_providers'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/airing.png'}},
        {
            'label': u'{}{{space}}{{item_type}}'.format(get_localized(32184)),
            'types': ['movie'],
            'params': {'info': 'revenue_movies'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/default.png'}},
        {
            'label': u'{}{{space}}{{item_type}}'.format(get_localized(32185)),
            'types': ['movie', 'tv'],
            'params': {'info': 'most_voted'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/default.png'}},
        {
            'label': u'{}{{space}}{{item_type}}'.format(get_localized(32186)),
            'types': ['collection', 'keyword', 'network', 'studio'],
            'params': {'info': 'all_items'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/default.png'}}]


def _get_basedir_main():
    return [i for i in _get_basedir_main_rows() if not (SERVICE_MENUS.get(i['params'].get('info')) and not get_setting(SERVICE_MENUS[i['params']['info']]))]


def _get_basedir_main_rows():
    return [
        {
            'label': get_localized(342),
            'types': [None],
            'params': {'info': 'dir_movie'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/movies.png'}},
        {
            'label': get_localized(20343),
            'types': [None],
            'params': {'info': 'dir_tv'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/tv.png'}},
        {
            'label': u'Anime',
            'types': [None],
            'params': {'info': 'dir_anime_hub'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/tv.png'}},
        {
            'label': u'Asian Cinema & TV',
            'types': [None],
            'params': {'info': 'dir_asian_hub'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/tv.png'}},
        {
            'label': get_localized(32172),
            'types': [None],
            'params': {'info': 'dir_person'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/cast.png'}},
        {
            'label': get_localized(137),
            'types': [None],
            'params': {'info': 'dir_multisearch'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/search.png'}},
        {
            'label': get_localized(32174),
            'types': [None],
            'params': {'info': 'dir_discover'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/discover.png'}},
        {
            'label': get_localized(32173),
            'types': [None],
            'params': {'info': 'dir_random'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/randomise.png'}},
        {
            'label': u'TheMovieDb',
            'types': [None],
            'params': {'info': 'dir_tmdb'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/default.png'}},
        {
            'label': u'TMDb User',
            'types': [None],
            'params': {'info': 'dir_tmdb_v4'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/default.png'}},
        {
            'label': u'Trakt',
            'types': [None],
            'params': {'info': 'dir_trakt'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/trakt.png'}},
        {
            'label': u'TVDb',
            'types': [None],
            'params': {'info': 'dir_tvdb'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/tvdb/tvdb.png'}},
        {
            'label': u'MDbList',
            'types': [None],
            'params': {'info': 'dir_mdblist'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/mdblist/mdblist.png'}},
        {
            'label': u'Simkl',
            'types': [None],
            'params': {'info': 'dir_simkl'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/simkl/simkl.png'}},
        {
            'label': u'Nodes',
            'types': [None],
            'params': {'info': 'dir_custom_node'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/default.png'}},
        {
            'label': u'Settings',
            'types': [None],
            'params': {'info': 'dir_settings'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/settings.png'}},
        {
            'label': u'Tools',
            'types': [None],
            'params': {'info': 'dir_tools'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/default.png'}}]


def _anime_calendar_item(movie=False):
    # Trakt's calendar filtered to its anime genre (the other anime rows come from AniList); movies are limited to Japan
    params = {'info': 'dir_calendar_shows_window', 'user': 'false', 'genres': 'anime', 'window_title': 'Anime TV Calendar'}   # the TV calendar is the one-day show page
    if movie:   # the movie calendar is the month poster-grid window (Japan only)
        params = {'info': 'dir_calendar_window', 'user': 'false', 'genres': 'anime', 'countries': 'jp', 'window_title': 'Anime Movies Calendar'}
    return {
        'label': 'Calendar',
        'params': params,
        'is_folder': False,
        'path': PLUGINPATH,
        'art': {
            'landscape': f'{ADDONPATH}/fanart.jpg',
            'icon': f'{ADDONPATH}/resources/icons/themoviedb/airing.png'}}


def _get_basedir_anime():
    # Anime lists are Otaku's own AniList lists (api/anilist/lists.py), matched to TMDb so TMDb Helper opens every row
    icons = f'{ADDONPATH}/resources/icons/themoviedb'
    rows = [
        {
            'label': label,
            'params': dict(params, tmdb_type='both'),
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{icons}/{icon}'}}
        for label, params, icon in (
            ('Airing Last Season', {'info': 'anime_season', 'period': 'last'}, 'airing.png'),
            ('Airing This Season', {'info': 'anime_season', 'period': 'this'}, 'airing.png'),
            ('Airing Next Season', {'info': 'anime_season', 'period': 'next'}, 'airing.png'),
            ('Trending', {'info': 'dir_anime_trending'}, 'upcoming.png'),
            ('Popular', {'info': 'dir_anime_popular'}, 'popular.png'),
            ('Top 100', {'info': 'anime_top100'}, 'toprated.png'),
            ('Genres & Tags', {'info': 'anime_genres'}, 'genre.png'),
            ('Search', {'info': 'scoped_search', 'search_scope': 'anime', 'search_media': 'tv'}, 'search.png'))]
    rows.insert(3, _anime_calendar_item())   # next to the season rows, after Airing Next Season
    return rows


def _get_basedir_anime_movies():
    # The Anime menu's rows again, each asking AniList for movies only (anime_format=MOVIE)
    icons = f'{ADDONPATH}/resources/icons/themoviedb'
    rows = [
        {
            'label': label,
            'params': dict(params, tmdb_type='both', anime_format='MOVIE'),
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{icons}/{icon}'}}
        for label, params, icon in (
            ('Last Season Releases', {'info': 'anime_season', 'period': 'last'}, 'airing.png'),
            ('This Season Releases', {'info': 'anime_season', 'period': 'this'}, 'airing.png'),
            ('Next Season Releases', {'info': 'anime_season', 'period': 'next'}, 'airing.png'),
            ('Trending', {'info': 'dir_anime_trending'}, 'upcoming.png'),
            ('Popular', {'info': 'dir_anime_popular'}, 'popular.png'),
            ('Top 100', {'info': 'anime_top100'}, 'toprated.png'),
            ('Genres & Tags', {'info': 'anime_genres'}, 'genre.png'),
            ('Search', {'info': 'scoped_search', 'search_scope': 'anime', 'search_media': 'movie'}, 'search.png'))]
    rows.insert(3, _anime_calendar_item(movie=True))   # next to the season rows, after Next Season Releases
    return rows


def _get_basedir_anime_trending(anime_format=None):
    from tmdbhelper.lib.api.anilist.lists import PERIODS
    icon = f'{ADDONPATH}/resources/icons/themoviedb/upcoming.png'
    extra = {'anime_format': anime_format} if anime_format == 'MOVIE' else {}
    return [
        {
            'label': label,
            'params': dict({'info': 'anime_trending', 'period': period, 'tmdb_type': 'both'}, **extra),
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': icon}}
        for period, label in PERIODS]


def _get_basedir_anime_popular(anime_format=None):
    from tmdbhelper.lib.api.anilist.lists import POPULAR_PERIODS
    icon = f'{ADDONPATH}/resources/icons/themoviedb/popular.png'
    extra = {'anime_format': anime_format} if anime_format == 'MOVIE' else {}
    return [
        {
            'label': label,
            'params': dict({'info': 'anime_popular', 'period': period, 'tmdb_type': 'both'}, **extra),
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': icon}}
        for period, label in POPULAR_PERIODS]


# ---- Asian Drama: TMDb Discover rows (dramas only), Trakt where its country filter returns a usable list ----------

ASIAN_ORIGINS = 'KR|CN|JP|TH|TW'
ASIAN_COUNTRIES = (('CN', 'Chinese'), ('KR', 'Korean'), ('JP', 'Japanese'), ('TH', 'Thai'), ('TW', 'Taiwanese'))
ASIAN_MIN_VOTES = {'KR': 100, 'CN': 40, 'JP': 50, 'TH': 40, 'TW': 15}   # Top Rated floor, from TMDb vote counts per country
# Trakt's country filter is only used where it returned a full list (KR popular); CN popular gave 4 rows, TH 3-7, TW 0 and
# JP is almost all anime, so those rows use TMDb Discover instead.
# Most Watched (week / month) is the exception: Trakt returns 100 rows for KR/CN/TH/TW (about 90, 55, 100, 98 left after the
# animation filter) but JP is 97% anime (1-3 of the first 100 left), so JP reads Trakt's list 8 pages deep instead
# (api/asiandrama/lists.py ListAsianMostWatched: about 45 live-action dramas).
ASIAN_MOST_WATCHED = (('Most Watched This Week', 'mostviewers'), ('Most Watched This Month', 'mostviewers_monthly'))
ASIAN_TRAKT = (
    ('KR', 'popular'),
    *((code, kind) for code in ('KR', 'CN', 'TH', 'TW') for _, kind in ASIAN_MOST_WATCHED))
# Country Trending is built by api/asiandrama/lists.py: TMDb dramas first aired in the last 90 days, ranked by the mean of
# their IMDb / TMDb / Trakt ratings (one batched MDBList request), so it has no Trakt or Discover row of its own here.
ASIAN_CALENDAR_COUNTRIES = ','.join(code.lower() for code, _ in ASIAN_COUNTRIES)   # Calendar rows cover all five; JP's anime is dropped by calendar_filter
ASIAN_TRAKT_COUNTRIES = 'kr,cn,th,tw'   # combined Most Watched rows leave JP out (anime would fill the 100 rows)
ASIAN_TRAKT_EXCLUDE = {'exclude_key': 'genre', 'exclude_value': 'Animation / Reality / Talk'}   # Trakt files donghua as drama
ASIAN_NOT_DRAMA = '16 / 10764 / 10767'   # TMDb TV genre ids: Animation, Reality, Talk (' / ' is TMDb Helper's list separator)
ASIAN_GENRES = (   # TMDb TV has no Romance/Historical genre, so those two are keywords
    ('Romance', {'with_genres': '18', 'with_keywords': '9840'}),
    ('Historical', {'with_genres': '18', 'with_keywords': '15060 / 195013 / 192772', 'with_separator': 'OR'}),
    ('Melodrama', {'with_genres': '18', 'with_keywords': '293016'}),
    ('Comedy', {'with_genres': '18 / 35'}),
    ('Crime', {'with_genres': '18 / 80'}),
    ('Mystery', {'with_genres': '18 / 9648'}),
    ('Action & Adventure', {'with_genres': '18 / 10759'}),
    ('Sci-Fi & Fantasy', {'with_genres': '18 / 10765'}),
    ('Family', {'with_genres': '18 / 10751'}))


def _asian_discover(category, origin=ASIAN_ORIGINS, **extra):
    # with_id: the values are already TMDb ids; relative dates (t-30, t+180) are resolved when the list opens
    params = {
        'info': 'discover', 'tmdb_type': 'tv', 'with_id': 'True', 'with_separator': 'AND',
        'with_origin_country': origin, 'with_genres': '18', 'without_genres': ASIAN_NOT_DRAMA,
        'plugin_category': category}
    params.update(extra)
    return params


def _asian_rows(rows):
    icons = f'{ADDONPATH}/resources/icons/themoviedb'
    return [
        {
            'label': label,
            'params': params,
            'path': PLUGINPATH,
            'is_folder': params.get('info') not in ('dir_calendar_window', 'dir_calendar_shows_window'),   # the calendars open a window, not a list
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{icons}/{icon}'}}
        for label, params, icon in rows]


def _get_basedir_asian_drama():
    return _asian_rows((
        ('Airing Now', _asian_discover(
            'Airing Now', **{'air_date.gte': 't-30', 'air_date.lte': 't+0', 'sort_by': 'popularity.desc'}), 'airing.png'),
        ('Airing Next', _asian_discover(
            'Airing Next', **{'first_air_date.gte': 't+1', 'first_air_date.lte': 't+180', 'sort_by': 'popularity.desc'}),
         'upcoming.png'),
        ('Calendar', {'info': 'dir_calendar_shows_window', 'user': 'false', 'countries': ASIAN_CALENDAR_COUNTRIES, 'calendar_filter': 'asian_tv', 'window_title': 'Asian Drama Calendar'}, 'airing.png'),
        ('Trending', {'info': 'asian_trending', 'country': 'ALL', 'tmdb_type': 'both'}, 'upcoming.png'),
        *((label, dict(
            {'info': f'trakt_{kind}', 'tmdb_type': 'tv', 'countries': ASIAN_TRAKT_COUNTRIES}, **ASIAN_TRAKT_EXCLUDE), 'popular.png')
          for label, kind in ASIAN_MOST_WATCHED),
        ('Popular', _asian_discover(
            'Popular', **{'sort_by': 'popularity.desc', 'vote_count.gte': '10'}), 'popular.png'),
        ('Top Rated', _asian_discover(
            'Top Rated', **{'sort_by': 'vote_average.desc', 'vote_count.gte': '100'}), 'toprated.png'),
        ('By Country', {'info': 'dir_asian_drama_country'}, 'default.png'),
        ('Genres', {'info': 'dir_asian_drama_genres'}, 'genre.png'),
        ('Search', {'info': 'scoped_search', 'search_scope': 'asian', 'search_media': 'tv', 'tmdb_type': 'both'},
         'search.png')))


def _get_basedir_asian_drama_country():
    return _asian_rows(
        (f'{name} Drama', {'info': 'dir_asian_drama_list', 'country': code}, 'default.png')
        for code, name in ASIAN_COUNTRIES)


def _get_basedir_asian_drama_list(country):
    name = dict(ASIAN_COUNTRIES).get(country)
    if not name:
        return []

    def _row(label, kind, icon, **discover):
        if kind == 'trending':
            params = {'info': 'asian_trending', 'country': country, 'tmdb_type': 'both'}
        elif (country, kind) in ASIAN_TRAKT:
            params = dict(
                {'info': f'trakt_{kind}', 'tmdb_type': 'tv', 'countries': country.lower()}, **ASIAN_TRAKT_EXCLUDE)
        elif kind.startswith('mostviewers'):
            # JP: Trakt's list is mostly anime, so a custom list scans deeper and keeps the live-action dramas
            params = {
                'info': 'asian_mostwatched', 'country': country, 'tmdb_type': 'both',
                'period': 'weekly' if kind == 'mostviewers' else 'monthly'}
        else:
            params = _asian_discover(f'{name} {label}', origin=country, **discover)
        return label, params, icon

    rows = (
        _row('Airing Now', 'airing', 'airing.png',
             **{'air_date.gte': 't-30', 'air_date.lte': 't+0', 'sort_by': 'popularity.desc'}),
        ('Calendar', {'info': 'dir_calendar_shows_window', 'user': 'false', 'countries': country.lower(), 'calendar_filter': 'asian_tv', 'window_title': f'{name} Drama Calendar'}, 'airing.png'),
        _row('Trending', 'trending', 'upcoming.png'),
        *(_row(label, kind, 'popular.png') for label, kind in ASIAN_MOST_WATCHED),
        _row('Popular', 'popular', 'popular.png', **{'sort_by': 'popularity.desc', 'vote_count.gte': '10'}),
        _row('Top Rated', 'toprated', 'toprated.png',
             **{'sort_by': 'vote_average.desc', 'vote_count.gte': str(ASIAN_MIN_VOTES[country])}))
    return _asian_rows(row for row in rows if row)


def _get_basedir_asian_drama_genres():
    return _asian_rows(
        (label, _asian_discover(label, **dict(genre, sort_by='popularity.desc', **{'vote_count.gte': '10'})), 'genre.png')
        for label, genre in ASIAN_GENRES)


# ---- Asian Movies: same layout as Asian Drama, built from TMDb Discover movies and Trakt's movie lists ---------------

ASIAN_MOVIE_MIN_VOTES = {'CN': 200, 'KR': 200, 'JP': 300, 'TH': 50, 'TW': 50}   # Top Rated floor, from TMDb movie vote counts
ASIAN_MOVIE_TOP_VOTES = '500'            # all five countries together (about 200 films)
ASIAN_MOVIE_TRAKT = tuple((code, kind) for code in ('KR', 'CN', 'TH', 'TW') for _, kind in ASIAN_MOST_WATCHED)
ASIAN_MOVIE_TRAKT_EXCLUDE = {'exclude_key': 'genre', 'exclude_value': 'Animation / Documentary'}
ASIAN_NOT_MOVIE = '16 / 99'              # TMDb movie genre ids: Animation, Documentary
ASIAN_MOVIE_GENRES = (
    ('Drama', '18'), ('Romance', '10749'), ('Comedy', '35'), ('Action', '28'), ('Thriller', '53'), ('Crime', '80'),
    ('Mystery', '9648'), ('Horror', '27'), ('Fantasy', '14'), ('Sci-Fi', '878'), ('History', '36'), ('War', '10752'),
    ('Family', '10751'))


def _asian_movie_discover(category, origin=ASIAN_ORIGINS, **extra):
    params = {
        'info': 'discover', 'tmdb_type': 'movie', 'with_id': 'True', 'with_separator': 'AND',
        'with_origin_country': origin, 'without_genres': ASIAN_NOT_MOVIE, 'plugin_category': category}
    params.update(extra)
    return params


def _get_basedir_asian_movies():
    return _asian_rows((
        ('In Theaters', _asian_movie_discover(
            'In Theaters', **{'primary_release_date.gte': 't-30', 'primary_release_date.lte': 't+0',
                              'sort_by': 'popularity.desc'}), 'airing.png'),
        ('Coming Soon', _asian_movie_discover(
            'Coming Soon', **{'primary_release_date.gte': 't+1', 'primary_release_date.lte': 't+180',
                              'sort_by': 'popularity.desc'}), 'upcoming.png'),
        ('Calendar', {'info': 'dir_calendar_window', 'user': 'false', 'countries': ASIAN_CALENDAR_COUNTRIES, 'calendar_filter': 'asian_movie', 'window_title': 'Asian Movies Calendar'}, 'airing.png'),
        ('Trending', {'info': 'asian_movie_trending', 'country': 'ALL', 'tmdb_type': 'both'}, 'upcoming.png'),
        *((label, dict(
            {'info': f'trakt_{kind}', 'tmdb_type': 'movie', 'countries': ASIAN_TRAKT_COUNTRIES},
            **ASIAN_MOVIE_TRAKT_EXCLUDE), 'popular.png')
          for label, kind in ASIAN_MOST_WATCHED),
        ('Popular', _asian_movie_discover(
            'Popular', **{'sort_by': 'popularity.desc', 'vote_count.gte': '20'}), 'popular.png'),
        ('Top Rated', _asian_movie_discover(
            'Top Rated', **{'sort_by': 'vote_average.desc', 'vote_count.gte': ASIAN_MOVIE_TOP_VOTES}), 'toprated.png'),
        ('By Country', {'info': 'dir_asian_movies_country'}, 'default.png'),
        ('Genres', {'info': 'dir_asian_movies_genres'}, 'genre.png'),
        ('Search', {'info': 'scoped_search', 'search_scope': 'asian', 'search_media': 'movie', 'tmdb_type': 'both'},
         'search.png')))


def _get_basedir_asian_movies_country():
    return _asian_rows(
        (f'{name} Movies', {'info': 'dir_asian_movies_list', 'country': code}, 'default.png')
        for code, name in ASIAN_COUNTRIES)


def _get_basedir_asian_movies_list(country):
    name = dict(ASIAN_COUNTRIES).get(country)
    if not name:
        return []

    def _row(label, kind, icon, **discover):
        if kind == 'trending':
            params = {'info': 'asian_movie_trending', 'country': country, 'tmdb_type': 'both'}
        elif (country, kind) in ASIAN_MOVIE_TRAKT:
            params = dict(
                {'info': f'trakt_{kind}', 'tmdb_type': 'movie', 'countries': country.lower()}, **ASIAN_MOVIE_TRAKT_EXCLUDE)
        elif kind.startswith('mostviewers'):
            # JP: Trakt's list is about 80% anime, so a custom list scans deeper and keeps the live-action films
            params = {
                'info': 'asian_movie_mostwatched', 'country': country, 'tmdb_type': 'both',
                'period': 'weekly' if kind == 'mostviewers' else 'monthly'}
        else:
            params = _asian_movie_discover(f'{name} {label}', origin=country, **discover)
        return label, params, icon

    return _asian_rows((
        _row('In Theaters', 'airing', 'airing.png',
             **{'primary_release_date.gte': 't-30', 'primary_release_date.lte': 't+0', 'sort_by': 'popularity.desc'}),
        ('Calendar', {'info': 'dir_calendar_window', 'user': 'false', 'countries': country.lower(), 'calendar_filter': 'asian_movie', 'window_title': f'{name} Movies Calendar'}, 'airing.png'),
        _row('Trending', 'trending', 'upcoming.png'),
        *(_row(label, kind, 'popular.png') for label, kind in ASIAN_MOST_WATCHED),
        _row('Popular', 'popular', 'popular.png', **{'sort_by': 'popularity.desc', 'vote_count.gte': '20'}),
        _row('Top Rated', 'toprated', 'toprated.png',
             **{'sort_by': 'vote_average.desc', 'vote_count.gte': str(ASIAN_MOVIE_MIN_VOTES[country])})))


def _get_basedir_asian_movies_genres():
    return _asian_rows(
        (label, _asian_movie_discover(
            label, with_genres=genre_id, sort_by='popularity.desc', **{'vote_count.gte': '20'}), 'genre.png')
        for label, genre_id in ASIAN_MOVIE_GENRES)


def _get_basedir_hub(movies_info, tv_info, search_scope):
    # Movies / TV Shows picker for Anime and Asian Cinema & TV (the two lists keep their own routes, so old links still work)
    icons = f'{ADDONPATH}/resources/icons/themoviedb'
    return [
        {
            'label': label,
            'params': {'info': info},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{icons}/{icon}'}}
        for label, info, icon in (('Movies', movies_info, 'movies.png'), ('TV Shows', tv_info, 'tv.png'))
    ] + [
        {
            'label': 'Search Movies & TV Shows',
            'params': {'info': 'scoped_search', 'search_scope': search_scope, 'tmdb_type': 'both'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{icons}/search.png'}}]


# Tools menu: Seren's Sync Tools / Clear Cache Tool / Open Settings Menu / Backup and Reset / Changelog, in Seren's order.
# Folder rows open dir_tools_* lists; action rows (is_folder False, like Seren's) run script/method/tools.py through
# dir_tools_action (any dir_* info is routed to ListBaseDir, so consts.py needs no entry).
def _tools_row(label, plot, icon, info, **params):
    return {
        'label': label,
        'types': [None],
        'is_folder': info != 'dir_tools_action',
        'params': dict(info=info, **params),
        'path': PLUGINPATH,
        'infolabels': {'plot': plot},
        'art': {
            'landscape': f'{ADDONPATH}/fanart.jpg',
            'icon': f'{ADDONPATH}/resources/icons/themoviedb/{icon}'}}


def _tools_action(label, plot, icon, action):
    return _tools_row(label, plot, icon, 'dir_tools_action', tools_action=action)


def _get_basedir_tools():
    rows = []
    if any(get_setting(f'service_{n}') and get_setting(f'{n}_token', 'str') for n in ('trakt', 'mdblist', 'simkl')):   # local settings only, as Seren gates it; a service that is switched off does not count
        rows.append(_tools_row('Sync Tools', 'Tools for the Trakt, MDBList and Simkl sync data', 'refresh.png', 'dir_tools_sync'))
    rows += [
        _tools_row('Clear Cache Tool', 'Clear cached data and search history', 'default.png', 'dir_tools_cache'),
        _tools_action('Open Settings Menu', 'Open the TMDb Helper settings', 'settings.png', 'open_settings'),
        _tools_row('Backup and Reset', 'Back up or restore the TMDb Helper settings', 'settings.png', 'dir_tools_backup'),
        _tools_action('Changelog', 'View the TMDb Helper changelog', 'reviews.png', 'changelog')]
    return _build_basedir(None, rows)


def _get_basedir_tools_sync():
    return _build_basedir(None, [
        _tools_action('Clear last sync time', 'Forget when each connected service was last synced, so the next sync checks everything again',
                      'refresh.png', 'sync_clear'),
        _tools_action('Force a sync', 'Sync Trakt, MDBList and Simkl now', 'refresh.png', 'sync_force'),
        _tools_action('Rebuild Database', 'Remove all synced data and sync it again from scratch', 'refresh.png', 'sync_rebuild')])


def _get_basedir_tools_cache():
    return _build_basedir(None, [
        _tools_action('Clear Cache', 'Clear the cached TMDb, Trakt, fanart.tv, OMDb and item details data', 'default.png', 'clear_cache'),
        _tools_action('Clear Search History', 'Clear the saved search history', 'search.png', 'clear_search_history'),
        _tools_action('Clear Anime and Asian Lists Cache', 'Clear the saved results of the Anime and Asian lists', 'default.png', 'clear_list_caches')])


def _get_basedir_tools_backup():
    return _build_basedir(None, [
        _tools_action('Backup Settings', 'Save a copy of the TMDb Helper settings', 'settings.png', 'backup'),
        _tools_action('Restore Settings', 'Restore the TMDb Helper settings from a backup', 'settings.png', 'restore')])


def _run_tools_action(tools_action=None):
    from tmdbhelper.lib.script.method.tools import run
    run(tools_action)


def _open_calendar_window(user=None, endpoint=None, window_title=None, countries=None, calendar_filter=None, genres=None):
    from tmdbhelper.lib.window.moviecalendar import open_movie_calendar
    open_movie_calendar(
        user=user, endpoint=endpoint, window_title=window_title, countries=countries, calendar_filter=calendar_filter, genres=genres)


def _open_calendar_shows_window(window_title=None, endpoint=None, user=None, countries=None, calendar_filter=None, genres=None):
    from tmdbhelper.lib.window.showcalendar import open_show_calendar
    open_show_calendar(
        window_title=window_title, endpoint=endpoint, user=user, countries=countries, calendar_filter=calendar_filter, genres=genres)


def _get_basedir_calendar_items():
    return [
        {
            'label': get_localized(32280),
            'params': {'startdate': -14, 'days': 14},
            'path': PLUGINPATH,
            'info_types': ['trakt_calendar', 'library_nextaired'],
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/default.png'}},
        {
            'label': get_localized(32281),
            'params': {'startdate': -7, 'days': 7},
            'path': PLUGINPATH,
            'info_types': ['trakt_calendar', 'library_nextaired'],
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/default.png'}},
        {
            'label': get_localized(32282),
            'params': {'startdate': -1, 'days': 1},
            'path': PLUGINPATH,
            'info_types': ['trakt_calendar', 'library_nextaired'],
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/default.png'}},
        {
            'label': get_localized(33006),
            'params': {'startdate': 0, 'days': 1},
            'path': PLUGINPATH,
            'info_types': ['trakt_calendar', 'library_nextaired'],
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/default.png'}},
        {
            'label': get_localized(33007),
            'params': {'startdate': 1, 'days': 1},
            'path': PLUGINPATH,
            'info_types': ['trakt_calendar', 'library_nextaired'],
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/default.png'}},
        {
            'label': u'{weekday}',
            'params': {'startdate': 2, 'days': 1},
            'path': PLUGINPATH,
            'info_types': ['trakt_calendar', 'library_nextaired'],
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/default.png'}},
        {
            'label': u'{weekday}',
            'params': {'startdate': 3, 'days': 1},
            'path': PLUGINPATH,
            'info_types': ['trakt_calendar', 'library_nextaired'],
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/default.png'}},
        {
            'label': u'{weekday}',
            'params': {'startdate': 4, 'days': 1},
            'path': PLUGINPATH,
            'info_types': ['trakt_calendar', 'library_nextaired'],
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/default.png'}},
        {
            'label': u'{weekday}',
            'params': {'startdate': 5, 'days': 1},
            'path': PLUGINPATH,
            'info_types': ['trakt_calendar', 'library_nextaired'],
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/default.png'}},
        {
            'label': u'{weekday}',
            'params': {'startdate': 6, 'days': 1},
            'path': PLUGINPATH,
            'info_types': ['trakt_calendar', 'library_nextaired'],
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/default.png'}},
        {
            'label': get_localized(32284),
            'params': {'startdate': 0, 'days': 7},
            'path': PLUGINPATH,
            'info_types': ['trakt_calendar', 'library_nextaired'],
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/default.png'}},
        {
            'label': get_localized(32285),
            'params': {'startdate': 0, 'days': 14},
            'path': PLUGINPATH,
            'info_types': ['trakt_calendar', 'library_nextaired'],
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/default.png'}}]


def _get_basedir_calendar(info=None, endpoint=None, user=None, tmdb_type=None, countries=None, calendar_filter=None, genres=None):
    items = []
    today = get_datetime_today()
    for i in _get_basedir_calendar_items():
        if info not in i['info_types']:
            continue
        date = today + get_timedelta(days=i.get('params', {}).get('startdate', 0))
        i['label'] = i['label'].format(weekday=date.strftime('%A'))
        i['params']['info'] = info
        if endpoint:
            i['params']['endpoint'] = endpoint
        if user:
            i['params']['user'] = user
        if tmdb_type == 'movie':  # TV rows keep their old params, so their paths (and widget caches) do not change
            i['params']['tmdb_type'] = tmdb_type
        if countries:  # Asian calendars: the country filter and the genre preset travel with every bucket
            i['params']['countries'] = countries
        if calendar_filter:
            i['params']['calendar_filter'] = calendar_filter
        if genres:  # Anime calendars: Trakt's anime genre filter
            i['params']['genres'] = genres
        items.append(i)
    return items


def is_anime_item(language, genre_names, animation_name):
    """Japanese-language animation, from data the caller already has. genre_names: the item's (localised) genre names, a list or an
    ' / ' string; animation_name: TMDb's localised name for genre 16. Where either is not known the language alone decides."""
    if language != 'ja':
        return False
    if isinstance(genre_names, str):
        genre_names = [g.strip() for g in genre_names.split('/')]
    if not animation_name or not genre_names:
        return True
    return animation_name in genre_names


def get_basedir_details(tmdb_type, tmdb_id, season=None, episode=None, detailed_item=None, include_play=False, anime=None):
    """anime: whether the title is Japanese animation (the Anime Recommendations entry only shows then); when None it is read from the
    detailed item's original language. Local data only: nothing is looked up to decide."""
    if anime is None:
        anime = ((detailed_item or {}).get('infoproperties') or {}).get('original_language') == 'ja'
    base_item = detailed_item or {}
    base_item.setdefault('params', {})
    base_item.setdefault('infolabels', {})
    base_item['path'] = PLUGINPATH
    base_item['params']['tmdb_id'] = tmdb_id
    base_item['params']['tmdb_type'] = tmdb_type
    base_item['params']['info'] = 'details'

    basedir_items = []
    if tmdb_type == 'movie':
        base_item['infolabels']['mediatype'] = 'movie'
        basedir_items = _build_basedir('movie', _get_play_item()) if include_play else []
        basedir_items += _build_basedir('movie', _get_basedir_details())
    elif tmdb_type == 'tv' and season is not None and episode is not None:
        base_item['params']['season'] = season
        base_item['params']['episode'] = episode
        base_item['infolabels']['mediatype'] = 'episode'
        basedir_items = _build_basedir('episode', _get_play_item()) if include_play else []
        basedir_items += _build_basedir('episode', _get_basedir_details())
    elif tmdb_type == 'tv' and season is not None:
        base_item['params']['info'] = 'episodes'
        base_item['params']['season'] = season
        base_item['infolabels']['mediatype'] = 'season'
        basedir_items = _build_basedir('season', _get_basedir_details())
    elif tmdb_type == 'tv':
        base_item['params']['info'] = get_flatseasons_info_param()
        base_item['infolabels']['mediatype'] = 'tvshow'
        basedir_items = _build_basedir('tv', _get_basedir_details())
    elif tmdb_type == 'person':
        base_item['infolabels']['mediatype'] = 'video'
        basedir_items = _build_basedir('person', _get_basedir_details())

    items = [merge_two_items(base_item, i) for i in basedir_items if i]
    if not anime:
        items = [i for i in items if (i.get('params') or {}).get('info') != 'anime_recommendations']
    if not get_setting('service_trakt'):   # Settings > Accounts > Trakt off: no Trakt rows (In Trakt Lists needs the Trakt service too)
        items = [i for i in items if (i.get('params') or {}).get('info') != 'trakt_inlists']

    if detailed_item:
        return [base_item] + items
    return items


def _get_basedir_tmdb_v4():
    return [
        {
            'label': u'{}{{space}}{{item_type}}'.format(get_localized(32223)),
            'types': ['movie', 'tv'],
            'params': {'info': 'tmdb_v4_recommendations'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/popular.png'}},
        {
            'label': u'{}{{space}}{{item_type}}'.format(get_localized(1036)),
            'types': ['movie', 'tv'],
            'params': {'info': 'tmdb_v4_favorites'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/recommended.png'}},
        {
            'label': u'{}{{space}}{{item_type}}'.format(get_localized(32521)),
            'types': ['movie', 'tv'],
            'params': {'info': 'tmdb_v4_rated'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/tags.png'}},
        {
            'label': u'{}{{space}}{{item_type}}'.format(get_localized(32193)),
            'types': ['movie', 'tv'],
            'params': {'info': 'tmdb_v4_watchlist'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/similar.png'}},
        {
            'label': get_localized(32211),
            'types': ['both'],
            'params': {'info': 'tmdb_v4_lists'},
            'path': PLUGINPATH,
            'art': {
                'landscape': f'{ADDONPATH}/fanart.jpg',
                'icon': f'{ADDONPATH}/resources/icons/themoviedb/reviews.png'}},
    ]


def _get_tmdb_v4():
    from tmdbhelper.lib.api.tmdb.users import TMDbUser
    if not TMDbUser().authenticator.authorised_access:
        return
    return _build_basedir(None, _get_basedir_tmdb_v4())


class ListBaseDir(ContainerDirectory):
    def get_items(self, info=None, **kwargs):
        route = {
            'dir_movie': lambda: _get_basedir_list('movie', tmdb=True, trakt=True),
            'dir_tv': lambda: _get_basedir_list('tv', tmdb=True, trakt=True),
            'dir_anime': lambda: _get_basedir_anime(),
            'dir_anime_hub': lambda: _get_basedir_hub('dir_anime_movies', 'dir_anime', 'anime'),
            'dir_asian_hub': lambda: _get_basedir_hub('dir_asian_movies', 'dir_asian_drama', 'asian'),
            'dir_anime_movies': lambda: _get_basedir_anime_movies(),
            'dir_anime_trending': lambda: _get_basedir_anime_trending(kwargs.get('anime_format')),
            'dir_anime_popular': lambda: _get_basedir_anime_popular(kwargs.get('anime_format')),
            'dir_asian_drama': lambda: _get_basedir_asian_drama(),
            'dir_asian_drama_country': lambda: _get_basedir_asian_drama_country(),
            'dir_asian_drama_list': lambda: _get_basedir_asian_drama_list(kwargs.get('country')),
            'dir_asian_drama_genres': lambda: _get_basedir_asian_drama_genres(),
            'dir_asian_movies': lambda: _get_basedir_asian_movies(),
            'dir_asian_movies_country': lambda: _get_basedir_asian_movies_country(),
            'dir_asian_movies_list': lambda: _get_basedir_asian_movies_list(kwargs.get('country')),
            'dir_asian_movies_genres': lambda: _get_basedir_asian_movies_genres(),
            'dir_person': lambda: _get_basedir_list('person', tmdb=True, trakt=True),
            'dir_tmdb': lambda: _get_basedir_list(None, tmdb=True),
            'dir_trakt': lambda: _get_basedir_list(None, trakt=True),
            'dir_mdblist': lambda: _get_basedir_list(None, mdblist=True),
            'dir_mdblist_progress': lambda: _get_basedir_mdblist_progress(kwargs.get('progress_filter')),
            'dir_simkl_progress': lambda: _get_basedir_simkl_progress(kwargs.get('progress_filter')),
            'dir_trakt_progress': lambda: _get_basedir_trakt_progress(kwargs.get('progress_filter')),
            'dir_simkl': lambda: _get_basedir_list(None, simkl=True),
            'dir_tvdb': lambda: _get_basedir_list(None, tvdb=True),
            'dir_random': lambda: _build_basedir(None, _get_basedir_random()),
            'dir_calendar_trakt': lambda: _get_basedir_calendar(info='trakt_calendar', endpoint=kwargs.get('endpoint'), user=kwargs.get('user'), tmdb_type=kwargs.get('tmdb_type'), countries=kwargs.get('countries'), calendar_filter=kwargs.get('calendar_filter'), genres=kwargs.get('genres')),
            'dir_calendar_library': lambda: _get_basedir_calendar(info='library_nextaired'),
            'dir_calendar_window': lambda: _open_calendar_window(
                user=kwargs.get('user'), endpoint=kwargs.get('endpoint'), window_title=kwargs.get('window_title'),
                countries=kwargs.get('countries'), calendar_filter=kwargs.get('calendar_filter'), genres=kwargs.get('genres')),
            'dir_calendar_shows_window': lambda: _open_calendar_shows_window(
                window_title=kwargs.get('window_title'), endpoint=kwargs.get('endpoint'), user=kwargs.get('user'),
                countries=kwargs.get('countries'), calendar_filter=kwargs.get('calendar_filter'), genres=kwargs.get('genres')),
            'dir_custom_node': lambda: _get_basedir_nodes(filename=kwargs.get('filename'), basedir=kwargs.get('basedir')),
            'dir_trakt_genre': lambda: _get_basedir_trakt_genre_types(genre=kwargs.get('genre'), tmdb_type=kwargs.get('tmdb_type')),
            'dir_tmdb_v4': lambda: _get_tmdb_v4(),
            'dir_settings': lambda: ADDON.openSettings(),
            'dir_tools': lambda: _get_basedir_tools(),
            'dir_tools_sync': lambda: _get_basedir_tools_sync(),
            'dir_tools_cache': lambda: _get_basedir_tools_cache(),
            'dir_tools_backup': lambda: _get_basedir_tools_backup(),
            'dir_tools_action': lambda: _run_tools_action(kwargs.get('tools_action'))
        }
        func = route.get(info, lambda: _build_basedir(None, _get_basedir_main()))
        items = func()
        return items if info == 'dir_custom_node' else add_descriptions(items)   # user nodes keep exactly what they define


class ListDetails(ContainerDirectory):
    def get_items(self, tmdb_type, tmdb_id, season=None, episode=None, **kwargs):
        def _get_container_content():
            if tmdb_type == 'tv' and season and episode:
                return convert_type('episode', 'container')
            elif tmdb_type == 'tv' and season:
                return convert_type('season', 'container')
            return convert_type(tmdb_type, 'container')
        base_item = ItemBuilder(tmdb_api=self.tmdb_api).get_item(tmdb_type, tmdb_id, season, episode)
        base_item = base_item['listitem'] if base_item else {}
        base_item = self.omdb_api.get_item_ratings(base_item) if self.omdb_api else base_item
        animation = {v: k for k, v in (self.tmdb_api.genres or {}).items()}.get(16)   # TMDb's name for the Animation genre in your language
        anime = is_anime_item((base_item.get('infoproperties') or {}).get('original_language'), (base_item.get('infolabels') or {}).get('genre'), animation)
        items = get_basedir_details(tmdb_type, tmdb_id, season, episode, base_item, anime=anime)
        self.container_content = _get_container_content()
        self.kodi_db = self.get_kodi_database(tmdb_type)
        return items
