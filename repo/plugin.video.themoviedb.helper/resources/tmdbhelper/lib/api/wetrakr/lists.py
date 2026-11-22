from tmdbhelper.lib.items.container import ContainerDirectory
from tmdbhelper.lib.addon.logger import kodi_log


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _stamp(row):
    return row.get('last_watched_at') or row.get('watched_at') or row.get('tracked_at') or ''


def _unique_newest(rows):
    """ rows = [(stamp, row)]: newest first, one row per TMDb id and kind """
    seen, out = set(), []
    for _, row in sorted(rows, key=lambda x: x[0], reverse=True):
        key = (row['mediatype'], row['id'])
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


def media_rows(rows, mediatype):
    """ Compact movie / show rows of a status list (ids.tmdb, tracked_at) -> [{id, mediatype, added_at}], newest first. A row with no TMDb id cannot
    be opened here and is left out. """
    out = []
    for row in rows or []:
        tmdb_id = _int((row.get('ids') or {}).get('tmdb'))
        if tmdb_id:
            stamp = _stamp(row)
            out.append((stamp, {'id': tmdb_id, 'mediatype': mediatype, 'added_at': stamp}))
    return _unique_newest(out)


def history_rows(movie_plays, episode_plays):
    """ Plays (one row per play, newest first) -> {'movies': [...], 'shows': [...]}: one row per movie and per show, by its latest play.
    An episode play belongs to its show (show_ids.tmdb). """
    movies = [(_stamp(r), {'id': i, 'mediatype': 'movie'}) for r in movie_plays or [] if (i := _int((r.get('ids') or {}).get('tmdb')))]
    shows = [(_stamp(r), {'id': i, 'mediatype': 'show'}) for r in episode_plays or [] if (i := _int((r.get('show_ids') or {}).get('tmdb')))]
    out = {}
    for key, rows in (('movies', movies), ('shows', shows)):
        rows = _unique_newest(rows)
        if rows:
            out[key] = rows
    return out


def paused_rows(movie_rows, episode_rows, progress):
    """ The paused rows of the playing list (a row that is playing now, or paused at 0 %, is no resume point): (movies, episodes) newest pause first.
    movies = [{id, mediatype}], episodes = [{id (show), season, episode, mediatype}]. progress(row) -> percent or None (the sync's own rule). """
    movies, episodes = [], []
    for row in movie_rows or []:
        tmdb_id = _int((row.get('ids') or {}).get('tmdb'))
        if tmdb_id and progress(row):
            movies.append((_stamp(row), {'id': tmdb_id, 'mediatype': 'movie'}))
    for row in episode_rows or []:
        tmdb_id, season, number = _int((row.get('show_ids') or {}).get('tmdb')), _int(row.get('season_number')), _int(row.get('number'))
        if tmdb_id and season is not None and number is not None and progress(row):
            episodes.append((_stamp(row), {'id': tmdb_id, 'mediatype': 'episode', 'season': season, 'episode': number}))
    return ([r for _, r in sorted(movies, key=lambda x: x[0], reverse=True)], [r for _, r in sorted(episodes, key=lambda x: x[0], reverse=True)])


def next_up_rows(shows, today):
    """ Rows of GET /sync/tracking/watching/shows?extended=episode_level_1 (already newest watched first) -> [{id, mediatype 'episode', season, episode,
    title}]. A show with no next_watching_episode is caught up; an episode whose air date is after today is nothing to watch yet. today = 'YYYY-MM-DD'. """
    out = []
    for row in shows or []:
        tmdb_id = _int((row.get('ids') or {}).get('tmdb'))
        nxt = row.get('next_watching_episode') or {}
        season, number = _int(nxt.get('season_number')), _int(nxt.get('number'))
        if not tmdb_id or season is None or number is None:
            continue
        aired = f'{nxt.get("air_date") or ""}'[:10]
        if aired and aired > today:
            continue
        out.append({'id': tmdb_id, 'mediatype': 'episode', 'season': season, 'episode': number, 'title': row.get('title')})
    return out


