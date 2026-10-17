""" One-line descriptions (the plot shown under a highlighted row) for the browse menus built by items/basedir.py.
Rows that already carry a plot (Tools, TVDb) are left alone; rows this module does not know stay as they are. """

_NOUNS = {'movie': 'movies', 'tv': 'TV shows', 'person': 'people', 'season': 'seasons', 'episode': 'episodes'}
_COUNTRIES = {'CN': 'Chinese', 'KR': 'Korean', 'JP': 'Japanese', 'TH': 'Thai', 'TW': 'Taiwanese'}
_WHEN = {
    'last_year': 'from last year', 'this_year': 'from this year', 'last_season': 'from last season', 'this_season': 'from this season',
    'all_time': 'of all time'}

# info -> text; {n} = the item type in the row ("movies", "TV shows" or "movies and TV shows"), {N} = the same capitalised
_BY_INFO = {
    # Main menu
    'dir_movie': 'Browse movies by popularity, rating, genre, provider and your Trakt lists',
    'dir_tv': 'Browse TV shows by popularity, rating, genre, provider and your Trakt lists',
    'dir_anime_hub': 'Anime movies and TV shows from AniList: seasons, trending, popular, top 100 and genres',
    'dir_asian_hub': 'Chinese, Korean, Japanese, Thai and Taiwanese movies and dramas',
    'dir_person': 'Browse popular people, or search for an actor or crew member',
    'dir_multisearch': 'Search movies, TV shows and people at once',
    'dir_discover': 'Build a list by filtering TMDb with genre, year, rating and more',
    'dir_random': 'Random movies and TV shows picked from genres, keywords and your Trakt lists',
    'dir_tmdb': 'Lists from TheMovieDb: popular, top rated, trending, upcoming, genres and more',
    'dir_tmdb_v4': 'Your TMDb account: recommendations, favourites, ratings, watchlist and lists',
    'dir_trakt': 'Your Trakt collection, watchlist, history, progress and lists, plus what is trending and popular on Trakt',
    'dir_tvdb': 'TV shows by TVDb awards and genres',
    'dir_mdblist': 'Your MDBList watchlist, history, collection, progress and lists, plus top and searchable lists',
    'dir_simkl': 'Your Simkl watching, plan to watch, history, on hold and dropped lists',
    'dir_custom_node': 'Your own menus (custom nodes)',
    'dir_settings': 'Open the TMDb Helper settings',
    'dir_tools': 'Sync, cache, backup and changelog tools',
    # TheMovieDb
    'dir_search': 'Search for {n}',
    'popular': 'The most popular {n} on TMDb right now',
    'top_rated': 'The highest rated {n} on TMDb',
    'upcoming': '{N} that have not been released yet',
    'trending_day': '{N} trending on TMDb today',
    'trending_week': '{N} trending on TMDb this week',
    'now_playing': 'Movies showing in theatres now',
    'airing_today': 'TV shows with an episode airing today',
    'on_the_air': 'TV shows that are currently airing',
    'dir_calendar_library': 'Upcoming episodes of the TV shows in your Kodi library',
    'library_airingnext': 'The next episode to air for each TV show in your Kodi library',
    'genres': 'Browse {n} by genre',
    'watch_providers': 'Browse {n} by streaming provider',
    'revenue_movies': '{N} ranked by box office revenue',
    'most_voted': '{N} with the most votes on TMDb',
    'all_items': 'Browse all {n} on TMDb',
    # TMDb account
    'tmdb_v4_recommendations': '{N} TMDb recommends from your account',
    'tmdb_v4_favorites': '{N} you marked as favourite on TMDb',
    'tmdb_v4_rated': '{N} you rated on TMDb',
    'tmdb_v4_watchlist': '{N} on your TMDb watchlist',
    'tmdb_v4_lists': 'Your TMDb lists',
    # Trakt
    'trakt_collection': '{N} in your Trakt collection',
    'trakt_favorites': '{N} on your Trakt favourites',
    'trakt_watchlist': '{N} on your Trakt watchlist',
    'trakt_watchlist_released': '{N} on your Trakt watchlist that are already out, newest first',
    'trakt_watchlist_anticipated': '{N} on your Trakt watchlist that are not out yet, soonest first',
    'trakt_history': '{N} you watched recently, from your Trakt history',
    'trakt_dropped': 'TV shows you dropped on Trakt',
    'trakt_mostwatched': '{N} you have watched the most, from your Trakt history',
    'trakt_inprogress': '{N} you started but have not finished, from Trakt',
    'trakt_ondeck': 'The next episode to watch of each show you are following on Trakt',
    'trakt_ondeck_unwatched': 'The next episode you have not watched yet, from your Trakt in-progress list',   # movie row: see describe()
    'trakt_towatch': '{N} you still have left to watch, from Trakt',
    'trakt_nextepisodes': 'The next episode to watch for each show you are watching',
    'trakt_recommendations': '{N} Trakt recommends for you',
    'trakt_becauseyouwatched': '{N} similar to ones you watched recently',
    'trakt_becausemostwatched': '{N} similar to the ones you have watched the most',
    'trakt_myairing': 'Episodes of the shows you follow that air this week',
    'trakt_airingnext': 'The next episode to air for each show you follow',
    'trakt_genres': 'Browse {n} on Trakt by genre',
    'trakt_trending': '{N} being watched on Trakt right now',
    'trakt_popular': 'The most popular {n} on Trakt',
    'trakt_mostplayed': '{N} played the most on Trakt this week',
    'trakt_mostviewers': '{N} watched by the most people on Trakt this week',
    'trakt_anticipated': '{N} Trakt users are looking forward to the most',
    'trakt_boxoffice': 'The ten highest earning movies at the box office this weekend',
    'trakt_trendinglists': 'Trakt lists that are trending now',
    'trakt_popularlists': 'The most popular Trakt lists of all time',
    'trakt_likedlists': 'Lists you have liked on Trakt',
    'trakt_mylists': 'Your own Trakt lists',
    'trakt_searchlists': 'Search Trakt for public lists by name',
    'trakt_userlists': "Type a Trakt username to browse that person's public lists",
    # MDBList
    'mdblist_upnext': '{N} you started but have not finished, from MDBList',
    'mdblist_nextup': 'The next episode to watch of each show you are watching on MDBList',
    'mdblist_watchlist': '{N} on your MDBList watchlist',
    'mdblist_history': '{N} you watched recently, from your MDBList history',
    'mdblist_collection': '{N} in your MDBList collection',
    'mdblist_dropped': 'TV shows you dropped on MDBList',
    'mdblist_yourlists': 'Your own MDBList lists',
    'mdblist_likedlists': 'Lists you have liked on MDBList',
    'mdblist_toplists': 'The top lists on MDBList',
    'mdblist_searchlists': 'Search MDBList for public lists by name',
    'mdblist_userlists': "Type an MDBList username to browse that person's public lists",
    # Simkl
    'simkl_upnext': '{N} you started but have not finished, from Simkl',
    'simkl_nextup': 'The next episode to watch of each show you are watching on Simkl',
    'simkl_watching': 'TV shows you are watching on Simkl',
    'simkl_plantowatch': '{N} on your Simkl plan to watch list',
    'simkl_history': '{N} you watched recently, from your Simkl history',
    'simkl_hold': 'TV shows you put on hold on Simkl',
    'simkl_dropped': '{N} you dropped on Simkl',
    # TVDb
    'dir_tvdb_awards': 'TV shows that won or were nominated for an award, from TVDb',
    'dir_tvdb_genres': 'Browse {n} by genre, from TVDb',
    # Random
    'random_genres': 'Random {n} from a genre you choose',
    'random_keyword': 'Random movies from a keyword you choose',
    'random_trending': 'A random pick from the {n} being watched on Trakt right now',
    'random_popular': 'A random pick from the most popular {n} on Trakt',
    'random_mostplayed': 'A random pick from the {n} played the most on Trakt this week',
    'random_mostviewers': 'A random pick from the {n} watched by the most people on Trakt this week',
    'random_anticipated': 'A random pick from the {n} Trakt users are looking forward to the most',
    'random_trendinglists': 'A random Trakt list from the lists trending now',
    'random_popularlists': 'A random Trakt list from the most popular lists',
    'random_likedlists': 'A random list from the Trakt lists you have liked',
    'random_mylists': 'A random list from your own Trakt lists',
    # Anime
    'anime_top100': 'The 100 highest rated anime {k} on AniList',
    'anime_genres': 'Browse anime {k} by genre and tag',
    'dir_anime': 'Anime TV shows from AniList: seasons, trending, popular, top 100 and genres',
    'dir_anime_movies': 'Anime movies from AniList: seasons, trending, popular, top 100 and genres',
    'dir_anime_trending': 'Anime {k} that are trending, by year or season',
    'dir_anime_popular': 'The most popular anime {k}, by year, season or of all time',
    # Asian hubs
    'dir_asian_drama': 'Asian dramas: airing, trending, most watched, popular, top rated, by country and by genre',
    'dir_asian_movies': 'Asian movies: in theatres, trending, most watched, popular, top rated, by country and by genre',
    'dir_asian_drama_country': 'Browse Asian dramas by country',
    'dir_asian_movies_country': 'Browse Asian movies by country',
    'dir_asian_drama_genres': 'Browse Asian dramas by genre',
    'dir_asian_movies_genres': 'Browse Asian movies by genre'}

