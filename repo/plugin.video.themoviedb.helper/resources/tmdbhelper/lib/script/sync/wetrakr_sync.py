from tmdbhelper.lib.files.ftools import cached_property
from tmdbhelper.lib.addon.plugin import get_localized, get_setting, executebuiltin
from tmdbhelper.lib.addon.dialog import BusyDialog
from tmdbhelper.lib.addon.logger import kodi_log
from xbmcgui import Dialog

SERVICE_NAME = 'WeTrakr'


def has_entries(value):
    """ True when a WeTrakr answer part (an array, or an object of arrays per target) holds anything """
    if isinstance(value, dict):
        return any(has_entries(v) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(has_entries(v) for v in value)
    return bool(value)


def read_outcome(response):
    """ What a write answered, as (status, detail): 'ok', 'already' (WeTrakr says the title already has that state) or 'failed'.

    A 2xx answer is not enough: WeTrakr answers 200 with counts of what changed under `added` / `removed`, and names what it did not do under
    `notFound` (id not resolved), `existing` or `errored` (e.g. "Already watched!", "Nothing to remove"). A season or a show can be partly done
    (some episodes changed, the rest listed as errored), so anything that really changed counts as done first; only an answer that changed nothing
    is 'already' (the title was in that state) or 'failed'. """
    if response is None:
        return 'failed', 'no answer from WeTrakr'
    code = response.status_code
    try:
        data = response.json()
    except ValueError:
        data = None
    if code == 404 and isinstance(data, dict) and data.get('error') == 'NO_SESSION':
        return 'already', 'no resume point left on WeTrakr'  # DELETE /scrobble/playing with nothing paused (live-probed 2026-11-21)
    if not 200 <= code < 300:
        return 'failed', f'HTTP {code}'
    if data is None:
        return 'ok', ''
    if not isinstance(data, dict):
        return 'ok', ''
    if has_entries(data.get('added')) or has_entries(data.get('removed')):
        return 'ok', ''
    if has_entries(data.get('notFound')) or has_entries(data.get('not_found')):
        return 'failed', 'WeTrakr could not find the title'
    if has_entries(data.get('existing')):
        return 'already', 'already in that state on WeTrakr'
    errored = data.get('errored') or data.get('errors')
    if has_entries(errored):
        if 'already' in f'{errored}'.lower():
            return 'already', 'already in that state on WeTrakr'
        return 'failed', 'WeTrakr refused the change'
    return 'ok', ''


class ItemWeTrakrSyncBase:
    """ Base of the WeTrakr context menu items (Set as watched / unwatched, Reset resume position, Add rating).

    Everything is sent by TMDb id and TMDb numbering (WeTrakr resolves titles by TMDb id; seasons and episodes of a show by number in the nested
    shows > seasons > episodes form). Nothing is written to the local WeTrakrSync.db (the rating write-through of ItemWeTrakrRating apart): after an
    accepted write the saved last_activities answer is dropped, so the list refreshed right afterwards asks WeTrakr again and reads the changed data
    (the same path a finished play takes).
    The menu is built from local data only (no request); an item is hidden when the local copy says it does not apply.
    """

    allow_seasons = True
    allow_episodes = True
    preconfigured = True

    def __init__(self, tmdb_type, tmdb_id, season=None, episode=None):
        self.tmdb_type = tmdb_type
        self.tmdb_id = tmdb_id
        self.season = season
        self.episode = episode

    # --- API + local copy ---

    @cached_property
    def wetrakr_api(self):
        from tmdbhelper.lib.api.wetrakr.api import WeTrakrAPI
        return WeTrakrAPI()

    @cached_property
    def wetrakr_syncdata(self):
        return self.wetrakr_api.wetrakr_syncdata

    @cached_property
    def is_authorized(self):
        return bool(self.wetrakr_api.is_authorized)

    # --- Item type ---

    @cached_property
    def wetrakr_type(self):
        if self.tmdb_type == 'movie':
            return 'movie'
        if self.tmdb_type != 'tv':
            return None
        if self.season is None:
            return 'show'
        if self.episode is None:
            return 'season'
        return 'episode'

    @cached_property
    def is_allowed_type(self):
        if self.wetrakr_type is None:
            return False
        if self.wetrakr_type == 'season':
            return self.allow_seasons
        if self.wetrakr_type == 'episode':
            return self.allow_episodes
        return True

    # --- Payloads ---

    @cached_property
    def tmdb_int(self):
        try:
            return int(self.tmdb_id)
        except (TypeError, ValueError):
            return None

    def tracking_payload(self, status='watched'):
        """ Body of POST /sync/tracking and /sync/tracking/remove/all (the same shape). A season or episode is the nested form, by number. """
        if self.tmdb_int is None:
            return {}
        ids = {'tmdb': self.tmdb_int}
        if self.wetrakr_type == 'movie':
            return {'movies': [{'ids': ids, 'status': status}]}
        if self.wetrakr_type == 'show':
            return {'shows': [{'ids': ids, 'status': status}]}
        if self.wetrakr_type == 'season':
            return {'shows': [{'ids': ids, 'status': status, 'seasons': [{'number': int(self.season)}]}]}
        if self.wetrakr_type == 'episode':
            return {'shows': [{'ids': ids, 'status': status, 'seasons': [
                {'number': int(self.season), 'episodes': [{'number': int(self.episode)}]}]}]}
        return {}

    def playing_payload(self):
        """ Body of DELETE /scrobble/playing: the movie, or the show and its episode (a resume point exists for those only) """
        if self.tmdb_int is None:
            return {}
        if self.wetrakr_type == 'movie':
            return {'movie': {'ids': {'tmdb': self.tmdb_int}}}
        if self.wetrakr_type == 'episode':
            return {'show': {'ids': {'tmdb': self.tmdb_int}}, 'episode': {'season': int(self.season), 'number': int(self.episode)}}
        return {}

    def rating_payload(self, rating=None):
        """ Body of POST /sync/ratings (with a rating) and /sync/ratings/remove (without). A season or episode is nested under its show by number. """
        if self.tmdb_int is None:
            return {}
        ids = {'tmdb': self.tmdb_int}
        extra = {} if rating is None else {'rating': rating}
        if self.wetrakr_type == 'movie':
            return {'movies': [{'ids': ids, **extra}]}
        if self.wetrakr_type == 'show':
            return {'shows': [{'ids': ids, **extra}]}
        if self.wetrakr_type == 'season':
            return {'shows': [{'ids': ids, 'seasons': [{'number': int(self.season), **extra}]}]}
        if self.wetrakr_type == 'episode':
            return {'shows': [{'ids': ids, 'seasons': [
                {'number': int(self.season), 'episodes': [{'number': int(self.episode), **extra}]}]}]}
        return {}

    # --- API call ---

    def call_api(self, method, path, payload):
        if not payload:
            return None
        with BusyDialog():
            return self.wetrakr_api.user_request(method, path, body=payload)

    # --- Local state (no request) ---

    def get_sync_value(self, key):
        """ One value of this item's row in the local copy; None when there is no row or it cannot be read. Never starts a sync. """
        try:
            db_type = 'movie' if self.tmdb_type == 'movie' else 'tv'
            season = self.season if self.wetrakr_type in ('season', 'episode') else None
            episode = self.episode if self.wetrakr_type == 'episode' else None
            item_id = self.wetrakr_syncdata.get_name(db_type, self.tmdb_id, season, episode)
            values = self.wetrakr_syncdata.cache.get_values(item_id=item_id, keys=(key,))
            return values[0] if values else None
        except Exception:
            return None

    @cached_property
    def watched_known(self):
        """ The local copy knows the watched state only while the watched read-back setting is on (it is empty otherwise) """
        return bool(get_setting('wetrakr_watchedindicators'))

    @cached_property
    def local_item_id(self):
        db_type = 'movie' if self.tmdb_type == 'movie' else 'tv'
        season = self.season if self.wetrakr_type == 'episode' else None
        episode = self.episode if self.wetrakr_type == 'episode' else None
        return self.wetrakr_syncdata.get_name(db_type, self.tmdb_id, season, episode)

    def shown_playcount(self):
        """ Play count of the focused list item when it is this movie / episode, else None (unknown). Local, no request: the fallback for when the
        local copy is empty because the watched read-back is off (the same idea as Simkl's episode_is_watched). """
        try:
            from tmdbhelper.lib.addon.plugin import get_infolabel
            if self.wetrakr_type == 'episode':
                shown = (get_infolabel('ListItem.Property(item.season)') or get_infolabel('ListItem.Season'),
                         get_infolabel('ListItem.Property(item.episode)') or get_infolabel('ListItem.Episode'))
                if shown != (f'{self.season}', f'{self.episode}'):
                    return None
            elif self.wetrakr_type == 'movie':
                if get_infolabel('ListItem.UniqueID(tmdb)') != f'{self.tmdb_id}':
                    return None
            else:
                return None
            return int(get_infolabel('ListItem.PlayCount') or 0)
        except Exception:
            return None

    @cached_property
    def watched_state(self):
        """ True / False when the watched state is known without a request, None when it is not.
        Read-back on: the local copy (movie / episode: it has a play; show / season: at least one episode is watched).
        Read-back off: the focused list item's play count, for a movie or episode only. """
        if self.watched_known:
            if self.wetrakr_type in ('movie', 'episode'):
                return bool(self.get_sync_value('plays'))
            return bool(self.get_sync_value('watched_episodes'))
        count = self.shown_playcount()
        return None if count is None else count > 0

    # --- Result ---

    @cached_property
    def sync_response(self):
        return self.get_sync_response()

    def get_sync_response(self):
        raise NotImplementedError

    @cached_property
    def outcome(self):
        return read_outcome(self.sync_response)

    @cached_property
    def is_successful_sync(self):
        return self.outcome[0] == 'ok'

    def reset_lastactivities(self):
        """ The write moved WeTrakr's stamps: forget the saved last_activities so the next list read looks again """
        try:
            self.wetrakr_syncdata.reset_lastactivities()
        except Exception as exc:
            kodi_log(f'WeTrakr: last activities not reset - {type(exc).__name__}', 1)

    def display_dialog(self):
        status, detail = self.outcome
        if status == 'ok':
            msg = get_localized(32297)
        elif status == 'already':
            Dialog().ok(self.name, f'{self.name}: {detail}.')
            return
        else:
            kodi_log(f'WeTrakr {self.__class__.__name__} {self.tmdb_type} {self.tmdb_id}: failed - {detail}', 1)
            msg = get_localized(32296)
        Dialog().ok(self.name, msg.format(SERVICE_NAME, self.tmdb_type, 'TMDb', self.tmdb_id))

    def refresh_containers(self, force=False):
        if not force and self.outcome[0] == 'failed':
            return
        from tmdbhelper.lib.script.method.kodi_utils import reload_widgets
        executebuiltin('Container.Refresh')
        reload_widgets()

    def sync(self):
        _ = self.sync_response
        if self.outcome[0] != 'failed':
            self.reset_lastactivities()
        self.display_dialog()
        self.refresh_containers()

    # --- Menu gate ---

    def get_self(self):
        if not self.is_authorized:
            return None
        if not get_setting('service_wetrakr'):
            return None
        if not self.is_allowed_type:
            return None
        return self

    @cached_property
    def name(self):
        return self.get_name()

    def get_name(self):
        raise NotImplementedError


class ItemWeTrakrWatched(ItemWeTrakrSyncBase):
    """ Mark a movie / show / season / episode as watched: POST /sync/tracking with status watched, allow_rewatch false (a title the account already
    watched is answered "Already watched!" and nothing is logged twice). A show is marked as a whole; a season or episode by number.
    Offered unless the watched state is known to be watched (a show / season is always offered: its total is not stored). """

    def get_self(self):
        if not super().get_self():
            return None
        if self.watched_state and self.wetrakr_type in ('movie', 'episode'):
            return None
        return self

    def get_name(self):
        return get_localized(16103)  # "Set as watched"

    def get_sync_response(self):
        payload = self.tracking_payload('watched')
        if payload:
            payload['allow_rewatch'] = False
        return self.call_api('POST', '/sync/tracking', payload)

    def clear_resume_point(self):
        """ A title set as watched must not keep a resume point. WeTrakr drops the paused row itself when a play is logged (live-probed 2026-11-21:
        the playing list is empty right after), so only the local row is cleared, at once, instead of waiting for the next playback read. """
        if self.wetrakr_type not in ('movie', 'episode') or self.outcome[0] == 'failed':
            return
        clear_local_resume_point(self.wetrakr_syncdata, self.local_item_id)

    def sync(self):
        _ = self.sync_response
        self.clear_resume_point()
        if self.outcome[0] != 'failed':
            self.reset_lastactivities()
        self.display_dialog()
        self.refresh_containers()


class ItemWeTrakrUnwatched(ItemWeTrakrSyncBase):
    """ Remove every play of a movie / show / season / episode: POST /sync/tracking/remove/all with status watched (remove alone takes only the
    latest play). Offered unless the watched state is known to be unwatched (a show / season: no watched episode in the local copy). """

    def get_self(self):
        if not super().get_self():
            return None
        if self.watched_state is False:
            return None
        return self

    def get_name(self):
        return get_localized(16104)  # "Set as unwatched"

    def get_sync_response(self):
        return self.call_api('POST', '/sync/tracking/remove/all', self.tracking_payload('watched'))


class ItemWeTrakrClearProgress(ItemWeTrakrSyncBase):
    """ Delete the resume point (paused row) of a movie / episode without marking it unwatched: DELETE /scrobble/playing.
    Offered only while the local copy holds a resume point, which needs the resume point read-back setting. """
    allow_seasons = False

    @cached_property
    def is_allowed_type(self):
        return self.wetrakr_type in ('movie', 'episode')

    def get_self(self):
        if not super().get_self():
            return None
        if not self.get_sync_value('paused_progress'):
            return None
        return self

    def get_name(self):
        return get_localized(38209)  # "Reset resume position"

    def get_sync_response(self):
        return self.call_api('DELETE', '/scrobble/playing', self.playing_payload())

    def sync(self):
        _ = self.sync_response
        if self.outcome[0] != 'failed':
            clear_local_resume_point(self.wetrakr_syncdata, self.local_item_id)
            self.reset_lastactivities()
        self.display_dialog()
        self.refresh_containers()


def clear_local_resume_point(syncdata, item_id):
    try:
        syncdata.cache.execute_sql('UPDATE simplecache SET paused_progress=NULL, paused_at=NULL WHERE id=?', (item_id,))
    except Exception:
        pass


class ItemWeTrakrRating(ItemWeTrakrSyncBase):
    """ Rate a movie / show / season / episode on WeTrakr (0-10, one decimal is kept by WeTrakr; 0 removes the rating, as the other services' items do).

    Add / change: POST /sync/ratings         {"movies": [{"ids": {"tmdb": N}, "rating": X}]}  (shows; seasons and episodes nested by number)
    Remove:       POST /sync/ratings/remove  the same without rating
    The rating is read back (7.5.7, WeTrakrSyncRatings, one list of the account's ratings, read again only when WeTrakr's ratings stamp moves) into the
    local copy, with the watched read-back ("Get watched indicators" on): the entry then says "Change rating (X)" (X as stored: 8.5 stays 8.5), else
    "Add rating". The dialog takes whole numbers 0-10 (as Simkl's). Rating changes nothing else on the account (live-probed 2026-11-21: Watching, Plan to
    Watch, watched and resume lists unchanged after rating a movie, a show, a season and an episode), so the entry needs no "is it in the library" gate. """

    @cached_property
    def current_rating(self):
        """ The rating the local copy holds, 0 when there is none or the ratings are not read back (they come with "Get watched indicators") """
        if not self.watched_known:
            return 0
        try:
            return float(self.get_sync_value('rating') or 0)
        except (TypeError, ValueError):
            return 0

    def get_name(self):
        if self.current_rating:
            return f'{get_localized(32489)} ({self.current_rating:g})'  # "Change rating (X)"
        return get_localized(32485)  # "Add rating"

    @cached_property
    def rating_item_id(self):
        db_type = 'movie' if self.tmdb_type == 'movie' else 'tv'
        season = self.season if self.wetrakr_type in ('season', 'episode') else None
        episode = self.episode if self.wetrakr_type == 'episode' else None
        return self.wetrakr_syncdata.get_name(db_type, self.tmdb_id, season, episode)

    def update_rating_in_db(self):
        """ Write the accepted rating through to the local copy so the next menu shows it at once (the read that follows replaces it with WeTrakr's own
        row). Only while the ratings are read back: with it off the copy holds no ratings and must not start collecting some. """
        rating = getattr(self, '_rating_value', None)
        if rating is None or not self.watched_known or self.outcome[0] == 'failed':
            return
        try:
            cache = self.wetrakr_syncdata.cache
            if not rating:
                cache.execute_sql('UPDATE simplecache SET rating=NULL, rated_at=NULL WHERE id=?', (self.rating_item_id,))
                return
            from datetime import datetime, timezone
            stamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
            season = int(self.season) if self.wetrakr_type in ('season', 'episode') else None
            episode = int(self.episode) if self.wetrakr_type == 'episode' else None
            cache.execute_sql(cache.statement_insert_or_ignore('simplecache', ('id',)), (self.rating_item_id,))
            cache.execute_sql(
                'UPDATE simplecache SET rating=?, rated_at=?, item_type=?, tmdb_type=?, tmdb_id=?, season_number=?, episode_number=? WHERE id=?',
                (float(rating), stamp, self.wetrakr_type, 'movie' if self.tmdb_type == 'movie' else 'tv', self.tmdb_int, season, episode, self.rating_item_id))
        except Exception as exc:
            kodi_log(f'WeTrakr: rating not written to the local copy - {type(exc).__name__}', 1)

    def get_sync_response(self):
        try:
            x = int(Dialog().numeric(0, f'{get_localized(32485)} (0-10)'))
        except ValueError:
            return None  # cancelled: _rating_value stays unset
        if x < 0 or x > 10:
            return None
        self._rating_value = x
        if x == 0:
            return self.call_api('POST', '/sync/ratings/remove', self.rating_payload())
        return self.call_api('POST', '/sync/ratings', self.rating_payload(x))

    def sync(self):
        _ = self.sync_response  # shows the input dialog, then sends
        if not hasattr(self, '_rating_value'):
            return  # cancelled or out of range
        self.update_rating_in_db()
        if self.outcome[0] != 'failed':
            self.reset_lastactivities()
        self.display_dialog()
        self.refresh_containers()
