from tmdbhelper.lib.files.ftools import cached_property
from tmdbhelper.lib.files.futils import json_loads as data_loads
from tmdbhelper.lib.files.futils import json_dumps as data_dumps
from tmdbhelper.lib.addon.tmdate import set_timestamp, get_timestamp
from tmdbhelper.lib.files.locker import mutexlock


MDBLIST_LASTACTIVITIES_DATA = 'MDbList.LastActivities'
MDBLIST_LASTACTIVITIES_EXPIRY = 600


class MDbListLastActivities:

    @property
    def mutex_lockname(self):
        return f'{self.cache._db_file}.mdblist_last_activities.lockfile'

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
    def mdblist_api(self):
        return self.class_instance_syncdata._class_instance_mdblist_api

    def get_json(self):
        return self.json_prop or self.json_data or {}

    @mutexlock
    def get_json_data(self):
        return self.json_prop or self.json_sync or {}

    def get_json_prop(self):
        data = data_loads(self.window.get_property(MDBLIST_LASTACTIVITIES_DATA))
        if not data or not get_timestamp(data.get('expiry') or 0):
            return
        return data

    def get_json_sync(self):
        from tmdbhelper.lib.addon.logger import kodi_log
        kodi_log('MDbList Sync: last_activities', 2)
        path = self.mdblist_api.get_request_url('sync', 'last_activities')
        response = self.mdblist_api.get_api_request(path, headers=self.mdblist_api.auth_headers)
        if response is None:
            return
        try:
            data = response.json()
        except (ValueError, AttributeError):
            return
        if not data:
            return
        data['expiry'] = set_timestamp(MDBLIST_LASTACTIVITIES_EXPIRY)
        self.window.get_property(MDBLIST_LASTACTIVITIES_DATA, set_property=data_dumps(data))
        return data

    def is_expired(self, timestamp, keys=None):
        if not timestamp:
            return True
        last_activity = self.json
        if not last_activity:
            return True
        # MDBList last_activities is flat: {'watched_at': '...', 'watchlisted_at': '...', ...}
        # keys is a 1-tuple like ('watched_at',) — look up directly
        value = last_activity.get((keys or ('watched_at',))[0]) if keys else last_activity.get('watched_at')
        if not value or value > timestamp:
            return True
        return False