_CALENDAR = {
    (None, 'true'): 'Episodes airing this week for the shows you follow, from your Trakt calendar',
    ('premieres', 'true'): 'Season and series premieres for the shows you follow, from your Trakt calendar',
    ('new', 'true'): 'New shows starting for the shows you follow, from your Trakt calendar',
    (None, 'false'): 'Episodes airing this week for every show, from the Trakt calendar',
    ('premieres', 'false'): 'Season and series premieres for every show, from the Trakt calendar',
    ('new', 'false'): 'New shows starting, from the Trakt calendar'}

_CALENDAR_MOVIES = {
    (None, 'true'): 'Movie releases coming up for the movies you follow, from your Trakt calendar',
    ('dvd', 'true'): 'DVD and Blu-ray releases for the movies you follow, from your Trakt calendar',
    (None, 'false'): 'Movies being released this week, from the Trakt calendar (by your region when the region filter is on)',
    ('dvd', 'false'): 'DVD and Blu-ray releases this week, from the Trakt calendar (by your region when the region filter is on)'}

_CALENDAR_WINDOW = {   # the month poster-grid window behind the Movies calendar rows
    (None, 'true'): 'A month poster grid of the movie releases for the movies you follow, from your Trakt calendar',
    ('dvd', 'true'): 'A month poster grid of the DVD and Blu-ray releases for the movies you follow, from your Trakt calendar',
    (None, 'false'): 'A month poster grid of the movies being released, from the Trakt calendar (by your region when the region filter is on)',
    ('dvd', 'false'): 'A month poster grid of the DVD and Blu-ray releases, from the Trakt calendar (by your region when the region filter is on)'}

