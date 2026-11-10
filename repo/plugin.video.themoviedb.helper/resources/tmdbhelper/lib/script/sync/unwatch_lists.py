"""'Unwatched seasons' and 'Unwatched episodes' entries of the Trakt / MDBList / Simkl options menus.

Each entry lists what ITS service has watched for the open show (read from that service's local sync data, refreshed first) and lets
the user tick the seasons / episodes to unwatch. The removal is sent as ONE request per service (a loop of single-item posts would hit
Trakt's write rate limit part-way), to the service whose menu it is first and then mirrored to the other two, as 'Mark as unwatched'
already does. Simkl keeps no episode rows, so it only has the seasons entry and episodes are not sent to it.
"""
from tmdbhelper.lib.files.ftools import cached_property
from tmdbhelper.lib.addon.dialog import BusyDialog
from tmdbhelper.lib.addon.logger import kodi_log
from tmdbhelper.lib.addon.plugin import executebuiltin, get_setting
from xbmcgui import Dialog

BULK = 9999  # watched_episodes sentinel written by a whole-show mark (see watched_totals.py)
SYNC_KEYS = {'trakt': ('plays', 'watched_episodes', 'aired_episodes'), 'mdblist': ('plays', 'watched_episodes'), 'simkl': ('plays', 'watched_episodes')}
SERVICE_NAMES = {'trakt': 'Trakt', 'mdblist': 'MDBList', 'simkl': 'Simkl'}
SEASON_TOLERANCE = 2  # episodes a season may differ by between TMDb and Simkl (a split double episode) before the numbering is called different
WHOLE = 'whole'       # choice marker of the whole-show row


def season_label(number):
    return 'Specials' if int(number) == 0 else f'Season {int(number)}'


def _rows(cache, sql, args):
    try:
        cursor = cache.execute_sql(sql, args, read_only=True)
        return cursor.fetchall() if cursor else []
    except Exception as exc:
        kodi_log(f'unwatch_lists: local read failed: {exc}', 2)
        return []


def read_watched(cache, tmdb_id, episodes=True):
    """(seasons, episodes) one service has watched for a show, from its local sync data.
    seasons = {season_number: watched episode count}; episodes = [(season, episode, title or None)].
    A season's count is the larger of its watched episode rows and its own `watched_episodes` (a bulk mark leaves no episode rows).
    Rows with an unusable season / episode number are skipped."""
    tmdb_id = int(tmdb_id)
    counts, listed = {}, []
    if episodes:
        title = 'title' if any(c[1] == 'title' for c in _rows(cache, 'PRAGMA table_info(simplecache)', ())) else 'NULL'
        rows = _rows(cache, f"SELECT season_number, episode_number, {title} FROM simplecache WHERE tmdb_id=? AND item_type='episode' AND plays>0"
                     " ORDER BY season_number, episode_number", (tmdb_id,))
        for season, episode, title in rows:
            if season is None or episode is None:
                continue
            counts[int(season)] = counts.get(int(season), 0) + 1
            listed.append((int(season), int(episode), title))
    for season, watched in _rows(cache, "SELECT season_number, watched_episodes FROM simplecache WHERE tmdb_id=? AND item_type='season'"
                                        " AND watched_episodes>0", (tmdb_id,)):
        if season is None or int(watched) >= BULK:
            continue
        counts[int(season)] = max(counts.get(int(season), 0), int(watched))
    return counts, listed


def has_bulk(cache, tmdb_id):
    """True when the show (or a season) is held as one whole-show / whole-season mark with no episode detail (the 9999 sentinel)."""
    return bool(_rows(cache, "SELECT 1 FROM simplecache WHERE tmdb_id=? AND item_type IN ('show','season') AND watched_episodes>=? LIMIT 1",
                      (int(tmdb_id), BULK)))


def _json(response):
    try:
        return response.json() or {}
    except Exception:
        return {}


