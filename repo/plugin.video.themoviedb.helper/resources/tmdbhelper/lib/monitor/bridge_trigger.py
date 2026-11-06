"""When the Cross-Service Sync bridge runs, besides its fixed interval (setting cross_sync_interval, the slow safety net).

Setting cross_sync_dynamic (default on) adds two triggers, checked every few seconds by the service's CronJobMonitor:

  - after a change made in Kodi: a mark (watched / unwatched, lists, playback ended) calls request_run() from its own
    script process (a window property); the bridge runs once the requests have been quiet for _DEBOUNCE seconds, so
    marking five episodes in a row gives one run.
  - when a service reports new activity: each service's cheap last-activities stamp is read fresh (never the cached copy
    the syncs use, which would hide the change) every few minutes and compared with the last stamp this trigger saw;
    a change made on the website or in an app starts a run. The first read after start only sets the baseline.

A run that leaves removals waiting for their second confirmation (see BridgeSync._removals) is followed by another run
after _FOLLOWUP_DELAY seconds, up to _MAX_FOLLOWUPS times, so an unwatch is carried over within minutes rather than at
the next interval. The confirmation itself still needs the removal to have been missing for _REMOVE_CONFIRM_SECONDS,
so two reads made seconds apart can never confirm each other.

Never while a video is playing; the request stays and is served afterwards.
"""
import json
import time

REQUEST_PROP = 'TMDbHelper.BridgeSync.Requested'   # set by request_run(), read here; cleared when a run starts
_RUNNING_PROP = 'TMDbHelper.BridgeSync.Running'

_DEBOUNCE = 20            # seconds the requests must have been quiet
_MIN_GAP = 30             # seconds between two triggered runs
_POLL_SECONDS = {'trakt': 120, 'simkl': 120, 'mdblist': 300}   # MDBList has the smallest daily request budget
_FOLLOWUP_DELAY = 190     # a little after BridgeSync's _REMOVE_CONFIRM_SECONDS
_MAX_FOLLOWUPS = 3


def request_run():
    """Ask the service to run the bridge soon. Safe to call from any process; does nothing harmful when the setting is off."""
    try:
        from jurialmunkey.window import get_property
        get_property(REQUEST_PROP, set_property=str(time.time()))
    except Exception:
        pass


class BridgeTrigger:

    def __init__(self, trakt_api, monitor):
        self.trakt_api = trakt_api
        self.monitor = monitor
        self._stamps = {}          # {service: last stamp seen}; a service is in here once its baseline is set
        self._last_poll = {}       # {service: time of the last read}
        self._last_run = 0.0
        self._followup_at = 0.0
        self._followups = 0
        self._mdb = None
        self._simkl = None

    # ── Settings ──────────────────────────────────────────────────────────────

    @staticmethod
    def _enabled():
        from tmdbhelper.lib.addon.plugin import get_setting
        return bool(get_setting('cross_sync_dynamic') and get_setting('cross_sync_services')
                    and get_setting('service_trakt') and (get_setting('service_mdblist') or get_setting('service_simkl')))

    # ── Fresh activity stamps (one cheap call each) ────────────────────────────

    def _fingerprint(self, service):
        """The service's latest-activity stamp(s) as a string, or None when it could not be read."""
        from tmdbhelper.lib.addon.plugin import get_setting
        try:
            if service == 'trakt':
                data = self.trakt_api.get_response_json('sync/last_activities')
                return str(data.get('all')) if data and data.get('all') else None
            if service == 'mdblist':
                if not get_setting('service_mdblist'):
                    return None
                if self._mdb is None:
                    from tmdbhelper.lib.api.mdblist.api import MDbList
                    self._mdb = MDbList()
                if not self._mdb.user_token.value:
                    return None
                path = self._mdb.get_request_url('sync', 'last_activities')
                response = self._mdb.get_api_request(path, headers=self._mdb.auth_headers)
                data = response.json() if response is not None else None
            elif service == 'simkl':
                if not get_setting('service_simkl'):
                    return None
                if self._simkl is None:
                    from tmdbhelper.lib.api.simkl.api import SimklAPI
                    self._simkl = SimklAPI()
                if not self._simkl.user_token.value:
                    return None
                url = f'{self._simkl.req_api_url}/sync/activities?client_id={self._simkl.client_id}'
                response = self._simkl.get_api_request(url, headers=self._simkl.auth_headers)
                data = response.json() if response is not None else None
            else:
                return None
            if not data or not isinstance(data, dict):
                return None
            data = {k: v for k, v in data.items() if k not in ('expiry', 'server_time')}   # server_time is the server's clock, different on every call
            return json.dumps(data, sort_keys=True)
        except Exception:
            return None

    def _poll(self, now):
        """True when a service's stamp changed since the last one this trigger saw."""
        changed = []
        for service, seconds in _POLL_SECONDS.items():
            if now - self._last_poll.get(service, 0) < seconds:
                continue
            self._last_poll[service] = now
            value = self._fingerprint(service)
            if value is None:
                continue                       # unreadable: no conclusion
            if service in self._stamps and self._stamps[service] != value:
                changed.append(service)
            self._stamps[service] = value      # the first read only sets the baseline
        return changed

    def _rebaseline(self):
        """After a run: take the services' stamps as they are now, so the run's own pushes do not start the next one."""
        now = time.time()
        for service in _POLL_SECONDS:
            value = self._fingerprint(service)
            if value is not None:
                self._stamps[service] = value
            self._last_poll[service] = now

    # ── Loop step ─────────────────────────────────────────────────────────────

    def tick(self):
        try:
            self._tick()
        except Exception as exc:
            from tmdbhelper.lib.addon.logger import kodi_log
            kodi_log(f'BridgeTrigger error: {exc}', 2)

    def _tick(self):
        if not self._enabled():
            return
        from jurialmunkey.window import get_property
        if get_property(_RUNNING_PROP):
            return
        now = time.time()
        requested = float(get_property(REQUEST_PROP) or 0)
        reason = None
        if requested and now - requested >= _DEBOUNCE:
            reason = 'a change made in Kodi'
        elif self._followup_at and now >= self._followup_at:
            reason = 'removals waiting for their second check'
        else:
            changed = self._poll(now)
            if changed:
                reason = f'new activity on {", ".join(changed)}'
        if not reason:
            return
        if now - self._last_run < _MIN_GAP:
            return
        from xbmc import Player
        if Player().isPlayingVideo():
            return
        self._run(reason, requested)

    def _run(self, reason, requested):
        from jurialmunkey.window import get_property
        from tmdbhelper.lib.addon.logger import kodi_log
        from tmdbhelper.lib.addon.plugin import get_setting
        from tmdbhelper.lib.api.mdblist.api import MDbList
        from tmdbhelper.lib.api.simkl.api import SimklAPI
        from tmdbhelper.lib.monitor.bridge_sync import BridgeSync
        if requested and float(get_property(REQUEST_PROP) or 0) == requested:
            get_property(REQUEST_PROP, clear_property=True)     # a request made while the run is going starts another
        self._followup_at = 0.0
        kodi_log(f'BridgeTrigger: running the bridge - {reason}', 1)
        simkl = SimklAPI() if get_setting('service_simkl') else None
        bridge = BridgeSync(self.trakt_api, MDbList(), simkl)
        bridge.run(force=True)
        self._last_run = time.time()
        self._rebaseline()
        if bridge.has_pending_removals and self._followups < _MAX_FOLLOWUPS:
            self._followups += 1
            self._followup_at = time.time() + _FOLLOWUP_DELAY
        else:
            self._followups = 0
