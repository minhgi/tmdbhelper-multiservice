from jurialmunkey.parser import try_int, del_empty_keys
from tmdbhelper.lib.items.builder import ItemBuilder
from tmdbhelper.lib.api.tmdb.api import TMDb


EXTERNAL_ID_TYPES = ['tmdb', 'tvdb', 'imdb', 'slug', 'trakt']

# Asian drama enrichment — languages and countries for CJK title extraction
ASIAN_LANGUAGE_CODES = {'zh', 'ko', 'ja', 'th', 'vi'}
ASIAN_TITLE_COUNTRIES = {
    'CN', 'KR', 'TW', 'TH', 'HK', 'VN', 'PH', 'MY', 'IN', 'SG', 'ID', 'JP',
}


def get_asian_drama_aliases(tmdb_type, tmdb_id):
    """Fetch CJK/Asian alternative titles and translations from TMDb.

    Calls two TMDb endpoints:
      1. /{type}/{id}/alternative_titles — titles tagged by country (CN, KR, TW, etc.)
      2. /{type}/{id}/translations — titles in zh, ko, ja, th, vi languages

    Returns a deduplicated list of native-language and romanized title strings.
    Results are cached by TMDb Helper's request cache (get_request_lc).
    """
    from tmdbhelper.lib.addon.logger import kodi_log
    tmdb_api = TMDb()
    seen = set()
    aliases = []

    def _add(title):
        if title and title.strip() and title not in seen:
            seen.add(title)
            aliases.append(title)

    # 1. alternative_titles — country-tagged titles
    try:
        alt_data = tmdb_api.get_request_lc(tmdb_type, tmdb_id, 'alternative_titles')
        if alt_data:
            # TV uses 'results', movies use 'titles'
            entries = alt_data.get('results', []) or alt_data.get('titles', [])
            for t in entries:
                if t.get('iso_3166_1') in ASIAN_TITLE_COUNTRIES:
                    _add(t.get('title'))
    except Exception as e:
        kodi_log([f'get_asian_drama_aliases: alternative_titles failed for {tmdb_type}/{tmdb_id}: ', e], 1)

    # 2. translations — language-tagged titles (includes romanized variants)
    try:
        trans_data = tmdb_api.get_request_lc(tmdb_type, tmdb_id, 'translations')
        if trans_data:
            for t in trans_data.get('translations', []):
                lang = t.get('iso_639_1', '')
                if lang in ASIAN_LANGUAGE_CODES:
                    data = t.get('data', {})
                    # TV shows use 'name', movies use 'title'
                    _add(data.get('name') or data.get('title'))
    except Exception as e:
        kodi_log([f'get_asian_drama_aliases: translations failed for {tmdb_type}/{tmdb_id}: ', e], 1)

    if aliases:
        kodi_log([f'get_asian_drama_aliases: {tmdb_type}/{tmdb_id} → {len(aliases)} aliases: ', aliases], 1)

    return aliases


def get_next_episodes(tmdb_id, season, episode, player=None):
    from tmdbhelper.lib.addon.thread import ParallelThread
    tmdb_api = TMDb()

    all_episodes = tmdb_api.get_flatseasons_list(tmdb_id)
    if not all_episodes:
        return

    snum, enum = try_int(season), try_int(episode)

    def _is_future_ep(i):
        i_snum = try_int(i['infolabels'].get('season', -1))
        i_enum = try_int(i['infolabels'].get('episode', -1))
        if i_snum == snum and i_enum >= enum:
            return True
        if i_snum > snum:
            return True
        return False

    nxt_episodes = [i for i in all_episodes if _is_future_ep(i)]
    if not nxt_episodes:
        return

    ib = ItemBuilder(tmdb_api=tmdb_api)
    ib.get_parents(tmdb_type='tv', tmdb_id=tmdb_id)
    ib.parent_params = {'tmdb_id': tmdb_id, 'tmdb_type': 'tv'}

    def _make_listitem(i):
        li = ib.get_listitem(i)
        if not li:
            return
        li.set_params_reroute()  # Reroute details to proper end point
        if player:
            li.params['player'] = player
            li.params['mode'] = 'play'
        return li

    with ParallelThread(nxt_episodes, _make_listitem) as pt:
        item_queue = pt.queue
    return [i for i in item_queue if i]