def removed_count(response):
    """Number the service says it removed, or None when the reply carries no count.
    Trakt and Simkl answer {'deleted': {...}}, MDBList {'removed': {...}}. An 'episodes' count is the figure reported when present
    (MDBList also counts the season / show rows it touched, which would inflate a plain sum)."""
    body = _json(response)
    counts = body.get('deleted') if isinstance(body.get('deleted'), dict) else body.get('removed')
    if not isinstance(counts, dict):
        return None
    try:
        if isinstance(counts.get('episodes'), (int, float)):
            return int(counts['episodes'])
        return sum(int(v) for v in counts.values() if isinstance(v, (int, float)))
    except Exception:
        return None


def build_payload(service, tmdb_id, seasons, episodes, whole=False):
    """One removal payload for a show. seasons = [season numbers]; episodes = {season: [episode numbers]} (Simkl takes seasons only);
    whole = remove the show's whole watched history (a whole-show mark has no season / episode detail to pick from)."""
    tmdb_id = int(tmdb_id)
    show = {'tmdb': tmdb_id} if service == 'mdblist' else {'ids': {'tmdb': tmdb_id}}
    if whole:
        return {'shows': [show]}
    entries = [{'number': int(s)} for s in sorted(seasons)]
    if service != 'simkl':
        entries += [{'number': int(s), 'episodes': [{'number': int(e)} for e in sorted(eps)]} for s, eps in sorted(episodes.items())
                    if int(s) not in {int(x) for x in seasons}]
    if not entries:
        return None
    show['seasons'] = entries
    return {'shows': [show]}


def simkl_numbering_agrees(tmdb_id, seasons, simkl_api):
    """Simkl numbers seasons the way TVDB does, TMDb / Trakt / MDBList their own way. True only when every ticked season exists on both
    sides with about the same number of episodes. Anything that cannot be checked (no Simkl match, request failed, Specials) is False,
    so a season is never removed on one service while it is a different season on the other."""
    try:
        wanted = {int(s) for s in seasons}
        if not wanted or 0 in wanted:
            return False
        from tmdbhelper.lib.api.tmdb.api import TMDb
        details = TMDb().get_request_sc('tv', int(tmdb_id)) or {}
        tmdb_counts = {int(x['season_number']): int(x.get('episode_count') or 0) for x in details.get('seasons') or [] if x.get('season_number') is not None}
        base, client = 'https://api.simkl.com', simkl_api.client_id
        found = simkl_api.get_api_request_json(f'{base}/search/id?tmdb={int(tmdb_id)}&type=show&client_id={client}')
        simkl_id = (((found[0] or {}).get('ids') or {}).get('simkl')) if isinstance(found, list) and found else None
        if not simkl_id:
            return False
        episodes = simkl_api.get_api_request_json(f'{base}/tv/episodes/{simkl_id}?extended=full&client_id={client}')
        if not isinstance(episodes, list):
            return False
        simkl_counts = {}
        for episode in episodes:
            if (episode.get('type') or 'episode') == 'episode' and episode.get('season') is not None:
                simkl_counts[int(episode['season'])] = simkl_counts.get(int(episode['season']), 0) + 1
        return all(s in tmdb_counts and s in simkl_counts and abs(tmdb_counts[s] - simkl_counts[s]) <= SEASON_TOLERANCE for s in wanted)
    except Exception as exc:
        kodi_log(f'unwatch_lists: Simkl season numbering check failed: {exc}', 2)
        return False


# --- Service handles: API object, local sync cache, authorization, write ----------------------------------------------------------------

