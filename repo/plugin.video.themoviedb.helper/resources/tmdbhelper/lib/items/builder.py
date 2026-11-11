import re
from tmdbhelper.lib.items.artselect import _ArtworkSelector
from tmdbhelper.lib.addon.plugin import get_setting, fanarttv_art_enabled
from tmdbhelper.lib.items.listitem import ListItem
from tmdbhelper.lib.files.bcache import BasicCache, BasicCacheService
from tmdbhelper.lib.api.tmdb.api import TMDb
from tmdbhelper.lib.api.fanarttv.api import FanartTV
from tmdbhelper.lib.addon.tmdate import set_timestamp, get_timestamp
from tmdbhelper.lib.addon.consts import IMAGEPATH_QUALITY_POSTER, IMAGEPATH_QUALITY_FANART, IMAGEPATH_QUALITY_THUMBS, IMAGEPATH_QUALITY_CLOGOS, IMAGEPATH_ALL, ARTWORK_BLACKLIST
from tmdbhelper.lib.addon.thread import ParallelThread
from tmdbhelper.lib.addon.logger import TimerList, kodi_log

IMAGEPATH_KEYS = {  # art key -> which of the quality tables it follows
    "fanart": IMAGEPATH_QUALITY_FANART,
    "tvshow.fanart": IMAGEPATH_QUALITY_FANART,
    "season.fanart": IMAGEPATH_QUALITY_FANART,
    "landscape": IMAGEPATH_QUALITY_THUMBS,
    "tvshow.landscape": IMAGEPATH_QUALITY_THUMBS,
    "season.landscape": IMAGEPATH_QUALITY_THUMBS,
    "clearlogo": IMAGEPATH_QUALITY_CLOGOS,
    "tvshow.clearlogo": IMAGEPATH_QUALITY_CLOGOS,
    "season.clearlogo": IMAGEPATH_QUALITY_CLOGOS,
    "thumb": IMAGEPATH_QUALITY_THUMBS,
    "tvshow.thumb": IMAGEPATH_QUALITY_THUMBS,
    "season.thumb": IMAGEPATH_QUALITY_THUMBS,
    "poster": IMAGEPATH_QUALITY_POSTER,
    "tvshow.poster": IMAGEPATH_QUALITY_POSTER,
    "season.poster": IMAGEPATH_QUALITY_POSTER
}


def get_artwork_quality():
    """The Artwork quality setting, read live and never at import: a long-running process (the service) must follow a change (GitHub issue #9)."""
    quality = get_setting('artwork_quality', 'int')
    return quality if 0 <= quality < len(IMAGEPATH_QUALITY_POSTER) else 0


def get_imagepath_map(quality):
    """Art key -> image path for a quality (0 original ... 3 low)."""
    return {k: table[quality] for k, table in IMAGEPATH_KEYS.items()}
CACHE_DAYS = 10000
BACKFILL_BLACKLIST = ['poster']

# TheTVDB art follows the same artwork quality setting as TMDb. TVDB only has the full image (fanart 1920 px, logo 800 px)
# and a thumbnail, so the thumbnail is used when it is at least TVDB_THUMB_MIN_SHARE of the TMDb size for the chosen quality
# (quality 0 = original: always the full image). Thumbnail widths are not in the TVDB answer (thumbnailWidth is 0), so
# these are measured values (fanart 1920x1080 -> 640x360, logo 800x310 -> 400x155).
TVDB_THUMB_SUFFIX = '.thumb'  # same key suffix api/tvdb/artwork.py stores the thumbnail under ('fanart.thumb')
TVDB_THUMB_WIDTH = {'clearlogo': 400, 'fanart': 640}
TVDB_THUMB_MIN_SHARE = 0.8


def _tvdb_thumb_fits(tmdb_imagepath, kind):
    match = re.search(r'/w(\d+)$', tmdb_imagepath)  # '.../original' has no width: full image
    return bool(match) and TVDB_THUMB_WIDTH[kind] >= TVDB_THUMB_MIN_SHARE * int(match.group(1))


def get_tvdb_use_thumb(quality):
    return {
        'clearlogo': _tvdb_thumb_fits(IMAGEPATH_QUALITY_CLOGOS[quality], 'clearlogo'),
        'fanart': _tvdb_thumb_fits(IMAGEPATH_QUALITY_FANART[quality], 'fanart'),
    }


