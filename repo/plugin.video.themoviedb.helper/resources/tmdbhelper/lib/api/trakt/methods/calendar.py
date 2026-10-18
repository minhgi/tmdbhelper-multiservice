from tmdbhelper.lib.api.trakt.api import is_authorized
from tmdbhelper.lib.files.bcache import use_simple_cache


@is_authorized
def get_calendar(self, trakt_type, user=True, start_date=None, days=None, endpoint=None, countries=None, genres=None, **kwargs):
    user = 'my' if user else 'all'
    return self.get_response_json('calendars', user, trakt_type, endpoint, start_date, days, extended='full', countries=countries, genres=genres)


# Genres (Trakt slugs) a filtered calendar leaves out. Trakt has no exclude filter and its country filter is mostly anime for
# Japan and donghua for China, so the Asian calendars drop these from the response (same exclusions as the Asian menus).
CALENDAR_EXCLUDE_GENRES = {
    'asian_tv': {'anime', 'donghua', 'animation', 'reality', 'talk-show', 'game-show'},
    'asian_movie': {'anime', 'donghua', 'animation', 'documentary'}}


def calendar_filter_item(i, key, calendar_filter):
    exclude = CALENDAR_EXCLUDE_GENRES.get(calendar_filter)
    if not exclude:
        return True
    return not exclude.intersection(((i.get(key) or {}).get('genres') or []))


CALENDAR_SHOW_COLOR = 'FF4FC3F7'


def get_calendar_episode_label(show_title, episode_code, title=None):
    """ "1x02. Episode Title (Show Name)" with the show name coloured (or "1x02 (Show Name)" when the episode has no title): the calendar mixes many shows """
    label = f'{episode_code}. {title}' if title else episode_code
    return f'{label} [COLOR {CALENDAR_SHOW_COLOR}]({show_title})[/COLOR]' if show_title else label


@use_simple_cache(cache_days=0.25)
def get_calendar_episodes(self, startdate=0, days=1, user=True, endpoint=None, countries=None, genres=None):
    # Broaden date range in case utc conversion bumps into different day
    from jurialmunkey.parser import try_int
    from tmdbhelper.lib.addon.tmdate import get_datetime_today, get_timedelta
    mod_date = try_int(startdate) - 1
    mod_days = try_int(days) + 2
    date = get_datetime_today() + get_timedelta(days=mod_date)
    if countries or genres:  # filtered "All" calendar: public endpoint, so a logged-out user is not sent to the Trakt login prompt
        return get_calendar(
            self, 'shows', user, start_date=date.strftime('%Y-%m-%d'), days=mod_days, endpoint=endpoint,
            countries=countries, genres=genres, authorize=bool(user))
    return get_calendar(self, 'shows', user, start_date=date.strftime('%Y-%m-%d'), days=mod_days, endpoint=endpoint, authorize=bool(user))


def get_local_air_datetime(first_aired):
    """ Trakt's UTC first_aired as a naive local datetime, using the DST rule in force on the air date itself. convert_timestamp applies
    today's DST state to every date, so an episode after the next DST change would show one hour off. Falls back to convert_timestamp
    when the OS cannot convert the date. """
    import datetime
    if not first_aired:
        return
    try:
        utc_time = datetime.datetime.strptime(first_aired[:19], '%Y-%m-%dT%H:%M:%S')
        return utc_time.replace(tzinfo=datetime.timezone.utc).astimezone().replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        from tmdbhelper.lib.addon.tmdate import convert_timestamp
        return convert_timestamp(first_aired, utc_convert=True)