class _Service:
    key = None
    setting = None  # the add-on setting that switches the service on

    @cached_property
    def api(self):
        return self._make_api()

    def enabled(self):
        """The service is switched on and signed in. Cross-Service Sync is NOT needed for an entry's own service, only for the mirrors."""
        return bool(get_setting(self.setting) and self._authorized())

    def cache(self):
        raise NotImplementedError

    def refresh(self, tmdb_id):
        """Force a fresh pull of this service's watched data, so a list never lags behind a mark made moments ago."""
        try:
            self.syncdata().sync('show', SYNC_KEYS[self.key], forced=True)
        except Exception as exc:
            kodi_log(f'unwatch_lists: {self.key} refresh failed: {exc}', 2)

    def post(self, payload):
        raise NotImplementedError

    def after_removal(self, tmdb_id, seasons, episodes, removed, whole=False):
        """Bring this service's local sync data in line with a successful removal, so the lists show it at once."""
        raise NotImplementedError


class _Trakt(_Service):
    key = 'trakt'
    setting = 'service_trakt'

    def _make_api(self):
        from tmdbhelper.lib.script.sync import cross_sync
        return cross_sync._trakt_api()

    def _authorized(self):
        return bool(self.api and self.api.authorization)

    def syncdata(self):
        return self.api.trakt_syncdata

    def cache(self):
        return self.api.trakt_syncdata.cache

    def post(self, payload):
        return self.api.post_response('sync', 'history/remove', postdata=payload)

    def after_removal(self, tmdb_id, seasons, episodes, removed, whole=False):
        try:
            self.api.trakt_syncdata.reset_lastactivities()  # the next list opened re-reads Trakt's watched data
        except Exception:
            pass


class _MDBList(_Service):
    key = 'mdblist'
    setting = 'service_mdblist'

    def _make_api(self):
        from tmdbhelper.lib.script.sync import cross_sync
        return cross_sync._mdb_api()

    def _authorized(self):
        return bool(self.api and (self.api.user_token.value or self.api.api_key))

    def syncdata(self):
        return self.api.mdblist_syncdata

    def cache(self):
        return self.api.mdblist_syncdata.cache

    def post(self, payload):
        url = self.api.get_request_url('sync', 'watched', 'remove')
        return self.api.get_api_request(url, postdata=payload, method='json', headers=self.api.auth_headers)

    def after_removal(self, tmdb_id, seasons, episodes, removed, whole=False):
        """MDBList keeps one row per watched episode: clear the removed ones, then recount the season and show totals."""
        from tmdbhelper.lib.script.sync.watched_totals import recount_totals
        try:
            cache, tmdb_id = self.cache(), int(tmdb_id)
            if whole:
                cache.execute_sql("UPDATE simplecache SET plays=0, last_watched_at=NULL WHERE tmdb_id=? AND item_type='episode'", (tmdb_id,))
                cache.execute_sql("UPDATE simplecache SET watched_episodes=0 WHERE tmdb_id=? AND item_type IN ('season','show')", (tmdb_id,))
                return
            touched = {int(s) for s in seasons} | {int(s) for s in episodes}
            for season in {int(s) for s in seasons}:
                cache.execute_sql("UPDATE simplecache SET plays=0, last_watched_at=NULL WHERE tmdb_id=? AND item_type='episode' AND season_number=?",
                                  (tmdb_id, season))
                cache.execute_sql("UPDATE simplecache SET watched_episodes=0 WHERE id=?", (f'tv.{tmdb_id}.{season}',))
            for season, eps in episodes.items():
                for episode in eps:
                    cache.execute_sql("UPDATE simplecache SET plays=0, last_watched_at=NULL WHERE id=?", (f'tv.{tmdb_id}.{int(season)}.{int(episode)}',))
            for season in touched:
                recount_totals(cache, tmdb_id, season)
        except Exception as exc:
            kodi_log(f'unwatch_lists: MDBList local update failed: {exc}', 2)