_ASIAN_KIND = (   # label (or its start) -> text; {w} = "Asian" or the country, {x} = dramas / movies
    ('Airing Now', 'Currently airing {w} {x}, most popular first'),
    ('Airing Next', '{w} {x} starting in the next six months'),
    ('In Theaters', '{w} {x} released in the last 30 days, most popular first'),
    ('Coming Soon', '{w} {x} coming in the next six months'),
    ('Trending', 'New {w} {x} from the last 90 days, ranked by their combined IMDb, TMDb and Trakt rating'),
    ('Most Watched This Week', '{w} {x} watched by the most people on Trakt this week'),
    ('Most Watched This Month', '{w} {x} watched by the most people on Trakt this month'),
    ('Popular', 'The most popular {w} {x} on TMDb'),
    ('Top Rated', 'The highest rated {w} {x} on TMDb'),
    ('By Country', 'Browse {w} {x} by country'),
    ('Genres', 'Browse {w} {x} by genre'))


def _nouns(params):
    return _NOUNS.get(params.get('tmdb_type'), 'movies and TV shows')


def _asian(params, label):
    movie = params.get('tmdb_type') == 'movie' or str(params.get('info', '')).startswith('asian_movie') or label.endswith(' Movies')
    x = 'movies' if movie else 'dramas'
    code = params.get('with_origin_country') or params.get('country') or params.get('countries')
    w = _COUNTRIES.get(str(code).upper(), 'Asian') if code and str(code).upper() != 'ALL' and len(str(code)) == 2 else 'Asian'
    info = params.get('info')
    if info in ('dir_asian_drama_list', 'dir_asian_movies_list'):
        return f'{label}: {"in theatres" if movie else "airing"}, trending, most watched, popular and top rated'
    if info == 'asian_mostwatched' or info == 'asian_movie_mostwatched':
        label = 'Most Watched This Week' if params.get('period') == 'weekly' else 'Most Watched This Month'
    category = params.get('plugin_category') or label
    for prefix, text in _ASIAN_KIND:
        if label.startswith(prefix) or category.endswith(prefix):
            return text.format(w=w, x=x)
    return f'Popular {w} {x} in the {label} genre'   # a genre row


