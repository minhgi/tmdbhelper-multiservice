from xbmcgui import Dialog
from tmdbhelper.lib.files.ftools import cached_property
from tmdbhelper.lib.items.listitem import ListItem
from tmdbhelper.lib.api.fanarttv.api import ARTWORK_TYPES, NO_LANGUAGE, get_encoded_url
from tmdbhelper.lib.addon.dialog import BusyDialog
from tmdbhelper.lib.addon.plugin import get_localized, executebuiltin, get_setting, set_setting
from tmdbhelper.lib.addon.consts import CACHE_SHORT
from tmdbhelper.lib.script.method.kodi_utils import reload_widgets

SORT_MODES = (32776, 32777)  # Source, Language (the string ids); the chosen index is remembered in the hidden setting artmgr_sort
SOURCE_RANK = {'tmdb': 0, 'fanarttv': 1, 'tvdb': 2}  # the order the sources are listed in
SOURCE_NAMES = {'tmdb': 'TMDb', 'fanarttv': 'fanart.tv', 'tvdb': 'TheTVDB'}
NO_LANGUAGE_CODES = ('', '00', 'null', 'none', 'xx')


def normalize_language(code):
    """One language code for all sources, so they can be filtered and sorted together: TMDb gives 'de' or None, fanart.tv 'de', '00' or '',
    TVDB mostly three letters ('deu', 'ger', 'eng') and sometimes two. No language -> ''. A code that is not known is kept as it is."""
    code = f'{code or ""}'.strip().lower()
    if code in NO_LANGUAGE_CODES:
        return ''
    if len(code) != 2:
        from tmdbhelper.lib.api.tvdb.artwork import LANGUAGE_CODES
        for two, others in LANGUAGE_CODES.items():
            if code in others:
                return two
    return code


def sort_art_records(records, mode, own_language):
    """Sort by source (TMDb, fanart.tv, TVDB) then language (yours, English, none, the rest) - mode 0 - or by language then source - mode 1. Inside
    each of those, larger images first (where the resolution is not known - fanart.tv has none - last), then by the source's own score. Scores of
    different sources are never compared with each other (TMDb 0-10, fanart.tv likes, TVDB 100000+). Season art comes before the show's art."""
    def language_rank(language):
        if language == own_language:
            return (0, '')
        if language == 'en':
            return (1, '')
        if not language:
            return (2, '')
        return (3, language)

    def key(record):
        source = SOURCE_RANK[record['source']]
        language = language_rank(record['lang'])
        area = -(record['width'] * record['height'])
        score = -record['score']
        if mode == 1:
            order = (language, source, area, score)
        else:
            order = (source, language, area, score)
        return (record['group'],) + order

    return sorted(records, key=key)