def get_calendar_episode_item(i):
    from jurialmunkey.parser import try_int
    from tmdbhelper.lib.addon.tmdate import get_region_date, get_days_to_air
    from tmdbhelper.lib.api.mapping import get_empty_item

    air_date = get_local_air_datetime(i.get('first_aired'))
    epsd = i.get('episode', {})
    show = i.get('show', {})
    sids = show.get('ids', {})
    item = get_empty_item()
    item['label'] = epsd.get('title')
    item['infolabels'] = {
        'mediatype': 'episode',
        'premiered': air_date.strftime('%Y-%m-%d'),
        'year': air_date.strftime('%Y'),
        'title': item['label'],
        'episode': epsd.get('number'),
        'season': epsd.get('season'),
        'tvshowtitle': show.get('title'),
        'duration': try_int(epsd.get('runtime', 0)) * 60,
        'plot': epsd.get('overview'),
        'mpaa': show.get('certification')}
    item['infoproperties'] = {
        'air_date': get_region_date(air_date, 'datelong'),
        'air_time': get_region_date(air_date, 'time'),
        'air_day': air_date.strftime('%A'),
        'air_day_short': air_date.strftime('%a'),
        'air_date_short': air_date.strftime('%d %b')}

    if epsd.get('episode_type'):  # Trakt's own value (series_premiere, season_premiere, season_finale, ...); the window shows it as a badge
        item['infoproperties']['calendar_episode_type'] = epsd['episode_type']

    days_to_air, is_aired = get_days_to_air(air_date)
    item['infoproperties']['days_from_aired' if is_aired else 'days_until_aired'] = str(days_to_air)
    item['unique_ids'] = {f'tvshow.{k}': v for k, v in sids.items()}
    item['params'] = {
        'info': 'details',
        'tmdb_type': 'tv',
        'tmdb_id': sids.get('tmdb'),
        'episode': epsd.get('number'),
        'season': epsd.get('season')}
    return item


def get_calendar_episode_item_bool(i, kodi_db, user, startdate, days):
    import datetime
    if kodi_db and not user:
        try:
            sids = i['show']['ids']
        except KeyError:
            return False
        if not kodi_db.get_info(info='dbid', tmdb_id=sids.get('tmdb'), tvdb_id=sids.get('tvdb'), imdb_id=sids.get('imdb')):
            return False
    # Do some timezone conversion so we check that we're in the date range for our timezone (same conversion as the item's air date and time)
    air_date = get_local_air_datetime(i.get('first_aired'))
    if not air_date:
        return False
    date_a = datetime.date.today() + datetime.timedelta(days=startdate)
    return date_a <= air_date.date() < date_a + datetime.timedelta(days=days)


def get_stacked_item(next_item, last_item, stack_info='episodes'):
    # If the next item is the same show then we stack the details onto the last item and add it next iteration
    from jurialmunkey.parser import try_int
    from tmdbhelper.lib.addon.plugin import get_localized
    ip = last_item['infoproperties']

    # First time setup
    if not ip.get('stacked_count'):
        ip['stacked_count'] = 1
        ti = last_item['infolabels']['title']
        se = f'{try_int(last_item["infolabels"]["season"])}x{try_int(last_item["infolabels"]["episode"]):0>2}'
        ep = f'{se}. {ti}'
        ip['stacked_labels'] = ep
        ip['stacked_titles'] = ti
        ip['stacked_episodes'] = se
        ip['stacked_first'] = f'{try_int(last_item["infolabels"].get("season"))}x{try_int(last_item["infolabels"].get("episode")):0>2}'
        ip['stacked_first_episode'] = last_item['infolabels']['episode']
        ip['stacked_first_season'] = last_item['infolabels']['season']
        ip['no_label_formatting'] = True
        last_item['params'].pop('episode', None)
        last_item['params']['info'] = stack_info
        last_item['is_folder'] = True

    # Stacked Setup
    ti = next_item['infolabels']['title']
    se = f'{try_int(next_item["infolabels"]["season"])}x{try_int(next_item["infolabels"]["episode"]):0>2}'
    ep = f'{se}. {ti}'
    ip['stacked_count'] = ip.get('stacked_count', 1) + 1
    ip['stacked_labels'] = f'{ip["stacked_labels"]}, {ep}'
    ip['stacked_titles'] = f'{ip["stacked_titles"]}, {ti}'
    ip['stacked_episodes'] = f'{ip["stacked_episodes"]}, {se}'
    ip['stacked_last'] = se
    ip['stacked_last_episode'] = next_item['infolabels']['episode']
    ip['stacked_last_season'] = next_item['infolabels']['season']
    last_item['label'] = get_calendar_episode_label(
        last_item['infolabels'].get('tvshowtitle'), f'{ip["stacked_first"]}-{ip["stacked_last"]}', f'{ip["stacked_count"]} {get_localized(20360)}')
    return last_item