class _Simkl(_Service):
    key = 'simkl'
    setting = 'service_simkl'

    def _make_api(self):
        from tmdbhelper.lib.script.sync import cross_sync
        return cross_sync._simkl_api()

    def _authorized(self):
        from tmdbhelper.lib.script.sync import cross_sync
        return bool(self.api and cross_sync._simkl_is_authorized(self.api))

    def syncdata(self):
        return self.api.simkl_syncdata

    def cache(self):
        return self.api.simkl_syncdata.cache

    def post(self, payload):
        url = f"https://api.simkl.com/sync/history/remove?client_id={self.api.client_id}"
        return self.api.get_api_request(url, postdata=payload, method='json', headers=self.api.auth_headers)

    def after_removal(self, tmdb_id, seasons, episodes, removed, whole=False):
        """Simkl keeps season totals only: zero the removed seasons and take their counts off the show total (a bulk mark stays as it is)."""
        try:
            cache, tmdb_id = self.cache(), int(tmdb_id)
            if whole:
                cache.execute_sql("UPDATE simplecache SET watched_episodes=0 WHERE tmdb_id=? AND item_type IN ('season','show')", (tmdb_id,))
                return
            for season in {int(s) for s in seasons}:
                row = cache.execute_sql('SELECT watched_episodes FROM simplecache WHERE id=?', (f'tv.{tmdb_id}.{season}',), read_only=True).fetchone()
                count = int(row[0] or 0) if row else 0
                cache.execute_sql('UPDATE simplecache SET watched_episodes=0 WHERE id=?', (f'tv.{tmdb_id}.{season}',))
                if season == 0 or not count:
                    continue
                show = cache.execute_sql('SELECT watched_episodes FROM simplecache WHERE id=?', (f'tv.{tmdb_id}',), read_only=True).fetchone()
                if show and show[0] is not None and int(show[0]) < BULK:
                    cache.execute_sql('UPDATE simplecache SET watched_episodes=? WHERE id=?', (max(0, int(show[0]) - count), f'tv.{tmdb_id}'))
        except Exception as exc:
            kodi_log(f'unwatch_lists: Simkl local update failed: {exc}', 2)


SERVICE_CLASSES = {'trakt': _Trakt, 'mdblist': _MDBList, 'simkl': _Simkl}


def unwatch(origin, tmdb_id, seasons, episodes, whole=False):
    """Remove the chosen seasons / episodes (or the whole show) on `origin` first, then mirror to the other two. One request per service.
    Returns [(service key, ok, text)] for the result dialog. ok is None for a mirror that was skipped on purpose.
    - Mirrors only run when Cross-Service Sync is on and never for a whole-show removal; a disabled mirror is left out.
    - If the origin fails nothing is sent to the others, so the services never end up half changed.
    - Simkl numbers seasons differently from the others for some shows: seasons are not mirrored to or from Simkl unless the numbering
      checks out (see simkl_numbering_agrees)."""
    results = []
    mirror = bool(get_setting('cross_sync_services')) and not whole  # a whole-show removal stays on the service it was picked on
    order = (origin, *[k for k in SERVICE_CLASSES if k != origin]) if mirror else (origin,)
    numbering = None
    for key in order:
        service = SERVICE_CLASSES[key]()
        if not service.enabled():
            if key == origin:
                results.append((key, False, 'not available'))
                break
            continue
        payload = build_payload(key, tmdb_id, seasons, episodes, whole)
        if not payload:
            continue  # Simkl with only episodes chosen: nothing to send
        if seasons and not whole and key != origin and 'simkl' in (key, origin):
            if numbering is None:
                simkl = service if key == 'simkl' else SERVICE_CLASSES['simkl']()
                numbering = simkl_numbering_agrees(tmdb_id, seasons, simkl.api)
            if not numbering:
                results.append((key, None, 'skipped - Simkl numbers these seasons differently, or it could not be checked'))
                continue
        try:
            response = service.post(payload)
        except Exception as exc:
            kodi_log(f'unwatch_lists: {key} request failed: {exc}', 2)
            response = None
        status = getattr(response, 'status_code', None)
        ok = response is not None and (status is None or 200 <= int(status) < 300)
        removed = removed_count(response) if ok else None
        if ok:
            service.after_removal(tmdb_id, seasons, episodes, removed, whole)
            text = 'done' if removed is None else (f'{removed} removed' if removed else 'nothing to remove there')
        else:
            text = f'failed{f" (HTTP {status})" if status else ""}'
            if key == origin:
                results.append((key, False, text + ' - nothing else was changed'))
                break
        results.append((key, ok, text))
    return results


