from tmdbhelper.lib.files.ftools import cached_property
from tmdbhelper.lib.files.futils import json_loads as data_loads
from tmdbhelper.lib.files.futils import json_dumps as data_dumps
from tmdbhelper.lib.addon.tmdate import set_timestamp, get_timestamp
from tmdbhelper.lib.files.locker import mutexlock


SIMKL_LASTACTIVITIES_DATA = 'Simkl.LastActivities'
SIMKL_LASTACTIVITIES_EXPIRY = 600


class SimklLastActivities:

    @property
    def mutex_lockname(self):
        return f'{self.cache._db_file}.simkl_last_activities.lockfile'

    @cached_property
    def json(self):
        return self.get_json()

    @cached_property
    def json_data(self):
        return self.get_json_data()

    @cached_property
    def json_prop(self):
        return self.get_json_prop()

    @cached_property
    def json_sync(self):
        return self.get_json_sync()

    def __init__(self, class_instance_syncdata):
        self.class_instance_syncdata = class_instance_syncdata

    @property
    def cache(self):
        return self.class_instance_syncdata.cache

    @property
    def window(self):
        return self.class_instance_syncdata.window

    @property
    def simkl_api(self):
        return self.class_instance_syncdata._class_instance_simkl_api

    def get_json(self):
        return self.json_prop or self.json_data or {}

    @mutexlock
    def get_json_data(self):
        return self.json_prop or self.json_sync or {}

    def get_json_prop(self):
        data = data_loads(self.window.get_property(SIMKL_LASTACTIVITIES_DATA))
        if not data or not get_timestamp(data.get('expiry') or 0):
            return
        return data

    def get_json_sync(self):
        from tmdbhelper.lib.addon.logger import kodi_log
        kodi_log('Simkl Sync: last_activities', 2)
        url = f'{self.simkl_api.req_api_url}/sync/activities?client_id={self.simkl_api.client_id}'
        response = self.simkl_api.get_api_request(url, headers=self.simkl_api.auth_headers)
        if response is None:
            return
        try:
            data = response.json()
        except (ValueError, AttributeError):
            return
        if not data:
            return
        data['expiry'] = set_timestamp(SIMKL_LASTACTIVITIES_EXPIRY)
        self.window.get_property(SIMKL_LASTACTIVITIES_DATA, set_property=data_dumps(data))
        return data

    def get_max_watched_timestamp(self):
        """Return the newest watched-related activity timestamp across movies, shows, and anime.

        rated_at is included because re-rating an already completed movie moves only that stamp
        (live-checked 2026-10-14), and the watched sync also stores the user's rating.
        movies.all is included because a movie that only changes list status (plan-to-watch, hold, dropped)
        moves none of the other movie stamps, and the watched sync also stores the list status.
        """
        d = self.json
        candidates = [
            (d.get('movies') or {}).get('completed'),
            (d.get('movies') or {}).get('all'),
            (d.get('movies') or {}).get('rated_at'),
            (d.get('tv_shows') or {}).get('all'),
            (d.get('tv_shows') or {}).get('rated_at'),
            (d.get('anime') or {}).get('all'),
            (d.get('anime') or {}).get('rated_at'),
        ]
        return max((v for v in candidates if v), default=None)

    def is_expired(self, timestamp, keys=None):
        if not timestamp:
            return True
        latest = self.get_max_watched_timestamp()
        if not latest:
            return True
        return latest > timestamp