def stack_calendar_episodes(episode_list, flipped=False, stack_info='episodes'):
    items = []
    last_item = None
    for i in reversed(episode_list) if flipped else episode_list:
        if not last_item:
            last_item = i
            continue

        # If the next item is a different show or day then we stop stacking and add the item
        if (
                last_item['infolabels']['tvshowtitle'] != i['infolabels']['tvshowtitle']
                or last_item['infolabels']['premiered'] != i['infolabels']['premiered']
        ):
            items.append(last_item)
            last_item = i
            continue

        last_item = get_stacked_item(i, last_item, stack_info=stack_info)

    else:  # The last item in the list won't be added in the for loop so do an extra action at the end to add it
        if last_item:
            items.append(last_item)
    return items[::-1] if flipped else items


def stack_calendar_tvshows(self, tvshow_list, flipped=False):
    items, titles = [], []
    for i in reversed(tvshow_list) if flipped else tvshow_list:
        t = i['infolabels']['title']
        if t not in titles:
            items.append(i)
            titles.append(t)
            continue
        x = titles.index(t)
        i = items[x]
        i['infoproperties']['stacked_count'] = i['infoproperties'].get('stacked_count', 0) + 1
    return items[::-1] if flipped else items


@use_simple_cache(cache_days=0.25)
def get_calendar_episodes_listitems(self, startdate=0, days=1, user=True, kodi_db=None, stack=True, endpoint=None, stack_info='episodes', countries=None, calendar_filter=None, genres=None):
    # Get response (countries / genres / calendar_filter are passed on only when set, so the unfiltered TV calendar keeps its cache keys)
    response = get_calendar_episodes(
        self, startdate=startdate, days=days, user=user, endpoint=endpoint,
        **{k: v for k, v in (('countries', countries), ('genres', genres)) if v})
    if not response:
        return
    if calendar_filter:
        response = [i for i in response if calendar_filter_item(i, 'show', calendar_filter)]
    # Reverse items for date ranges in past
    traktitems = reversed(response) if startdate < -1 else response
    items = [get_calendar_episode_item(i) for i in traktitems if get_calendar_episode_item_bool(i, kodi_db, user, startdate, days)]
    if not stack:
        return items
    return stack_calendar_episodes(items, flipped=startdate < -1, stack_info=stack_info)


def get_calendar_episodes_list(self, startdate=0, days=1, user=True, kodi_db=None, page=1, limit=None, endpoint=None, countries=None, calendar_filter=None, genres=None, stack=None):
    from tmdbhelper.lib.addon.plugin import get_setting
    from tmdbhelper.lib.items.pages import PaginatedItems
    limit = limit or self.sync_item_limit
    if stack is None:  # the Your TV Calendar window asks for unstacked episodes (unstacked=true); every other calendar follows the setting
        stack = get_setting('calendar_flatten')
    stack_info = 'details' if kodi_db and get_setting('nextaired_linklibrary') else 'episodes'  # Fix for stacked episodes linking to library
    extra = {k: v for k, v in (('countries', countries), ('calendar_filter', calendar_filter), ('genres', genres)) if v}
    response_items = get_calendar_episodes_listitems(self, startdate, days, user, kodi_db, stack=stack, endpoint=endpoint, stack_info=stack_info, **extra)
    response = PaginatedItems(response_items, page=page, limit=limit)
    if response and response.items:
        return response.items + response.next_page


# Movies: Trakt calendars/{my|all}/movies (theatrical / digital releases) and calendars/{my|all}/dvd (physical releases).
# Release dates are plain dates (no time), so the window is requested exactly and there is no UTC conversion.
MOVIE_DATE_FMT = '%Y-%m-%d'
MOVIE_DATE_LEN = 10