# --- Menu entries -------------------------------------------------------------------------------------------------------------------------

class UnwatchedListBase:
    """A menu entry that lists what one service has watched for the show and unwatches the ticked seasons / episodes.
    The entry is offered for any TV item whose service is switched on and signed in. Nothing is read or synced when the menu opens:
    the service's watched data is pulled fresh when the entry is picked, so the list is never behind a mark made moments ago."""
    service = None      # 'trakt' | 'mdblist' | 'simkl'
    episodes = False    # False: seasons entry, True: episodes entry

    def __init__(self, tmdb_type, tmdb_id, season=None, episode=None):
        self.tmdb_type = tmdb_type
        self.tmdb_id = tmdb_id
        self.season = season
        self.episode = episode

    @cached_property
    def name(self):
        return 'Unwatched episodes' if self.episodes else 'Unwatched seasons'

    @cached_property
    def handle(self):
        return SERVICE_CLASSES[self.service]()

    def get_self(self):
        if self.tmdb_type != 'tv':
            return None
        try:
            return self if self.handle.enabled() else None
        except Exception as exc:
            kodi_log(f'unwatch_lists: {self.service} gate failed: {exc}', 2)
            return None

    def read(self):
        """(seasons, episodes, whole) from the service's local data after a forced refresh."""
        self.handle.refresh(self.tmdb_id)
        cache = self.handle.cache()
        counts, listed = read_watched(cache, self.tmdb_id, episodes=self.service != 'simkl')
        return counts, listed, has_bulk(cache, self.tmdb_id)

    def choices(self, data):
        """[(label, season, episode or None)] in list order; season is WHOLE for the whole-show row."""
        counts, listed, whole = data
        if self.episodes:
            return [(f'{s}x{e:02d}' + (f'. {t}' if t else ''), s, e) for s, e, t in listed]
        rows = [(f'{season_label(s)} ({counts[s]} watched)', s, None) for s in sorted(counts)]
        if whole:
            rows.append(('Whole show (marked as one block)', WHOLE, None))
        return rows

    def sync(self):
        with BusyDialog():
            choices = self.choices(self.read())
        if not choices:
            Dialog().ok(self.name, f'Nothing is marked as watched on {SERVICE_NAMES[self.service]} for this show.')
            return
        picked = Dialog().multiselect(self.name, [c[0] for c in choices])
        if not picked:  # None = cancelled, [] = nothing ticked
            return
        seasons, episodes, whole = [], {}, False
        for index in picked:
            _, season, episode = choices[index]
            if season == WHOLE:
                whole = True
            elif episode is None:
                seasons.append(season)
            else:
                episodes.setdefault(season, []).append(episode)
        with BusyDialog():
            results = unwatch(self.service, self.tmdb_id, seasons, episodes, whole)
        lines = [f'{SERVICE_NAMES[key]}: {text}' for key, _, text in results]
        Dialog().ok(self.name, '\n'.join(lines) if lines else 'Nothing was sent.')
        from tmdbhelper.lib.script.method.kodi_utils import reload_widgets
        executebuiltin('Container.Refresh')
        reload_widgets()


class ItemTraktUnwatchedSeasons(UnwatchedListBase):
    service = 'trakt'


class ItemTraktUnwatchedEpisodes(UnwatchedListBase):
    service = 'trakt'
    episodes = True


class ItemMDbListUnwatchedSeasons(UnwatchedListBase):
    service = 'mdblist'


class ItemMDbListUnwatchedEpisodes(UnwatchedListBase):
    service = 'mdblist'
    episodes = True


class ItemSimklUnwatchedSeasons(UnwatchedListBase):
    service = 'simkl'