class WeTrakrList(ContainerDirectory):
    """ Base of the WeTrakr lists. The rows are TMDb ids and TMDb numbering (no numbering check, unlike Simkl); everything is read in one or two
    requests per list, and a list that could not be read completely is left empty rather than shown partial. """

    plugin_label = None

    @property
    def api(self):
        api = self.wetrakr_api
        return api if api is not None and api.is_authorized else None

    def read(self, status, target):
        """ All rows of one status list (compact, every page): the rows, or None when the read failed """
        ok, rows = self.api.get_list(f'/sync/tracking/{status}/{target}')
        if not ok:
            kodi_log(f'WeTrakr list {status}/{target}: could not be read', 1)
            return None
        return rows

    def keep_scope(self, normalized, scope):
        """ The Anime / Asian / Movies and TV Shows Progress menus: only the titles that belong to `scope` ('anime', 'asian' or 'other'; the rule of the other
        services' progress menus, api/mdblist/lists._in_scope, from TMDb details, cached). 'movies' rows are movies, 'shows' and 'episodes' rows are shows.
        Done before paging, so a page is full. A title whose TMDb details cannot be read counts as neither anime nor Asian (it stays in 'other'). """
        if not scope:
            return normalized or {}
        from tmdbhelper.lib.api.mdblist.lists import _in_scope
        seen, kept = {}, {}
        for key, rows in (normalized or {}).items():
            tmdb_type = 'movie' if key == 'movies' else 'tv'
            keep = []
            for row in rows:
                if (tmdb_type, row['id']) not in seen:
                    seen[(tmdb_type, row['id'])] = _in_scope(self.tmdb_api.get_request_sc(tmdb_type, row['id']) or {}, scope, tmdb_type)
                if seen[(tmdb_type, row['id'])]:
                    keep.append(row)
            if keep:
                kept[key] = keep
        return kept

    def finish(self, normalized, page, scope=None):
        normalized = {k: v for k, v in self.keep_scope(normalized, scope).items() if v}
        if not normalized:
            return
        response = self.api.get_paginated(normalized, page=page or 1)
        self.tmdb_cache_only = False
        self.set_mixed_content(response.paginated_items_dict)
        self.library = 'video'
        if self.plugin_label:
            self.plugin_category = self.plugin_label
        return response.items + response.next_page

    def status_list(self, status, tmdb_type=None, page=None, movies=True, shows=True):
        if self.api is None:
            return
        normalized = {}
        if movies and tmdb_type != 'tv':
            rows = self.read(status, 'movies')
            if rows is None:
                return
            normalized['movies'] = media_rows(rows, 'movie')
        if shows and tmdb_type != 'movie':
            rows = self.read(status, 'shows')
            if rows is None:
                return
            normalized['shows'] = media_rows(rows, 'show')
        return self.finish(normalized, page)


class ListPlanToWatch(WeTrakrList):
    """ WeTrakr plan to watch: GET /sync/tracking/planning/{movies,shows} """

    def get_items(self, tmdb_type=None, page=None, **kwargs):
        return self.status_list('planning', tmdb_type, page)


class ListDropped(WeTrakrList):
    """ WeTrakr dropped: GET /sync/tracking/dropped/{movies,shows} """

    def get_items(self, tmdb_type=None, page=None, **kwargs):
        return self.status_list('dropped', tmdb_type, page)


class ListWatching(WeTrakrList):
    """ WeTrakr watching: GET /sync/tracking/watching/shows (shows only: a movie has no watching status) """

    def get_items(self, tmdb_type=None, page=None, **kwargs):
        return self.status_list('watching', 'tv', page, movies=False)


class ListHold(WeTrakrList):
    """ WeTrakr on hold: GET /sync/tracking/hold/shows (shows only; WeTrakr's older name for the status is paused) """

    def get_items(self, tmdb_type=None, page=None, **kwargs):
        return self.status_list('hold', 'tv', page, movies=False)