@use_simple_cache(cache_days=0.25)
def get_calendar_movies(self, startdate=0, days=1, user=True, endpoint=None, countries=None, genres=None):
    from tmdbhelper.lib.addon.tmdate import get_datetime_today, get_timedelta
    from jurialmunkey.parser import try_int
    date = get_datetime_today() + get_timedelta(days=try_int(startdate))
    # Public endpoints: a logged-out "All" calendar must not stop at the Trakt login prompt (authorize=False skips it)
    return get_calendar(
        self, 'dvd' if endpoint == 'dvd' else 'movies', user,
        start_date=date.strftime('%Y-%m-%d'), days=try_int(days), countries=countries, genres=genres, authorize=bool(user))


def get_calendar_movie_item(i):
    from jurialmunkey.parser import try_int
    from tmdbhelper.lib.addon.tmdate import convert_timestamp, get_region_date, get_days_to_air
    from tmdbhelper.lib.api.mapping import get_empty_item

    movie = i.get('movie') or {}
    ids = movie.get('ids') or {}
    if not ids.get('tmdb'):
        return
    release_date = convert_timestamp(i.get('released'), MOVIE_DATE_FMT, MOVIE_DATE_LEN)
    if not release_date:
        return
    item = get_empty_item()
    item['label'] = movie.get('title')
    item['infolabels'] = {
        'mediatype': 'movie',
        'premiered': release_date.strftime('%Y-%m-%d'),
        'year': movie.get('year') or release_date.strftime('%Y'),
        'title': item['label'],
        'duration': try_int(movie.get('runtime', 0)) * 60,
        'plot': movie.get('overview'),
        'mpaa': movie.get('certification')}
    item['infoproperties'] = {
        'air_date': get_region_date(release_date, 'datelong'),
        'air_day': release_date.strftime('%A'),
        'air_day_short': release_date.strftime('%a'),
        'air_date_short': release_date.strftime('%d %b')}
    days_to_air, is_aired = get_days_to_air(release_date)
    item['infoproperties']['days_from_aired' if is_aired else 'days_until_aired'] = str(days_to_air)
    item['unique_ids'] = {k: v for k, v in ids.items() if v}
    item['params'] = {
        'info': 'details',
        'tmdb_type': 'movie',
        'tmdb_id': ids.get('tmdb')}
    return item


def get_calendar_movie_item_bool(i, startdate, days):
    from tmdbhelper.lib.addon.tmdate import date_in_range
    return bool(date_in_range(i.get('released'), days, startdate, date_fmt=MOVIE_DATE_FMT, date_lim=MOVIE_DATE_LEN))


@use_simple_cache(cache_days=0.25)
def get_calendar_movies_listitems(self, startdate=0, days=1, user=True, endpoint=None, countries=None, calendar_filter=None, genres=None):
    response = get_calendar_movies(
        self, startdate=startdate, days=days, user=user, endpoint=endpoint, countries=countries, **({'genres': genres} if genres else {}))
    if not response or not isinstance(response, list):
        return
    if calendar_filter:
        response = [i for i in response if calendar_filter_item(i, 'movie', calendar_filter)]
    # Reverse items for date ranges in past
    traktitems = reversed(response) if startdate < -1 else response
    items = [get_calendar_movie_item(i) for i in traktitems if get_calendar_movie_item_bool(i, startdate, days)]
    return [i for i in items if i]


def get_calendar_movies_list(self, startdate=0, days=1, user=True, page=1, limit=None, endpoint=None, countries=None, calendar_filter=None, genres=None):
    from tmdbhelper.lib.items.pages import PaginatedItems
    limit = limit or self.sync_item_limit
    extra = {k: v for k, v in (('calendar_filter', calendar_filter), ('genres', genres)) if v}   # only when set: earlier movie calendars keep their cache keys
    response_items = get_calendar_movies_listitems(self, startdate, days, user, endpoint=endpoint, countries=countries, **extra)
    response = PaginatedItems(response_items, page=page, limit=limit)
    if response and response.items:
        return response.items + response.next_page