def _asian_calendar(params):
    code = str(params.get('countries') or '').upper()
    w = _COUNTRIES.get(code, 'Asian') if len(code) == 2 else 'Asian'
    if params.get('tmdb_type') == 'movie':
        return f'{w} movies released this week, from the Trakt calendar (anime, animation and documentaries left out)'
    return f'{w} dramas airing this week, from the Trakt calendar (anime, animation, reality and talk shows left out)'


def describe(item):
    """The description for a basedir row (a dict with label and params), or None."""
    text = _describe(item)
    params = item.get('params') or {}
    if text and params.get('anime_only'):
        text = f'{text} (anime only)'
    elif text and params.get('asian_only'):
        text = f'{text} (Asian only)'
    elif text and params.get('other_only'):
        text = f'{text} (anime and Asian left out)'
    return text


def _describe(item):
    params = item.get('params') or {}
    info = params.get('info')
    label = item.get('label') or ''
    if info in ('dir_mdblist_progress', 'dir_simkl_progress', 'dir_trakt_progress'):
        service = {'dir_mdblist_progress': 'MDBList', 'dir_simkl_progress': 'Simkl'}.get(info, 'Trakt')
        if params.get('progress_filter') == 'anime':
            return f'Your anime in progress on {service}: movies, TV shows, episodes, the unwatched lists and your next episodes'
        if params.get('progress_filter') == 'asian':
            return f'Your Asian movies and dramas in progress on {service}: movies, TV shows, episodes, the unwatched lists and your next episodes'
        if params.get('progress_filter') == 'other':
            return f'Your in-progress movies and TV shows on {service}, with anime and Asian titles left out (those are under Anime Progress and Asian Progress)'
        return None
    if info == 'dir_calendar_window' and params.get('calendar_filter'):
        code = str(params.get('countries') or '').upper()
        return 'A month poster grid of the {} movies released, from the Trakt calendar (anime, animation and documentaries left out)'.format(
            _COUNTRIES.get(code, 'Asian') if len(code) == 2 else 'Asian')
    if info == 'dir_calendar_window' and params.get('genres') == 'anime':
        return 'A month poster grid of the anime movies released, from the Trakt calendar (Japanese titles Trakt tags as anime)'
    if info == 'dir_calendar_shows_window' and params.get('calendar_filter'):
        return 'One page with the {} dramas airing on a day, grouped by show, from the Trakt calendar (anime, animation, reality and talk shows left out). Left and Right change the day'.format(
            _COUNTRIES.get(str(params.get('countries') or '').upper(), 'Asian') if len(str(params.get('countries') or '')) == 2 else 'Asian')
    if info == 'dir_calendar_shows_window' and params.get('genres') == 'anime':
        return 'One page with the anime episodes airing on a day, grouped by show, from the Trakt calendar (shows Trakt tags as anime). Left and Right change the day'
    if info == 'dir_calendar_shows_window' and params.get('user') == 'false' and params.get('endpoint') == 'premieres':
        return 'One page with the season and series premieres of every show airing on a day, from the Trakt calendar. Left and Right change the day'
    if info == 'dir_calendar_shows_window' and params.get('user') == 'false' and params.get('endpoint') == 'new':
        return 'One page with the new shows starting on a day, from the Trakt calendar. Left and Right change the day'
    if info == 'dir_calendar_shows_window' and params.get('user') == 'false':
        return 'One page with every episode airing on a day, grouped by show, from the Trakt calendar. Left and Right change the day. Select a show for its unwatched episodes, an episode for its information and Play'
    if info == 'dir_calendar_shows_window' and params.get('endpoint') == 'premieres':
        return 'One page with the season and series premieres of the shows you follow, grouped by show. Select a show for its unwatched episodes, an episode for its information and Play, from your Trakt calendar'
    if info == 'dir_calendar_shows_window' and params.get('endpoint') == 'new':
        return 'One page with the new shows starting that you follow, grouped by show. Select a show for its unwatched episodes, an episode for its information and Play, from your Trakt calendar'
    if info == 'dir_calendar_shows_window':
        return 'One page with every show you follow: its episodes under it, aired and not yet aired. Select a show for its unwatched episodes, an episode for its information and Play, from your Trakt calendar'
    if info == 'dir_calendar_window':
        return _CALENDAR_WINDOW.get((params.get('endpoint'), params.get('user', 'true')))
    if info == 'trakt_calendar':   # the Today / This Week ... rows inside a calendar carry no description (also when they carry countries)
        return None
    if info == 'dir_calendar_trakt' and params.get('calendar_filter'):
        return _asian_calendar(params)
    if info == 'dir_calendar_trakt' and params.get('genres') == 'anime':
        if params.get('tmdb_type') == 'movie':
            return 'Anime movies released this week, from the Trakt calendar (Japanese titles Trakt tags as anime)'
        return 'Anime episodes airing this week, from the Trakt calendar (shows Trakt tags as anime)'
    if info == 'dir_calendar_trakt':
        calendar = _CALENDAR_MOVIES if params.get('tmdb_type') == 'movie' else _CALENDAR
        return calendar.get((params.get('endpoint'), params.get('user', 'true')))
    if info == 'scoped_search':
        scope = {'anime': 'anime', 'asian': 'Asian'}.get(params.get('search_scope'), '')
        media = {'movie': 'movies', 'tv': 'TV shows'}.get(params.get('search_media'), 'movies and TV shows')
        return f'Search {scope} {media}'.replace('  ', ' ')
    if info == 'anime_season':
        movie = params.get('anime_format') == 'MOVIE'
        return {
            'last': 'Anime {x} that aired last season', 'this': 'Anime {x} airing this season',
            'next': 'Anime {x} starting next season'}.get(params.get('period'), '').format(x='movies' if movie else 'TV shows') or None
    if info in ('anime_trending', 'anime_popular'):
        k = 'movies' if params.get('anime_format') == 'MOVIE' else 'TV shows'
        when = _WHEN.get(params.get('period'), '')
        return f'{"Trending" if info == "anime_trending" else "Popular"} anime {k} {when}'.strip()
    if info in ('anime_top100', 'anime_genres', 'dir_anime_trending', 'dir_anime_popular'):
        k = 'movies' if params.get('anime_format') == 'MOVIE' else 'TV shows'
        return _BY_INFO[info].format(k=k)
    if info in ('dir_asian_drama_list', 'dir_asian_movies_list', 'asian_trending', 'asian_movie_trending', 'asian_mostwatched',
                'asian_movie_mostwatched') or (info == 'discover' and 'with_origin_country' in params) or \
            (str(info).startswith('trakt_') and 'countries' in params):
        return _asian(params, label)
    if info in ('dir_asian_drama_genres', 'dir_asian_movies_genres', 'dir_asian_drama_country', 'dir_asian_movies_country',
                'dir_asian_drama', 'dir_asian_movies'):
        return _BY_INFO[info]
    if info == 'trakt_ondeck_unwatched' and params.get('tmdb_type') == 'movie':
        return 'Movies from your Trakt in-progress list that you have not watched yet'
    text = _BY_INFO.get(info)
    if not text:
        return None
    n = _nouns(params)
    return text.format(n=n, N=n[0].upper() + n[1:])


def add_descriptions(items):
    """Give every row without a plot its description (in place). Anything that is not a list of rows is returned untouched."""
    if not isinstance(items, list):
        return items
    for item in items:
        if not isinstance(item, dict) or (item.get('infolabels') or {}).get('plot'):
            continue
        text = describe(item)
        if text:
            item.setdefault('infolabels', {})['plot'] = text
    return items
