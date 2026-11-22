from tmdbhelper.lib.files.ftools import cached_property
from tmdbhelper.lib.files.futils import json_loads as data_loads
from tmdbhelper.lib.files.futils import json_dumps as data_dumps
from tmdbhelper.lib.addon.tmdate import set_timestamp, get_timestamp
from tmdbhelper.lib.files.locker import mutexlock

WETRAKR_LASTACTIVITIES_DATA = 'WeTrakr.LastActivities'
WETRAKR_LASTACTIVITIES_EXPIRY = 600  # seconds the answer of /sync/last_activities is reused; a play made here resets it at once
NO_ACTIVITY = '2000-01-01T00:00:00.000Z'  # the stamp of an account with nothing to sync


class WeTrakrLastActivities:
    """ WeTrakr's /sync/last_activities: {'all', 'movies': {...}, 'shows': {...}, 'episodes': {...}, 'account': {...}} with one stamp per kind of change.
    The watched data moves on last_tracking_watched_at (a play added) and last_tracking_removed_at (a play removed: the only stamp an unwatch
    moves); the resume points move on last_playback_at. Keeping them apart means a start, pause or seek never costs a refetch of the watched data. """

    WATCHED_KEYS = ('last_tracking_watched_at', 'last_tracking_removed_at')
    PLAYBACK_KEYS = ('last_playback_at',)
    RATINGS_KEYS = ('last_updated_at', 'last_removed_at')
    KINDS = ('movies', 'shows', 'episodes')

    def __init__(self, class_instance_syncdata):
        self.class_instance_syncdata = class_instance_syncdata

    @property
    def mutex_lockname(self):
        return f'{self.cache._db_file}.wetrakr_last_activities.lockfile'

    @property
    def cache(self):
        return self.class_instance_syncdata.cache

    @property
    def window(self):
        return self.class_instance_syncdata.window

    @property
    def wetrakr_api(self):
        return self.class_instance_syncdata._class_instance_wetrakr_api

    @cached_property
    def json(self):
        return self.get_json()

    def get_json(self):
        return self.get_json_prop() or self.get_json_data() or {}

    @mutexlock
    def get_json_data(self):
        return self.get_json_prop() or self.get_json_sync() or {}

    def get_json_prop(self):
        data = data_loads(self.window.get_property(WETRAKR_LASTACTIVITIES_DATA))
        if not data or not get_timestamp(data.get('expiry') or 0):
            return
        return data

    def get_json_sync(self):
        data = self.wetrakr_api.get_json('/sync/last_activities')
        if not data:
            return
        data['expiry'] = set_timestamp(WETRAKR_LASTACTIVITIES_EXPIRY)
        self.window.get_property(WETRAKR_LASTACTIVITIES_DATA, set_property=data_dumps(data))
        return data

    def _newest(self, keys):
        """ Newest stamp among these keys across movies, shows and episodes. NO_ACTIVITY when the account has none; None when the answer
        could not be read at all (then nothing can be said about what changed) """
        data = self.json
        if not data:
            return None
        stamps = [(data.get(kind) or {}).get(key) for kind in self.KINDS for key in keys]
        return max((v for v in stamps if v), default=NO_ACTIVITY)

    def watched_stamp(self):
        return self._newest(self.WATCHED_KEYS)

    def ratings_stamp(self):
        """ The ratings of every kind (movies, shows, seasons, episodes) move the account-level 'ratings' object: last_updated_at for a rating added or
        changed, last_removed_at for one removed (both live-probed 2026-11-21). The per-kind last_rated_at stamps are not walked: they have no
        'seasons' entry in KINDS and a season-only rating would be missed. NO_ACTIVITY when the account has none; None when unreadable. """
        data = self.json
        if not data:
            return None
        ratings = data.get('ratings') or {}
        stamps = [ratings.get(key) for key in self.RATINGS_KEYS]
        return max((v for v in stamps if v), default=NO_ACTIVITY)

    def playback_stamp(self):
        return self._newest(self.PLAYBACK_KEYS)