class _ArtworkSelector():
    @cached_property
    def tmdb_imagepath(self):
        from tmdbhelper.lib.api.tmdb.images import TMDbImagePath
        return TMDbImagePath()

    def get_art_languages(self):
        """Languages the Artwork Manager offers (unless "every language" is chosen): the add-on language, art without a language and, with
        "Fallback to English", English (the same set TMDb is asked for). Codes are normalized, see normalize_language()."""
        languages = {self.tmdb_api.iso_language, ''}
        if get_setting('fanarttv_enfallback'):
            languages.add('en')
        return languages

    @staticmethod
    def _art_record(source, url, thumb, language, score, rating, width=0, height=0, group=0):
        return {
            'source': source, 'url': url, 'thumb': thumb or url, 'lang': normalize_language(language), 'score': float(score or 0), 'rating': rating,
            'width': int(width or 0), 'height': int(height or 0), 'group': group}

    def get_ftv_art(self, ftv_type, ftv_id, artwork_type, season=None, group=0):
        ftv_items = self.ftv_api.get_all_artwork(ftv_id, ftv_type, season=season, artlist_type=artwork_type, season_type='season_only')
        return [
            self._art_record('fanarttv', get_encoded_url(i), None, i.get('lang'), i.get('likes'), f'{i.get("likes", 0)} likes', group=group)
            for i in ftv_items if get_encoded_url(i)]

    def get_tmdb_art(self, tmdb_type, tmdb_id, artwork_type, season=None, group=0):
        mappings = {
            'poster': {'func': self.tmdb_imagepath.get_imagepath_poster, 'key': 'posters'},
            'fanart': {'func': self.tmdb_imagepath.get_imagepath_fanart, 'key': 'backdrops'},
            'landscape': {'func': self.tmdb_imagepath.get_imagepath_thumbs, 'key': 'backdrops'},
            'clearlogo': {'func': self.tmdb_imagepath.get_imagepath_clogos, 'key': 'logos'}}
        if artwork_type not in mappings:
            return []
        tmdb_iargs = ['images'] if season is None else ['season', season, 'images']
        # TMDb().get_request_sc() always adds include_image_language=<your language>,null[,en]: that request never returns other languages, so
        # "every language" could not show any from TMDb. Ask without it (the language is then chosen here); the old request is the fallback.
        tmdb_items = self.tmdb_api.get_request(tmdb_type, tmdb_id, *tmdb_iargs, cache_days=CACHE_SHORT) or self.tmdb_api.get_request_sc(tmdb_type, tmdb_id, *tmdb_iargs) or {}
        tmdb_items = tmdb_items.get(mappings[artwork_type]['key']) or []
        func = mappings[artwork_type]['func']
        return [
            self._art_record(
                'tmdb', func(i.get('file_path')), None, i.get('iso_639_1'), i.get('vote_average'),
                f'{float(i.get("vote_average") or 0):.1f} ({i.get("vote_count") or 0} votes)', i.get('width'), i.get('height'), group)
            for i in tmdb_items if i.get('file_path') and i.get('file_path', '')[-4:] != '.svg']

    def get_tvdb_art(self, tmdb_type, item, artwork_type):
        """TheTVDB's art of this type for a movie or show (when the TheTVDB setting is on; it is asked once, then cached). Not for seasons and not
        for landscape, which TheTVDB does not have."""
        tvdb_api = getattr(self, 'tvdb_art_api', None)
        if not tvdb_api or tmdb_type not in ('movie', 'tv') or artwork_type not in ('poster', 'fanart', 'banner', 'clearart', 'clearlogo'):
            return []
        found = tvdb_api.get_artwork_list(tmdb_type, (item.get('listitem') or {}).get('unique_ids') or {}) or []
        return [
            self._art_record('tvdb', i[1], i[2], i[3], i[4], f'score {int(i[4])}', i[5], i[6])
            for i in found if i[0] == artwork_type]

    def get_art_records(self, tmdb_type, tmdb_id, artwork_type, item, ftv_type, ftv_id, season=None):
        """Every artwork of one type from every source as one list of records (see _art_record); a season's own art is group 0, its show's art group 1."""
        records = self.get_ftv_art(ftv_type, ftv_id, artwork_type, season=season) if ftv_type and ftv_id else []
        records += self.get_tmdb_art(tmdb_type, tmdb_id, artwork_type, season=season)
        if season is not None:  # Also get base artwork at end of list
            records += self.get_ftv_art(ftv_type, ftv_id, artwork_type, group=1) if ftv_type and ftv_id else []
            records += self.get_tmdb_art(tmdb_type, tmdb_id, artwork_type, group=1)
        else:
            records += self.get_tvdb_art(tmdb_type, item, artwork_type)
        return records

    @staticmethod
    def _art_listitem(record):
        size = f'{record["width"]}x{record["height"]}' if record['width'] and record['height'] else '?'
        language = record['lang'].upper() or '--'
        return ListItem(
            label=record['url'],
            label2=f'{SOURCE_NAMES[record["source"]]} | {language} | {size} | {record["rating"]}',
            art={'thumb': record['thumb']}).get_listitem()

    def get_art_sort_mode(self):
        mode = get_setting('artmgr_sort', 'int')
        return mode if 0 <= mode < len(SORT_MODES) else 0

    def select_art_sort(self):
        """The Sort by picker: Source, Language or Quality first, and whether every language is shown. Both are remembered for next time."""
        mode = self.get_art_sort_mode()
        every = get_setting('artmgr_alllang')
        choice = Dialog().select(get_localized(32781), [get_localized(i) for i in SORT_MODES] + [get_localized(32783 if every else 32782)], preselect=mode)
        if choice == -1:
            return
        if choice < len(SORT_MODES):
            set_setting('artmgr_sort', choice, 'int')
            return
        set_setting('artmgr_alllang', not every, 'bool')

    def show_art_records(self, records, artwork_type):
        """The artwork list with the Sort by row on top; returns the chosen image, or None when the user went back. Re-sorts in memory."""
        while True:
            mode = self.get_art_sort_mode()
            every = get_setting('artmgr_alllang')
            shown = records
            if not every and artwork_type not in NO_LANGUAGE:
                allowed = self.get_art_languages()
                shown = [i for i in records if i['lang'] in allowed] or records  # none in those languages: offer every language instead of a dead end
            shown = sort_art_records(shown, mode, self.tmdb_api.iso_language)
            sort_row = ListItem(
                label=get_localized(32775).format(get_localized(SORT_MODES[mode])),
                label2=get_localized(32780 if every else 32779)).get_listitem()
            choice = Dialog().select(get_localized(13511), [sort_row] + [self._art_listitem(i) for i in shown], useDetails=True)
            if choice == -1:
                return
            if choice == 0:
                self.select_art_sort()
                continue
            return shown[choice - 1]['url']

    def select_type(self, ftv_type, blacklist=None, item_artwork=None):
        if blacklist is None:
            blacklist = []
        artwork_types = [i for i in ARTWORK_TYPES.get(ftv_type, []) if i not in blacklist]  # Remove types that we previously looked for
        ditems = [ListItem(label=i, art={'thumb': item_artwork.get(i)}).get_listitem() for i in artwork_types] if item_artwork else artwork_types
        choice = Dialog().select(get_localized(13511), ditems, useDetails=True if item_artwork else False)
        if choice == -1:
            return
        return artwork_types[choice]

    def select_artwork(self, tmdb_type, tmdb_id, container_refresh=False, blacklist=None, season=None, layout_season=None):
        if blacklist is None:
            blacklist = []
        with BusyDialog():
            item = self.get_item(tmdb_type, tmdb_id, season)
        if not item:
            return
        ftv_id, ftv_type = self.get_ftv_typeid(tmdb_type, item, season=season)
        item_artwork = self.get_item_artwork(item['artwork'], is_season=season is not None)
        if layout_season is not None:  # a layout season has no artwork of its own at TMDb: show what was chosen for it (real season 1's otherwise)
            from tmdbhelper.lib.api.tmdb.season_layout import layout_art
            item_artwork = {**item_artwork, **layout_art(tmdb_id, layout_season)}
        artwork_type = self.select_type(ftv_type if season is None else 'season', blacklist, item_artwork=item_artwork)
        if not artwork_type:
            if container_refresh:
                executebuiltin('Container.Refresh')
                reload_widgets(bridge=False)
            return

        # Get artwork of type from every source (TMDb, fanart.tv and, when set up, TheTVDB)
        with BusyDialog():
            records = self.get_art_records(tmdb_type, tmdb_id, artwork_type, item, ftv_type, ftv_id, season=season)

        # Nothing found so notify user
        if not records:
            Dialog().notification(
                get_localized(39123),
                get_localized(32217).format(tmdb_type, tmdb_id))
            blacklist.append(artwork_type)  # Blacklist artwork type if not found before reprompting
            return self.select_artwork(tmdb_type, tmdb_id, container_refresh, blacklist, season=season, layout_season=layout_season)

        # Choose artwork from options (sorted as last chosen)
        success = self.show_art_records(records, artwork_type)
        if success is None:  # If user hits back go back to main menu rather than exit completely
            return self.select_artwork(tmdb_type, tmdb_id, container_refresh, blacklist, season=season, layout_season=layout_season)
        if not success:
            return
        container_refresh = True

        # Cache our artwork
        if layout_season is not None:  # kept apart from real season 1's artwork, which the other layout seasons still show
            from tmdbhelper.lib.api.tmdb.season_layout import set_layout_art
            if not set_layout_art(tmdb_id, layout_season, artwork_type, success):
                Dialog().notification(get_localized(39123), get_localized(32217).format(tmdb_type, tmdb_id))
        else:
            manual = item['artwork'].setdefault('manual', {})
            manual[artwork_type] = success
            item['expires'] = self._timestamp()  # Reup our timestamp to force child items to recache
            self._cache.set_cache(item, cache_name=self.get_cache_name(tmdb_type, tmdb_id, season), cache_days=10000)
        return self.select_artwork(tmdb_type, tmdb_id, container_refresh, blacklist, season=season, layout_season=layout_season)

    def refresh_all_artwork(self, tmdb_type, tmdb_id, ok_dialog=True, container_refresh=True, season=None):
        old_cache_refresh = self.ftv_api.cache_refresh
        self.ftv_api.cache_refresh = True
        tvdb_api = getattr(self, 'tvdb_art_api', None)  # TheTVDB (when on) is asked again too, like fanart.tv, instead of reusing its stored answer
        old_tvdb_refresh = tvdb_api.cache_refresh if tvdb_api else False
        if tvdb_api:
            tvdb_api.cache_refresh = True

        with BusyDialog():
            item = self.get_item(tmdb_type, tmdb_id, season, cache_refresh=True)
        if not item:
            return Dialog().ok(
                get_localized(39123),
                get_localized(32217).format(tmdb_type, tmdb_id)) if ok_dialog else None
        if ok_dialog:
            artwork_types = {k.capitalize() for k, v in item['artwork'].get('tmdb', {}).items() if v}
            artwork_types |= {k.capitalize() for k, v in item['artwork'].get('fanarttv', {}).items() if v}
            Dialog().ok(
                get_localized(39123),
                get_localized(32218).format(tmdb_type, tmdb_id, ', '.join(artwork_types)))

        # Refresh container to display new artwork
        if container_refresh:
            executebuiltin('Container.Refresh')
            reload_widgets(bridge=False)
        self.ftv_api.cache_refresh = old_cache_refresh  # Set it back to previous setting
        if tvdb_api:
            tvdb_api.cache_refresh = old_tvdb_refresh

    def manage_artwork(self, tmdb_id=None, tmdb_type=None, season=None, layout_season=None):
        if not tmdb_id or not tmdb_type:
            return
        choice = Dialog().contextmenu([
            get_localized(32220),
            get_localized(32221)])
        if choice == -1:
            return
        if choice == 0:
            return self.select_artwork(tmdb_id=tmdb_id, tmdb_type=tmdb_type, season=season, layout_season=layout_season)
        if choice == 1:
            return self.refresh_all_artwork(tmdb_id=tmdb_id, tmdb_type=tmdb_type, season=season)