def get_external_ids(tmdb_type, tmdb_id, season=None, episode=None):
    from tmdbhelper.lib.api.trakt.api import TraktAPI
    trakt_api = TraktAPI()
    trakt_type = 'movie' if tmdb_type == 'movie' else 'show'
    if not tmdb_id or not trakt_type:
        return

    trakt_id = trakt_api.get_id(id_type='tmdb', unique_id=tmdb_id, trakt_type=trakt_type, output_type='trakt')
    if not trakt_id:
        return

    details = trakt_api.get_details(trakt_type, trakt_id, extended=None)
    if not details or not details.get('ids'):
        return

    if episode is not None:
        _uids = {f'tvshow.{i}': details['ids'][i] for i in EXTERNAL_ID_TYPES if details['ids'].get(i)}
        _episode_details_ids = trakt_api.get_details(trakt_type, trakt_id, season=season, episode=episode, extended=None).get('ids') or {}
        _uids.update({f'{i}': _episode_details_ids[i] for i in EXTERNAL_ID_TYPES if _episode_details_ids.get(i)})
        _uids['tvshow.tmdb'] = tmdb_id
    else:
        _uids = {i: details['ids'][i] for i in EXTERNAL_ID_TYPES if details['ids'].get(i)}
        _uids['tmdb'] = tmdb_id

    return {'unique_ids': _uids}


def get_item_details(tmdb_type, tmdb_id, season=None, episode=None, language=None):
    from tmdbhelper.lib.items.listitem import ListItem
    tmdb_api = TMDb(language=language) if language else TMDb()
    ib = ItemBuilder(tmdb_api=tmdb_api)
    details = ib.get_item(tmdb_type, tmdb_id, season, episode)
    try:
        artwork = details['artwork']
        details = details['listitem']
    except (KeyError, TypeError):
        return
    if not details:
        return
    details['art'] = ib.get_item_artwork(artwork, is_season=True if season else False)
    details = ListItem(**details)
    details.infolabels['mediatype'] == 'movie' if tmdb_type == 'movie' else 'episode'
    return details


def _get_language_details(tmdb_type, tmdb_id, season=None, episode=None, language=None):
    affix = ''
    if tmdb_type == 'tv':
        if season is not None:
            affix = f'{affix}season/{season}/'
            if episode is not None:
                affix = f'{affix}episode/{episode}/'
    affix = f'{affix}translations'
    details = TMDb().get_request_lc(tmdb_type, tmdb_id, affix)
    if not details or not details.get('translations'):
        return

    item = {}
    for i in details['translations']:
        if i.get('iso_639_1') == language:
            item['title'] = i.get('data', {}).get('title') or i.get('data', {}).get('name')
            item['plot'] = i.get('data', {}).get('overview')
            return item


def _get_language_item(tmdb_type, tmdb_id, season=None, episode=None, language=None, year=None):
    item = _get_language_details(tmdb_type, tmdb_id, language=language)
    if not item:
        return

    item['showname'] = item['clearname'] = item['tvshowtitle'] = item.get('title')
    item['name'] = f'{item["title"]} ({year})' if item.get('title') and year else None
    if season is None or episode is None:
        return item

    episode = _get_language_details(tmdb_type, tmdb_id, season, episode, language=language)
    if not episode:
        return item

    item['title'] = episode.get('title')
    item['plot'] = episode.get('plot') or item.get('plot')
    item['name'] = f'{item["showname"]} S{try_int(season):02d}E{try_int(episode):02d}' if item.get('showname') else None
    return item


def get_language_details(base, tmdb_type, tmdb_id, season=None, episode=None, language=None, year=None):
    if not language:
        return base
    item = _get_language_item(tmdb_type, tmdb_id, season, episode, language, year)
    if not item:
        return base
    item = {k: v or base.get(k) for k, v in item.items()}  # Fallback to default key in base if translation is empty
    item = _url_encode_item(item)
    for k, v in item.items():
        base[f'{language}_{k}'] = v
    return _url_encode_item(base)


def _url_encode_item(item, base=None):
    from tmdbhelper.lib.addon.consts import PLAYERS_URLENCODE
    from urllib.parse import quote_plus, quote
    from json import dumps
    base = base or item.copy()
    for k, v in base.items():
        if k not in PLAYERS_URLENCODE:
            continue
        v = f'{v}'
        d = {k: v, f'{k}_meta': dumps(v)}
        for key, value in d.items():
            item[key] = value.replace(',', '')
            item[key + '_+'] = value.replace(',', '').replace(' ', '+')
            item[key + '_-'] = value.replace(',', '').replace(' ', '-')
            item[key + '_escaped'] = quote(quote(value))
            item[key + '_escaped+'] = quote(quote_plus(value))
            item[key + '_url'] = quote(value)
            item[key + '_url+'] = quote_plus(value)
    return item