class ListHistory(WeTrakrList):
    """ Recently watched: the newest plays, GET /sync/tracking/watched/history/{movies,episodes} (first page only, 5,000 plays), one row per movie and per
    show by its latest play. A watch history can be very long, so the list is the recent part of it, not all of it. """

    def get_items(self, tmdb_type=None, page=None, **kwargs):
        if self.api is None:
            return
        movie_plays = episode_plays = None
        if tmdb_type != 'tv':
            ok, movie_plays = self.api.get_first_page('/sync/tracking/watched/history/movies')
            if not ok:
                kodi_log('WeTrakr history: movie plays could not be read', 1)
                return
        if tmdb_type != 'movie':
            ok, episode_plays = self.api.get_first_page('/sync/tracking/watched/history/episodes')
            if not ok:
                kodi_log('WeTrakr history: episode plays could not be read', 1)
                return
        return self.finish(history_rows(movie_plays, episode_plays), page)


class _PausedList(WeTrakrList):
    """ The paused rows of GET /sync/tracking/playing/{movies,episodes} """

    def paused(self, want_movies=True, want_episodes=True):
        from tmdbhelper.lib.api.wetrakr.sync.itemdata import WeTrakrSyncItem
        movie_rows = self.read('playing', 'movies') if want_movies else []
        episode_rows = self.read('playing', 'episodes') if want_episodes else []
        if movie_rows is None or episode_rows is None:
            return None
        return paused_rows(movie_rows, episode_rows, WeTrakrSyncItem._progress)


class ListInProgress(_PausedList):
    """ In-Progress: the movies paused part-way and the shows that have an episode paused part-way, newest pause first. Shows open at their seasons.
    anime_only / asian_only / other_only = 'true' (the Anime / Asian / Movies and TV Shows Progress menus) keep only that kind of title. """

    def get_items(self, tmdb_type=None, page=None, anime_only=None, asian_only=None, other_only=None, **kwargs):
        from tmdbhelper.lib.api.mdblist.lists import _get_scope
        if self.api is None:
            return
        paused = self.paused(tmdb_type != 'tv', tmdb_type != 'movie')
        if paused is None:
            return
        movies, episodes = paused
        normalized = {}
        if tmdb_type != 'tv':
            normalized['movies'] = movies
        if tmdb_type != 'movie':
            seen, shows = set(), []
            for row in episodes:
                if row['id'] not in seen:
                    seen.add(row['id'])
                    shows.append({'id': row['id'], 'mediatype': 'show'})
            normalized['shows'] = shows
        return self.finish(normalized, page, _get_scope(anime_only, asian_only, other_only))


class ListInProgressEpisodes(_PausedList):
    """ In-Progress Episodes: every episode paused part-way, newest pause first, one row per episode """

    plugin_label = 'In-Progress Episodes'

    def get_items(self, page=None, anime_only=None, asian_only=None, other_only=None, **kwargs):
        from tmdbhelper.lib.api.mdblist.lists import _get_scope
        if self.api is None:
            return
        paused = self.paused(False, True)
        if paused is None:
            return
        return self.finish({'episodes': paused[1]}, page, _get_scope(anime_only, asian_only, other_only))


class ListNextUp(WeTrakrList):
    """ Next Up: one row per show WeTrakr lists as watching, showing the next episode to watch (next_watching_episode of
    GET /sync/tracking/watching/shows?extended=episode_level_1, most recently watched show first). Caught-up shows and episodes that have not aired
    are left out. Up to 1,000 shows (10 pages of 100). anime_only / asian_only / other_only = 'true' keep only that kind of show (the Progress submenus). """

    def get_items(self, tmdb_type=None, page=None, anime_only=None, asian_only=None, other_only=None, **kwargs):
        from datetime import datetime, timezone
        from tmdbhelper.lib.addon.plugin import get_localized
        from tmdbhelper.lib.api.mdblist.lists import _get_scope
        if self.api is None:
            return
        ok, shows = self.api.get_pages('/sync/tracking/watching/shows', params={'extended': 'episode_level_1', 'sort_by': 'my_last_watched'})
        if not ok:
            kodi_log('WeTrakr Next Up: the watching list could not be read completely', 1)
            return
        if not shows:
            kodi_log('WeTrakr Next Up: no shows are being watched', 1)
            return
        rows = next_up_rows(shows, datetime.now(timezone.utc).strftime('%Y-%m-%d'))
        self.plugin_label = get_localized(32572)
        return self.finish({'episodes': rows}, page, _get_scope(anime_only, asian_only, other_only))