class ItemBuilder(_ArtworkSelector):
    _basic_cache_cls = BasicCache

    def __init__(self, tmdb_api=None, ftv_api=None, trakt_api=None, cache_only=False, log_timers=False, timer_lists: dict = None, tvdb_art_api=None, ftv_art_forced=False):
        self.ftv_art_forced = ftv_art_forced  # the skin path says fanarttv=true: fanart.tv art is wanted whatever the settings
        self.parent_tv = {}
        self.parent_season = {}
        self.tmdb_api = tmdb_api or TMDb()
        self.ftv_api = ftv_api or FanartTV()
        self.trakt_api = trakt_api
        self.tvdb_art_api = tvdb_art_api  # optional TheTVDB clearlogo/fanart gap-fill: lookups happen only when this is set
        self.tvdb_art_enabled = get_setting('tvdb_artwork')  # cached TVDB art is shown only while the setting is on
        self._cache = self._basic_cache_cls(filename='ItemBuilder.db')
        self._regex = re.compile(r'({})'.format('|'.join(IMAGEPATH_ALL)))
        self.parent_params = None
        self.cache_only = cache_only
        self.timer_lists = timer_lists if isinstance(timer_lists, dict) else {}
        self.log_timers = log_timers

    def _timestamp(self, days=14):
        return set_timestamp(days * 24 * 3600)

    def _timeint(self, x, div=5):
        return int(x) // div

    def get_parents(self, tmdb_type, tmdb_id, season=None):
        with TimerList(self.timer_lists, f'{tmdb_type}.{tmdb_id}.{season}', log_threshold=0.05, logging=self.log_timers):
            if tmdb_type != 'tv' or not tmdb_id:
                return
            self.parent_tv = self.get_item(tmdb_type=tmdb_type, tmdb_id=tmdb_id)
            if season is None:
                return
            self.parent_season = self.get_item(tmdb_type=tmdb_type, tmdb_id=tmdb_id, season=season)

    def map_artwork(self, artwork):
        """ Remaps artwork from TMDb to expected quality """
        imagepath_map = get_imagepath_map(get_artwork_quality())
        return {k: self._regex.sub(imagepath_map[k], v) for k, v in artwork.items() if v and k in imagepath_map}

    def join_base_artwork(self, base_artwork, base_items, prefix='', backfill=False):
        for k, v in base_artwork.items():
            if not v:
                continue
            if k[:7] == 'tvshow.':
                if not prefix:
                    base_items[k] = v
                continue
            k = f'{prefix}{k}'
            base_items[k] = v
        backfill_items = base_items.copy() if backfill else {}
        for k, v in backfill_items.items():
            if k[:7] != 'tvshow.':
                continue
            k = k.replace('tvshow.', '')
            if k in base_items:
                continue
            if k not in BACKFILL_BLACKLIST:
                base_items[k] = v
        return base_items

    def get_ftv_typeid(self, tmdb_type, item, season=None, tmdb_id=None):
        unique_ids = item['listitem'].get('unique_ids', {}) if item else {}
        if tmdb_type in ('movie', 'collection'):
            return (tmdb_id or unique_ids.get('tmdb'), 'movies')
        if tmdb_type == 'tv':
            if season is None:
                return (unique_ids.get('tvdb'), 'tv')
            return (unique_ids.get('tvshow.tvdb') or unique_ids.get('tvdb'), 'tv')
        return None, None

    def _get_ftv_artwork(self, tmdb_type, item, season=None, tmdb_id=None):
        with TimerList(self.timer_lists, 'item_ftv', log_threshold=0.05, logging=self.log_timers):
            artwork = None
            if not self.ftv_api:
                return
            ftv_id, ftv_type = self.get_ftv_typeid(tmdb_type, item, season, tmdb_id)
            if not ftv_type:
                return
            if not ftv_id:
                return -1 if ftv_type == 'movies' else None
            artwork = self.ftv_api.get_all_artwork(ftv_id, ftv_type, season)
        return artwork

    def _get_tvdb_artwork(self, tmdb_type, item, art, ftv_art):
        """ Gap-fill from TheTVDB. Only asked when TMDb and fanart.tv gave no clearlogo or no fanart.
        Returns {} when nothing is needed or TVDB has nothing, None when TVDB could not be reached (retried later). """
        with TimerList(self.timer_lists, 'item_tvdb', log_threshold=0.05, logging=self.log_timers):
            ftv_art = ftv_art or {}
            if all(art.get(k) or ftv_art.get(k) for k in ('clearlogo', 'fanart')):
                return {}
            return self.tvdb_art_api.get_artwork(tmdb_type, item['listitem'].get('unique_ids', {}), self.tmdb_api.language)

    def _needs_tvdb(self, item):
        return bool(self.tvdb_art_api) and not self.tvdb_art_api.cache_only and 'tvdb' not in item['artwork']  # cache-only TVDB cannot fetch, so an up-to-date item is not rebuilt for it

    def _get_tmdb_artwork(self, item):
        if not item or 'artwork' not in item:
            return {}
        return item['artwork'].setdefault(get_artwork_quality(), self.map_artwork(item['artwork'].get('tmdb')) or {})

    def get_artwork(self, item, tmdb_type, season=None, episode=None, base_item=None, prefix='', ftv_art=None):
        if not item:
            return

        # TMDb Artwork reconfigure quality and merge base_item
        item_artwork = self._get_tmdb_artwork(item)
        item_artwork = self.join_base_artwork(self._get_tmdb_artwork(base_item), item_artwork, prefix=prefix, backfill=True)
        item['artwork'][get_artwork_quality()] = item_artwork

        # FanartTV retrieve artwork and merge base_item
        ftv_art = ftv_art or item['artwork'].setdefault('fanarttv', {})
        if not ftv_art and episode is None:  # No episode art on ftv so don't look it up
            ftv_art = self._get_ftv_artwork(tmdb_type, base_item or item, season=season) or {}
        if ftv_art == -1:
            ftv_art = {}
        item['artwork']['fanarttv'] = ftv_art
        if base_item and 'artwork' in base_item:
            self.join_base_artwork(base_item['artwork'].get('fanarttv', {}), ftv_art, prefix=prefix, backfill=True)

        # TheTVDB gap-fill (optional). Movies and shows ask TVDB; seasons and episodes inherit the show's TVDB art
        if self.tvdb_art_api and 'tvdb' not in item['artwork']:
            if season is None and episode is None:
                tvdb_art = self._get_tvdb_artwork(tmdb_type, item, item_artwork, ftv_art)
            else:
                base_tvdb = base_item['artwork'].get('tvdb') if base_item and 'artwork' in base_item else {}  # None = the show has not been asked yet
                tvdb_art = None if base_tvdb is None else self.join_base_artwork(base_tvdb, {}, prefix=prefix, backfill=True)
            if tvdb_art is not None:  # None = TVDB unreachable: leave unset so a later build tries again
                item['artwork']['tvdb'] = tvdb_art
        return item

    def get_tmdb_item(
            self, tmdb_type, tmdb_id, season=None, episode=None, base_item=None, manual_art=None,
            base_is_season=False, cache_refresh=False):
        if season == -1:
            return
        with TimerList(self.timer_lists, 'item_tmdb', log_threshold=0.05, logging=self.log_timers) as tl:
            details = self.tmdb_api.get_details_request(tmdb_type, tmdb_id, season, episode, cache_refresh=cache_refresh)
            if not details:
                if self.log_timers and tl.total_time > tl.log_threshold:  # TMDb API missing item so log fail time
                    kodi_log(f'item_tmdb -- get_details_request({tmdb_type},{tmdb_id},{season},{episode}) FAILED after {tl.total_time:.3f} sec', 1)
                return
            if season is not None:
                tmdb_type = 'season' if episode is None else 'episode'
            item = {
                'listitem': self.tmdb_api.mapper.get_info(
                    details, tmdb_type,
                    base_item=base_item['listitem'] if base_item else None,
                    base_is_season=base_is_season),
                'expires': self._timestamp(),
                'artwork': {}}
            item['artwork']['tmdb'] = item['artwork'][get_artwork_quality()] = item['listitem'].pop('art')
            if manual_art:
                item['artwork']['manual'] = manual_art
            item['listitem']['art'] = {}
        return item

    def get_cache_name(self, tmdb_type, tmdb_id, season=None, episode=None, salted=True):
        language = self.tmdb_api.language
        # Fallback to English decides which TMDb images are requested and which fanart.tv art is picked, both baked into the stored item.
        # Added to the name only when OFF (the default is on), like the language: existing caches stay valid, a change starts a fresh build.
        fallback = '' if not salted or getattr(self.tmdb_api, 'artlang_fallback', True) else '.nofb'
        # English plot fallback (GitHub issue #11) is baked into the stored plot: added only when ON (the default is off) and only for a language other
        # than English, so existing caches stay valid and a change starts a fresh build (2 = since 7.0.86, which also fills person biographies)
        if salted and language[:2] != 'en' and get_setting('force_english_plot_fallback'):
            fallback += '.enplot2'
        return f'v2.{language}{fallback}.{tmdb_type}.{tmdb_id}.{season}.{episode}'

    def get_item(self, tmdb_type, tmdb_id, season=None, episode=None, cache_refresh=False):
        if not tmdb_type or not tmdb_id:
            return

        # Get cached item
        name = self.get_cache_name(tmdb_type, tmdb_id, season, episode)
        item = None if cache_refresh else self._cache.get_cache(name)
        if self.cache_only:
            return item

        # Get the parent tvshow/season for season/episode
        base_item = None
        base_name_season = None
        if season is not None:
            if episode is not None:
                base_name_season = season
            parent = self.parent_tv if base_name_season is None else self.parent_season
            base_name = self.get_cache_name(tmdb_type, tmdb_id, base_name_season)
            base_item = parent or self._cache.get_cache(base_name)

        # Check that our current item hasn't expired and needs refreshing
        if item and get_timestamp(item['expires']):  # Our item hasn't expired
            # Check that our parent item doesn't have newer details that we need to merge
            if not base_item or self._timeint(base_item['expires']) <= self._timeint(item['expires']):  # No new details in parent item
                # Check that we aren't missing any artwork or need to remap artwork quality
                if not self.ftv_api or item['artwork'].get('fanarttv'):  # We have fanarttv artwork (or user disabled fanarttv)
                    if item['artwork'].get(get_artwork_quality()) and not self._needs_tvdb(item):  # We also have artwork at the correct quality level (and TVDB, if on, has been asked)
                        return item  # Our item is up-to-date so we return it
                # Else we've got current item details but we need to grab some artwork or remap quality
                prefix = 'tvshow.' if season is not None and episode is None else ''  # Seasons should map tvshow art with prefix
                item = self.get_artwork(item, tmdb_type, season, episode, base_item, prefix=prefix)  # Get art and map it
                return self._cache.set_cache(item, name, cache_days=CACHE_DAYS)  # Re-add our item to the cache with new details

        # Item isn't current so it needs a refresh but let's make sure we keep manually set artwork
        prefix = ''
        plain_name = self.get_cache_name(tmdb_type, tmdb_id, season, episode, salted=False)
        previous = item or (self._cache.get_cache(plain_name) if plain_name != name else None)  # after a Fallback to English change the item is new: carry over the art picked by hand
        manual_art = previous['artwork'].get('manual', {}) if previous and episode is None else {}  # Episodes inherit season/tvshow art so don't have their own manual art
        manual_art = {k: v for k, v in manual_art.items() if v and '.' not in k}  # Only get main art and filter out parent prefixed items
        if season is not None:
            prefix = 'tvshow.' if episode is None else prefix  # Seasons should prefix tvshow parent art to avoid mixing
            base_artwork = base_item['artwork'].get('manual', {}) if base_item else {}  # Get parent manual art if available
            base_artwork = {k: v for k, v in base_artwork.items() if v}  # Filter out empties
            if cache_refresh or not base_item:  # No parent item or refreshing so let's try to get a new one
                base_item = self.get_item(tmdb_type, tmdb_id, base_name_season, cache_refresh=cache_refresh)
            manual_art = self.join_base_artwork(base_artwork, manual_art, prefix=prefix)  # Join our manual artwork with our base

        # Try to get FTV artwork (if IDs are available) in parallel thread at same time as item
        with ParallelThread(
                [tmdb_type] if episode is None else [],
                self._get_ftv_artwork, base_item or item,
                season=season, tmdb_id=tmdb_id) as pt:
            item = self.get_tmdb_item(
                tmdb_type, tmdb_id, season=season, episode=episode,
                base_item=base_item, manual_art=manual_art,
                base_is_season=base_name_season is not None,
                cache_refresh=cache_refresh)
            item_queue = pt.queue
        ftv_art = item_queue[0] if item_queue else None
        item = self.get_artwork(item, tmdb_type, season, episode, base_item, prefix=prefix, ftv_art=ftv_art)
        return self._cache.set_cache(item, name, cache_days=CACHE_DAYS)

    def get_item_artwork(self, artwork, art_dict=None, is_season=False):
        def set_artwork(details=None, blacklist=None):
            if not details:
                return
            if not blacklist:
                art_dict.update(details)
                return
            for k, v in details.items():
                if not v:
                    continue
                if k in blacklist and art_dict.get(k):
                    continue
                art_dict[k] = v
        art_dict = {} if art_dict is None else art_dict
        quality = get_artwork_quality()
        tmdb_art = artwork.get(quality) or self.map_artwork(artwork.get('tmdb', {}))
        if not (self.ftv_art_forced or fanarttv_art_enabled()):  # no fanart.tv option is on: TMDb art only (GitHub issue #9); the saved value of "Prefer TMDb first" does not matter then
            set_artwork(tmdb_art)
        elif get_setting('fanarttv_secondpref'):  # read now, not at import: a long-running service must follow a settings change
            set_artwork(artwork.get('fanarttv'))
            set_artwork(tmdb_art, blacklist=['landscape'] if is_season else [])
        else:
            set_artwork(tmdb_art)
            set_artwork(artwork.get('fanarttv'), blacklist=ARTWORK_BLACKLIST[quality])
        if self.tvdb_art_enabled:  # gap-fill only: never replaces TMDb, fanart.tv or manual art. Read-time gate so turning the setting off takes effect at once
            tvdb_use_thumb = get_tvdb_use_thumb(quality)
            tvdb_art = artwork.get('tvdb') or {}
            for k, v in tvdb_art.items():
                if k.endswith(TVDB_THUMB_SUFFIX):  # the smaller image of the key before it, picked below
                    continue
                if tvdb_use_thumb.get(k.rsplit('.', 1)[-1]):  # items built before 7.0.70 have no thumbnail: they keep the full image
                    v = tvdb_art.get(f'{k}{TVDB_THUMB_SUFFIX}') or v
                if v and not art_dict.get(k):
                    art_dict[k] = v
        set_artwork(artwork.get('manual'))
        return art_dict

    def _undo_iterprops(self, item, tmdb_type, tmdb_id, season, episode):
        if 'infoproperties_basic' not in item:
            item['infoproperties_basic'] = {
                k: v for k, v in item['listitem']['infoproperties'].items()
                if not re.match(r'.*\.[0-9]*\..*', k)}  # Filter out indexed properties to leave only basic props
            name = self.get_cache_name(tmdb_type, tmdb_id, season, episode)
            self._cache.set_cache(item, name, cache_days=CACHE_DAYS)  # Set back to cache
        item['listitem']['infoproperties'] = item['infoproperties_basic']  # Set filtered ip to ip
        return item

    def get_listitem(self, i, use_iterprops=True):
        li = ListItem(parent_params=self.parent_params, **i)
        mediatype = li.infolabels.get('mediatype')
        tmdb_type = li.tmdb_type
        tmdb_id = li.unique_ids.get('tvshow.tmdb') if mediatype in ['season', 'episode'] else li.unique_ids.get('tmdb')
        season = li.infolabels.get('season', 0) if mediatype in ['season', 'episode'] else None
        episode = li.infolabels.get('episode') if mediatype == 'episode' else None
        if mediatype == 'episode' and not episode and tmdb_id and li.infoproperties.get('calendar_code'):  # a calendar episode not placed in TMDb's numbering (anime "E3"): there is no episode to look up, so it carries the show's artwork
            show = self.get_item('tv', tmdb_id)
            if show and 'artwork' in show:
                art = self.get_item_artwork(show['artwork'])
                li.art = {**art, **{f'tvshow.{k}': art[k] for k in ('poster', 'fanart') if art.get(k)}}
            return li
        item = self.get_item(tmdb_type, tmdb_id, season, episode)
        if not item or 'listitem' not in item:
            return li
        if not use_iterprops:
            item = self._undo_iterprops(item, tmdb_type, tmdb_id, season, episode)
        li.set_details(item['listitem'], override=True)  # Old method for override checked (if self.tmdb_api.iso_language == 'en') to override only for non-English but lets standardise behaviour
        li.art = self.get_item_artwork(item['artwork'], is_season=mediatype in ['season', 'episode'])
        if mediatype == 'episode':
            from tmdbhelper.lib.items.anime_enrichment import apply_anime_overlay
            apply_anime_overlay(li)   # anime episode titles/plots/stills (setting "Anime episode enrichment"); not stored in the item cache
        return li


class ItemBuilderService(ItemBuilder):
    _basic_cache_cls = BasicCacheService  # Use a different mem cache queue level for service to write out more frequently since it runs continuously