def set_detailed_item(tmdb_type, tmdb_id, season=None, episode=None, details=None):
    from tmdbhelper.lib.addon.tmdate import get_datetime_now
    from collections import defaultdict
    if not details:
        return
    item = defaultdict(lambda: '_')
    item['id'] = item['tmdb'] = tmdb_id
    item['imdb'] = details.unique_ids.get('imdb')
    item['tvdb'] = details.unique_ids.get('tvdb')
    item['trakt'] = details.unique_ids.get('trakt')
    item['slug'] = details.unique_ids.get('slug')
    item['season'] = season
    item['episode'] = episode
    item['originaltitle'] = details.infolabels.get('originaltitle')
    item['title'] = details.infolabels.get('tvshowtitle') or details.infolabels.get('title')
    item['showname'] = item['clearname'] = item['tvshowtitle'] = item.get('title')
    item['year'] = details.infolabels.get('year')
    item['name'] = f'{item.get("title")} ({item.get("year")})'
    item['premiered'] = item['firstaired'] = item['released'] = details.infolabels.get('premiered')
    item['plot'] = details.infolabels.get('plot')
    item['cast'] = item['actors'] = " / ".join([i.get('name') for i in details.cast if i.get('name')])
    item['thumbnail'] = details.art.get('thumb')
    item['poster'] = details.art.get('poster')
    item['fanart'] = details.art.get('fanart')
    item['now'] = get_datetime_now().strftime('%Y%m%d%H%M%S%f')

    if tmdb_type == 'tv' and season is not None and episode is not None:
        item['id'] = item['epid'] = item['eptvdb'] = item.get('tvdb')
        item['title'] = details.infolabels.get('title')  # Set Episode Title
        item['name'] = f'{item.get("showname")} S{try_int(season):02d}E{try_int(episode):02d}'
        item['season'] = season
        item['episode'] = episode
        item['showpremiered'] = details.infoproperties.get('tvshow.premiered')
        item['showyear'] = details.infoproperties.get('tvshow.year')
        item['eptmdb'] = details.unique_ids.get('tmdb')
        item['epimdb'] = details.unique_ids.get('imdb')
        item['eptrakt'] = details.unique_ids.get('trakt')
        item['epslug'] = details.unique_ids.get('slug')
        item['tmdb'] = details.unique_ids.get('tvshow.tmdb')
        item['imdb'] = details.unique_ids.get('tvshow.imdb')
        item['tvdb'] = details.unique_ids.get('tvshow.tvdb')
        item['trakt'] = details.unique_ids.get('tvshow.trakt')
        item['slug'] = details.unique_ids.get('tvshow.slug')

    # Asian drama alias enrichment — pass CJK titles to Seren via two channels:
    #   1. Window property TMDbHelper.AsianDramaAliases (primary — reliable, bypasses URL pipeline)
    #   2. Player URL template variable {aliases_urlencoded} (backup — requires modified player JSONs)
    # Seren reads whichever is available; window property takes priority.
    from urllib.parse import quote
    from jurialmunkey.window import get_property
    import json
    item['aliases_urlencoded'] = quote('[]', safe='')  # Default: empty array
    try:
        original_language = details.infoproperties.get('original_language', '')
        if original_language in ASIAN_LANGUAGE_CODES:
            # For episodes, use the show-level tmdb_id for title lookups
            show_tmdb_id = tmdb_id
            api_type = tmdb_type
            if tmdb_type == 'tv' and season is not None and episode is not None:
                show_tmdb_id = details.unique_ids.get('tvshow.tmdb') or tmdb_id
            if tmdb_type != 'movie':
                api_type = 'tv'
            _asian_aliases = get_asian_drama_aliases(api_type, show_tmdb_id)
            if _asian_aliases:
                _aliases_json = json.dumps(_asian_aliases, ensure_ascii=False)
                # Channel 1: Window property (Seren reads this directly — no URL encoding issues)
                get_property('AsianDramaAliases', set_property=_aliases_json)
                # Channel 2: URL template variable (for player JSONs that include {aliases_urlencoded})
                item['aliases_urlencoded'] = quote(_aliases_json, safe='')
            else:
                # No aliases — clear window property so Seren doesn't read stale data
                get_property('AsianDramaAliases', clear_property=True)
        else:
            # Non-Asian content — clear window property
            get_property('AsianDramaAliases', clear_property=True)
    except Exception:
        get_property('AsianDramaAliases', clear_property=True)

    return _url_encode_item(item)


def get_playerstring(tmdb_type, tmdb_id, season=None, episode=None, details=None):
    from json import dumps
    if not details:
        return None
    playerstring = {}
    playerstring['tmdb_type'] = 'episode' if tmdb_type in ['episode', 'tv'] else 'movie'
    playerstring['tmdb_id'] = tmdb_id
    playerstring['imdb_id'] = details.unique_ids.get('imdb')
    if tmdb_type in ['episode', 'tv']:
        playerstring['imdb_id'] = details.unique_ids.get('tvshow.imdb')
        playerstring['tvdb_id'] = details.unique_ids.get('tvshow.tvdb')
        playerstring['season'] = season
        playerstring['episode'] = episode
    return dumps(del_empty_keys(playerstring))
