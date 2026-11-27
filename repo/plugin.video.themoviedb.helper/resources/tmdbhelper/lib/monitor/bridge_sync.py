"""
Bidirectional Trakt ↔ MDBList ↔ Simkl library reconciliation (bridge sync).

A port of Seren's snapshot-delta bridge (Seren modules/bridgeSync.py).

Complements the on-action mirror in cross_sync.py:
  - on-action mirror: real-time, fires on Kodi context menu actions (including removes)
  - bridge sync: catches additions made outside Kodi (web clients, apps)

Strategy - snapshot-delta, not a raw current-state diff:
  Each (domain, service, media) keeps a persisted snapshot of the items already accounted for
  (addon_data/bridge_snapshot_v1.json). Every run:  newly = current - snapshot, and only the
  newly added items are pushed to the services that are missing them. An item enters the next
  snapshot only once it is confirmed present everywhere (already there, or the push succeeded);
  a failed push leaves it out so it is retried. Items that vanish from a service drop out of its
  snapshot, so a later genuine re-add is recognised as new.
  This is what stops a removal on service A (made outside Kodi) from being undone by service B's
  old copy: B's copy is old news, not a new addition.
  Since 7.0.32 such a removal is also carried over to the other services (setting cross_sync_removals, see
  "Removals made outside Kodi" below): confirmed on two runs, never for a bulk disappearance, an item played
  again elsewhere wins, and every removal is journalled first.

  A service whose read failed (or whose Trakt data is not fully synced yet) is skipped for the
  cycle - as source and as target - and its snapshot is left untouched. A failed read is never
  treated as "empty".

Domains (Seren's scope, widened to Simkl at the user's request - Seren itself keeps Simkl to movies):
  Watched movies:   Trakt <-> MDBList <-> Simkl.
  Watched episodes: Trakt <-> MDBList <-> Simkl (Simkl's shows bucket; its anime bucket numbers every cour from 1,
                    so anime episodes only flow FROM Simkl TO Trakt / MDBList, mapped to TMDb numbers by the Fribb
                    index - setting cross_sync_anime, see _anime_mapped). Whole shows are never pushed - a
                    show-level mark means "every episode".
  Watchlist:        Trakt <-> MDBList <-> Simkl plan-to-watch. An item Simkl already has in another
                    status (completed, watching, hold, dropped, not interested) is never overwritten.
  Collection:       Trakt <-> MDBList <-> WeTrakr library (Simkl has no collection endpoint).

Simkl is read only when /sync/activities says it changed since the last cycle (Simkl asks apps not to
poll its lists unconditionally); otherwise the views saved from the last cycle are used. Simkl writes
are spaced 21 s apart (its per-user write lock) and capped per cycle. Episodes Simkl only holds as
synthetic rows (completed shows without per-episode records) are never sent to Trakt history.
  Progress (resume points): Trakt -> MDBList and Trakt -> Simkl only. A point goes only when the target holds none
                    for the item or Trakt's is newer than the target's (its paused_at), so progress made on the target is
                    never overwritten.
  Ratings: on-action mirror only.
  WeTrakr (setting cross_sync_wetrakr: off / log only / on): watched movies and episodes. 7.5.30: On makes it a full member of the two
                    watched domains - a source and a target, with removals both ways (it was a one-way copy before). See "WeTrakr: watched marks".
                    7.5.33: setting cross_sync_wetrakr_lists (off / log only, default / on) adds its planning list to Watchlist and its library to
                    Collection. See "WeTrakr: watchlist and collection".

Trakt history pushes (watched movies / episodes) add a new play on every POST, so they carry
the source's real watch date and go out at most _HISTORY_CAP items per cycle.

Auth gate (7.5.14): at least TWO of Trakt, MDBList, Simkl and WeTrakr (when its setting is On, 7.5.30) must be enabled and authorized, any two (Trakt is no longer required).
A service that is switched off, or that has no sign-in, is simply absent from every view (like Seren's bridge). Trakt that is switched on
with a stored sign-in but is not authorized in this process yet skips the run (as before 7.5.14); a Trakt READ that fails inside a run
makes it "unreadable" for that cycle (present as None), as before.
Loop safety: no Item* classes instantiated here. Direct API + DB only.
"""

import json
import os
import re

from tmdbhelper.lib.addon.logger import kodi_log
from tmdbhelper.lib.addon.plugin import ADDONDATA, get_setting

_LAST_RUN_PROP = 'TMDbHelper.BridgeSync.LastRun'
_RUNNING_PROP = 'TMDbHelper.BridgeSync.Running'
_DEFAULT_INTERVAL_MINUTES = 180   # same cadence as Seren's bridge
_REMOVE_CONFIRM_SECONDS = 180     # an item must have been missing this long, as well as on two runs: runs can now follow each other within a minute (bridge_trigger.py)
_BATCH_SIZE = 100     # max items per Trakt / Simkl call
_MDB_CHUNK = 50       # max items per MDBList call (Seren's live-tested chunk size)
_SIMKL_BASE = 'https://api.simkl.com'
# Simkl names the Dropped status per app: this addon's token reads back 'notinteresting', a Seren-app token 'dropped'
# (live-probed 2026-10-14). Both mean the user dropped it.
_SIMKL_DROPPED = ('dropped', 'notinteresting')
_SNAPSHOT_FILE = 'bridge_snapshot_v1.json'   # addon_data: what has been reconciled. Do NOT delete it once the bridge has run (copies it pushed would look new)
_HISTORY_CAP = 50     # max Trakt history pushes (each one is a new play) per domain per cycle
_MDB_EPISODE_CAP = 250   # max MDBList episode pushes per cycle (API-call budget while the first runs converge)
_SIMKL_CAP = 300      # max Simkl pushes per domain per cycle (<= 3 calls: Simkl locks writes for 20 s per user)
_SIMKL_LOCK_SECONDS = 21
_TARGET_NAMES = {'mdblist': 'MDBList', 'simkl': 'Simkl', 'trakt': 'Trakt', 'wetrakr': 'WeTrakr'}
_GAP_SERVICES = ('trakt', 'mdblist', 'simkl', 'wetrakr')   # the services whose gaps are queued (WeTrakr only once it has joined, see _gap_service)
# WeTrakr (setting cross_sync_wetrakr): 0 off, 1 log only (default), 2 on = a full member of the watched domains (movies, episodes).
# Its view is the watch HISTORY (one row per play), not the watched list: a title counts as unwatched only when no play is left, whatever list it is moved to.
_WETRAKR_PATHS = {'movie': '/sync/tracking/watched/history/movies', 'episode': '/sync/tracking/watched/history/episodes'}
_WETRAKR_CAP = {'movie': 100, 'episode': 300}   # pushes per run (a first run copies a large library over several runs; WeTrakr's free quota is shared per app key)
_WETRAKR_CHUNK = 100       # items per POST /sync/tracking (a 120-item batch was accepted live, 2026-11-21)
_WETRAKR_TRIES = 3         # runs a pushed title may stay missing from WeTrakr's list before it is given up
_WETRAKR_LOG_SECONDS = 6 * 3600   # log-only mode reads WeTrakr at most this often per media type
# WeTrakr watchlist + collection (setting cross_sync_wetrakr_lists, only with cross_sync_wetrakr On): its planning list and its library
_WETRAKR_LISTS = {'movie': ('planning', 'watched', 'dropped', 'hold', 'playing'),     # the lists each media type has (others answer 400)
                  'show': ('planning', 'watching', 'waiting', 'watched', 'dropped', 'hold')}
_WETRAKR_COPY_PREFIX = 'bridge:'       # copy_key of the library copies the bridge makes (TMDb Helper and Seren alike): removal only touches these
_WETRAKR_LIST_CAP = 100                # titles sent to WeTrakr's watchlist / library per run
_WETRAKR_FULL_SECONDS = 86400          # after the library quota answered 420 (free plan: 100 titles) nothing is sent to the library for this long
_STAMP_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}')
# Removals made outside Kodi (setting cross_sync_removals): 0 off, 1 log only, 2 watchlist + collection, 3 also watched history
_MIRROR_TOLERANCE = 2.0        # percent: a MDBList / Simkl resume point within this of the other service's is the same point (resume mirror)
_MIRROR_MIN, _MIRROR_MAX = 4.0, 96.0   # the same 4-96 % gate the In Progress lists show points in
_RESUME_FAIL_RUNS = 2          # a Simkl resume point refused on this many runs in a row is not sent again for _RESUME_FAIL_SECONDS ...
_RESUME_FAIL_SECONDS = 86400   # ... unless Trakt's copy of it changes (a title Simkl does not know answers 404 every time, ~8 s a run)
_REMOVE_MASS_CAP = 10         # more than this many items (or 5% of the snapshot) vanishing from one service at once looks like an
_REMOVE_MASS_CAP_EPISODES = 40  # incomplete read, not the user: nothing is removed from the other services for it
_REMOVE_CLOCK_MARGIN = 90       # seconds a play must be newer than the last run's end to count as 'played again'
_REMOVE_RECENT_DAYS = 3        # how long an item the bridge removed is not taken for a new addition if the service still lists it
_VOUCH_DAYS = 14               # a service that held a title when it was removed may vouch for the removal only once every service it was removed on has been out of the sync this long
_REMOVAL_QUEUE_DAYS = 90       # a queued removal on a list (watchlist, collection) older than this is dropped, not carried out: a list has no date that tells a later re-add apart
_JOURNAL_FILE = 'bridge_removals.jsonl'   # addon_data: one line per removal the bridge made (what, where, the dates it held)
_JOURNAL_LINES = 2000
_ANIME_JOURNAL_FILE = 'bridge_anime_simkl.jsonl'   # addon_data: one line per anime episode the bridge sent to Simkl (entry ids, number, TMDb key, date)
_ANIME_REFUSED_SECONDS = 7 * 86400   # an anime episode Simkl refused is not sent again for this long
_ANIME_PUSH_CHUNK = 100              # episodes per Simkl anime request (several entries share one request)
_SIMKL_MARK_GIVE_UP = 7 * 86400   # a show whose numbering check has failed this long without a break: its held episodes are not sent
_SIMKL_MARK_LOOKUPS = 40   # shows whose Simkl season numbering may be looked up per run for episode copies; the rest wait for a later run
_ANIME_CYCLE_CAP = 100               # anime episodes sent to Simkl per cycle: the 21 s write spacing keeps a run short, a backlog takes a few cycles


# ── Which services take part (one place: cronjob.py, bridge_trigger.py) ─────────
# A service takes part when its setting is on; Trakt also needs a stored sign-in (without one there is nothing to read, and asking its API
# would open a login dialog). MDBList / Simkl authorization is checked by the bridge itself (_mdb_ok / _simkl_ok).

def _trakt_signed_in(trakt_api):
    try:
        return bool(trakt_api is not None and trakt_api.user_token.value)
    except Exception:
        return False


def _wetrakr_setting_mode():
    """cross_sync_wetrakr when WeTrakr takes part at all: 0 off, 1 log only, 2 on."""
    if not (get_setting('cross_sync_services') and get_setting('service_wetrakr')):
        return 0
    try:
        mode = int(get_setting('cross_sync_wetrakr', 'int'))
    except Exception:
        return 1
    return mode if 0 <= mode <= 2 else 1


def _wetrakr_member():
    """WeTrakr is a member: setting On and signed in (log only is not membership)."""
    if _wetrakr_setting_mode() != 2:
        return False
    try:
        from tmdbhelper.lib.api.wetrakr.api import WeTrakrAPI
        return bool(WeTrakrAPI().is_authorized)
    except Exception:
        return False


def _wetrakr_lists_mode():
    """cross_sync_wetrakr_lists (watchlist + collection): 0 off, 1 log only, 2 on. Only while watched marks with WeTrakr are On."""
    if _wetrakr_setting_mode() != 2:
        return 0
    try:
        mode = int(get_setting('cross_sync_wetrakr_lists', 'int'))
    except Exception:
        return 1
    return mode if 0 <= mode <= 2 else 1


def _wetrakr_resume_mode():
    """cross_sync_wetrakr_resume (Trakt resume points to WeTrakr): 0 off, 1 log only, 2 on. Only while watched marks with WeTrakr are On."""
    if _wetrakr_setting_mode() != 2:
        return 0
    try:
        mode = int(get_setting('cross_sync_wetrakr_resume', 'int'))
    except Exception:
        return 1
    return mode if 0 <= mode <= 2 else 1


def wetrakr_lists_stamp(data):
    """The part of /sync/last_activities that moves when a title enters or leaves any tracking list, or None."""
    if not isinstance(data, dict) or not data:
        return None
    fields = ['last_tracking_%s_at' % n for n in ('planning', 'watching', 'waiting', 'watched', 'dropped', 'hold', 'paused', 'removed')]
    return json.dumps([[kind] + [(data.get(kind) or {}).get(f) for f in fields + ['last_playback_at']] for kind in ('movies', 'shows', 'seasons', 'episodes')])


def wetrakr_stamp(data):
    """The part of /sync/last_activities that moves when a watched mark is added or removed, or None."""
    if not isinstance(data, dict) or not data:
        return None
    fields = ['last_tracking_watched_at', 'last_tracking_removed_at']
    lists = _wetrakr_lists_mode() == 2
    if lists:
        # a watchlist or library change on WeTrakr then starts a run too. The library fields are the documented ones; an account that never used
        # its library may not send them (they then read as empty and appear when it does)
        fields += ['last_tracking_planning_at', 'last_added_to_library_at', 'collected_at']
    stamp = [[kind] + [(data.get(kind) or {}).get(f) for f in fields] for kind in ('movies', 'shows', 'seasons', 'episodes')]
    if lists:
        library = data.get('library') or {}
        stamp.append(['library', library.get('last_updated_at'), library.get('last_removed_at')])
    return json.dumps(stamp)


def bridge_services(trakt_api):
    """The services the bridge may use now, in order: ['trakt', 'mdblist', 'simkl', 'wetrakr'] filtered by setting (Trakt by stored sign-in, WeTrakr by membership)."""
    return [name for name, on in (('trakt', get_setting('service_trakt') and _trakt_signed_in(trakt_api)),
                                  ('mdblist', get_setting('service_mdblist')),
                                  ('simkl', get_setting('service_simkl')),
                                  ('wetrakr', _wetrakr_member())) if on]


def bridge_enabled(trakt_api, dynamic=False):
    """Cross-service sync is on and at least two services take part (dynamic=True: also the 'run when something changes' setting)."""
    if not get_setting('cross_sync_services'):
        return False
    if dynamic and not get_setting('cross_sync_dynamic'):
        return False
    return len(bridge_services(trakt_api)) >= 2


def make_bridge(trakt_api):
    """A BridgeSync that knows only the services taking part (None for the others)."""
    services = bridge_services(trakt_api)
    mdb = simkl = None
    if 'mdblist' in services:
        from tmdbhelper.lib.api.mdblist.api import MDbList
        mdb = MDbList()
    if 'simkl' in services:
        from tmdbhelper.lib.api.simkl.api import SimklAPI
        simkl = SimklAPI()
    return BridgeSync(trakt_api if 'trakt' in services else None, mdb, simkl)
_ANIME_COORDS_GONE_SECONDS = 3 * 86400   # how long the entry coordinates of an anime episode that left Simkl's mapped view are kept for the removal check
_ANIME_SENT_SECONDS = 3 * 86400      # how long a sent episode is waited for to show up when Simkl is read back
_TRAKT_AWAIT_SECONDS = 86400   # a key the bridge sent to Trakt may take this long to show in the local Trakt copy (it lags after bulk pushes)
# Simkl anime episodes -> Trakt / MDBList (setting cross_sync_anime): 0 off, 1 log only (default), 2 on


class BridgeSync:

    def __init__(self, trakt_api, mdblist_api, simkl_api=None, wetrakr_api=None):
        self.trakt = trakt_api
        self.mdb = mdblist_api
        self.simkl = simkl_api
        self.wetrakr = wetrakr_api   # created on first use (see _wetrakr_client): only the WeTrakr reads and writes need it
        self.wetrakr_last_stamp = None   # wetrakr_stamp() of this run's last_activities read; None after a write
        self._snapshots = None     # {label: {item_key: tmdb_id}} loaded lazily from _SNAPSHOT_FILE
        self._snapshots_ok = True  # False when the snapshot file can't be read/written: no pushes
        self._meta = {}            # persisted beside the snapshots: Simkl's last-seen activity stamp + its saved views
        self._simkl = None         # this run's Simkl views (see _simkl_prepare); None = Simkl unreadable
        self._simkl_prepared = False
        self._simkl_loaded = False    # True when this run re-read Simkl (not the saved views)
        self._simkl_last_write = 0.0
        self._stamps = {}          # {(media, item_key): newest last_watched_at seen on MDBList / Simkl / WeTrakr}
        self._by_service = {}      # {(service, media, item_key): the date that service itself holds}: what a copy FROM it carries
        self._date_source = None   # the service the WeTrakr push in progress copies from
        self._wt_lists = None      # this run's WeTrakr tracking lists {'planning': {key: tracked_at}, 'other': {key: list name}}; None = not read
        self._wt_activity = None   # this run's /sync/last_activities answer (False = it failed)
        self._wt_kept = set()      # library titles WeTrakr still lists after the bridge removed its own copies (the user's copies stay)
        self._coll_at = {}         # {(service, item_key): when it was collected there}: what a collection copy carries
        self._mdb_watched_cache = None   # one MDBList /sync/watched read per run (movies + episodes)
        self._pushed = {}          # {service: items confirmed pushed to it this run}, for the finishing notification
        self._tstamps = {}         # {(media, item_key): last_watched_at from the Trakt DB}, to tell a newer play from an old one
        self._list_stamps = {}     # {'trakt' / 'mdblist': {'movie:<id>' / 'show:<id>': when it was put on that service's watchlist}} (Simkl's are in its saved views, 'added'): only read to tell a re-add from an old entry
        self._simkl_prev_real = set()   # episodes Simkl held a real record for at the last cycle
        self._anime_cache = None   # (mapped {key: watched_at}, stats) of Simkl's anime episodes in TMDb numbering, one per run
        self._anime_synth_cache = None   # TMDb keys of anime episodes Simkl holds only as bulk-marked rows
        self._anime_coords = {}          # {TMDb key: (AniList id, number inside the entry)} of Simkl's real anime episodes this run (and of those just sent)
        self._anime_gone = {}            # same, kept for a few days for keys that left the mapped view: what an un-watch on Simkl is checked against
        self._anime_removal_logged = set()
        self._anime_known_memo = {}   # {tmdb id: True if the Fribb index knows the show}
        self._anime_dirty = False  # this run wrote anime episodes to Simkl: its saved views are stale, read it again next cycle
        self._anime_stamps = {}    # {key: Simkl's watched_at} of the mapped anime episodes: the date they carry on Trakt, whichever service sends them
        self._simkl_mark_lookups = 0   # shows looked up this run by _gate_simkl_episodes
        self._simkl_gave_up = set()    # (show, season) given up on in this run: every target gets the same answer
        self._anime_keys = set()   # Simkl anime episodes taken into this run's Simkl view (mode 2): only ever sources, never removal sources or Simkl targets
        self._run_started = ''     # UTC ISO time this run started
        self._removed = {}         # {service: items the bridge removed this run}, for the finishing notification
        self._trakt_progress_failed = False   # set by _trakt_progress_rows / _sync_progress; read by _mirror_resume
        self._trakt_authorized = None         # Trakt's authorization, read once per run (see _trakt_ok)

    # ── Per-service auth checks ────────────────────────────────────────────────

    @property
    def _mdb_ok(self):
        return bool(self.mdb and self.mdb.user_token.value)

    @property
    def _simkl_ok(self):
        return bool(self.simkl and self.simkl.user_token.value)

    @property
    def _trakt_listed(self):
        """Trakt takes part in this run (the caller passed it: setting on and a stored sign-in, see make_bridge)."""
        return self.trakt is not None

    @property
    def _trakt_ok(self):
        """Trakt takes part AND is authorized in this process. Read once per run (run() resets it). Anything that would touch Trakt's local
        data or API (trakt_syncdata opens a login dialog when there is no authorization) is gated on this."""
        if self._trakt_authorized is None:
            self._trakt_authorized = bool(self.trakt is not None and getattr(self.trakt, 'authorization', None))
        return self._trakt_authorized

    def _is_authorized(self):
        """At least two of Trakt, MDBList, Simkl and WeTrakr (as a member) are authorized (any two). Trakt that takes part but is not authorized in this process
        (its sign-in not loaded yet, a lapsed token) skips the whole run, as in 7.5.13: a partial run would use up the interval and delay the
        first run that includes Trakt, and what was exchanged without it would never reach Trakt through the other services' snapshots."""
        if self._trakt_listed and not self._trakt_ok:
            return False
        return (self._trakt_ok + self._mdb_ok + self._simkl_ok + self._wetrakr_ok) >= 2

    @property
    def _wetrakr_ok(self):
        """WeTrakr takes part as a member (setting On and signed in)."""
        try:
            return self._wetrakr_mode() == 2 and bool(self._wetrakr_client().is_authorized)
        except Exception:
            return False

    # ── Interval gate ──────────────────────────────────────────────────────────

    def should_run(self):
        import time
        from jurialmunkey.window import get_property
        last = float(get_property(_LAST_RUN_PROP) or 0)
        minutes = get_setting('cross_sync_interval', 'int') or _DEFAULT_INTERVAL_MINUTES
        return (time.time() - last) >= (minutes * 60)

    @property
    def has_pending_removals(self):
        """True while an item first found missing within the last two confirm windows waits for its second check (read after a
        run). An entry that stays pending for good (log-only mode, a target that keeps failing) stops counting, so it cannot keep
        the trigger scheduling follow-up runs."""
        import time
        seen = self._meta.get('removals_seen') or {}
        now = time.time()
        for label, items in (self._meta.get('removals') or {}).items():
            for key in items or ():
                at = (seen.get(label) or {}).get(key)
                if at and now - at <= 2 * _REMOVE_CONFIRM_SECONDS:
                    return True
        return False

    def _mark_done(self):
        import time
        from jurialmunkey.window import get_property
        get_property(_LAST_RUN_PROP, set_property=str(time.time()))

    # ── Entry point ────────────────────────────────────────────────────────────

    def run(self, force=False):
        from jurialmunkey.window import get_property
        if get_property(_RUNNING_PROP):
            return
        if not force and not self.should_run():
            return
        self._trakt_authorized = None
        if not self._is_authorized():
            kodi_log('BridgeSync: skipped — fewer than two of Trakt, MDBList, Simkl and WeTrakr are authorized', 2)
            return
        get_property(_RUNNING_PROP, set_property='true')
        self._mdb_watched_cache = None
        self.wetrakr_last_stamp = None
        self._wt_lists = self._wt_activity = None
        self._wt_kept, self._coll_at = set(), {}
        self._simkl_prepared = False
        self._pushed = {}
        self._removed = {}
        self._tstamps = {}
        self._run_started = self._utc_now()
        ui = self._ui_open()
        try:
            kodi_log('BridgeSync: starting reconciliation', 1)
            steps = ((self._sync_watched_movies, 'Watched movies'), (self._sync_watched_episodes, 'Watched episodes'),
                     (self._sync_watchlist, 'Watchlist'), (self._sync_collection, 'Collection'),
                     (self._sync_progress, 'Resume points'))
            for n, (fn, label) in enumerate(steps):
                self._ui_update(ui, n * 100 // len(steps), label)
                try:
                    fn()
                except Exception as exc:
                    kodi_log(f'BridgeSync: {fn.__name__} error: {exc}', 2)
            self._simkl_finish()
            self._finish_run()
            self._mark_done()
            kodi_log('BridgeSync: reconciliation complete', 1)
            self._ui_close(ui)
            ui = None
            self._ui_summary()
        finally:
            self._ui_close(ui)
            get_property(_RUNNING_PROP, clear_property=True)

    # ── Notifications ──────────────────────────────────────────────────────────
    # Like the Trakt / MDBList / Simkl syncs they follow the 'sync progress notifications' setting, and none is shown
    # while a video is playing (the sync itself is unaffected). Failures here are logged and never stop the sync.

    @staticmethod
    def _video_playing():
        try:
            from xbmc import Player
            return Player().isPlayingVideo()
        except Exception:
            return False

    def _ui_open(self):
        """Background progress bar for the run, or None (setting off, video playing, or no dialog)."""
        if self._video_playing():
            return None
        try:
            from tmdbhelper.lib.addon.dialog import DialogProgressSyncBG
            ui = DialogProgressSyncBG()
            ui.heading = 'Cross-Service Sync'
            ui.create()     # does nothing when the setting is off
            return ui
        except Exception as exc:
            kodi_log(f'BridgeSync: progress dialog error: {exc}', 2)
            return None

    @staticmethod
    def _ui_update(ui, progress, message):
        if ui is None:
            return
        try:
            ui.update_or_create(progress, message)
        except Exception as exc:
            kodi_log(f'BridgeSync: progress dialog update error: {exc}', 2)

    @staticmethod
    def _ui_close(ui):
        if ui is None:
            return
        try:
            ui.close()
        except Exception as exc:
            kodi_log(f'BridgeSync: progress dialog close error: {exc}', 2)

    def _ui_summary(self):
        """One toast at the end of a run that actually changed something on a service (nothing for a quiet run)."""
        total = sum(self._pushed.values())
        if not total or not get_setting('sync_notifications') or self._video_playing():
            return
        try:
            from xbmcgui import Dialog
            from tmdbhelper.lib.addon.plugin import ADDONPATH
            parts = ', '.join(f'{_TARGET_NAMES.get(t, t)} {n}' for t, n in sorted(self._pushed.items()))
            Dialog().notification('Cross-Service Sync', f'{total} item{"s" if total != 1 else ""} synced ({parts})',
                                  icon=f'{ADDONPATH}/icon.png', time=5000)
        except Exception as exc:
            kodi_log(f'BridgeSync: summary notification error: {exc}', 2)

    # ── Trakt local DB readers (DB is kept fresh by CronJob) ───────────────────
    # Every reader returns None on failure (never an empty dict that would look like "Trakt has nothing").

    def _trakt_rows(self, keys, item_type, extra_condition):
        try:
            cache = self.trakt.trakt_syncdata.cache
            rows = cache.get_list_values(
                keys=keys,
                values=(item_type,),
                conditions=f'item_type=? AND tmdb_id IS NOT NULL AND {extra_condition}')
            return list(rows or [])
        except Exception as exc:
            kodi_log(f'BridgeSync: trakt rows({item_type},{extra_condition}) error: {exc}', 2)
            return None

    def _trakt_ids(self, item_type, clause_key):
        """{tmdb_id_str: tmdb_id} from the Trakt DB where item_type=type AND clause_key IS NOT NULL, or None."""
        rows = self._trakt_rows(('tmdb_id',), item_type, f'{clause_key} IS NOT NULL')
        if rows is None:
            return None
        return {str(r[0]): int(r[0]) for r in rows if r and r[0]}

    def _trakt_episode_keys(self):
        """{'show:season:number': show_tmdb_id} for every watched episode in the Trakt DB, or None.
        Specials (season 0) are left out on both sides: the Trakt sync does not store them, so
        they would all look missing from Trakt."""
        rows = self._trakt_rows(
            ('tmdb_id', 'season_number', 'episode_number'), 'episode',
            'last_watched_at IS NOT NULL AND season_number > 0 AND episode_number IS NOT NULL')
        if rows is None:
            return None
        return {f'{r[0]}:{r[1]}:{r[2]}': int(r[0]) for r in rows if r and r[0]}

    def _trakt_progress_rows(self):
        """Return (movie_rows, episode_rows) from Trakt DB for progress sync."""
        movies, episodes = [], []
        self._trakt_progress_failed = False     # an empty answer and a failed read look alike below; the resume mirror must tell them apart
        try:
            cache = self.trakt.trakt_syncdata.cache
            movies = cache.get_list_values(
                keys=('tmdb_id', 'playback_progress', 'playback_paused_at'),
                values=('movie',),
                conditions='item_type=? AND playback_progress IS NOT NULL AND tmdb_id IS NOT NULL') or []
            episodes = cache.get_list_values(
                keys=('tmdb_id', 'playback_progress', 'season_number', 'episode_number', 'playback_paused_at'),
                values=('episode',),
                conditions=(
                    'item_type=? AND playback_progress IS NOT NULL'
                    ' AND tmdb_id IS NOT NULL'
                    ' AND season_number IS NOT NULL AND episode_number IS NOT NULL')) or []
        except Exception as exc:
            self._trakt_progress_failed = True
            kodi_log(f'BridgeSync: trakt_progress_rows error: {exc}', 2)
        return movies, episodes

    # ── MDBList API readers ────────────────────────────────────────────────────

    def _mdb_get(self, *path_parts, **params):
        """GET from MDBList API with Bearer token. Returns parsed JSON or None."""
        try:
            url = self.mdb.get_request_url(*path_parts, **params)
            resp = self.mdb.get_api_request(url, headers=self.mdb.auth_headers)
            if resp is None:
                return None
            return resp.json()
        except Exception as exc:
            kodi_log(f'BridgeSync: mdb_get{path_parts} error: {exc}', 2)
            return None

    def _mdb_get_paginated(self, *path_parts, max_pages=50):
        """Walk a shared-cursor MDBList sync endpoint (single offset/limit window
        spanning movies+shows+seasons+episodes combined — confirmed via MDBList's
        API spec) to completion via has_more, merging each page's arrays.

        Endpoints like /sync/watched, /sync/collection and /watchlist/items
        default to limit=1000 with no offset if called unparameterised — on an
        episode-heavy account, movies can sort outside that first page and be
        silently missed. Bounded to max_pages (50 * 1000 = 50,000 items) as a
        safety cap against a malformed has_more loop.

        Returns None if any page fails or the page budget runs out: a failed read must
        not look like an empty MDBList. A successful read of an empty account is a real {}.
        """
        combined = {}
        offset = 0
        for _ in range(max_pages):
            page = self._mdb_get(*path_parts, offset=offset, limit=1000)
            if not isinstance(page, dict):
                return None
            for key in ('movies', 'shows', 'seasons', 'episodes'):
                if page.get(key):
                    combined.setdefault(key, []).extend(page[key])
            if not (page.get('pagination') or {}).get('has_more'):
                return combined
            offset += 1000
        return None

    def _mdb_watched_raw(self):
        """GET /sync/watched to completion, once per run. None if the read failed."""
        if self._mdb_watched_cache is None:
            self._mdb_watched_cache = self._mdb_get_paginated('sync', 'watched')
        return self._mdb_watched_cache

    def _mdb_watched_movies(self):
        """{tmdb_id_str: tmdb_id} of watched movies on MDBList, or None if the read failed.
        The newest last_watched_at of each is remembered for the push to Trakt / Simkl."""
        raw = self._mdb_watched_raw()
        if raw is None:
            return None
        result = {}
        for item in (raw.get('movies') or []):
            tmdb_id = ((item.get('movie') or {}).get('ids') or {}).get('tmdb')
            if tmdb_id:
                result[str(tmdb_id)] = int(tmdb_id)
                self._note_stamp('movie', str(tmdb_id), item.get('last_watched_at'), 'mdblist')
        return result

    def _mdb_watched_episodes(self):
        """{'show:season:number': show_tmdb_id} of watched episodes on MDBList, or None if the read failed.
        Whole-show / whole-season marks have no episode rows and so do not appear here."""
        raw = self._mdb_watched_raw()
        if raw is None:
            return None
        result = {}
        for item in (raw.get('episodes') or []):
            ep = item.get('episode') or {}
            show = ep.get('show') if isinstance(ep.get('show'), dict) else {}
            tmdb_id = (show.get('ids') or {}).get('tmdb')
            season, number = ep.get('season'), ep.get('number')
            if not tmdb_id or not season or number is None:    # season 0 (specials) is not bridged
                continue
            key = f'{tmdb_id}:{season}:{number}'
            result[key] = int(tmdb_id)
            self._note_stamp('episode', key, item.get('last_watched_at'), 'mdblist')
        return result

    def _mdb_list_ids(self, *path_parts):
        """GET list endpoint → {'movie:<id>' / 'show:<id>': tmdb_id}, or None if the read failed.
        Handles both flat list format and dict with movies/shows/items keys
        (both nested Trakt-style and flat direct-id format).
        sync/collection and watchlist/items share the same shared-cursor pagination contract
        as sync/watched, so this also walks to completion — see _mdb_get_paginated.
        """
        raw = self._mdb_get_paginated(*path_parts)
        if raw is None:
            return None
        result = {}
        if not isinstance(raw, dict):
            return result

        def add(media, tmdb_id):
            if tmdb_id:
                result[f'{media}:{tmdb_id}'] = int(tmdb_id)

        for item in (raw.get('movies') or []):
            add('movie', item.get('id') or ((item.get('movie') or {}).get('ids') or {}).get('tmdb'))
        for item in (raw.get('shows') or []):
            add('show', item.get('id') or ((item.get('show') or {}).get('ids') or {}).get('tmdb'))
        for item in (raw.get('items') or []):
            mediatype = str(item.get('mediatype') or item.get('type') or '')
            add('movie' if 'movie' in mediatype else 'show', item.get('id') or item.get('tmdb'))
        if path_parts == ('sync', 'collection'):
            for bucket, media in (('movies', 'movie'), ('shows', 'show')):
                for item in (raw.get(bucket) or []):
                    tmdb_id = item.get('id') or ((item.get(media) or {}).get('ids') or {}).get('tmdb')
                    if tmdb_id and isinstance(item.get('collected_at'), str):
                        self._coll_at[('mdblist', f'{media}:{tmdb_id}')] = item['collected_at']
        if path_parts == ('watchlist', 'items'):
            stamps = {}
            for bucket, media in (('movies', 'movie'), ('shows', 'show')):
                for item in (raw.get(bucket) or []):
                    tmdb_id, at = item.get('id') or ((item.get(media) or {}).get('ids') or {}).get('tmdb'), item.get('watchlist_at')
                    if tmdb_id and isinstance(at, str):
                        stamps[f'{media}:{tmdb_id}'] = at
            self._list_stamps['mdblist'] = stamps
        return result

    # ── Simkl API readers ──────────────────────────────────────────────────────

    def _simkl_get_ok(self, path):
        """Returns (ok, data): ok is False for a failed request; HTTP 200 with a null / empty body is
        an empty bucket (Simkl answers that way for a bucket with nothing in it)."""
        try:
            sep = '&' if '?' in path else '?'
            url = f'{_SIMKL_BASE}{path}{sep}client_id={self.simkl.client_id}'
            resp = self.simkl.get_api_request(url, headers=self.simkl.auth_headers)
            if resp is None:
                return False, None
            try:
                return True, resp.json()
            except (ValueError, AttributeError):
                return (not (getattr(resp, 'text', None) or '').strip()), None
        except Exception as exc:
            kodi_log(f'BridgeSync: simkl_get({path}) error: {exc}', 2)
            return False, None

    # Simkl views: {'movies': {tmdb: [status, last_watched_at]}, 'shows': {tmdb: [status, last_watched_at]},
    #   'anime': [tmdb of anime shows], 'ep_yes': {'show:season:number': show tmdb} (every episode Simkl counts as
    #   watched, including synthetic rows for completed shows), 'ep_real': {key: watched_at} (episodes the user
    #   actually recorded, the only ones that may go to Trakt history), 'anime_eps': {AniList id: {'tmdb': id, 'eps': {number in the
    # entry: watched_at}}} (Simkl's anime episodes, numbered per entry; mapped to TMDb numbers at use - see _anime_mapped)}.
    # Shows = the shows bucket plus the anime bucket (statuses; the anime episodes only go from Simkl to the other services).

    def _simkl_activity(self):
        """Simkl's top-level activity stamp (GET /sync/activities, cheap), or None if the request failed."""
        ok, data = self._simkl_get_ok('/sync/activities')
        stamp = (data or {}).get('all') if ok and isinstance(data, dict) else None
        return stamp if isinstance(stamp, str) and stamp else None

    def _simkl_load(self):
        """Read Simkl's library (5 requests). None if any request failed - never a partial view."""
        reads = {}
        for name, path in (
                ('movies', '/sync/all-items/movies/'),
                ('yes', '/sync/all-items/shows/?extended=full&episode_watched_at=yes&include_all_episodes=yes'),
                ('real', '/sync/all-items/shows/?extended=full&episode_watched_at=yes&include_all_episodes=original'),
                ('anime', '/sync/all-items/anime/?extended=full&episode_watched_at=yes&include_all_episodes=original'),
                ('anime_all', '/sync/all-items/anime/?extended=full&episode_watched_at=yes&include_all_episodes=yes')):   # also the bulk-marked rows
            ok, data = self._simkl_get_ok(path)
            if not ok or not (data is None or isinstance(data, dict)):
                kodi_log(f'BridgeSync: Simkl {path.split("?")[0]} read failed - skipping Simkl this cycle', 1)
                return None
            reads[name] = data or {}
        views = {'movies': {}, 'shows': {}, 'anime': [], 'ep_yes': {}, 'ep_real': {}, 'anime_eps': {}, 'anime_status': {}, 'anime_synth': {}, 'added': {}}
        for bucket, name, kind in (('movies', 'movies', 'movie'), ('shows', 'yes', 'show'), ('anime', 'anime', None)):
            for item in reads[name].get(bucket) or []:
                media = item.get('show') or item.get('movie') or item
                tmdb_id, at = (media.get('ids') or {}).get('tmdb'), item.get('added_to_watchlist_at')
                if tmdb_id and isinstance(at, str) and item.get('status') == 'plantowatch':
                    views['added'][f'{kind or ("movie" if item.get("anime_type") == "movie" or "movie" in item else "show")}:{tmdb_id}'] = at
        for item in reads['movies'].get('movies') or []:
            tmdb_id = ((item.get('movie') or {}).get('ids') or {}).get('tmdb')
            if tmdb_id:
                views['movies'][str(tmdb_id)] = [item.get('status'), item.get('last_watched_at')]
        for item in reads['anime'].get('anime') or []:
            media = item.get('show') or item.get('movie') or item
            tmdb_id = (media.get('ids') or {}).get('tmdb')
            self._simkl_anime_episodes(views, item, media)
            if not tmdb_id:
                continue
            if item.get('anime_type') == 'movie' or 'movie' in item:
                views['movies'][str(tmdb_id)] = [item.get('status'), item.get('last_watched_at')]
            else:
                views['shows'][str(tmdb_id)] = [item.get('status'), item.get('last_watched_at')]
                views['anime'].append(str(tmdb_id))
        for item in reads['anime_all'].get('anime') or []:
            # episodes Simkl holds only as bulk-marked rows (an entry completed in one go): watched for 'is it on Simkl', never sent on
            media = item.get('show') or item.get('movie') or item
            ids = media.get('ids') or {}
            if item.get('anime_type') == 'movie' or 'movie' in item:
                continue
            try:
                anilist_id = int(ids.get('anilist'))
            except (TypeError, ValueError):
                continue
            real = (views['anime_eps'].get(str(anilist_id)) or {}).get('eps') or {}
            extra = {str(e['number']): '' for s in item.get('seasons') or [] for e in s.get('episodes') or []
                     if e.get('number') is not None and str(e['number']) not in real}
            if extra:
                views['anime_synth'][str(anilist_id)] = {'tmdb': ids.get('tmdb'), 'eps': extra}
        for name, target in (('yes', 'ep_yes'), ('real', 'ep_real')):
            for item in reads[name].get('shows') or []:
                tmdb_id = ((item.get('show') or {}).get('ids') or {}).get('tmdb')
                if not tmdb_id:
                    continue
                if name == 'yes':
                    views['shows'][str(tmdb_id)] = [item.get('status'), item.get('last_watched_at')]
                for season in item.get('seasons') or []:
                    if not season.get('number'):           # season 0 (specials) is not bridged
                        continue
                    for episode in season.get('episodes') or []:
                        if episode.get('number') is None:
                            continue
                        key = f'{tmdb_id}:{season["number"]}:{episode["number"]}'
                        views[target][key] = str(tmdb_id) if name == 'yes' else (episode.get('watched_at') or '')
        return views

    @staticmethod
    def _simkl_anime_episodes(views, item, media):
        """Keep the episodes Simkl really recorded for one anime entry, as numbered INSIDE the entry:
        views['anime_eps'] = {AniList id: {'tmdb': Simkl's TMDb id or None, 'eps': {number: watched_at}}}. They are mapped to TMDb
        numbers every cycle (_anime_mapped), not here, so a corrected Fribb index takes effect without re-reading Simkl."""
        if item.get('anime_type') == 'movie' or 'movie' in item:
            return
        ids = media.get('ids') or {}
        try:
            anilist_id = int(ids.get('anilist'))
        except (TypeError, ValueError):
            return
        views.setdefault('anime_status', {})[str(anilist_id)] = item.get('status')   # every entry, also the ones with no watched episodes
        eps = {}
        for season in item.get('seasons') or []:
            for episode in season.get('episodes') or []:
                if episode.get('number') is not None:
                    eps[str(episode['number'])] = episode.get('watched_at') or ''
        if eps:
            views['anime_eps'][str(anilist_id)] = {'tmdb': ids.get('tmdb'), 'eps': eps}

    @staticmethod
    def _anime_mode():
        """cross_sync_anime: 0 off, 1 log only (default), 2 on."""
        try:
            mode = int(get_setting('cross_sync_anime', 'int'))
        except Exception:
            return 1
        return mode if 0 <= mode <= 2 else 1

    @staticmethod
    def _anime_removal_mode():
        """cross_sync_anime_removals: 0 off, 1 log only (default), 2 on - an anime episode un-watched on one service is un-watched on the others
        (needs cross_sync_removals as well; the same two-run / 3-minute / mass-vanish guards apply)."""
        try:
            mode = int(get_setting('cross_sync_anime_removals', 'int'))
        except Exception:
            return 1
        return mode if 0 <= mode <= 2 else 1

    def _anime_coords_update(self):
        """Remember, for the removal check, which entry and number each mapped anime key is on Simkl, and for a few days after a key leaves
        the mapping (a removal needs two runs, so the coordinates must outlive the first one)."""
        import time
        self._snapshot_data()                  # the saved meta first: it is what the previous run left
        prev = self._meta.get('anime_coords') or {}
        gone = self._meta.get('anime_coords_gone') or {}
        now_t = time.time()
        current = {k: [int(v[0]), int(v[1])] for k, v in self._anime_coords.items()}
        for key, value in prev.items():
            if key not in current and key not in gone:
                gone[key] = list(value) + [now_t]
        for key in list(gone):
            if key in current or now_t - gone[key][2] > _ANIME_COORDS_GONE_SECONDS:
                del gone[key]
        self._meta['anime_coords'] = current
        self._meta['anime_coords_gone'] = gone
        self._anime_gone = {k: (v[0], v[1]) for k, v in gone.items()}

    def _anime_unwatched(self, key):
        """True when Simkl itself dropped this anime episode: the entry is still on the list with a watching / completed / on-hold status
        and no longer holds that episode among the episodes it really recorded. Decided in the entry's own numbering, never by the TMDb key
        (a key can leave the mapping only because the index or TMDb changed). Needs cross_sync_anime_removals = 2 to count."""
        mode = self._anime_removal_mode()
        coord = self._anime_gone.get(key)
        if not mode or coord is None:
            return False
        views = self._simkl or {}
        al, n = str(coord[0]), str(coord[1])
        status = (views.get('anime_status') or {}).get(al)
        if status is None or status in _SIMKL_DROPPED or status == 'plantowatch':
            return False                       # off the list, dropped, or back on plan-to-watch: a status move, not an episode un-watch
        real = ((views.get('anime_eps') or {}).get(al) or {}).get('eps') or {}
        synth = ((views.get('anime_synth') or {}).get(al) or {}).get('eps') or {}
        if n in real or n in synth:
            return False                       # Simkl still holds it: only the mapping changed
        had = sum(1 for v in list((self._meta.get('anime_coords') or {}).values()) + [list(c) for c in self._anime_gone.values()] if str(v[0]) == al)
        if not real and had > 1:
            if (al, 'reset') not in self._anime_removal_logged:
                self._anime_removal_logged.add((al, 'reset'))
                kodi_log(f'BridgeSync: Simkl anime entry {al} lost all its recorded episodes at once - looks like a status reset, not removed elsewhere', 1)
            return False
        if mode == 1:
            if key not in self._anime_removal_logged:
                self._anime_removal_logged.add(key)
                kodi_log(f'BridgeSync (anime removals, log only): {key} (AniList {al} episode {n}) was un-watched on Simkl - would be removed from the other services', 1)
            return False
        return True

    def _anime_mapped(self):
        """(mapped {'tmdb:season:episode': watched_at}, stats) of the anime episodes Simkl recorded, in TMDb numbering; ({}, None)
        when off or Simkl is unreadable. Computed once per run."""
        if self._anime_cache is not None:
            return self._anime_cache
        self._anime_cache = ({}, None)
        views = self._simkl_state() if (self._anime_mode() or self._anime_simkl_mode()) else None
        entries = (views or {}).get('anime_eps')
        if not entries:
            return self._anime_cache
        try:
            from tmdbhelper.lib.api.anilist.entry_map import map_entries
            from tmdbhelper.lib.api.tmdb.api import TMDb
            coords = {}
            self._anime_cache = map_entries(entries, TMDb(), None, coords)
            self._anime_coords.update(coords)
        except Exception as exc:
            kodi_log(f'BridgeSync: anime episode mapping failed - {type(exc).__name__}: {exc}', 2)
        return self._anime_cache

    def _anime_synth_keys(self):
        """TMDb keys of the anime episodes Simkl holds only as bulk-marked rows (an entry completed in one go: the 'original' view lists no
        episodes for it although Simkl counts them watched). They are on Simkl, so the Trakt -> Simkl pass must not send them (found live
        2026-11-05: the first entry sent was already completed 12/12 there); they never go the other way (approximate dates)."""
        if self._anime_synth_cache is not None:
            return self._anime_synth_cache
        self._anime_synth_cache = set()
        entries = (self._simkl_state() or {}).get('anime_synth')
        if entries and (self._anime_mode() or self._anime_simkl_mode()):
            try:
                from tmdbhelper.lib.api.anilist.entry_map import map_entries
                from tmdbhelper.lib.api.tmdb.api import TMDb
                self._anime_synth_cache = set(map_entries(entries, TMDb())[0])
            except Exception as exc:
                kodi_log(f'BridgeSync: anime bulk-marked mapping failed - {type(exc).__name__}: {exc}', 2)
        return self._anime_synth_cache

    def _anime_known(self, tmdb_id):
        """True when the Fribb anime index knows the show (an anime Simkl numbers by entry)."""
        key = str(tmdb_id)
        if key not in self._anime_known_memo:
            try:
                from tmdbhelper.lib.api.anilist import fribb
                self._anime_known_memo[key] = bool(fribb.tv_entries(tmdb_id))
            except Exception:
                self._anime_known_memo[key] = False
        return self._anime_known_memo[key]

    @staticmethod
    def _anime_simkl_mode():
        """cross_sync_anime_simkl: 0 off, 1 log only (default), 2 on - anime episodes watched on Trakt are sent to Simkl."""
        try:
            mode = int(get_setting('cross_sync_anime_simkl', 'int'))
        except Exception:
            return 1
        return mode if 0 <= mode <= 2 else 1

    def _anime_journal(self, rows):
        """Append what was sent to Simkl to bridge_anime_simkl.jsonl so a bad batch can be undone exactly."""
        try:
            import xbmcvfs
            path = xbmcvfs.translatePath(ADDONDATA + _ANIME_JOURNAL_FILE)
            with open(path, 'a', encoding='utf-8') as f:
                for row in rows:
                    f.write(json.dumps(row) + '\n')
        except Exception as exc:
            kodi_log(f'BridgeSync: anime Simkl journal not written ({type(exc).__name__})', 2)

    def _anime_simkl_sync(self, current):
        """Anime episodes watched on Trakt -> Simkl (cross_sync_anime_simkl). Simkl numbers every cour from 1, so each TMDb episode is
        resolved to its entry (AniZip air date) and round-tripped through the same mapping that reads Simkl back; it goes out as
        {'anime': [{'ids': {anilist, mal}, 'episodes': [{number, watched_at}]}]} with Trakt's own watch date. This is a separate pass,
        not part of the reconcile: the reconcile recorded every anime episode as 'not for Simkl' long ago, so the backlog would never be
        'new' there. What Simkl already has (read back every cycle) decides what is missing, so nothing is sent twice."""
        mode = self._anime_simkl_mode()
        if not mode or not self._simkl_ok:
            return
        views = self._simkl_state()
        trakt, simkl = current.get('trakt'), current.get('simkl')
        if views is None or trakt is None or simkl is None:
            return
        import time
        mapped, _stats = self._anime_mapped()
        synth = self._anime_synth_keys()
        anime_shows, shows = set(views.get('anime') or []), views.get('shows') or {}
        refused = self._meta.setdefault('anime_simkl_refused', {})
        unwatching = (self._meta.get('removals') or {}).get('watched|simkl|episode') or {}   # un-watched on Simkl, removal waiting its second run: not to be sent back
        now = time.time()
        for key in [k for k, at in refused.items() if now - at > _ANIME_REFUSED_SECONDS]:
            refused.pop(key, None)
        sent_book = self._meta.setdefault('anime_simkl_sent', {})
        lost = 0
        for key, at in list(sent_book.items()):
            if key in mapped or key in simkl or key in synth:
                sent_book.pop(key, None)                  # read back: it landed
            elif self._simkl_loaded and now - at > 120:
                sent_book.pop(key, None)                  # Simkl was read again and does not hold it as that episode: never send it again
                refused[key] = now
                lost += 1
            elif now - at > _ANIME_SENT_SECONDS:
                sent_book.pop(key, None)
        if lost:
            kodi_log(f'BridgeSync: {lost} anime episode(s) sent to Simkl did not read back as the same episode - not sent again for a week', 1)
        counts = {'on_trakt': 0, 'present': 0, 'regular_show': 0, 'no_date': 0, 'refused': 0}
        cand = {}
        for key, tid in trakt.items():
            sv = str(tid)
            if not self._anime_known(tid):
                continue                                  # not an anime the Fribb index knows
            counts['on_trakt'] += 1
            if sv in shows and sv not in anime_shows:
                counts['regular_show'] += 1               # Simkl keeps it as an ordinary show: the normal path handles it
            elif key in simkl or key in mapped or key in synth:
                counts['present'] += 1
            elif key in refused or key in unwatching:
                counts['refused'] += 1
            elif not self._tstamps.get(('episode', key)):
                counts['no_date'] += 1                    # without Trakt's date Simkl would date it today
            else:
                cand[key] = int(tid)
        try:
            from tmdbhelper.lib.api.anilist.entry_map import forward_entry
            from tmdbhelper.lib.api.tmdb.api import TMDb
            tmdb, cache = TMDb(), {}
        except Exception as exc:
            kodi_log(f'BridgeSync: anime to Simkl unavailable - {type(exc).__name__}: {exc}', 2)
            return
        status = views.get('anime_status') or {}
        out = {'later': 0, 'none': 0, 'dropped': 0}
        none_by_show = {}
        resolved = {}
        for key in sorted(cand):
            _, season, episode = key.split(':')
            try:
                state, found = forward_entry(cand[key], int(season), int(episode), tmdb, cache)
            except Exception as exc:
                kodi_log(f'BridgeSync: anime entry lookup {key} failed - {type(exc).__name__}', 1)
                state, found = 'later', None
            if state != 'ok':
                out[state] += 1
                if state == 'none':
                    none_by_show[cand[key]] = none_by_show.get(cand[key], 0) + 1
            elif status.get(str(found['anilist'])) in _SIMKL_DROPPED:
                out['dropped'] += 1
            else:
                resolved[key] = found
        groups = {}
        for key, found in resolved.items():
            groups.setdefault(found['anilist'], {'ids': found['ids'], 'eps': []})['eps'].append((found['number'], key))
        summary = (f'anime on Trakt {counts["on_trakt"]}: already on Simkl {counts["present"]}, ordinary Simkl shows {counts["regular_show"]}, '
                   f'refused before {counts["refused"]}, no Trakt date {counts["no_date"]}; missing on Simkl {len(cand)}: '
                   f'resolvable {len(resolved)} in {len(groups)} entries, later {out["later"]}, no entry {out["none"]}, dropped on Simkl {out["dropped"]}')
        if mode == 1:
            kodi_log(f'BridgeSync (anime to Simkl, log only): {summary}', 1)
            if none_by_show:
                top = sorted(none_by_show.items(), key=lambda kv: -kv[1])[:12]
                kodi_log(f'BridgeSync (anime to Simkl, log only): no entry for (TMDb show: episodes) {", ".join(f"{s}: {n}" for s, n in top)}', 1)
            return
        if not resolved:
            if cand:
                kodi_log(f'BridgeSync (anime to Simkl): nothing to send now - {summary}', 1)
            return
        limit = self._meta.get('anime_simkl_limit')       # entries per cycle; set by hand for a first small write, None = no limit
        entries = sorted(groups)
        if isinstance(limit, int) and limit > 0:
            entries = entries[:limit]
        flat = [(al, groups[al]['ids'], n, k) for al in entries for n, k in sorted(groups[al]['eps'])][:_ANIME_CYCLE_CAP]
        kodi_log(f'BridgeSync (anime to Simkl): sending {len(flat)} episodes of {len({f[0] for f in flat})} entries - {summary}', 1)
        sent_total = 0
        for j in range(0, len(flat), _ANIME_PUSH_CHUNK):
            part = flat[j:j + _ANIME_PUSH_CHUNK]
            by_entry = {}
            for al, ids, n, k in part:
                by_entry.setdefault(al, {'ids': ids, 'episodes': []})['episodes'].append(
                    {'number': n, 'watched_at': self._tstamps[('episode', k)]})
            ok, data = self._post('simkl', ('sync', 'history'), {'anime': list(by_entry.values())})
            if not ok:
                break                                     # no usable answer: nothing assumed, retried next cycle
            if (data.get('not_found') or {}) and any(data['not_found'].values()):
                for _, _, _, k in part:                   # Simkl named something it could not match: do not send these again for a while
                    refused[k] = now
                kodi_log(f'BridgeSync: Simkl did not take part of an anime request ({len(part)} episodes) - not retried for a week: {self._result_text(data)}', 1)
                continue
            for al, ids, n, k in part:
                simkl[k] = int(k.split(':')[0])
                self._anime_keys.add(k)
                self._anime_coords[k] = (al, n)
                sent_book[k] = now
            self._anime_journal([{'at': now, 'anilist': al, 'ids': ids, 'number': n, 'key': k,
                                  'watched_at': self._tstamps[('episode', k)]} for al, ids, n, k in part])
            sent_total += len(part)
            self._anime_dirty = True
        if sent_total:
            self._pushed['simkl'] = self._pushed.get('simkl', 0) + sent_total
            kodi_log(f'BridgeSync →Simkl: {sent_total} anime episodes', 1)

    def _simkl_prepare(self):
        """Fill self._simkl for this run: the saved views when Simkl's activity stamp has not moved since the
        last cycle, else a fresh read. Simkl asks apps to check /sync/activities before reading lists."""
        self._simkl_prepared = True
        self._simkl = None
        self._simkl_loaded = False
        if not self._simkl_ok:
            return
        self._snapshot_data()
        stamp = self._simkl_activity()
        if stamp is None:
            kodi_log('BridgeSync: Simkl activity check failed - skipping Simkl this cycle', 1)
            return
        saved = self._meta.get('simkl')
        self._simkl_prev_real = set((saved or {}).get('ep_real') or ())
        if saved and self._meta.get('simkl_activity') == stamp and 'anime_eps' in saved and 'anime_status' in saved and 'anime_synth' in saved:   # views saved before 7.0.52 lack the bulk-marked anime rows
            self._simkl = saved
            return
        views = self._simkl_load()
        if views is not None:
            self._simkl, self._simkl_loaded = views, True

    def _simkl_state(self):
        if not self._simkl_prepared:
            self._simkl_prepare()
        return self._simkl

    def _simkl_finish(self):
        """End of a run: remember Simkl's views and (after our own writes) its activity stamp so the next
        cycle can skip the reads when nothing moved."""
        if not self._simkl_prepared or self._simkl is None or not self._snapshots_ok:
            return
        stamp = self._simkl_activity()
        if stamp is None or self._anime_dirty:
            self._meta.pop('simkl_activity', None)      # unknown (or anime episodes were just written to Simkl): re-read next cycle
        else:
            self._meta['simkl_activity'] = stamp
        self._meta['simkl'] = self._simkl
        self._snapshot_save()

    def _simkl_watched_movies(self):
        """{tmdb_id_str: tmdb_id} of completed Simkl movies, or None if Simkl is unreadable."""
        views = self._simkl_state()
        if views is None:
            return None
        result = {}
        for tmdb_id, (status, stamp) in views['movies'].items():
            if status == 'completed':
                result[tmdb_id] = int(tmdb_id)
                self._note_stamp('movie', tmdb_id, stamp, 'simkl')
        return result

    def _simkl_watched_episodes(self):
        """{'show:season:number': show_tmdb_id} Simkl counts as watched (shows bucket), or None."""
        views = self._simkl_state()
        if views is None:
            return None
        for key, stamp in views['ep_real'].items():
            self._note_stamp('episode', key, stamp, 'simkl')
        result = {k: int(v) for k, v in views['ep_yes'].items()}
        if self._anime_mode() == 2:
            # Simkl's anime episodes in TMDb numbering. Never put in ep_yes / ep_real (saved views) and never a removal source
            mapped, _stats = self._anime_mapped()
            self._anime_coords_update()
            for key, stamp in mapped.items():
                if key not in result:
                    result[key] = int(key.split(':')[0])
                    self._anime_keys.add(key)
                    if isinstance(stamp, str) and _STAMP_RE.match(stamp):
                        self._anime_stamps[key] = stamp
        return result

    def _simkl_set(self):
        """{'movie:<id>' / 'show:<id>': id} Simkl has on plan-to-watch, or None."""
        views = self._simkl_state()
        if views is None:
            return None
        result = {}
        for kind, bucket in (('movie', 'movies'), ('show', 'shows')):
            for tmdb_id, (status, _) in views[bucket].items():
                if status == 'plantowatch':
                    result[f'{kind}:{tmdb_id}'] = int(tmdb_id)
        return result

    def _simkl_status(self, key):
        """Simkl's status for a 'movie:<id>' / 'show:<id>' key, or None when it is not in the library."""
        views = self._simkl_state() or {}
        kind, _, tmdb_id = key.partition(':')
        entry = (views.get('movies' if kind == 'movie' else 'shows') or {}).get(tmdb_id)
        return entry[0] if entry else None

    # ── Readiness: the Trakt side of every diff comes from the local Trakt DB ──
    # Before 6.0.94 that DB held only page 1 of each paginated Trakt list (100 watched movies of
    # 417). The Trakt sync writes a `<method>.paged_v1` marker once it has read a list in full;
    # until it exists the Trakt side is treated as unreadable (skipped this cycle).

    def _trakt_refresh(self, media, columns):
        """Bring the local Trakt data for `media` up to date before it is read. It otherwise only
        refreshes when a Trakt list is opened, so without this a watch made elsewhere (Seren scrobbles to
        every service) would look "missing from Trakt" and be posted to its history as a second play.
        The sync is gated on Trakt's last-activities, so an unchanged account costs one small request.
        True only when every route is current afterwards; a failed sync means Trakt is skipped this cycle."""
        try:
            from jurialmunkey.modimp import importmodule
            syncdata = self.trakt.trakt_syncdata
            fresh = True
            for route in {j for j in (syncdata.routes.get(c) for c in columns) if j}:
                datatype = importmodule(*route)(syncdata, media)
                datatype.sync()
                if datatype.is_expired:
                    fresh = False
        except Exception as exc:
            kodi_log(f'BridgeSync: Trakt refresh({media},{columns}) error: {exc}', 2)
            return False
        if not fresh:
            kodi_log(f'BridgeSync: Trakt {media} data could not be refreshed - skipping Trakt this cycle', 1)
        return fresh

    def _trakt_ready(self, media, method):
        try:
            ready = bool(self.trakt.trakt_syncdata.cache.get_activity(media, f'{method}.paged_v1'))
        except Exception as exc:
            kodi_log(f'BridgeSync: trakt readiness check({media},{method}) error: {exc}', 2)
            return False
        if not ready:
            kodi_log(f'BridgeSync: Trakt {method} {media}s not fully synced yet (paginated refetch pending) - skipping Trakt this cycle', 1)
        return ready

    def _trakt_episodes_ready(self):
        """The Trakt episode rows are rebuilt show by show from /progress/watched; the rebuild marker is
        'ok' only when every show was fetched ('partial:<retry>' = some shows have no episode rows yet)."""
        if not self._trakt_ready('show', 'watched'):
            return False
        try:
            state = self.trakt.trakt_syncdata.cache.get_activity('show', 'watched_progress_v1')
        except Exception as exc:
            kodi_log(f'BridgeSync: trakt episode readiness check error: {exc}', 2)
            return False
        if state != 'ok':
            kodi_log('BridgeSync: Trakt watched episodes not fully rebuilt yet - skipping Trakt episodes this cycle', 1)
            return False
        return True

    @staticmethod
    def _resp_ok(resp):
        """A service answered with success. Trakt's post_response hands back the raw response even for
        a 4xx/5xx, so a response object alone is not proof (None = no answer at all)."""
        return resp is not None and getattr(resp, 'ok', True) is not False

    # ── Snapshots (see module docstring) ───────────────────────────────────────

    def _snapshot_path(self):
        import xbmcvfs
        return xbmcvfs.translatePath(ADDONDATA + _SNAPSHOT_FILE)

    def _snapshot_data(self):
        if self._snapshots is not None:
            return self._snapshots
        self._snapshots = {}
        try:
            with open(self._snapshot_path(), encoding='utf-8') as f:
                raw = json.load(f) or {}
            self._snapshots = {k: {str(i): int(t) for i, t in v.items()} for k, v in (raw.get('snapshots') or {}).items()}
            self._meta = raw.get('meta') or {}
        except FileNotFoundError:
            pass
        except Exception as exc:
            self._snapshots_ok = False
            kodi_log(f'BridgeSync: snapshot file unreadable ({type(exc).__name__}) - no pushes until it is fixed or deleted', 2)
        return self._snapshots

    def _snapshot_save(self):
        try:
            path = self._snapshot_path()
            with open(path + '.tmp', 'w', encoding='utf-8') as f:
                json.dump({'v': 2, 'snapshots': self._snapshots, 'meta': self._meta}, f)
            os.replace(path + '.tmp', path)
            return True
        except Exception as exc:
            self._snapshots_ok = False
            kodi_log(f'BridgeSync: snapshot not saved ({type(exc).__name__}) - no further pushes this cycle', 2)
            return False

    def _trakt_awaiting(self, label, cur):
        """Keys the bridge sent to Trakt that the local Trakt copy has not listed yet. They are not removals and not missing:
        the copy lags after bulk pushes (seen live 2026-11-05: 50 items 'no longer listed' for several runs, and the same episodes
        sent to Trakt twice). A key is forgotten once Trakt's listing shows it, or after _TRAKT_AWAIT_SECONDS: one that was never
        listed under that number (Trakt placed the play elsewhere) is dropped from the snapshot, not treated as a removal."""
        import time
        book = self._meta.setdefault('trakt_awaiting', {})
        rec = book.get(label)
        if not rec:
            book.pop(label, None)
            return set()
        now = time.time()
        expired = {k for k, at in rec.items() if now - at > _TRAKT_AWAIT_SECONDS}
        listed = {k for k in rec if cur is not None and k in cur}
        for key in expired | listed:
            rec.pop(key, None)
        if not rec:
            book.pop(label, None)
        never = expired - listed
        if never:
            for key in never:
                (self._snapshots or {}).get(label, {}).pop(key, None)
            kodi_log(f'BridgeSync: {label}: {len(never)} item(s) sent to Trakt were never listed under that number - dropped, not removed elsewhere', 1)
        return set(rec)

    def _trakt_awaiting_add(self, label, keys):
        import time
        if not keys:
            return
        rec = self._meta.setdefault('trakt_awaiting', {}).setdefault(label, {})
        now = time.time()
        for key in [k for k, at in rec.items() if now - at > _TRAKT_AWAIT_SECONDS]:
            rec.pop(key, None)
        rec.update({k: now for k in keys})

    def _reconcile(self, domain, media, current, push_fn, caps=None, skip=None, remove_fn=None, removal_ok=None, gate=None):
        """current: {service: {item_key: tmdb_id} or None (read failed / not ready)}.
        remove_fn(items, source, target) -> succeeded keys, removal_ok(source, key) -> bool: carry removals made outside
        Kodi over to the other services (see _removals; the cross_sync_removals setting decides whether and what).
        push_fn(items, source, target) -> (succeeded, given_up), both subsets of items.keys():
        succeeded = confirmed present on the target now; given_up = permanently unresolvable there
        (e.g. a tmdb id the target does not know) - counted as reconciled so it is not retried forever.
        Anything in neither set stays unresolved and is retried next cycle.
        caps: {target: max items pushed to that target per cycle, across all sources}; the rest stay unresolved.
        skip(missing, source, target) -> keys that must not go to that target (counted as reconciled, and
        taken out before the cap so they cannot use its budget).
        Mutates `current` in place so a push earlier in this run is visible to later sources. An item this run
        pushed to a service is accounted for on that service's side too (its snapshot, and its `newly`) - it
        did not originate there, so it is never sent onward from there (that would launder an item past the
        filters applied to its real source)."""
        import time
        if len(current) < 2:
            return                             # one service alone has nothing to reconcile with; nothing is read, written or snapshotted
        snapshots = self._snapshot_data()
        if not self._snapshots_ok:
            return
        self._queue_housekeeping(current)
        services = list(current)
        before = {s: set(snapshots.get(f'{domain}|{s}|{media}', {})) for s in services}    # for the gap queue (7.5.16), taken before _removals
        budget = dict(caps or {})
        wait_label = f'{domain}|trakt|{media}'
        awaiting = self._trakt_awaiting(wait_label, current.get('trakt')) if 'trakt' in current else set()
        pushed = {}                            # {service: {item_key: id}} put there by this run
        if remove_fn:
            self._removal_queue_readds(domain, media, current, snapshots)     # 7.5.19: before _removals, whose retry loop must not see what the user added back
        held = self._removals(domain, media, current, snapshots, remove_fn, removal_ok, awaiting) if remove_fn else {}
        if remove_fn:
            self._removal_gap_apply(domain, media, current, snapshots, remove_fn)     # 7.5.17: removals made while a service was out
        recent = self._meta.get('removed_recent') or {}
        for source in services:
            cur = current[source]
            if cur is None:
                continue                       # unreadable: snapshot untouched, nothing sourced from it
            label = f'{domain}|{source}|{media}'
            snap = snapshots.get(label, {})
            # an item the bridge removed from this service that it still lists is the removal not showing yet, not a new addition
            lagging = {k for k, at in (recent.get(label) or {}).items()
                       if time.time() - at <= _REMOVE_RECENT_DAYS * 86400
                       and not (domain == 'watched' and self._newer_than(media, k, self._iso_utc(at)))}    # played again since: new, not lagging
            newly = {k: v for k, v in cur.items() if k not in snap and k not in pushed.get(source, {}) and k not in lagging
                     and not self._simkl_keeps(domain, media, source, k)}   # kept on Simkl: never new there, even if its snapshot was not saved
            carried = {k: v for k, v in snap.items() if k in cur or k in held.get(source, ()) or (source == 'trakt' and k in awaiting)}
            unresolved = set()
            for target in services:
                if target == source or not newly:
                    continue
                if current[target] is None:    # cannot tell what the target has: retry later
                    unresolved.update(newly)
                    continue
                missing = {k: v for k, v in newly.items() if k not in current[target] and not (target == 'trakt' and k in awaiting)}
                if missing and skip:
                    skipped = set(skip(missing, source, target))
                    missing = {k: v for k, v in missing.items() if k not in skipped}
                if missing and gate:           # before the cap, like skip; held keys stay unresolved and come back next run
                    drop, hold = gate(missing, source, target)
                    unresolved.update(hold)
                    missing = {k: v for k, v in missing.items() if k not in drop and k not in hold}
                if not missing:
                    continue
                if target in budget:
                    cap = budget[target]
                    if len(missing) > cap:
                        kodi_log(f'BridgeSync: {label} →{_TARGET_NAMES[target]}: {len(missing)} new, sending {cap} this cycle', 1)
                        keep = dict(sorted(missing.items())[:cap])
                        unresolved.update(set(missing) - set(keep))
                        missing = keep
                    budget[target] = cap - len(missing)
                    if not missing:
                        continue
                try:
                    succeeded, given_up = push_fn(missing, source, target)
                except Exception as exc:
                    succeeded, given_up = set(), set()
                    kodi_log(f'BridgeSync: {label} →{_TARGET_NAMES[target]} push error: {exc}', 2)
                if target == 'trakt':
                    sent = set(succeeded) | set(given_up)     # Trakt answered for these: its local copy will list them later
                    self._trakt_awaiting_add(wait_label, sent)
                    awaiting |= sent
                if succeeded:
                    current[target].update({k: missing[k] for k in succeeded})
                    pushed.setdefault(target, {}).update({k: missing[k] for k in succeeded})
                    self._pushed[target] = self._pushed.get(target, 0) + len(succeeded)
                    kodi_log(f'BridgeSync →{_TARGET_NAMES[target]}: {len(succeeded)} {media} ({domain})', 1)
                if given_up:
                    kodi_log(f'BridgeSync: {label} →{_TARGET_NAMES[target]}: {len(given_up)} not accepted, giving up on them', 1)
                unresolved.update(set(missing) - set(succeeded) - set(given_up))
            snapshots[label] = {**carried, **{k: v for k, v in newly.items() if k not in unresolved}}
        for target, items in pushed.items():
            label = f'{domain}|{target}|{media}'
            snapshots[label] = {**snapshots.get(label, {}), **{k: v for k, v in items.items() if k in current[target]}}
        self._gap_record(domain, media, current, snapshots, before, pushed)
        self._gap_catch_up(domain, media, current, snapshots, push_fn, skip, budget, awaiting, gate)
        self._queues_prune(domain, media, current)
        self._snapshot_save()

    # ── Gap queue (7.5.16): what a service missed while it was out of the bridge ──
    # A service that is switched off or not signed in is absent from `current`. What the other services then take in (new watches, list adds)
    # is accounted for in THEIR snapshots, so on the absent service's return it would be old news everywhere and never be sent to it. So every
    # run that accounts new items while a service that has taken part before (its snapshot label exists) is absent queues those keys for it
    # (meta 'gap_queue'). When the service is present and readable again the queue is worked off directly: each key goes to it from one holder,
    # through the same skip / cap / push / confirmation path as any push. Snapshots and removal handling are NOT touched, so a removal
    # made meanwhile on a holder is still seen. Not queued: a service that never took part (its first run is not a catch-up of a whole library).
    # Each queued key remembers where it CAME FROM ('from': the services it was new on, not the ones the bridge copied it to), and is only ever
    # sent from one of those: a copy made by the bridge has lost the rules tied to the real source (e.g. a Simkl row the user never recorded
    # must not reach Trakt history through MDBList's copy). If no origin lists the key any more it is dropped, never sent from a copy.

    def _gap_service(self, domain, service, media):
        """A WeTrakr label written by the old one-way copy is not a membership: nothing is queued for it until it has joined (_wetrakr_join)."""
        return service != 'wetrakr' or f'{domain}|{service}|{media}' in (self._meta.get('wetrakr_member') or {})

    def _gap_record(self, domain, media, current, snapshots, before, pushed):
        import time
        origins = {}
        for service in current:
            for key in set(snapshots.get(f'{domain}|{service}|{media}', {})) - before.get(service, set()):
                if key not in pushed.get(service, {}) and not self._simkl_keeps(domain, media, service, key):
                    origins.setdefault(key, []).append(service)
        if not origins:
            return
        absent = [s for s in _GAP_SERVICES if s not in current and self._gap_service(domain, s, media) and f'{domain}|{s}|{media}' in snapshots]
        if not absent:
            return
        queue = self._meta.setdefault('gap_queue', {})
        now = time.time()
        for service in absent:
            entry = queue.setdefault(f'{domain}|{service}|{media}', {})
            for key, sources in origins.items():
                rec = entry.get(key)
                if not isinstance(rec, dict):
                    rec = entry[key] = {'at': now, 'from': []}
                for source in sources:
                    if source not in rec['from']:
                        rec['from'].append(source)

    def _gap_catch_up(self, domain, media, current, snapshots, push_fn, skip, budget, awaiting, gate=None):
        queue = self._meta.get('gap_queue')
        if not queue:
            return
        pending = self._meta.get('removals') or {}
        recent = self._meta.get('removed_recent') or {}
        for target in list(current):
            label_t = f'{domain}|{target}|{media}'
            entry = queue.get(label_t)
            cur_t = current[target]
            if entry is None or cur_t is None:
                continue                       # nothing queued, or the service cannot be read now: the queue stays as it is
            snap_t = snapshots.get(label_t, {})
            by_source = {}
            for key in sorted(entry):
                if key in cur_t or key in snap_t or key in (recent.get(label_t) or {}):
                    entry.pop(key)             # already there, or it was accounted for there before, or the bridge removed it there
                    continue
                rec = entry[key]
                origins = [s for s in (rec.get('from') or ()) if s != target] if isinstance(rec, dict) else []
                holders = [s for s in current if s in origins and current[s] is not None and key in current[s]]    # an ORIGIN that still lists it
                if media == 'episode' and domain == 'watched':
                    holders.sort(key=lambda s: s == 'simkl')      # episodes only, Simkl last: its numbering has to pass a check
                if not holders:
                    if not origins or all(current.get(s) is not None for s in origins):
                        entry.pop(key)         # every origin was read and none lists it any more (or the record is empty): never sent from a copy
                    continue                   # an origin that is out of the sync or unreadable now: wait for it
                # (the pending record is kept under the service that LOST the title, which is not one of the holders)
                if (target == 'trakt' and key in awaiting) or any(key in (pending.get(f'{domain}|{s}|{media}') or {}) for s in current if s != target):
                    continue                   # sent to Trakt and not listed yet / a removal of it is pending on another service: leave it queued
                by_source.setdefault(holders[0], {})[key] = current[holders[0]][key]     # one holder per key: one push
            for source, items in by_source.items():
                if skip:
                    skipped = set(skip(items, source, target))
                    for key in skipped:
                        entry.pop(key, None)   # skipped = reconciled by the same rule as in the main pass
                    items = {k: v for k, v in items.items() if k not in skipped}
                if gate:
                    drop, hold = gate(items, source, target)
                    for key in drop:
                        entry.pop(key, None)
                    items = {k: v for k, v in items.items() if k not in drop and k not in hold}    # held keys stay queued
                if target in budget:
                    cap = budget[target]
                    if len(items) > cap:
                        kodi_log(f'BridgeSync: {label_t} →{_TARGET_NAMES[target]}: {len(items)} missed while it was out, sending {cap} this cycle', 1)
                        items = dict(sorted(items.items())[:cap])
                    budget[target] = cap - len(items)
                if not items:
                    continue
                try:
                    succeeded, given_up = push_fn(items, source, target)
                except Exception as exc:
                    succeeded, given_up = set(), set()
                    kodi_log(f'BridgeSync: {label_t} →{_TARGET_NAMES[target]} catch-up push error: {exc}', 2)
                if target == 'trakt':
                    sent = set(succeeded) | set(given_up)
                    self._trakt_awaiting_add(f'{domain}|trakt|{media}', sent)
                    awaiting |= sent
                if succeeded:
                    current[target].update({k: items[k] for k in succeeded})
                    snapshots.setdefault(label_t, {}).update({k: items[k] for k in succeeded})
                    self._pushed[target] = self._pushed.get(target, 0) + len(succeeded)
                    kodi_log(f'BridgeSync →{_TARGET_NAMES[target]}: {len(succeeded)} {media} ({domain}), missed while it was out of the sync', 1)
                if given_up:
                    kodi_log(f'BridgeSync: {label_t} →{_TARGET_NAMES[target]}: {len(given_up)} not accepted, giving up on them', 1)
                for key in set(succeeded) | set(given_up):
                    entry.pop(key, None)       # a given-up key is NOT put in the target's snapshot: it would look like a removal there
            if not entry:
                queue.pop(label_t, None)

    # ── Removals made outside Kodi ─────────────────────────────────────────────
    # An item that was reconciled (it is in a service's snapshot) and is no longer listed by that service was removed
    # there: the user unwatched it or took it off a list on the website / in an app. Kodi's own removals are mirrored at once
    # by cross_sync. Because removing is destructive (Trakt history/remove deletes every play of the item) it is cautious:
    #  - an item must be missing on two runs in a row before anything is removed (a bad read does not repeat itself)
    #  - more than a few items vanishing from one service at once is treated as an incomplete read and removes nothing
    #  - an item played again on another service since the last run is kept and sent back to the service that lost it
    #  - what the bridge removed is written to bridge_removals.jsonl first, with the dates it held
    #  - the cross_sync_removals setting: 0 off, 1 log only, 2 watchlist + collection, 3 also watched history

    @staticmethod
    def _iso_utc(epoch):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')

    @staticmethod
    def _utc_now():
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')

    @staticmethod
    def _removal_mode():
        try:
            mode = int(get_setting('cross_sync_removals', 'int'))
        except Exception:
            return 2
        return mode if 0 <= mode <= 3 else 2

    @staticmethod
    def _removal_active(domain, mode):
        return mode == 1 or mode == 3 or (mode == 2 and domain in ('watchlist', 'collection'))

    def _newer_than(self, media, key, since):
        """True when an item was played (on any service that still lists it) after `since`, a UTC ISO time (the end of
        the last run: what the bridge itself pushed during that run carries stamps up to then). The margin covers the
        difference between this computer's clock and the services'."""
        from datetime import datetime
        newest = max(self._stamps.get((media, key), ''), self._tstamps.get((media, key), ''), self._anime_stamps.get(key, '') if media == 'episode' else '')
        if not since or not newest:
            return False
        try:
            fmt = '%Y-%m-%dT%H:%M:%S'
            return (datetime.strptime(newest[:19], fmt) - datetime.strptime(since[:19], fmt)).total_seconds() > _REMOVE_CLOCK_MARGIN
        except ValueError:
            return False

    def _removal_ok(self, domain, media):
        """removal_ok(source, key): False for a change that only looks like a removal on that service."""
        def ok(source, key):
            if source == 'wetrakr' and domain == 'watchlist':
                return self._wetrakr_status(key) is None       # moved to watched / watching / on hold ... is not a removal
            if source != 'simkl':
                return True
            views = self._simkl or {}
            if domain == 'watched' and media == 'movie':
                entry = (views.get('movies') or {}).get(key)      # unwatched on Simkl = back on plan-to-watch (or gone)
                return entry is None or entry[0] == 'plantowatch'
            if domain == 'watched' and key in self._anime_keys:
                return False           # a mapped anime episode: its mapping can change with the index, so it never counts as a removal
            if domain == 'watched' and key in self._anime_gone:
                return self._anime_unwatched(key)      # left the mapping: an un-watch only if Simkl itself dropped (entry, number)
            if domain == 'watched':
                # only an episode Simkl really recorded (the rest are synthetic rows); one already waiting for its second run
                # no longer shows in the views saved after the first
                return key in self._simkl_prev_real or key in (self._meta.get('removals') or {}).get('watched|simkl|episode', {})
            if domain == 'watchlist':
                return self._simkl_status(key) is None           # a move to completed / dropped / hold is not a removal
            return True
        return ok

    def _journal(self, domain, media, source, target, items):
        """Write what is about to be removed to bridge_removals.jsonl (service, item, the dates it held) before the call."""
        try:
            import xbmcvfs
            path = xbmcvfs.translatePath(ADDONDATA + _JOURNAL_FILE)
            with open(path, 'a', encoding='utf-8') as f:
                for key, tmdb_id in sorted(items.items()):
                    f.write(json.dumps({
                        'at': self._utc_now(), 'from': source, 'removed_on': target, 'domain': domain, 'media': media,
                        'key': key, 'tmdb': tmdb_id, 'mdblist_simkl_watched_at': self._stamps.get((media, key)),
                        'trakt_watched_at': self._tstamps.get((media, key))}) + '\n')
        except Exception as exc:
            kodi_log(f'BridgeSync: removal journal not written: {exc}', 2)

    def _journal_trim(self):
        try:
            import xbmcvfs
            path = xbmcvfs.translatePath(ADDONDATA + _JOURNAL_FILE)
            with open(path, encoding='utf-8') as f:
                lines = f.readlines()
            if len(lines) > _JOURNAL_LINES:
                with open(path, 'w', encoding='utf-8') as f:
                    f.writelines(lines[-_JOURNAL_LINES:])
        except FileNotFoundError:
            pass
        except Exception as exc:
            kodi_log(f'BridgeSync: removal journal not trimmed: {exc}', 2)

    def _finish_run(self):
        """Remember when this run ended (the baseline for 'played again since') and keep the journal short."""
        if not self._snapshots_ok:
            return
        self._snapshot_data()
        self._meta['last_run_utc'] = self._utc_now()      # the END of the run: its own pushes are not 'played again'
        self._journal_trim()
        self._snapshot_save()

    def _removed_done(self, domain, media, source, target, done, current, snapshots):
        """Bookkeeping for items just removed from `target`: out of this run's view and its snapshot, and noted so a
        service that is slow to stop listing them does not make them look like new additions."""
        import time
        label = f'{domain}|{target}|{media}'
        recent = self._meta.setdefault('removed_recent', {}).setdefault(label, {})
        for key in done:
            current[target].pop(key, None)
            snapshots.get(label, {}).pop(key, None)
            recent[key] = time.time()
        if done:
            self._removed[target] = self._removed.get(target, 0) + len(done)
            kodi_log(f'BridgeSync ✕{_TARGET_NAMES[target]}: removed {len(done)} {media} ({domain})'
                     f'{" - removed on " + _TARGET_NAMES[source] if source in _TARGET_NAMES else ""}', 1)

    def _removals(self, domain, media, current, snapshots, remove_fn, removal_ok, trakt_waiting=frozenset()):
        """Carry removals over to the other services. Returns {source: keys that must stay in its snapshot until the
        removal is resolved}. Runs before the additions so a removed item is not mistaken for 'old news' or 'new'."""
        import time
        mode = self._removal_mode()
        if not self._removal_active(domain, mode):
            return {}
        pending = self._meta.setdefault('removals', {})
        recent = self._meta.setdefault('removed_recent', {})
        prev_run = self._meta.get('last_run_utc') or self._run_started
        held = {}
        if mode >= 2:
            # an item the bridge removed that its service still lists: ask again, for a few days
            for target, cur in current.items():
                label = f'{domain}|{target}|{media}'
                rec = recent.get(label)
                if not rec or cur is None:
                    continue
                for key, at in list(rec.items()):
                    if key not in cur or time.time() - at > _REMOVE_RECENT_DAYS * 86400:
                        rec.pop(key, None)
                again = {k: cur[k] for k in rec if k in cur}
                if domain == 'watched':
                    for key in [k for k in again if self._newer_than(media, k, self._iso_utc(rec[k]))]:
                        rec.pop(key, None)             # played again after the bridge removed it: a new watch, not a removal still showing
                        snapshots.get(label, {}).pop(key, None)   # new on this service again: the additions pass carries it to the others
                        del again[key]
                        kodi_log(f'BridgeSync: {label}: {key} was played again after it was removed - kept', 1)
                if again:
                    self._journal(domain, media, 'retry', target, again)
                    try:
                        done = remove_fn(again, 'retry', target)
                    except Exception as exc:
                        done = set()
                        kodi_log(f'BridgeSync: {label} removal retry error: {exc}', 2)
                    self._removed_done(domain, media, 'retry', target, done, current, snapshots)
        for source, cur in current.items():
            if cur is None:
                continue                       # unreadable: nothing is concluded from it
            label = f'{domain}|{source}|{media}'
            snap = snapshots.get(label, {})
            pend = pending.setdefault(label, {})
            seen = self._meta.setdefault('removals_seen', {}).setdefault(label, {})   # when each key was first found missing
            gone = {k for k in snap if k not in cur}
            if source == 'trakt' and trakt_waiting:
                gone = {k for k in gone if k not in trakt_waiting}    # sent to Trakt, not listed yet: not a removal
            if removal_ok:
                gone = {k for k in gone if removal_ok(source, k)}
            for key in [k for k in pend if k not in gone]:
                del pend[key]                  # listed again, or only a status move: not a removal
            for key in [k for k in seen if k not in pend and k not in gone]:
                del seen[key]
            if not gone:
                continue
            cap = max(_REMOVE_MASS_CAP_EPISODES if media == 'episode' else _REMOVE_MASS_CAP, len(snap) // 20)
            waiting = (self._meta.get('simkl_remove_held') or {}) if domain == 'watched' and media == 'episode' else {}
            if source == 'simkl' and domain == 'watched' and media == 'episode':
                waiting = {**waiting, **(self._meta.get('simkl_unwatch_held') or {})}
            if len([k for k in gone if k not in waiting]) > cap:   # held for Simkl's check: counted when they first went
                kodi_log(f'BridgeSync: {label}: {len(gone)} items vanished at once - looks like an incomplete read, '
                         f'nothing removed from the other services for it', 2)
                pend.clear()
                continue
            # missing on a second run AND for long enough (a pending entry from before this check has no first-seen time: old enough)
            confirmed = {k for k in gone if k in pend and time.time() - seen.get(k, 0) >= _REMOVE_CONFIRM_SECONDS}
            for key in gone:
                pend.setdefault(key, prev_run)
                seen.setdefault(key, time.time())
            held[source] = set(gone)
            if not confirmed:
                kodi_log(f'BridgeSync: {label}: {len(gone)} item(s) no longer listed - removed elsewhere only if still missing next run', 1)
                continue
            if mode == 1:
                kodi_log(f'BridgeSync (log only): {label}: would remove {len(confirmed)} from the other services: '
                         f'{", ".join(sorted(confirmed)[:10])}', 1)
                continue
            had = {k: [s for s in current if k in (snapshots.get(f'{domain}|{s}|{media}') or {})] for k in confirmed}      # who held it, before the removal empties their snapshots
            unresolved = self._apply_removals(domain, media, source, confirmed, pend, current, snapshots, remove_fn)
            self._removal_gap_record(domain, media, current, snapshots, source, {k: pend.get(k) for k in confirmed - unresolved - self._simkl_blocked}, had)
            for key in confirmed - unresolved:
                pend.pop(key, None)
                held[source].discard(key)
        return held

    # ── Removal queue (7.5.17): what was removed elsewhere while a service was out of the bridge ──
    # The mirror image of the gap queue. A removal confirmed while a service is switched off or not signed in cannot reach it (it is not in
    # `current`), and that service's own snapshot still holds the item and still lists it, so on its return nothing would ever say it must go.
    # Each confirmed removal is therefore queued for every absent service whose snapshot holds the key (meta 'removal_queue'; the snapshot
    # test is also the 'has taken part' test), with where it was removed ('from') and the pending time the played-again check needs ('pend').
    # On the service's return the queue is worked off through _apply_removals (journal, removed_recent, played-again check, Simkl anime path),
    # after re-checking live that it is still a removal: a recorded source present and not listing the key, no other service listing it (it
    # was added again) or unreadable, and capped per run like a mass removal. Nothing expires: an age limit would bring the stale item back.
    # Known limit: an item added to the returning service by the user while it was out, after the removal elsewhere, loses on a watchlist
    # (no reliable timestamp there); a watched item played again on it is kept (_newer_than).

    def _removal_gap_record(self, domain, media, current, snapshots, source, keys, had=None):
        """keys: {key: pending time} of removals just carried out from `source`; had: {key: the services that held it when it was removed}."""
        if not keys:
            return
        queue = None
        for service in _GAP_SERVICES:
            if service in current or not self._gap_service(domain, service, media):
                continue
            held = snapshots.get(f'{domain}|{service}|{media}') or {}
            wanted = [k for k in keys if k in held]
            if not wanted:
                continue
            if queue is None:
                queue = self._meta.setdefault('removal_queue', {})
            entry = queue.setdefault(f'{domain}|{service}|{media}', {})
            for key in wanted:
                rec = entry.get(key)
                if not isinstance(rec, dict):
                    rec = entry[key] = {'at': self._utc_now(), 'from': [], 'pend': keys[key], 'had': list((had or {}).get(key) or [])}
                if source not in rec['from']:
                    rec['from'].append(source)

    def _added_after(self, service, key, since):
        """True when `service` put `key` on its watchlist clearly (more than the clock margin) after `since`; False when it cannot tell."""
        from datetime import datetime
        at = ((self._simkl or {}).get('added') or {}).get(key) if service == 'simkl' else (self._list_stamps.get(service) or {}).get(key)
        if not at or not since:
            return False
        try:
            fmt = '%Y-%m-%dT%H:%M:%S'
            return (datetime.strptime(str(at)[:19], fmt) - datetime.strptime(str(since)[:19], fmt)).total_seconds() > _REMOVE_CLOCK_MARGIN
        except ValueError:
            return False

    def _days_since(self, iso):
        from datetime import datetime
        fmt = '%Y-%m-%dT%H:%M:%S'
        try:
            return (datetime.strptime(self._utc_now()[:19], fmt) - datetime.strptime(str(iso)[:19], fmt)).total_seconds() / 86400
        except ValueError:
            return 0

    def _absent_days(self, service):
        """How long `service` has been out of the sync: since it was last seen taking part (the clock starts when it is first seen missing)."""
        stamp = (self._meta.get('last_present') or {}).get(service)
        return self._days_since(stamp) if stamp else 0

    def _queue_housekeeping(self, current):
        """Each run, first: note which services take part (the clock for 'out of the sync for good', refreshed daily) and drop expired list removals."""
        stamps = self._meta.setdefault('last_present', {})
        now = self._utc_now()
        for service in _GAP_SERVICES:
            old = stamps.get(service)
            if not old or (service in current and self._days_since(old) >= 1):
                stamps[service] = now
        queue = self._meta.get('removal_queue')
        if queue:
            self._removal_queue_expire(queue)

    def _queues_prune(self, domain, media, current):
        """Each run, for this domain/media and whether or not the target service is back: drop queued entries the evidence has settled, so what is left is real pending
        work. Gap queue: every origin was read and none lists the title any more. Removal queue: a service lists it again (not just the bridge's own removal showing)."""
        if domain == 'watched' and media == 'episode':
            if current.get('simkl') is not None:       # a kept key Simkl no longer lists needs no booking
                for key in [k for k in self._meta.get('simkl_kept') or () if k not in current['simkl']]:
                    del self._meta['simkl_kept'][key]
            for name in ('simkl_remove_held', 'simkl_unwatch_held'):
                waiting = self._meta.get(name)
                if waiting:                                # a held removal no longer pending or queued (listed again) is forgotten
                    live = set()
                    for queue_name in ('removals', 'removal_queue'):
                        for label, entry in (self._meta.get(queue_name) or {}).items():
                            if label.startswith('watched|') and label.endswith('|episode'):
                                live.update(entry)
                    for key in [k for k in waiting if k not in live]:
                        del waiting[key]
        recent = self._meta.get('removed_recent') or {}
        for name in ('gap_queue', 'removal_queue'):
            queue = self._meta.get(name)
            for label in list(queue or ()):
                parts = label.split('|')
                if len(parts) != 3 or parts[0] != domain or parts[2] != media:
                    continue
                target, entry = parts[1], queue[label]
                for key in list(entry):
                    rec = entry[key]
                    if name == 'gap_queue':
                        origins = [s for s in (rec.get('from') or ()) if s != target] if isinstance(rec, dict) else []
                        if origins and all(current.get(s) is not None for s in origins) and not any(key in current[s] for s in origins):
                            entry.pop(key)
                    elif any(s != target and current[s] is not None and key in current[s] and key not in (recent.get(f'{domain}|{s}|{media}') or {})
                             and not self._simkl_keeps(domain, media, s, key) for s in current):
                        entry.pop(key)
                if not entry:
                    queue.pop(label)

    def _removal_queue_expire(self, queue):
        """Drop queued removals on lists older than _REMOVAL_QUEUE_DAYS, whether or not the service is back (the watched domain has the play date instead)."""
        from datetime import datetime
        fmt = '%Y-%m-%dT%H:%M:%S'
        now = datetime.strptime(self._utc_now()[:19], fmt)
        for label in list(queue):
            if label.split('|')[0] == 'watched':
                continue
            entry = queue[label]
            old = []
            for key, rec in entry.items():
                try:
                    if (now - datetime.strptime(str(rec.get('at'))[:19], fmt)).days >= _REMOVAL_QUEUE_DAYS:
                        old.append(key)
                except (ValueError, AttributeError):
                    continue
            for key in old:
                entry.pop(key)
            if old:
                kodi_log(f'BridgeSync: {label}: {len(old)} queued removal(s) older than {_REMOVAL_QUEUE_DAYS} days dropped, not carried out', 1)
            if not entry:
                queue.pop(label)

    def _removal_queue_readds(self, domain, media, current, snapshots):
        """Watchlist: a queued removal whose title the returning service put on its watchlist AFTER the removal (taken off there and added again while it was away,
        its own date says so) is a new addition there, not a removal: the record is dropped, the title is new to the additions pass (it leaves that service's snapshot)
        and the bridge's own earlier removal of it (removed_recent) is forgotten, so nothing takes it off the other services again."""
        queue = self._meta.get('removal_queue')
        if not queue or domain != 'watchlist':
            return
        recent = self._meta.get('removed_recent') or {}
        for target in list(current):
            label_t = f'{domain}|{target}|{media}'
            entry, cur_t = queue.get(label_t), current[target]
            if not entry or cur_t is None:
                continue
            snap_t = snapshots.get(label_t) or {}
            for key in sorted(entry):
                rec = entry[key]
                if key in cur_t and key in snap_t and isinstance(rec, dict) and self._added_after(target, key, rec.get('pend') or rec.get('at')):
                    entry.pop(key)
                    snap_t.pop(key, None)
                    for lab, lagging in recent.items():
                        if lab.startswith(f'{domain}|') and lab.endswith(f'|{media}'):
                            lagging.pop(key, None)
                    kodi_log(f'BridgeSync: {label_t}: {key} was added on {_TARGET_NAMES[target]} after it was removed elsewhere - kept', 1)
            if not entry:
                queue.pop(label_t, None)

    def _removal_gap_apply(self, domain, media, current, snapshots, remove_fn):
        queue = self._meta.get('removal_queue')
        if not queue:
            return
        mode = self._removal_mode()
        if mode < 2 or not self._removal_active(domain, mode):
            return                             # removals are off or log-only for this domain: the queue stays as it is
        recent = self._meta.get('removed_recent') or {}
        for target in list(current):
            label_t = f'{domain}|{target}|{media}'
            entry = queue.get(label_t)
            cur_t = current[target]
            if entry is None or cur_t is None:
                continue                       # nothing queued, or the service cannot be read now
            snap_t = snapshots.get(label_t, {})
            others = [s for s in current if s != target]
            ready = {}                         # source -> {key: tmdb id}
            for key in sorted(entry):
                rec = entry[key]
                if key not in cur_t or key not in snap_t or not isinstance(rec, dict):
                    entry.pop(key)             # already gone there, or it is not something the bridge accounted for there: not ours to remove
                    continue
                if any(current[s] is None for s in others):
                    continue                   # cannot tell whether it was added again: wait
                listing = [s for s in others if key in current[s] and not self._simkl_keeps(domain, media, s, key)]
                if any(key not in (recent.get(f'{domain}|{s}|{media}') or {}) for s in listing):
                    entry.pop(key)             # listed by another service again (added back / sent there): no longer a removal
                    continue
                if listing:
                    continue                   # a service the bridge removed it from still lists it (slow to drop it): wait
                sources = [s for s in (rec.get('from') or ()) if s in current and s != target]
                if not sources and rec.get('from') and all(self._absent_days(s) >= _VOUCH_DAYS for s in rec['from']):
                    # every service it was removed on has been out of the sync for good (not just switched off for a while): a service that held the
                    # title when it was removed, and no longer lists it, vouches for the removal
                    sources = [s for s in (rec.get('had') or ()) if s in current and s != target]
                if media == 'episode' and domain == 'watched':
                    sources.sort(key=lambda s: s == 'simkl')      # episodes only, Simkl last: its numbering has to pass a check
                if not sources:
                    continue                   # the service it was removed on is out of the sync now: wait
                ready.setdefault(sources[0], {})[key] = cur_t[key]
            cap = _REMOVE_MASS_CAP_EPISODES if media == 'episode' else _REMOVE_MASS_CAP
            for source, items in ready.items():
                keys = sorted(items)
                if len(keys) > cap:
                    kodi_log(f'BridgeSync: {label_t}: {len(keys)} removals made while it was out of the sync, applying {cap} this cycle', 1)
                    keys = keys[:cap]
                keys = set(keys)
                pend = {k: entry[k].get('pend') for k in keys}
                unresolved = self._apply_removals(domain, media, source, keys, pend, {target: cur_t}, snapshots, remove_fn)
                for key in keys - unresolved:
                    entry.pop(key, None)       # done, or played again on the returning service (kept; the additions pass sends it back)
            if not entry:
                queue.pop(label_t, None)

    def _apply_removals(self, domain, media, source, confirmed, pend, current, snapshots, remove_fn):
        """Remove the confirmed items from every other service that still lists them. Returns the keys not finished.
        self._simkl_blocked = those finished without being removed anywhere (see _gate_simkl_unwatches)."""
        unresolved = set()
        self._simkl_blocked = set()
        if source == 'simkl' and media == 'episode' and domain == 'watched':
            self._simkl_blocked, held = self._gate_simkl_unwatches(confirmed, current, snapshots)
            unresolved.update(held)        # stays pending: removed elsewhere once the numbering can be checked
            confirmed = set(confirmed) - self._simkl_blocked - held
        for target, cur in current.items():
            if target == source:
                continue
            if cur is None:
                # unreadable this run: if it may still hold the item the removal is not finished, so it stays pending
                held_there = snapshots.get(f'{domain}|{target}|{media}') or {}
                unresolved.update(k for k in confirmed if k in held_there)
                continue
            items = {k: cur[k] for k in confirmed if k in cur}
            if domain == 'watched':
                newer = {k for k in items if self._newer_than(media, k, pend.get(k))}
                if newer:
                    kodi_log(f'BridgeSync: {domain}|{target}|{media}: {len(newer)} item(s) played again on {_TARGET_NAMES[target]} '
                             f'since they were removed on {_TARGET_NAMES[source]} - kept, and sent back', 1)
                    snap_target = snapshots.setdefault(f'{domain}|{target}|{media}', {})
                    for key in newer:
                        snap_target.pop(key, None)       # new on the target again: the additions pass sends it back to the source
                        del items[key]
            anime_items = {}
            if target == 'simkl' and media == 'episode':
                # TMDb numbers are the wrong coordinates on Simkl's anime entries: those go by (entry, number) or not at all
                anime_items = self._anime_removable({k: v for k, v in items.items() if k in self._anime_keys})
                items = {k: v for k, v in items.items() if k not in self._anime_keys}
            if target == 'simkl' and media == 'episode' and domain == 'watched' and items:
                items, held = self._gate_simkl_removals(items, cur, snapshots.setdefault('watched|simkl|episode', {}))
                unresolved.update(held)   # stays pending: removed once the numbering can be checked
            if not items and not anime_items:
                continue
            self._journal(domain, media, source, target, {**items, **anime_items})
            done = set()
            try:
                if items:
                    done |= set(remove_fn(items, source, target) or ())
                if anime_items:
                    done |= self._remove_anime_simkl(anime_items)
            except Exception as exc:
                kodi_log(f'BridgeSync: {domain}|{target}|{media} removal error: {exc}', 2)
            self._removed_done(domain, media, source, target, done, current, snapshots)
            kept = self._wt_kept if (target, domain) == ('wetrakr', 'collection') else set()
            unresolved.update((set(items) | set(anime_items)) - done - kept)    # a kept copy stays listed and accounted for: finished
        return unresolved

    def _anime_removable(self, items):
        """The anime episodes among `items` ({key: tmdb id}) that may be un-watched on Simkl: cross_sync_anime_removals = 2 and the key's
        entry coordinates are known from this run's reading of Simkl. Log only (1) names them and removes nothing."""
        mode = self._anime_removal_mode()
        if not items or not mode:
            return {}
        known = {k: v for k, v in items.items() if k in self._anime_coords}
        if mode == 1:
            for key in sorted(known):
                if ('to', key) not in self._anime_removal_logged:
                    self._anime_removal_logged.add(('to', key))
                    al, n = self._anime_coords[key]
                    kodi_log(f'BridgeSync (anime removals, log only): {key} was un-watched elsewhere - would be removed from Simkl (AniList {al} episode {n})', 1)
            return {}
        return known

    def _remove_anime_simkl(self, items):
        """Un-watch anime episodes {key: tmdb id} on Simkl by (AniList entry, number): {'anime': [{'ids': ..., 'episodes': [{'number': n}]}]} to
        /sync/history/remove (verified live 2026-11-05: deleted.episodes counts them and Simkl moves the entry's status itself). Returns the keys done."""
        try:
            from tmdbhelper.lib.api.anilist import fribb
        except Exception:
            return set()
        groups = {}
        for key in items:
            al, n = self._anime_coords[key]
            groups.setdefault(al, {'tmdb': items[key], 'eps': []})['eps'].append((n, key))
        entries = []
        for al, g in sorted(groups.items()):
            ids = {'anilist': al}
            mal = next((r.get('m') for r in fribb.score_records(g['tmdb']) or [] if r.get('a') == al and r.get('m')), None)
            if mal:
                ids['mal'] = mal
            entries.append({'ids': ids, 'episodes': [{'number': n} for n, _ in sorted(g['eps'])]})
        done = set()
        for j in range(0, len(entries), 25):
            part = entries[j:j + 25]
            ok, data = self._post('simkl', ('sync', 'history', 'remove'), {'anime': part})
            if not ok or any((data.get('not_found') or {}).values()):
                kodi_log(f'BridgeSync: Simkl anime removal not confirmed - {self._result_text(data) if ok else "no answer"}', 1)
                continue
            for entry in part:
                for episode in entry['episodes']:
                    key = next(k for n, k in groups[entry['ids']['anilist']]['eps'] if n == episode['number'])
                    done.add(key)
                    ((self._simkl or {}).get('anime_eps', {}).get(str(entry['ids']['anilist'])) or {}).get('eps', {}).pop(str(episode['number']), None)
        if done:
            self._anime_dirty = True
            kodi_log(f'BridgeSync ✕Simkl: removed {len(done)} anime episode(s)', 1)
        return done

    def _forget_in_views(self, domain, media, keys):
        """After removing from Simkl: keep the views saved for the next cycle in step with it."""
        views = self._simkl
        if views is None:
            return
        for key in keys:
            if domain == 'watched' and media == 'movie':
                views['movies'].pop(key, None)
            elif domain == 'watched':
                views['ep_yes'].pop(key, None)
                views['ep_real'].pop(key, None)
            else:
                kind, _, tmdb_id = key.partition(':')
                views['movies' if kind == 'movie' else 'shows'].pop(tmdb_id, None)

    def _remove_watched(self, media, items, source, target):
        """Unwatch items {key: tmdb_id} on `target`. Returns the keys that went through."""
        if target == 'wetrakr':
            return self._remove_wetrakr(media, items)
        succeeded = set()
        path = ('sync', 'watched', 'remove') if target == 'mdblist' else ('sync', 'history', 'remove')
        for chunk in self._chunks(items, _MDB_CHUNK if target == 'mdblist' else _BATCH_SIZE):
            if media == 'movie':
                payload = {'movies': [{'ids': {'tmdb': int(v)}} for _, v in sorted(chunk.items())]}
            else:
                shows = []
                for key, show_id in sorted(chunk.items()):
                    _, season, number = key.split(':')
                    shows.append({'ids': {'tmdb': int(show_id)}, 'seasons': [{'number': int(season), 'episodes': [{'number': int(number)}]}]})
                payload = {'shows': shows}
            ok, _data = self._post(target, path, payload)
            if not ok:
                break
            succeeded |= set(chunk)
            if target == 'simkl':
                self._forget_in_views('watched', media, chunk)
        if succeeded and target == 'trakt':
            self._trakt_force_refresh()
        return succeeded

    def _remove_set(self, domain, items, source, target):
        """Take items {'movie:<id>' / 'show:<id>': tmdb_id} off the watchlist / collection of `target`."""
        if target == 'wetrakr':
            return self._remove_wetrakr_set(domain, items)
        succeeded = set()
        if target == 'simkl':
            path = ('sync', 'history', 'remove')       # clears every status, so only an item still on plan-to-watch goes
            done_already = {k for k in items if self._simkl_status(k) != 'plantowatch'}
            succeeded |= done_already
            items = {k: v for k, v in items.items() if k not in done_already}
        elif target == 'trakt':
            path = ('sync', domain, 'remove')
        else:
            path = ('watchlist', 'items', 'remove') if domain == 'watchlist' else ('sync', 'collection', 'remove')
        for chunk in self._chunks(items, _MDB_CHUNK if target == 'mdblist' else _BATCH_SIZE):
            payload = {}
            for key, tmdb_id in sorted(chunk.items()):
                kind = 'movies' if key.startswith('movie:') else 'shows'
                entry = {'tmdb': int(tmdb_id)} if (target == 'mdblist' and domain == 'watchlist') else {'ids': {'tmdb': int(tmdb_id)}}
                payload.setdefault(kind, []).append(entry)
            ok, _data = self._post(target, path, payload)
            if not ok:
                break
            succeeded |= set(chunk)
            if target == 'simkl':
                self._forget_in_views(domain, 'movie_or_show', chunk)
        if succeeded and target == 'trakt':
            self._trakt_force_refresh()
        return succeeded

    def _trakt_force_refresh(self):
        """Make the next Trakt read re-fetch: a removal may not move the activity stamps the sync is gated on."""
        try:
            self.trakt.trakt_syncdata.reset_lastactivities()
        except Exception as exc:
            kodi_log(f'BridgeSync: could not reset the Trakt activity stamps: {exc}', 2)

    def _load_trakt_stamps(self, media):
        """Trakt's last_watched_at per watched item, for the 'played again since' check."""
        if media == 'movie':
            rows = self._trakt_rows(('tmdb_id', 'last_watched_at'), 'movie', 'last_watched_at IS NOT NULL')
            keyed = ((f'{int(r[0])}', r[1]) for r in rows or () if r and r[0])
        else:
            rows = self._trakt_rows(
                ('tmdb_id', 'season_number', 'episode_number', 'last_watched_at'), 'episode',
                'last_watched_at IS NOT NULL AND season_number > 0 AND episode_number IS NOT NULL')
            keyed = ((f'{r[0]}:{r[1]}:{r[2]}', r[3]) for r in rows or () if r and r[0])
        for key, stamp in keyed:
            if isinstance(stamp, str) and _STAMP_RE.match(stamp) and stamp > self._tstamps.get((media, key), ''):
                self._tstamps[(media, key)] = stamp

    # ── Push helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _chunks(items, size):
        keys = sorted(items)
        for i in range(0, len(keys), size):
            yield {k: items[k] for k in keys[i:i + size]}

    def _note_stamp(self, media, item_key, stamp, service=None):
        """Remember the newest valid ISO last_watched_at seen for an item (used as watched_at on Trakt / Simkl)."""
        if not item_key or not isinstance(stamp, str) or not _STAMP_RE.match(stamp):
            return
        key = (media, str(item_key))
        if service and stamp > self._by_service.get((service,) + key, ''):
            self._by_service[(service,) + key] = stamp
        if stamp > self._stamps.get(key, ''):
            self._stamps[key] = stamp

    def _source_date(self, source, media, key):
        """The date the service being copied from holds (a mapped anime episode: Simkl's own), or '' when it holds none."""
        if media == 'episode' and self._anime_stamps.get(key):
            return self._anime_stamps[key]
        if source == 'trakt':
            return self._tstamps.get((media, key), '')
        return self._by_service.get((source, media, key), '')

    def _push_date(self, source, media, key):
        """Date a copy carries: the source's own. Only when it holds none, the newest any other service holds - never one that merely
        records when a copy arrived somewhere ahead of the real one."""
        return self._source_date(source, media, key) or self._stamps.get((media, key))

    def _watched_since(self, media, item_key, paused_at):
        """True when MDBList or Simkl recorded the item as watched at or after `paused_at` (when Trakt saved its resume point).
        An item watched before that, or only on Trakt, is a play in progress and keeps its resume point."""
        stamp = self._stamps.get((media, item_key))
        if not stamp or not isinstance(paused_at, str) or not _STAMP_RE.match(paused_at):
            return False
        return stamp[:19] >= paused_at[:19]

    @staticmethod
    def _count(data, media):
        """Total of added + existing + updated for one media key in a push response."""
        total = 0
        for part in ('added', 'existing', 'updated'):
            value = ((data or {}).get(part) or {})
            value = value.get(media, 0) if isinstance(value, dict) else 0
            total += len(value) if isinstance(value, list) else (value if isinstance(value, int) else 0)
        return total

    @staticmethod
    def _not_found_ids(data, media):
        """Itemized tmdb ids from response['not_found'][media] ([{'ids': {'tmdb': N}}]); empty when
        the service only reports a count."""
        entries = ((data or {}).get('not_found') or {}).get(media)
        found = set()
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    tmdb_id = (entry.get('ids') or {}).get('tmdb') or entry.get('tmdb')
                    if tmdb_id:
                        found.add(str(tmdb_id))
        return found

    @staticmethod
    def _not_found_episode_keys(data):
        """'show:season:number' of every episode in response['not_found']['episodes'] - MDBList itemizes unmatched
        episodes as [{'season': S, 'number': N, 'show': {'ids': {'tmdb': T}}}] (live-captured 2026-10-14). Empty when
        the service only reports a count, in which case nothing can be attributed and the chunk is retried."""
        keys = set()
        entries = ((data or {}).get('not_found') or {}).get('episodes')
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    tmdb_id = ((entry.get('show') or {}).get('ids') or {}).get('tmdb')
                    season, number = entry.get('season'), entry.get('number')
                    if tmdb_id and season is not None and number is not None:
                        keys.add(f'{tmdb_id}:{season}:{number}')
        return keys

    @staticmethod
    def _not_found_count(data, media):
        value = ((data or {}).get('not_found') or {}).get(media)
        return len(value) if isinstance(value, list) else (value if isinstance(value, int) else 0)

    @staticmethod
    def _result_text(data):
        """Counts only, from a push response ({'added': {'movies': 3}, 'not_found': {'movies': [..]}})."""
        parts = []
        for key in ('added', 'existing', 'updated', 'not_found'):
            value = data.get(key) if isinstance(data, dict) else None
            if isinstance(value, dict):
                counts = {k: (len(v) if isinstance(v, list) else v) for k, v in value.items()
                          if isinstance(v, (int, list)) and v}
                if counts:
                    parts.append(f'{key}={counts}')
        return ' '.join(parts)

    def _wait(self, seconds):
        try:
            import xbmc
            xbmc.Monitor().waitForAbort(seconds)
        except Exception:
            import time
            time.sleep(seconds)

    def _simkl_pace(self):
        """Simkl locks writes per user for 20 s (a second write inside it is answered 400 rate_limit)."""
        import time
        if self._simkl_last_write:
            remaining = _SIMKL_LOCK_SECONDS - (time.monotonic() - self._simkl_last_write)
            if remaining > 0:
                self._wait(remaining)

    def _post(self, target, path, payload):
        """POST payload to a service. Returns (ok, data); ok False = no usable answer (an HTTP error,
        nothing assumed applied)."""
        if target == 'simkl':
            self._simkl_pace()
        try:
            if target == 'trakt':
                resp = self.trakt.post_response(*path, postdata=payload)
            elif target == 'mdblist':
                resp = self.mdb.get_api_request(
                    self.mdb.get_request_url(*path), postdata=payload, method='json', headers=self.mdb.auth_headers)
            else:
                url = f'{_SIMKL_BASE}/{"/".join(path)}?client_id={self.simkl.client_id}'
                resp = self.simkl.get_api_request(url, postdata=payload, method='json', headers=self.simkl.auth_headers)
        except Exception as exc:
            kodi_log(f'BridgeSync: {_TARGET_NAMES[target]} {"/".join(path)} error: {exc}', 2)
            return False, {}
        if target == 'simkl':
            import time
            self._simkl_last_write = time.monotonic()
        if not self._resp_ok(resp):
            kodi_log(f'BridgeSync: {_TARGET_NAMES[target]} {"/".join(path)} push failed '
                     f'(HTTP {getattr(resp, "status_code", "no answer")}) - retry next cycle', 2)
            return False, {}
        try:
            data = resp.json()
        except Exception:
            data = {}
        data = data if isinstance(data, dict) else {}
        text = self._result_text(data)
        if text:
            kodi_log(f'BridgeSync: {_TARGET_NAMES[target]} {"/".join(path)} -> {text}', 1)
        return True, data

    def _push_watched_movies(self, items, source, target):
        """items {tmdb_id_str: tmdb_id}. Trakt history logs a new play per item, so a chunk whose result
        does not add up is given up on rather than retried; MDBList / Simkl are set-membership
        endpoints and are simply retried."""
        if target == 'wetrakr':
            self._date_source = source
            return self._push_wetrakr('movie', items)
        succeeded, given_up = set(), set()
        for chunk in self._chunks(items, _MDB_CHUNK if target == 'mdblist' else _BATCH_SIZE):
            entries = []
            for key, tmdb_id in sorted(chunk.items()):
                entry = {'ids': {'tmdb': int(tmdb_id)}}
                stamp = self._push_date(source, 'movie', key)
                if stamp:
                    entry['watched_at'] = stamp
                entries.append(entry)
            ok, data = self._post(target, ('sync', 'watched') if target == 'mdblist' else ('sync', 'history'),
                                  {'movies': entries})
            if not ok:
                break
            rejected = self._not_found_ids(data, 'movies')
            good = {k for k, v in chunk.items() if str(v) not in rejected}
            given_up |= set(chunk) - good
            if self._count(data, 'movies') >= len(good):
                succeeded |= good
            elif target in ('trakt', 'simkl'):
                kodi_log(f'BridgeSync: {_TARGET_NAMES[target]} history confirmed fewer movies than sent ({len(good)}) - not retrying', 2)
                given_up |= good
        return succeeded, given_up

    def _skip_episodes(self, missing, source, target):
        """Episodes that must not go to `target`: anime shows to Simkl (different numbering), and from
        Simkl to Trakt anything the user did not actually record there (synthetic rows, approximate dates) - except the anime
        episodes mapped to TMDb numbers (_anime_keys), which are real records by construction."""
        views = self._simkl or {}
        if target == 'simkl':
            anime = set(views.get('anime') or [])
            shows = views.get('shows') or {}
            guard = self._anime_simkl_mode() != 0     # the anime pass sends these by entry; TMDb numbers can land in the wrong cour

            def is_anime(v):
                sv = str(v)
                if sv in anime:
                    return True
                return guard and sv not in shows and self._anime_known(v)    # new to Simkl: not an ordinary Simkl show
            return {k for k, v in missing.items()
                    if is_anime(v) or shows.get(str(v), [None])[0] in _SIMKL_DROPPED}
        if source == 'simkl' and target in ('trakt', 'wetrakr'):
            return {k for k in missing if k not in (views.get('ep_real') or {}) and k not in self._anime_keys}
        return set()

    def _gate_simkl_removals(self, items, listed, snapshot):
        """(items that may be removed from Simkl, keys held for a later run) for episode removals the sync is about to send there, checked
        like the copies. Where Simkl numbers the season differently the same numbers may be another episode, so nothing is removed: the key
        counts as dealt with, stays in Simkl's snapshot (so it is never copied back as new) and is booked in meta 'simkl_kept', so that
        Simkl still listing it does not pass for the title having been added again."""
        import time
        drop, hold = self._gate_simkl_episodes(items, 'removal', 'simkl', removal=True)
        waiting = self._meta.setdefault('simkl_remove_held', {})
        kept = self._meta.setdefault('simkl_kept', {})
        now = time.time()
        for key in items:
            if key in hold:
                waiting.setdefault(key, now)
            else:
                waiting.pop(key, None)
            if key in drop:
                kept[key] = now
                snapshot[key] = listed[key]
            elif key not in hold:
                kept.pop(key, None)
        for label, entry in (self._meta.get('gap_queue') or {}).items():   # a queued copy must not take a kept key from Simkl
            if label.startswith('watched|') and label.endswith('|episode'):
                for key in [k for k in drop if k in entry]:
                    rec = entry[key]
                    if isinstance(rec, dict):
                        rec['from'] = [s for s in (rec.get('from') or ()) if s != 'simkl']
                    if not isinstance(rec, dict) or not rec['from']:
                        del entry[key]
        return {k: v for k, v in items.items() if k not in drop and k not in hold}, hold

    def _gate_simkl_unwatches(self, keys, current, snapshots):
        """(keys not to be removed anywhere, keys held for a later run) among episodes Simkl no longer lists. Where Simkl numbers the
        season differently, the same numbers elsewhere may be another episode: the other services keep it, and as they have it in their
        snapshots it is not copied back. Only episodes another service holds are looked up; mapped anime is already in TMDb numbering."""
        import time
        exempt = set(self._anime_keys) | set(self._anime_gone)
        items = {}
        for target, cur in current.items():
            if target == 'simkl':
                continue
            listed = cur if cur is not None else (snapshots.get(f'watched|{target}|episode') or {})
            items.update({k: listed[k] for k in keys if k in listed and k not in exempt})
        drop, hold = self._gate_simkl_episodes(items, 'simkl', 'other', removal=True)
        waiting = self._meta.setdefault('simkl_unwatch_held', {})
        now = time.time()
        for key in keys:
            if key in hold:
                waiting.setdefault(key, now)
            else:
                waiting.pop(key, None)
        return drop, hold

    def _simkl_keeps(self, domain, media, service, key):
        """True when Simkl lists this episode only because its removal was not carried out there (see _gate_simkl_removals)."""
        return service == 'simkl' and media == 'episode' and domain == 'watched' and key in (self._meta.get('simkl_kept') or {})

    def _gate_simkl_episodes(self, missing, source, target, removal=False):
        """(drop, hold) for episodes about to be copied to Simkl, or from Simkl to another service (and for Simkl's un-watches, see
        _gate_simkl_unwatches): Simkl numbers some shows' seasons differently (7.5.45). Dropped =
        the numbering differs (or Specials, or Simkl does not know the show): dealt with like a skipped episode, never sent there.
        Held = it could not be checked this run (a failed request, or the run's lookup budget is used up): left for a later run."""
        incoming = source == 'simkl' and target != 'simkl'
        if incoming:       # mapped anime episodes are already in TMDb numbering (Fribb): their numbers are not Simkl's
            missing = {k: v for k, v in missing.items() if k not in self._anime_keys}
        if not (incoming or target == 'simkl') or not missing:
            return set(), set()
        shows = {}
        for key, show_id in missing.items():
            shows.setdefault(str(show_id), {}).setdefault(int(key.split(':')[1]), set()).add(key)
        drop, hold = set(), set()
        for show_id in sorted(shows):
            wanted = {season: {int(k.split(':')[2]) for k in keys} for season, keys in shows[show_id].items()}
            states = self._simkl_numbering_states(show_id, wanted)
            for season, keys in shows[show_id].items():
                for key in keys:
                    state = states[season, int(key.split(':')[2])]
                    if state is None:
                        hold.add(key)
                    elif not state:
                        drop.add(key)
        if incoming and (drop or hold):
            what = ('un-watches from Simkl: {0} not removed from the other services' if removal
                    else 'copies from Simkl to ' + _TARGET_NAMES.get(target, target) + ': {0} not sent').format(len(drop))
            kodi_log(f'BridgeSync: episode {what} (Simkl numbers the season differently or does not know the show), '
                     f'{len(hold)} held for a later run (numbering not checked yet)', 1)
        elif removal and (drop or hold):
            kodi_log(f'BridgeSync: episode removals for Simkl: {len(drop)} not removed there (Simkl numbers the season differently or does not know the '
                     f'show - kept on Simkl), {len(hold)} held for a later run (numbering not checked yet)', 1)
        elif drop or hold:
            kodi_log(f'BridgeSync: episodes for Simkl: {len(drop)} not sent (Simkl numbers the season differently or does not know the show), '
                     f'{len(hold)} held for a later run (numbering not checked yet)', 1)
        return drop, hold

    def _simkl_numbering_states(self, show_id, wanted):
        """{(season, number): True / False / None} for wanted {season: {episode numbers}}: is the number the same episode on TMDb and on
        Simkl (simkl_numbering.same_numbers: titles and air dates; before 7.5.48 only the seasons' episode counts were compared, which
        passes a shifted season). None = not checked. One TMDb read per season plus one and two Simkl reads answer a show. A season's
        answer is remembered (two weeks when everything on it is the same, a day when not) and asked again for a number it has not
        seen. A show whose check failed is not asked again for an hour."""
        import time
        self._snapshot_data()
        now = time.time()
        self._meta.pop('simkl_numbering_marks', None)          # the answers of the count test are not to be trusted
        book = self._meta.setdefault('simkl_numbering_eps', {})      # {show:season: [same numbers, when, numbers either side listed]}
        for old in [k for k, v in book.items() if now - v[1] > 14 * 86400]:
            del book[old]
        states, ask = {}, []
        for season, numbers in wanted.items():
            rec = book.get(f'{show_id}:{season}')
            if season <= 0 or (str(show_id), season) in self._simkl_gave_up:
                states.update({(season, n): False for n in numbers})
            elif rec and numbers <= set(rec[2]) and now - rec[1] < (14 * 86400 if set(rec[0]) == set(rec[2]) else 86400):
                states.update({(season, n): n in rec[0] for n in numbers})
            else:
                ask.append(season)
        if not ask:
            return states
        waiting = {(season, n): None for season in ask for n in wanted[season]}
        states.update(waiting)
        failed = book.get(f'{show_id}:failed')
        if (failed and now - failed[1] < 3600) or self._simkl_mark_lookups >= _SIMKL_MARK_LOOKUPS:
            return states
        self._simkl_mark_lookups += 1
        lists = None
        try:
            lists = self._numbering_lists(int(show_id), ask)
        except Exception as exc:
            kodi_log(f'BridgeSync: Simkl numbering check error: {exc}', 2)
        if lists is None:
            # [None, last try, first failure]; a pause of two days or more (the sync was off) starts the count again
            since = failed[2] if failed and now - failed[1] < 2 * 86400 else now
            if now - since < _SIMKL_MARK_GIVE_UP:
                book[f'{show_id}:failed'] = [None, now, since]
                return states
            book.pop(f'{show_id}:failed', None)
            kodi_log(f'BridgeSync: Simkl numbering of show {show_id} could not be checked for 7 days - its episodes waiting for Simkl are not sent', 1)
            states.update(dict.fromkeys(waiting, False))
            self._simkl_gave_up.update((str(show_id), season) for season in ask)
            return states
        book.pop(f'{show_id}:failed', None)
        from tmdbhelper.lib.script.sync.simkl_numbering import same_numbers
        for season in ask:
            ours, theirs = lists[season]
            good = same_numbers(ours, theirs)
            book[f'{show_id}:{season}'] = [sorted(good), now, sorted(set(ours) | set(theirs))]
            states.update({(season, n): n in good for n in wanted[season]})
        return states

    def _numbering_lists(self, tmdb_id, seasons):
        """{season: (TMDb rows, Simkl rows)}, or None when a request failed (simkl_numbering.season_lists)."""
        from tmdbhelper.lib.script.sync.simkl_numbering import season_lists
        from tmdbhelper.lib.api.tmdb.api import TMDb
        tmdb = TMDb(language='en-US')       # the titles are compared with Simkl's, which are English

        def tmdb_get(season):
            return tmdb.get_request_sc('tv', tmdb_id) if season is None else tmdb.get_request_sc('tv', tmdb_id, 'season', season)

        def simkl_get(kind, value):
            path = f'/search/id?tmdb={value}&type=show' if kind == 'search' else f'/tv/episodes/{value}?extended=full'
            ok, data = self._simkl_get_ok(path)
            return data if ok else None
        return season_lists(tmdb_id, seasons, simkl_get, tmdb_get)

    def _skip_movies(self, missing, source, target):
        """A watched mark moves a Simkl movie out of its status: leave a movie the user dropped alone."""
        if target != 'simkl':
            return set()
        movies = (self._simkl or {}).get('movies') or {}
        return {k for k in missing if (movies.get(k) or [None])[0] in _SIMKL_DROPPED}

    def _skip_watchlist(self, missing, source, target):
        """Plan-to-watch adds move an item's status: never overwrite one Simkl or WeTrakr has in another status."""
        if target == 'wetrakr':
            blocked = {k for k in missing if self._wetrakr_status(k) not in (None, 'planning')}
            if blocked:
                kodi_log(f'BridgeSync: {len(blocked)} watchlist item(s) already on another WeTrakr list - left alone', 1)
            return blocked
        if target != 'simkl':
            return set()
        blocked = {k for k in missing if self._simkl_status(k) not in (None, 'plantowatch')}
        if blocked:
            kodi_log(f'BridgeSync: {len(blocked)} watchlist item(s) already in another Simkl status - left alone', 1)
        return blocked

    def _push_watched_episodes(self, items, source, target):
        """items {'show:season:number': show_tmdb_id} -> one shows[] entry per episode (Seren's payload).
        Simkl: anime shows are never pushed TO it (different numbering). From Simkl to Trakt only episodes the user
        actually recorded on Simkl go out (plus the mapped anime episodes) - the rest are synthetic rows with approximate dates."""
        if target == 'wetrakr':
            self._date_source = source
            return self._push_wetrakr('episode', items)
        succeeded, given_up = set(), set()
        for chunk in self._chunks(items, _MDB_CHUNK if target == 'mdblist' else _BATCH_SIZE):
            shows = []
            for key, show_id in sorted(chunk.items()):
                _, season, number = key.split(':')
                episode = {'number': int(number)}
                # MDBList takes the date too (without one it stamps 'now', which then looked like the newest watch)
                stamp = self._push_date(source, 'episode', key)
                if stamp:
                    episode['watched_at'] = stamp
                shows.append({'ids': {'tmdb': int(show_id)}, 'seasons': [{'number': int(season), 'episodes': [episode]}]})
            ok, data = self._post(target, ('sync', 'watched') if target == 'mdblist' else ('sync', 'history'),
                                  {'shows': shows})
            if not ok:
                break
            if target == 'simkl':
                rejected = self._not_found_ids(data, 'shows')
                sent = {k for k, v in chunk.items() if str(v) not in rejected}
            elif target == 'mdblist':
                # MDBList itemizes the episodes it cannot match: those are given up (never retried), the rest count normally
                rejected = self._not_found_episode_keys(data)
                sent = {k for k in chunk if k not in rejected}
            else:
                sent = set(chunk)
            given_up |= set(chunk) - sent
            if self._count(data, 'episodes') >= len(sent):
                succeeded |= sent
            elif target in ('trakt', 'simkl'):
                kodi_log(f'BridgeSync: {_TARGET_NAMES[target]} history confirmed fewer episodes than sent ({len(sent)}) - not retrying', 2)
                given_up |= sent
        return succeeded, given_up

    def _collected_at(self, source, key):
        """When `source` says the title was collected, in the form a collection copy carries; '' when it holds no usable date (none, unreadable
        or in the future)."""
        at = self._coll_at.get((source, key), '')
        if isinstance(at, str) and _STAMP_RE.match(at) and at[:19] <= self._utc_now()[:19]:
            return f'{at[:19]}.000Z'
        return ''

    def _push_set(self, domain, items, source, target):
        """Watchlist / collection push. items {'movie:<id>' / 'show:<id>': tmdb_id}."""
        if target == 'wetrakr':
            return self._push_wetrakr_set(domain, items, source)
        succeeded, given_up = set(), set()
        is_watchlist = domain == 'watchlist'
        path = ('sync', domain) if target == 'trakt' else (('watchlist', 'items', 'add') if is_watchlist else ('sync', 'collection'))
        if target == 'simkl':
            path = ('sync', 'add-to-list')       # moves an item's status; items in another status are skipped beforehand
        for chunk in self._chunks(items, _MDB_CHUNK if target == 'mdblist' else _BATCH_SIZE):
            payload = {}
            for key, tmdb_id in sorted(chunk.items()):
                kind = 'movies' if key.startswith('movie:') else 'shows'
                if target == 'simkl':
                    entry = {'ids': {'tmdb': int(tmdb_id)}, 'to': 'plantowatch'}
                else:
                    entry = {'tmdb': int(tmdb_id)} if (target == 'mdblist' and is_watchlist) else {'ids': {'tmdb': int(tmdb_id)}}
                    collected = '' if is_watchlist else self._collected_at(source, key)
                    if collected:
                        entry['collected_at'] = collected      # without it Trakt / MDBList date the copy with the moment it arrives
                payload.setdefault(kind, []).append(entry)
            ok, data = self._post(target, path, payload)
            if not ok:
                break
            if target == 'simkl':
                rejected = {(m, i) for m in ('movies', 'shows') for i in self._not_found_ids(data, m)}
                sent = set()
                for key, tmdb_id in chunk.items():
                    kind = 'movies' if key.startswith('movie:') else 'shows'
                    (given_up if (kind, str(tmdb_id)) in rejected else sent).add(key)
                if sum(self._count(data, m) for m in ('movies', 'shows')) >= len(sent):
                    succeeded |= sent
                else:
                    kodi_log(f'BridgeSync: Simkl confirmed fewer watchlist items than sent ({len(sent)}) - not retrying', 2)
                    given_up |= sent
                continue
            if target == 'trakt':
                rejected = {(m, i) for m in ('movies', 'shows') for i in self._not_found_ids(data, m)}
                for key, tmdb_id in chunk.items():
                    kind = 'movies' if key.startswith('movie:') else 'shows'
                    (given_up if (kind, str(tmdb_id)) in rejected else succeeded).add(key)
                continue
            landed = sum(self._count(data, m) for m in ('movies', 'shows'))
            if landed >= len(chunk):
                succeeded |= set(chunk)
            elif is_watchlist and sum(self._not_found_count(data, m) for m in ('movies', 'shows')):
                # MDBList only counts rejections: read the watchlist back to see which items landed
                readback = self._mdb_list_ids('watchlist', 'items')
                if readback is not None:
                    for key in chunk:
                        (succeeded if key in readback else given_up).add(key)
        return succeeded, given_up

    # ── Sync domains ───────────────────────────────────────────────────────────

    def _current(self, trakt, mdb=None, simkl=None, wetrakr=None):
        """Assemble {service: dict or None} for the services that take part; `mdb` / `simkl` are
        zero-argument readers. `trakt` is Trakt's view, None when it could not be read; Trakt is absent when it does not take part."""
        current = {}
        if self._trakt_listed:
            current['trakt'] = trakt
        if mdb and self._mdb_ok:
            current['mdblist'] = mdb()
            if current['mdblist'] is None:
                kodi_log('BridgeSync: MDBList read failed - skipping MDBList this cycle', 1)
        if simkl and self._simkl_ok:
            current['simkl'] = simkl()
            if current['simkl'] is None:
                kodi_log('BridgeSync: Simkl read failed - skipping Simkl this cycle', 1)
        if wetrakr and self._wetrakr_ok:
            current['wetrakr'] = wetrakr()
            if current['wetrakr'] is None:
                kodi_log('BridgeSync: WeTrakr read failed - skipping WeTrakr this cycle', 1)
        return current

    def _sync_watched_movies(self):
        """Trakt ↔ MDBList ↔ Simkl, newly watched movies only (see module docstring)."""
        trakt = None
        if self._trakt_ok:
            fresh = self._trakt_refresh('movie', ('last_watched_at',))
            trakt = self._trakt_ids('movie', 'last_watched_at') if fresh and self._trakt_ready('movie', 'watched') else None
        current = self._current(trakt, self._mdb_watched_movies, self._simkl_watched_movies, lambda: self._wetrakr_watched('movie'))
        self._wetrakr_join('movie', current)
        if trakt is not None:
            self._load_trakt_stamps('movie')
        self._reconcile('watched', 'movie', current, self._push_watched_movies,
                        caps={'trakt': _HISTORY_CAP, 'simkl': _SIMKL_CAP, 'wetrakr': _WETRAKR_CAP['movie']}, skip=self._skip_movies,
                        remove_fn=lambda i, s, t: self._remove_watched('movie', i, s, t),
                        removal_ok=self._removal_ok('watched', 'movie'))
        self._simkl_record_movies(current.get('simkl'))
        self._wetrakr_preview('movie', current)

    def _sync_watched_episodes(self):
        """Trakt ↔ MDBList ↔ Simkl. No show-level pushes: a whole-show mark means every episode."""
        if not (self._mdb_ok or self._simkl_ok or self._wetrakr_ok):
            return
        trakt = None
        if self._trakt_ok:
            fresh = self._trakt_refresh('show', ('last_watched_at',))
            trakt = self._trakt_episode_keys() if fresh and self._trakt_episodes_ready() else None
        current = self._current(trakt, self._mdb_watched_episodes, self._simkl_watched_episodes, lambda: self._wetrakr_watched('episode'))
        self._wetrakr_join('episode', current)
        if trakt is not None:
            self._load_trakt_stamps('episode')
        self._reconcile('watched', 'episode', current, self._push_watched_episodes,
                        caps={'trakt': _HISTORY_CAP, 'mdblist': _MDB_EPISODE_CAP, 'simkl': _SIMKL_CAP, 'wetrakr': _WETRAKR_CAP['episode']},
                        skip=self._skip_episodes, gate=self._gate_simkl_episodes,
                        remove_fn=lambda i, s, t: self._remove_watched('episode', i, s, t),
                        removal_ok=self._removal_ok('watched', 'episode'))
        if self._simkl is not None and current.get('simkl') is not None:
            self._simkl['ep_yes'].update({k: str(v) for k, v in current['simkl'].items() if k not in self._anime_keys})
        if self._anime_mode() == 1:
            self._anime_log_only(current)
        self._anime_simkl_sync(current)
        self._wetrakr_preview('episode', current)

    def _anime_log_only(self, current):
        # cross_sync_anime = 1: say what mode 2 would send, change nothing (no snapshot, no push)
        mapped, stats = self._anime_mapped()
        if stats is None:
            return
        parts = []
        for service in ('trakt', 'mdblist'):
            have = current.get(service)
            if have is None:
                continue
            would = sorted(k for k in mapped if k not in have)
            shows = {k.split(':')[0] for k in would}
            parts.append(f'{_TARGET_NAMES[service]} {len(would)} episodes of {len(shows)} shows')
        kodi_log(f'BridgeSync (anime, log only): Simkl anime episodes {stats["episodes"]}, mapped {len(mapped)} '
                 f'({stats["mapped_entries"]}/{stats["entries"]} entries); would send: {"; ".join(parts) or "nothing"}; '
                 f'left out: no Fribb record {stats["no_record"]}, not a tv season {stats["not_tv"]}, show id mismatch {stats["tmdb_mismatch"]}, '
                 f'season missing at TMDb {stats["no_season"]}, air date unconfirmed {stats["unconfirmed"]}, rejected {stats["rejected"]}', 1)

    def _simkl_record_movies(self, current_simkl):
        """Keep the saved Simkl views in step with what this cycle pushed to Simkl."""
        if self._simkl is None or current_simkl is None:
            return
        for tmdb_id in current_simkl:
            entry = self._simkl['movies'].get(tmdb_id)
            if not entry or entry[0] != 'completed':
                self._simkl['movies'][tmdb_id] = ['completed', self._stamps.get(('movie', tmdb_id)) or '']

    def _trakt_set(self, clause_key, method):
        """Watchlist / collection from the Trakt DB as {'movie:<id>' / 'show:<id>': id}; None until both
        media types are fully synced or if the DB cannot be read."""
        if not (self._trakt_refresh('movie', (clause_key,)) and self._trakt_refresh('show', (clause_key,))):
            return None
        if not (self._trakt_ready('movie', method) and self._trakt_ready('show', method)):
            return None
        movies = self._trakt_ids('movie', clause_key)
        shows = self._trakt_ids('show', clause_key)
        if movies is None or shows is None:
            return None
        self._snapshot_data()                  # loads the meta (the removal queue) if nothing has yet
        if clause_key == 'watchlist_listed_at' and self._meta.get('removal_queue'):
            stamps = {}
            for kind in ('movie', 'show'):
                for row in self._trakt_rows(('tmdb_id', clause_key), kind, f'{clause_key} IS NOT NULL') or ():
                    if row and row[0] and isinstance(row[1], str):
                        stamps[f'{kind}:{row[0]}'] = row[1]
            self._list_stamps['trakt'] = stamps
        if clause_key == 'collection_last_collected_at':
            for kind in ('movie', 'show'):
                for row in self._trakt_rows(('tmdb_id', clause_key), kind, f'{clause_key} IS NOT NULL') or ():
                    if row and row[0] and isinstance(row[1], str):
                        self._coll_at[('trakt', f'{kind}:{row[0]}')] = row[1]
        return {**{f'movie:{k}': v for k, v in movies.items()}, **{f'show:{k}': v for k, v in shows.items()}}

    def _sync_watchlist(self):
        """Trakt ↔ MDBList ↔ Simkl plan-to-watch ↔ WeTrakr planning."""
        lists = self._wetrakr_lists_mode()
        wt = lists == 2 and self._wetrakr_ok
        if not (self._mdb_ok or self._simkl_ok or wt):
            return
        trakt = self._trakt_set('watchlist_listed_at', 'watchlist') if self._trakt_ok else None
        current = self._current(trakt, lambda: self._mdb_list_ids('watchlist', 'items'), self._simkl_set,
                                self._wetrakr_watchlist if wt else None)
        if wt:
            self._wetrakr_join('movie_or_show', current, 'watchlist')
        self._reconcile('watchlist', 'movie_or_show', current, lambda i, s, t: self._push_set('watchlist', i, s, t),
                        caps={'simkl': _SIMKL_CAP, 'wetrakr': _WETRAKR_LIST_CAP}, skip=self._skip_watchlist,
                        remove_fn=lambda i, s, t: self._remove_set('watchlist', i, s, t),
                        removal_ok=self._removal_ok('watchlist', 'movie_or_show'))
        if self._simkl is not None and current.get('simkl') is not None:
            for key in current['simkl']:
                kind, _, tmdb_id = key.partition(':')
                bucket = self._simkl['movies' if kind == 'movie' else 'shows']
                bucket.setdefault(tmdb_id, ['plantowatch', ''])
        if lists == 1:
            self._wetrakr_lists_preview('watchlist', current)

    def _sync_collection(self):
        """Trakt ↔ MDBList ↔ WeTrakr library - Simkl has no collection endpoint."""
        lists = self._wetrakr_lists_mode()
        wt = lists == 2 and self._wetrakr_ok
        if self._trakt_ok + self._mdb_ok + wt < 2:
            return                             # one service alone has nothing to reconcile with (and no request is spent)
        trakt = self._trakt_set('collection_last_collected_at', 'collection') if self._trakt_ok else None
        current = self._current(trakt, lambda: self._mdb_list_ids('sync', 'collection'), None,
                                self._wetrakr_library if wt else None)
        if wt:
            self._wetrakr_join('movie_or_show', current, 'collection')
        self._reconcile('collection', 'movie_or_show', current, lambda i, s, t: self._push_set('collection', i, s, t),
                        caps={'wetrakr': _WETRAKR_LIST_CAP},
                        remove_fn=lambda i, s, t: self._remove_set('collection', i, s, t),
                        removal_ok=self._removal_ok('collection', 'movie_or_show'))
        if lists == 1:
            self._wetrakr_lists_preview('collection', current)

    # ── WeTrakr: watchlist and collection (setting cross_sync_wetrakr_lists, 7.5.33) ──
    #  - watchlist = WeTrakr's planning list. A title WeTrakr holds in ANY other list is never sent there (adding would move it off that list),
    #    and leaving planning counts as a removal only when the title is then on no list at all. Removals name status planning, never remove/all.
    #  - collection = WeTrakr's library. The bridge's copies carry a copy_key 'bridge:...' and removals touch only those. A title still listed
    #    afterwards is the user's own copy: it stays on WeTrakr, stays accounted for there, and is not sent back to the others.

    @staticmethod
    def _wetrakr_lists_mode():
        return _wetrakr_lists_mode()

    def _wetrakr_forget_activity(self):
        """Drops this run's /sync/last_activities answer and the stamp taken from it (after a write both are older than the data)."""
        self.wetrakr_last_stamp = self._wt_activity = None

    def _wetrakr_activity(self):
        if self._wt_activity is None:
            data = self._wetrakr_client().get_json('/sync/last_activities')
            self._wt_activity = data if isinstance(data, dict) and data else False
        return self._wt_activity or None

    @staticmethod
    def _wetrakr_list_rows(kind, rows):
        found = {}
        for row in rows or ():
            tmdb = (row.get('ids') or {}).get('tmdb') if isinstance(row, dict) else None
            if tmdb:
                found[f'{kind}:{tmdb}'] = str(row.get('tracked_at') or row.get('last_watched_at') or '')
        return found

    def _wetrakr_tracking(self):
        """{'planning': {key: tracked_at}, 'other': {key: list}} from every tracking list (re-read only when a stamp moved), or None."""
        if self._wt_lists is not None:
            return self._wt_lists
        self._snapshot_data()
        if not self._snapshots_ok:
            return None
        stamp = wetrakr_lists_stamp(self._wetrakr_activity())
        if stamp is None:
            return None
        saved = self._meta.get('wetrakr_tracking')
        if not saved or saved.get('stamp') != stamp:
            planning, other = {}, {}
            for kind, statuses in _WETRAKR_LISTS.items():
                for status in statuses:
                    ok, rows = self._wetrakr_client().get_list(f'/sync/tracking/{status}/{kind}s')
                    if not ok:
                        return None            # never a partial view: a missing list would let planning take a title off it
                    found = self._wetrakr_list_rows(kind, rows)
                    if status == 'planning':
                        planning.update(found)
                    else:
                        other.update(dict.fromkeys(found, status))
            saved = self._meta['wetrakr_tracking'] = {'stamp': stamp, 'planning': planning, 'other': other}
        self._wt_lists = {'planning': dict(saved['planning']), 'other': dict(saved['other'])}
        return self._wt_lists

    def _wetrakr_watchlist(self):
        lists = self._wetrakr_tracking()
        if lists is None:
            return None
        self._list_stamps['wetrakr'] = {k: v for k, v in lists['planning'].items() if v}
        return {k: int(k.partition(':')[2]) for k in lists['planning']}

    def _wetrakr_status(self, key):
        """The WeTrakr list a 'movie:<id>' / 'show:<id>' is on ('planning' or another), or None."""
        lists = self._wt_lists or {}
        return lists.get('other', {}).get(key) or ('planning' if key in lists.get('planning', {}) else None)

    def _wetrakr_library_read(self):
        """Every title in the library as ({'movie:<id>' / 'show:<id>': id}, {key: collected date, where WeTrakr knows it}), or None (never a
        partial list)."""
        found, dates, page, pages = {}, {}, 1, 1
        rows_read = 0
        total = None
        while page <= pages:
            if page > 100:
                return None
            response = self._wetrakr_client().user_request('GET', '/sync/library/all', params={'page': page, 'limit': 100})
            if response is None or response.status_code != 200:
                return None
            try:
                data = response.json()
                pages = int(response.headers.get('X-Pagination-Page-Count') or 0)
                total = int(response.headers.get('X-Pagination-Item-Count'))
            except (ValueError, TypeError):
                return None
            if not isinstance(data, list):
                return None
            rows_read += len(data)
            for row in data:
                media = row.get('type')
                tmdb = (row.get('ids') or {}).get('tmdb')
                if media not in ('movie', 'show'):
                    return None                # an answer not understood must not read as an emptier library
                if tmdb:
                    found[f'{media}:{tmdb}'] = int(tmdb)
                    held = row.get('library') or {}
                    if not held.get('collected_at_unknown') and isinstance(held.get('collected_at'), str):
                        dates[f'{media}:{tmdb}'] = held['collected_at']
            page += 1
        return (found, dates) if rows_read == total else None

    def _wetrakr_library_unchanged(self, since):
        response = self._wetrakr_client().user_request('GET', '/sync/library/all', params={'from_date': since, 'limit': 1})
        if response is None or response.status_code != 200:
            return False
        try:
            return int(response.headers.get('X-Pagination-Item-Count')) == 0
        except (ValueError, TypeError):
            return False

    def _wetrakr_library(self):
        """The library, re-read only when WeTrakr reports a change since the saved read (one small request otherwise)."""
        from datetime import datetime, timedelta
        self._snapshot_data()
        if not self._snapshots_ok:
            return None
        saved = self._meta.get('wetrakr_library')
        if saved and 'dates' in saved and self._wetrakr_library_unchanged(saved['since']):   # a read saved without dates (7.5.34) is redone
            return self._wetrakr_library_view(saved)
        fmt = '%Y-%m-%dT%H:%M:%S'
        since = (datetime.strptime(self._utc_now()[:19], fmt) - timedelta(minutes=10)).strftime(fmt) + '.000Z'   # margin for clock difference
        read = self._wetrakr_library_read()
        if read is None:
            return None
        saved = self._meta['wetrakr_library'] = {'since': since, 'rows': read[0], 'dates': read[1]}
        return self._wetrakr_library_view(saved)

    def _wetrakr_library_view(self, saved):
        self._coll_at.update({('wetrakr', key): at for key, at in saved['dates'].items()})
        return dict(saved['rows'])

    @staticmethod
    def _wetrakr_list_payload(items, entry_of):
        payload = {}
        for key, tmdb_id in sorted(items.items()):
            payload.setdefault(key.partition(':')[0] + 's', []).append({'ids': {'tmdb': int(tmdb_id)}, **entry_of(key)})
        return payload

    def _wetrakr_list_post(self, path, payload):
        response = self._wetrakr_client().user_request('POST', path, body=payload)
        code = getattr(response, 'status_code', None)
        if code is None or not 200 <= code < 300:
            kodi_log(f'BridgeSync: WeTrakr {path.strip("/")} failed (HTTP {code or "no answer"})', 2)
        return code

    def _wetrakr_list_view(self, domain):
        """A fresh read after a write."""
        self._wetrakr_forget_activity()       # the trigger takes a fresh baseline: our own list write is not a change to react to
        if domain == 'watchlist':
            self._wt_lists = None
            self._meta.pop('wetrakr_tracking', None)
            return self._wetrakr_watchlist()
        self._meta.pop('wetrakr_library', None)
        return self._wetrakr_library()

    def _push_wetrakr_set(self, domain, items, source):
        """(succeeded, given_up) for the watchlist / library. Success is what WeTrakr lists afterwards."""
        import time
        if domain == 'collection' and time.time() - self._meta.get('wetrakr_library_full', 0) < _WETRAKR_FULL_SECONDS:
            return set(), set()
        posted = set()
        for chunk in self._chunks(items, _WETRAKR_CHUNK):
            if domain == 'watchlist':
                path, body = '/sync/tracking', self._wetrakr_list_payload(chunk, lambda key: {'status': 'planning'})
            else:
                def copy_of(key):
                    entry = {'copy_key': _WETRAKR_COPY_PREFIX + key}
                    collected = self._collected_at(source, key)
                    if collected:
                        entry['collected_at'] = collected
                    else:
                        entry['collected_at_unknown'] = True
                    return entry
                path, body = '/sync/library', self._wetrakr_list_payload(chunk, copy_of)
            code = self._wetrakr_list_post(path, body)
            if code == 420 and domain == 'collection':
                self._meta['wetrakr_library_full'] = time.time()
                kodi_log('BridgeSync: the WeTrakr library is full for this account plan - nothing more is sent to it for a day', 2)
                break
            if code is None or not 200 <= code < 300:
                break
            posted |= set(chunk)
        if not posted:
            return set(), set()
        after = self._wetrakr_list_view(domain)
        if after is None:
            return set(), set()
        done = {k for k in posted if k in after}
        given_up = set()
        tries = self._meta.setdefault('wetrakr_tries', {})
        for key in posted - done:
            book = f'{domain}|{key}'
            tries[book] = tries.get(book, 0) + 1
            if tries[book] >= _WETRAKR_TRIES:
                given_up.add(key)
                kodi_log(f'BridgeSync: WeTrakr {domain}: {key} still not listed after {_WETRAKR_TRIES} tries - giving up on it', 1)
        for key in done | given_up:
            tries.pop(f'{domain}|{key}', None)
        return done, given_up

    def _remove_wetrakr_set(self, domain, items):
        """Take titles off the watchlist (status planning only) / the bridge's own library copies. Returns the keys no longer listed. A library
        title still listed after an accepted removal has a copy of the user's own: it is named in _wt_kept and not retried."""
        sent = set()
        for chunk in self._chunks(items, _WETRAKR_CHUNK):
            if domain == 'watchlist':
                path, body = '/sync/tracking/remove', self._wetrakr_list_payload(chunk, lambda key: {'status': 'planning'})
            else:
                path = '/sync/library/remove'
                body = {**self._wetrakr_list_payload(chunk, lambda key: {}), 'copy_key_prefix': _WETRAKR_COPY_PREFIX}
            code = self._wetrakr_list_post(path, body)
            if code is None or not 200 <= code < 300:
                break
            sent |= set(chunk)
        if not sent:
            return set()
        after = self._wetrakr_list_view(domain)
        if after is None:
            return set()
        still = sent & set(after)
        if domain == 'collection' and still:
            self._wt_kept |= still
            kodi_log(f'BridgeSync: WeTrakr library: {len(still)} title(s) kept - they hold a copy the sync did not make ({", ".join(sorted(still)[:10])})', 1)
        return sent - still

    def _wetrakr_lists_preview(self, domain, current):
        """Log only: what turning 'watchlist and collection' On would copy each way. Nothing is changed on any service and no snapshot is touched."""
        import time
        try:
            if not self._wetrakr_client().is_authorized:
                return
            snapshots = self._snapshot_data()
            if not self._snapshots_ok:
                return
            logged = self._meta.setdefault('wetrakr_logged', {})
            label = f'{domain}|wetrakr|movie_or_show'
            if time.time() - logged.get(label, 0) < _WETRAKR_LOG_SECONDS or label in (self._meta.get('wetrakr_member') or {}):
                return
            if not current or any(v is None for v in current.values()):
                return
            view = self._wetrakr_watchlist() if domain == 'watchlist' else self._wetrakr_library()
            if view is None:
                return
            others = {s: snapshots.get(f'{domain}|{s}|movie_or_show', {}) for s in _GAP_SERVICES if s != 'wetrakr'}
            member, queued, held = self._wetrakr_plan('movie_or_show', current, view, snapshots.get(label, {}), others, domain)
            new = {k: v for k, v in view.items() if k not in member}
            parts = []
            for target, items in current.items():
                missing = {k: v for k, v in new.items() if k not in items}
                if domain == 'watchlist':
                    for key in set(self._skip_watchlist(missing, 'wetrakr', target)):
                        missing.pop(key, None)
                parts.append(f'{_TARGET_NAMES[target]} {len(missing)}')
            sendable = self._wetrakr_sendable('movie_or_show', current, queued, domain)
            kodi_log(f'BridgeSync (WeTrakr, log only): {domain} - On would copy from WeTrakr: {", ".join(parts)}; queue {sendable} to be sent to '
                     f'WeTrakr ({len(queued) - sendable} left alone: on another WeTrakr list); already matched {len(member)} of the {len(view)} WeTrakr holds', 1)
            logged[label] = time.time()
            self._snapshot_save()
        except Exception as exc:
            kodi_log(f'BridgeSync: WeTrakr ({domain}) preview error: {exc}', 2)

    # ── WeTrakr: watched marks (setting cross_sync_wetrakr) ─────────────────────
    # 7.5.30: with the setting On WeTrakr is a member of the two watched domains like the other three: _reconcile copies to and from it, removals go both
    # ways under cross_sync_removals (journal, two runs, mass limit, played-again check), and the gap / removal queues cover it.
    #  - its view is the watch history (one row per play), so "unwatched" means no play is left; moving a title to another WeTrakr list is not a removal.
    #  - the history is read only when /sync/last_activities says watched marks were added or removed (otherwise the view saved from the last read is used).
    #  - pushes: POST /sync/tracking with allow_rewatch false and the play's own date, confirmed by reading the history back.
    #  - removals: POST /sync/tracking/remove/all, confirmed the same way.
    #  - joining (_wetrakr_join) turns the labels the one-way copy wrote into member snapshots, so nothing looks removed.
    #  - Log only reads WeTrakr and writes to the log what would be copied each way.

    @staticmethod
    def _wetrakr_mode():
        return _wetrakr_setting_mode()

    def _wetrakr_client(self):
        if self.wetrakr is None:
            from tmdbhelper.lib.api.wetrakr.api import WeTrakrAPI
            self.wetrakr = WeTrakrAPI()
        return self.wetrakr

    def _wetrakr_watched(self, media):
        """{key: id} of what WeTrakr has at least one play of (movies 'tmdb', episodes 'show:season:number' without specials), or None."""
        self._snapshot_data()                      # the saved view lives in the snapshot meta
        if not self._snapshots_ok:
            return None
        api = self._wetrakr_client()
        if self.wetrakr_last_stamp is None:        # one read per run serves the lists, both media types and the trigger's new baseline
            self.wetrakr_last_stamp = wetrakr_stamp(self._wetrakr_activity())
        stamp = self.wetrakr_last_stamp
        if stamp is None:
            return None
        views = self._meta.setdefault('wetrakr_views', {})
        saved = views.get(media)
        if not saved or saved.get('stamp') != stamp:
            ok, rows = api.get_list(_WETRAKR_PATHS[media])
            if not ok:
                return None
            saved = views[media] = {'stamp': stamp, 'rows': self._wetrakr_rows(media, rows)}
        result = {}
        for key, at in saved['rows'].items():
            result[key] = int(key.split(':')[0])
            self._note_stamp(media, key, at, 'wetrakr')
        return result

    @staticmethod
    def _wetrakr_rows(media, rows):
        """History rows -> {key: newest watched_at}."""
        found = {}
        for row in rows or ():
            if not isinstance(row, dict):
                continue
            if media == 'movie':
                tmdb = (row.get('ids') or {}).get('tmdb')
                key = f'{tmdb}' if tmdb else None
            else:
                tmdb, season, number = (row.get('show_ids') or {}).get('tmdb'), row.get('season_number'), row.get('number')
                key = f'{tmdb}:{season}:{number}' if tmdb and season and number is not None else None
            if not key:
                continue
            at = row.get('watched_at')
            at = at if isinstance(at, str) else ''
            if key not in found or at > found[key]:
                found[key] = at
        return found

    def _wetrakr_plan(self, media, current, view, old, others_snaps, domain='watched'):
        """What joining does, worked out without changing anything (Log only reports the same figures): (member snapshot, {key: services
        to send it to WeTrakr from}, keys any other service holds or held)."""
        held = set()
        for service, snap in others_snaps.items():
            held |= set(snap) | set(current.get(service) or ())
        waiting = set()                       # a removal waiting for its second check: not a title only WeTrakr has, so never copied back
        for label, items in (self._meta.get('removals') or {}).items():
            if label.startswith(f'{domain}|') and label.endswith(f'|{media}'):
                waiting |= set(items or ())
        if domain == 'watched':
            # the old one-way copy dropped a title from its snapshot once no other service listed it, but kept what it SENT in
            # 'wetrakr_sent'. Both mean 'dealt with': such a title left on WeTrakr alone is never copied back to the others
            sent = (self._meta.get('wetrakr_sent') or {}).get(f'{domain}|wetrakr|{media}') or {}
            member = {k: v for k, v in view.items() if k in waiting or k in old or k in sent}
        else:
            member = {k: v for k, v in view.items() if k in waiting or (k in old and k in held)}
        queued = {}
        for service, items in current.items():
            if service == 'wetrakr':
                continue
            for key in items or ():
                if key in others_snaps.get(service, {}) and key not in view and key not in old:   # not yet accounted there: the normal pass sends it
                    queued.setdefault(key, []).append(service)
        return member, queued, held

    def _wetrakr_sendable(self, media, current, queued, domain='watched'):
        """How many queued keys the catch-up would really send (for the messages): the holder it picks (the first of the key's origins) and
        the same skip rule, so a Simkl row the user never recorded is not counted."""
        skip = {'watched': self._skip_movies if media == 'movie' else self._skip_episodes, 'watchlist': self._skip_watchlist}.get(domain)
        recent = (self._meta.get('removed_recent') or {}).get(f'{domain}|wetrakr|{media}') or {}
        by_source = {}
        for key, sources in queued.items():
            if key not in recent:
                holder = next(s for s in current if s in sources)
                by_source.setdefault(holder, {})[key] = current[holder][key]
        return sum(len(items) - (len(set(skip(items, source, 'wetrakr'))) if skip else 0) for source, items in by_source.items())

    def _wetrakr_join(self, media, current, domain='watched'):
        """First run as a member, per media type. The label the one-way copy wrote means 'dealt with', not 'WeTrakr has it', so:
          - for watched marks it keeps every title WeTrakr still has that the one-way copy dealt with or sent, even one no other service holds
            any more (it was removed there: never copied back); only a title in neither book is new and is copied to the others. Watchlist and
            collection keep only what another service also holds;
          - what the others hold that WeTrakr lacks and the one-way copy never dealt with is queued to be sent to it. What it did deal with and
            WeTrakr no longer has (unwatched there, or refused) is not sent again.
        Needs every service in this run to be readable; otherwise WeTrakr waits."""
        import time
        if current.get('wetrakr') is None:
            return
        label = f'{domain}|wetrakr|{media}'
        what = f'{media}s' if domain == 'watched' else domain
        snapshots = self._snapshot_data()
        if not self._snapshots_ok:
            current.pop('wetrakr')
            return
        if label in (self._meta.get('wetrakr_member') or {}):
            return
        others = [s for s in current if s != 'wetrakr']
        if not others or any(current[s] is None for s in others):
            current.pop('wetrakr')
            kodi_log(f'BridgeSync: WeTrakr {what} join the sync on a run in which every other service can be read', 1)
            return
        view = current['wetrakr']
        others_snaps = {s: snapshots.get(f'{domain}|{s}|{media}', {}) for s in _GAP_SERVICES if s != 'wetrakr'}
        member, queued, held = self._wetrakr_plan(media, current, view, snapshots.get(label, {}), others_snaps, domain)
        for key in list(queued):       # an episode only Simkl still lists because its removal was not carried out there is not sent on
            queued[key] = [s for s in queued[key] if not self._simkl_keeps(domain, media, s, key)]
            if not queued[key]:
                del queued[key]
        if queued:
            entry = self._meta.setdefault('gap_queue', {}).setdefault(label, {})
            for key, sources in queued.items():
                entry[key] = {'at': time.time(), 'from': sources}
        snapshots[label] = member
        self._meta.setdefault('wetrakr_member', {})[label] = self._utc_now()
        for book in ('wetrakr_sent', 'wetrakr_gone', 'wetrakr_lost', 'wetrakr_remove_tries', 'wetrakr_tries', 'wetrakr_logged'):
            (self._meta.get(book) or {}).pop(label, None)
        kodi_log(f'BridgeSync: WeTrakr {what} joined the sync: {len(set(view) - held - set(member))} only on WeTrakr (to be copied to the others), '
                 f'{self._wetrakr_sendable(media, current, queued, domain)} to be sent to WeTrakr, {len(member)} already matched', 1)
        self._snapshot_save()

    def _wetrakr_preview(self, media, current):
        """Log only: what turning WeTrakr On would copy each way. Nothing is changed on any service and no snapshot is touched."""
        import time
        if self._wetrakr_mode() != 1:
            return
        try:
            if not self._wetrakr_client().is_authorized:
                return
            self._snapshot_data()
            if not self._snapshots_ok:
                return
            logged = self._meta.setdefault('wetrakr_logged', {})
            label = f'watched|wetrakr|{media}'
            if time.time() - logged.get(label, 0) < _WETRAKR_LOG_SECONDS:
                return
            if not current or any(v is None for v in current.values()):
                return
            if label in (self._meta.get('wetrakr_member') or {}):
                return                 # already joined once: the join figures no longer apply
            view = self._wetrakr_watched(media)
            if view is None:
                return
            snapshots = self._snapshot_data()
            others_snaps = {s: snapshots.get(f'watched|{s}|{media}', {}) for s in _GAP_SERVICES if s != 'wetrakr'}
            member, queued, held = self._wetrakr_plan(media, current, view, snapshots.get(label, {}), others_snaps)
            skip = self._skip_movies if media == 'movie' else self._skip_episodes
            new = {k: v for k, v in view.items() if k not in member}
            parts = []
            for target, items in current.items():
                missing = {k: v for k, v in new.items() if k not in items}
                for key in set(skip(missing, 'wetrakr', target)):
                    missing.pop(key, None)
                parts.append(f'{_TARGET_NAMES[target]} {len(missing)}')
            only = sorted(set(view) - held)
            kodi_log(f'BridgeSync (WeTrakr, log only): {media}s - On would copy from WeTrakr: {", ".join(parts)}; '
                     f'only on WeTrakr {len(only)} ({", ".join(only[:10])}); queue {self._wetrakr_sendable(media, current, queued)} to be sent to WeTrakr '
                     f'(plus anything new on the others); already matched {len(member)} of the {len(view)} WeTrakr holds', 1)
            logged[label] = time.time()
            self._snapshot_save()
        except Exception as exc:
            kodi_log(f'BridgeSync: WeTrakr ({media}) preview error: {exc}', 2)

    def _wetrakr_date(self, entry, media, key):
        """The date held by the service copied from (else the newest any service holds) as tracked_at; none known: tracked_at_unknown,
        which WeTrakr logs as 'now' and flags. A date in the future is not sent."""
        best = (self._source_date(self._date_source, media, key) or '')[:19]
        for stamp in () if best else (self._stamps.get((media, key)), self._tstamps.get((media, key))):
            if isinstance(stamp, str) and _STAMP_RE.match(stamp) and stamp[:19] > best:
                best = stamp[:19]
        if best and best <= self._utc_now():
            entry['tracked_at'] = f'{best}.000Z'
        else:
            entry['tracked_at_unknown'] = True

    def _wetrakr_payload(self, media, items):
        """Body of POST /sync/tracking: a movie by TMDb id, an episode in the nested shows > seasons > episodes form by number."""
        if media == 'movie':
            movies = []
            for key, tmdb_id in sorted(items.items()):
                entry = {'ids': {'tmdb': int(tmdb_id)}, 'status': 'watched'}
                self._wetrakr_date(entry, media, key)
                movies.append(entry)
            return {'allow_rewatch': False, 'movies': movies}
        shows = {}
        for key, show_id in sorted(items.items()):
            _, season, number = key.split(':')
            entry = {'number': int(number)}
            self._wetrakr_date(entry, media, key)
            shows.setdefault(int(show_id), {}).setdefault(int(season), []).append(entry)
        return {'allow_rewatch': False, 'shows': [
            {'ids': {'tmdb': show_id}, 'status': 'watched',
             'seasons': [{'number': season, 'episodes': episodes} for season, episodes in sorted(seasons.items())]}
            for show_id, seasons in sorted(shows.items())]}

    @staticmethod
    def _wetrakr_rejected(media, chunk, data):
        """Keys of a chunk WeTrakr could not resolve (live-captured 2026-11-21: notFound lists the movie as sent, a show as sent - every
        episode of it - and an episode as show.ids.tmdb + season + number). They are given up on, not retried."""
        part = data.get('notFound') or data.get('not_found') or {}
        part = part if isinstance(part, dict) else {}
        if media == 'movie':
            found = {f'{((e or {}).get("ids") or {}).get("tmdb")}' for e in part.get('movies') or () if isinstance(e, dict)}
            return {k for k in chunk if k in found}
        shows = {f'{((e or {}).get("ids") or {}).get("tmdb")}' for e in part.get('shows') or () if isinstance(e, dict)}
        episodes = {f'{(((e or {}).get("show") or {}).get("ids") or {}).get("tmdb")}:{e.get("season")}:{e.get("number")}'
                    for e in part.get('episodes') or () if isinstance(e, dict)}
        return {k for k in chunk if k.split(':')[0] in shows or k in episodes}

    def _wetrakr_push(self, media, batch):
        """POST the batch in chunks. Returns (posted keys, rejected keys): `posted` are the keys of the chunks WeTrakr answered 2xx to."""
        api = self._wetrakr_client()
        posted, rejected = set(), set()
        for chunk in self._chunks(batch, _WETRAKR_CHUNK):
            response = api.user_request('POST', '/sync/tracking', body=self._wetrakr_payload(media, chunk))
            code = getattr(response, 'status_code', None)
            if code is None or not 200 <= code < 300:
                kodi_log(f'BridgeSync: WeTrakr sync/tracking push failed (HTTP {code or "no answer"}) - retry next cycle', 2)
                break
            posted |= set(chunk)
            try:
                data = response.json()
            except ValueError:
                data = {}
            rejected |= self._wetrakr_rejected(media, chunk, data if isinstance(data, dict) else {})
        return posted, rejected

    def _wetrakr_changed(self):
        """After a write: the saved views and the menus' last-activities answer are stale."""
        self._wetrakr_forget_activity()
        self._meta.pop('wetrakr_views', None)
        try:
            self._wetrakr_client().wetrakr_syncdata.reset_lastactivities()
        except Exception as exc:
            kodi_log(f'BridgeSync: WeTrakr last activities not reset - {type(exc).__name__}', 1)

    def _wetrakr_read_back(self, media):
        ok, rows = self._wetrakr_client().get_list(_WETRAKR_PATHS[media])
        return set(self._wetrakr_rows(media, rows)) if ok else None

    def _push_wetrakr(self, media, items):
        """(succeeded, given_up) like the other pushes. Success is what the history shows afterwards; a title accepted but not listed on
        _WETRAKR_TRIES runs is given up on."""
        posted, rejected = self._wetrakr_push(media, items)
        if not posted:
            return set(), set()
        self._wetrakr_changed()
        after = self._wetrakr_read_back(media)
        if after is None:
            return set(), set()                     # cannot tell: retried next run (allow_rewatch false, so nothing is logged twice)
        done = {k for k in posted if k in after}
        given_up = {k for k in rejected if k not in after}
        tries = self._meta.setdefault('wetrakr_tries', {})
        for key in posted - done - given_up:
            book = f'{media}|{key}'
            tries[book] = tries.get(book, 0) + 1
            if tries[book] >= _WETRAKR_TRIES:
                given_up.add(key)
                kodi_log(f'BridgeSync: WeTrakr: {key} still not listed after {_WETRAKR_TRIES} tries - giving up on it', 1)
        for key in done | given_up:
            tries.pop(f'{media}|{key}', None)
        return done, given_up

    @staticmethod
    def _wetrakr_remove_payload(media, items):
        """Body of POST /sync/tracking/remove/all: the same shape the context menu sends (movies by TMDb id, episodes nested by number), no dates."""
        if media == 'movie':
            return {'movies': [{'ids': {'tmdb': int(tmdb_id)}, 'status': 'watched'} for _, tmdb_id in sorted(items.items())]}
        shows = {}
        for key, show_id in sorted(items.items()):
            _, season, number = key.split(':')
            shows.setdefault(int(show_id), {}).setdefault(int(season), []).append({'number': int(number)})
        return {'shows': [{'ids': {'tmdb': show_id}, 'status': 'watched',
                           'seasons': [{'number': season, 'episodes': episodes} for season, episodes in sorted(seasons.items())]}
                          for show_id, seasons in sorted(shows.items())]}

    def _remove_wetrakr(self, media, items):
        """Take every play of these titles off WeTrakr. Returns the keys the history no longer shows."""
        done = set()
        for chunk in self._chunks(items, _WETRAKR_CHUNK):
            response = self._wetrakr_client().user_request('POST', '/sync/tracking/remove/all', body=self._wetrakr_remove_payload(media, chunk))
            code = getattr(response, 'status_code', None)
            if code is None or not 200 <= code < 300:
                kodi_log(f'BridgeSync: WeTrakr sync/tracking/remove/all failed (HTTP {code or "no answer"}) - retry next cycle', 2)
                break
            done |= set(chunk)
        if not done:
            return done
        self._wetrakr_changed()
        after = self._wetrakr_read_back(media)
        return set() if after is None else {k for k in done if k not in after}

    def _resume_refused(self, key, paused_at, book='simkl_resume_failed'):
        """True when Simkl refused this exact resume point (same Trakt paused_at) on the last runs and a day has not passed.
        book='mdblist_resume_skipped': the same for a point MDBList answered with action 'skip' (it ignores a pause shortly after a watched mark)."""
        import time
        self._snapshot_data()      # loads self._meta (a no-op once loaded)
        rec = (self._meta.get(book) or {}).get(key)
        return bool(rec and rec[0] == (paused_at or '') and rec[1] >= _RESUME_FAIL_RUNS and time.time() - rec[2] < _RESUME_FAIL_SECONDS)

    def _resume_note(self, key, paused_at, ok, book='simkl_resume_failed'):
        """Remember a Simkl resume post that got no usable answer (and forget it once it works), so an item Simkl does not
        know is not posted again on every run. Entries older than a week go."""
        import time
        self._snapshot_data()
        now = time.time()
        book = self._meta.setdefault(book, {})
        for old in [k for k, v in book.items() if now - v[2] > 7 * 86400]:
            del book[old]
        if ok:
            book.pop(key, None)
            return
        rec = book.get(key)
        if rec and rec[0] == (paused_at or ''):
            rec[1] += 1
            rec[2] = now
        else:
            book[key] = [paused_at or '', 1, now]

    @staticmethod
    def _resume_due(have, paused_at):
        """Whether a Trakt resume point should go to a target. `have` is the paused_at of the target's resume point for
        the item (None = it has none, False = it has one without a time)."""
        if have is None:
            return True            # the target holds none (or it expired): send
        if have is False or not isinstance(paused_at, str) or not _STAMP_RE.match(paused_at):
            return False           # cannot tell which is newer: leave what the target holds
        return paused_at[:19] > str(have)[:19]

    @staticmethod
    def _note_have(have, key, at):
        if not key[1] or key[1] == 'None':
            return
        if isinstance(at, str) and _STAMP_RE.match(at):
            prev = have.get(key)
            if not isinstance(prev, str) or at > prev:
                have[key] = at
        else:
            have.setdefault(key, False)

    def _mdb_playback(self):
        """MDBList's resume points as {('movie', tmdb) / ('episode', show_tmdb, season, number): paused_at}, or None."""
        data = self._mdb_get('sync', 'playback')
        if not isinstance(data, list):
            return None
        self._mdb_playback_raw = data     # the resume mirror reads the same answer (progress included) instead of asking again
        have = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            at = item.get('paused_at') or item.get('updated_at')
            if item.get('type') == 'episode' or item.get('episode'):
                ep = item.get('episode') or {}
                tmdb_id = ((item.get('show') or {}).get('ids') or {}).get('tmdb')
                self._note_have(have, ('episode', str(tmdb_id), ep.get('season'), ep.get('number')), at)
            else:
                self._note_have(have, ('movie', str(((item.get('movie') or {}).get('ids') or {}).get('tmdb'))), at)
        return have

    def _simkl_playback(self):
        """Simkl's resume points as {('movie', tmdb) / ('show', tmdb): paused_at} (one per show), or None."""
        ok, data = self._simkl_get_ok('/sync/playback')
        if not ok:
            return None
        data = [] if data is None else data
        if not isinstance(data, list):
            return None
        self._simkl_playback_raw = data
        have = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            at = item.get('paused_at')
            if item.get('movie') or item.get('type') == 'movie':
                self._note_have(have, ('movie', str(((item.get('movie') or {}).get('ids') or {}).get('tmdb'))), at)
            else:
                media = item.get('show') or item.get('anime') or {}
                self._note_have(have, ('show', str((media.get('ids') or {}).get('tmdb'))), at)
        return have

    @staticmethod
    def _playback_points(data, simkl):
        """({('movie', tmdb) / ('episode', show tmdb, season, number): (progress, paused_at)}, anime entries left out) of one service's
        /sync/playback answer. Simkl's anime entries (their own bucket, numbered per cour) are not read at all."""
        points, anime = {}, 0
        for item in data or []:
            if not isinstance(item, dict):
                continue
            try:
                progress = float(item.get('progress'))
            except (TypeError, ValueError):
                continue
            at = item.get('paused_at') or item.get('updated_at')
            if not isinstance(at, str) or not _STAMP_RE.match(at):
                continue
            if item.get('movie') or item.get('type') == 'movie':
                tmdb = ((item.get('movie') or {}).get('ids') or {}).get('tmdb')
                key = ('movie', tmdb)
            else:
                if simkl and item.get('anime') and not item.get('show'):
                    anime += 1
                    continue
                ep = item.get('episode') or {}
                tmdb = ((item.get('show') or {}).get('ids') or {}).get('tmdb')
                key = ('episode', tmdb, ep.get('season'), ep.get('number'))
                if key[2] is None or key[3] is None:
                    continue
            try:
                key = (key[0], int(key[1])) + tuple(int(i) for i in key[2:])
            except (TypeError, ValueError):
                continue
            if key not in points or at > points[key][1]:
                points[key] = (progress, at)
        return points, anime

    def _simkl_numbering_ok(self, tmdb_id, season, episode):
        """For a resume point: True only when this number is the same episode on TMDb and on Simkl (simkl_numbering.same_numbers).
        Anything that cannot be checked is False. The season's answer is remembered: two weeks for a number that is the same, a day
        for any other (or a failed check), so a point that stays due does not repeat the requests."""
        import time
        self._snapshot_data()
        now = time.time()
        self._meta.pop('simkl_numbering', None)                # the answers of the count test are not to be trusted
        book = self._meta.setdefault('simkl_numbering_points', {})   # {show:season: [same numbers, when]}
        for old in [k for k, v in book.items() if now - v[1] > 14 * 86400]:
            del book[old]
        key = f'{tmdb_id}:{season}'
        rec = book.get(key)
        if rec and (int(episode) in rec[0] or now - rec[1] < 86400):
            return int(episode) in rec[0]
        good = []
        try:
            if int(season) > 0:
                from tmdbhelper.lib.script.sync.simkl_numbering import same_numbers
                lists = self._numbering_lists(int(tmdb_id), [int(season)])
                if lists is not None:
                    good = sorted(same_numbers(*lists[int(season)]))
        except Exception as exc:
            kodi_log(f'BridgeSync: Simkl numbering check error: {exc}', 2)
        book[key] = [good, now]
        return int(episode) in good

    def _mirror_confirm(self, key):
        """Called after a post answered: remember that this point was mirrored just now (only for a point _mirror_resume chose this run)."""
        if key in getattr(self, '_mirror_chosen', ()):
            import time
            self._meta.setdefault('resume_mirrored', {})[key] = time.time()

    def _mirror_resume(self, trakt_movies, trakt_eps):
        """MDBList <-> Simkl resume points that Trakt does not hold (the Trakt pass above owns every point it holds, so nothing is
        mirrored twice). Returns ([(tmdb, progress, paused_at)] for Simkl, {show: episode row} for Simkl, same movies for MDBList, same
        episode rows for MDBList) in the row shapes of the Trakt pass, so the posting code below sends them like any other, and a log text.
        A point goes to the other service only when it holds none (an episode on Simkl: none for the show) or the source is newer AND the
        progress differs (the same progress is the same point: without that a mirrored point, stamped 'now' on arrival, would be sent
        back, and back again). Not mirrored: anime (numbered per cour), points outside 4-96 %, items watched at or after the pause on either
        service (a finished item must not come back In Progress), episodes (either direction) whose Simkl season numbering is not confirmed.
        Nothing is written to the 'resume_mirrored' book here: the posting code calls _mirror_confirm for each point whose post answered. """
        none = ([], {}, [], [], '')
        m, s = getattr(self, '_mdb_playback_raw', None), getattr(self, '_simkl_playback_raw', None)
        if m is None or s is None or self._trakt_progress_failed:
            return none
        # watched stamps come from the watched pass: without both services' watched reads this cycle a finished item could not be told
        if self._mdb_watched_raw() is None or self._simkl_state() is None:
            return none
        mp, _ = self._playback_points(m, False)
        sp, skipped_anime = self._playback_points(s, True)
        import time
        from calendar import timegm
        from time import strptime
        tol, low, high = _MIRROR_TOLERANCE, _MIRROR_MIN, _MIRROR_MAX
        stats = {'anime': skipped_anime, 'numbering': 0, 'dropped': 0}
        self._snapshot_data()
        now = time.time()
        # {'m:tmdb' / 'e:show:season:number': when the service last ANSWERED a mirrored post for it}. A point is only written to the book by
        # _mirror_confirm after its post got a usable answer; a failed post leaves no entry, so the next cycle chooses the point again.
        sent = self._meta.setdefault('resume_mirrored', {})
        chosen = self._mirror_chosen = set()
        # An entry lives as long as one service still holds a point for the item (its copy is what a clear on the other service must not
        # bring back); it is forgotten two weeks after the last mirroring once neither service holds one.
        held = {f'm:{k[1]}' if k[0] == 'movie' else f'e:{k[1]}:{k[2]}:{k[3]}' for k in list(mp) + list(sp)}
        for old in [k for k, v in sent.items() if now - v > 14 * 86400 and k not in held]:
            del sent[old]

        def epoch(at):
            try:
                return timegm(strptime(at[:19], '%Y-%m-%dT%H:%M:%S'))
            except (ValueError, OverflowError):
                return now

        def due(point, have, key_str):
            progress, at = point
            if not (low <= progress <= high):
                return False
            if have is None:
                # The other service holds nothing. If this item was mirrored before, a point that is only the copy that arrived (stamped
                # within half an hour of the mirroring) means the original holder dropped it: cleared or finished there. A pause made
                # after that is new and goes.
                if key_str in sent and epoch(at) <= sent[key_str] + 1800:
                    stats['dropped'] += 1
                    return False
                return True
            return at[:19] > have[1][:19] and abs(progress - have[0]) > tol

        def finished(kind, key, at):
            return self._watched_since(kind, key, at)
        to_simkl_movies, to_mdb_movies, to_mdb_eps, to_simkl_eps = [], [], [], {}
        for key, point in mp.items():                                   # MDBList -> Simkl
            if key[0] == 'movie':
                if key[1] not in trakt_movies and due(point, sp.get(key), f'm:{key[1]}') and not finished('movie', str(key[1]), point[1]):
                    to_simkl_movies.append((key[1], point[0], point[1]))
                    chosen.add(f'm:{key[1]}')
        by_show = {}
        for key, point in mp.items():
            if key[0] == 'episode' and key[1:] not in trakt_eps and not self._anime_known(key[1]):
                by_show.setdefault(key[1], []).append((point[1], key, point))
        for show, points in by_show.items():
            at, key, point = max(points)
            key_str = f'e:{show}:{key[2]}:{key[3]}'
            theirs = [(v[1], k, v) for k, v in sp.items() if k[0] == 'episode' and k[1] == show]   # Simkl keeps one point per show
            if theirs:
                their_at, their_key, their = max(theirs)
                ok = low <= point[0] <= high and at[:19] > their_at[:19] and (their_key != key or abs(point[0] - their[0]) > tol)   # another episode: the dates decide
            else:
                ok = due(point, None, key_str)
            if ok and not finished('episode', f'{show}:{key[2]}:{key[3]}', at):
                if self._simkl_numbering_ok(show, key[2], key[3]):
                    to_simkl_eps[show] = (show, point[0], key[2], key[3], at)
                    chosen.add(key_str)
                else:
                    stats['numbering'] += 1
        for key, point in sp.items():                                   # Simkl -> MDBList
            if key[0] == 'movie':
                if key[1] not in trakt_movies and due(point, mp.get(key), f'm:{key[1]}') and not finished('movie', str(key[1]), point[1]):
                    to_mdb_movies.append((key[1], point[0], point[1]))
                    chosen.add(f'm:{key[1]}')
            elif key[1:] not in trakt_eps and due(point, mp.get(key), f'e:{key[1]}:{key[2]}:{key[3]}')                     and not finished('episode', f'{key[1]}:{key[2]}:{key[3]}', point[1]):
                if self._simkl_numbering_ok(key[1], key[2], key[3]):
                    to_mdb_eps.append((key[1], point[0], key[2], key[3], point[1]))
                    chosen.add(f'e:{key[1]}:{key[2]}:{key[3]}')
                else:
                    stats['numbering'] += 1
        text = ''
        if to_simkl_movies or to_simkl_eps or to_mdb_movies or to_mdb_eps or stats['anime'] or stats['numbering'] or stats['dropped']:
            text = (f'BridgeSync (resume mirror): MDBList -> Simkl {len(to_simkl_movies)} movie + {len(to_simkl_eps)} episode, '
                    f'Simkl -> MDBList {len(to_mdb_movies)} movie + {len(to_mdb_eps)} episode; not mirrored: {stats["anime"]} Simkl anime, '
                    f'{stats["numbering"]} with unconfirmed Simkl numbering, {stats["dropped"]} dropped where they began')
        return to_simkl_movies, to_simkl_eps, to_mdb_movies, to_mdb_eps, text

    # ── WeTrakr: resume points (setting cross_sync_wetrakr_resume, 7.5.36) ─────

    def _wetrakr_paused(self):
        """{'m:<tmdb>' / 'e:<tmdb>:<season>:<episode>': when it was paused (False: a row that is not a dated resume point, such as a
        session being played)}, or None when either list could not be read."""
        have = {}
        for target in ('movies', 'episodes'):
            ok, rows = self._wetrakr_client().get_list(f'/sync/tracking/playing/{target}')
            if not ok:
                return None
            for row in rows:
                if target == 'movies':
                    tmdb = (row.get('ids') or {}).get('tmdb')
                    key = f'm:{tmdb}'
                else:
                    tmdb = (row.get('show_ids') or {}).get('tmdb')
                    key = f"e:{tmdb}:{row.get('season_number')}:{row.get('number')}"
                if tmdb:
                    at = row.get('tracked_at')
                    have[key] = at if row.get('status') == 'paused' and isinstance(at, str) else False
        return have

    @staticmethod
    def _wetrakr_resume_payload(batch):
        """Body of POST /sync/tracking. For an episode the status sits on the EPISODE only: 'paused' on a show means 'on hold'."""
        movies, shows = [], {}
        for point in batch.values():
            paused = {'status': 'paused', 'progress': point['progress'], 'paused_at': f"{point['at'][:19]}.000Z"}
            if point['season'] is None:
                movies.append({'ids': {'tmdb': point['tmdb']}, **paused})
            else:
                shows.setdefault(point['tmdb'], {}).setdefault(point['season'], []).append({'number': point['episode'], **paused})
        body = {}
        if movies:
            body['movies'] = movies
        if shows:
            body['shows'] = [{'ids': {'tmdb': tmdb}, 'seasons': [{'number': n, 'episodes': eps} for n, eps in seasons.items()]}
                             for tmdb, seasons in shows.items()]
        return body

    def _wetrakr_resume(self, movie_rows, ep_rows):
        """Trakt's resume points -> WeTrakr. The upload logs no play; WeTrakr refuses a title already watched and keeps a position that is
        the same or newer. Each point is dealt with once (meta 'wetrakr_resume_done'), so one cleared on WeTrakr is not put back."""
        import time
        mode = _wetrakr_resume_mode()
        if not mode or getattr(self, '_trakt_progress_failed', False):
            return
        if not self._wetrakr_client().is_authorized:
            return
        self._snapshot_data()
        if not self._snapshots_ok:
            return
        now = self._utc_now()
        points = {}
        rows = [(r[0], r[1], None, None, r[2]) for r in movie_rows] + [(r[0], r[1], r[2], r[3], r[4]) for r in ep_rows]
        for tmdb, progress, season, episode, at in rows:
            try:
                progress, tmdb = round(float(progress), 2), int(tmdb)
                season, episode = (None, None) if season is None else (int(season), int(episode))
            except (TypeError, ValueError):
                continue
            if season == 0 or not 0 < progress < 100 or not isinstance(at, str) or not _STAMP_RE.match(at) or at[:19] > now[:19]:
                continue                           # specials are left out, as for watched marks; a date in the future is refused
            key = f'm:{tmdb}' if season is None else f'e:{tmdb}:{season}:{episode}'
            points[key] = {'tmdb': tmdb, 'season': season, 'episode': episode, 'progress': progress, 'at': at}
        done = self._meta.get('wetrakr_resume_done') or {}
        todo = {k: p for k, p in points.items() if done.get(k) != p['at'] and not self._resume_refused(k, p['at'], 'wetrakr_resume_refused')}
        if not todo:
            return
        if mode == 1:
            logged = self._meta.setdefault('wetrakr_logged', {})
            if time.time() - logged.get('resume', 0) < _WETRAKR_LOG_SECONDS:
                return
        have = self._wetrakr_paused()
        if have is None:
            kodi_log('BridgeSync: WeTrakr resume points could not be read - none sent to it this cycle', 1)
            return
        due = {k: p for k, p in todo.items() if self._resume_due(have.get(k), p['at'])}
        if mode == 1:
            kodi_log(f'BridgeSync (WeTrakr resume points, log only): On would send {len(due)} of {len(points)} Trakt resume point(s)', 1)
            logged['resume'] = time.time()
            self._snapshot_save()
            return
        done = {k: v for k, v in done.items() if k in points}
        self._meta['wetrakr_resume_done'] = done
        for key, point in todo.items():
            if key not in due and isinstance(have.get(key), str):
                done[key] = point['at']                # WeTrakr holds this position or a newer one
        batch = dict(sorted(due.items(), key=lambda kv: kv[1]['at'], reverse=True)[:_WETRAKR_LIST_CAP])
        if not batch:
            self._snapshot_save()
            return
        code = self._wetrakr_list_post('/sync/tracking', self._wetrakr_resume_payload(batch))
        if code is None or not 200 <= code < 300:
            self._snapshot_save()
            return
        self._wetrakr_forget_activity()            # the trigger takes a fresh baseline: our own write is not a change to react to
        after = self._wetrakr_paused()
        if after is None:
            kodi_log(f'BridgeSync: {len(batch)} resume point(s) sent to WeTrakr, but its list could not be read back', 2)
            self._snapshot_save()
            return
        landed = 0
        for key, point in batch.items():
            ok = isinstance(after.get(key), str) and after[key][:19] >= point['at'][:19]     # success is what WeTrakr lists afterwards
            self._resume_note(key, point['at'], ok, 'wetrakr_resume_refused')
            if ok:
                done[key] = point['at']
                landed += 1
        if landed:
            kodi_log(f'BridgeSync →WeTrakr: {landed} resume point(s)', 1)
        if landed < len(batch):
            kodi_log(f'BridgeSync: {len(batch) - landed} Trakt resume point(s) not taken by WeTrakr (already watched there, being played, '
                     f'or not known to it)', 1)
        self._snapshot_save()

    def _sync_progress(self):
        """Push Trakt resume points to MDBList and Simkl; mirror the ones only MDBList or Simkl holds between those two (_mirror_resume).
        Reverse direction not possible: Trakt /sync/playback is read-only.
        """
        wetrakr = bool(_wetrakr_resume_mode())
        if not self._mdb_ok and not self._simkl_ok and not wetrakr:
            return
        self._mdb_playback_raw = self._simkl_playback_raw = None
        self._trakt_progress_failed = False
        if not self._trakt_listed:
            # Trakt does not take part (7.5.14): it holds no points, so nothing is sent from it and the mirror below owns every point
            movie_rows, ep_rows = [], []
        else:
            if not self._trakt_ok:
                return                         # takes part but is not authorized yet: its points are unknown, so the mirror must not guess
            # The local copy of Trakt's resume points only changes when something reads it, so a point deleted or finished on
            # Trakt would be sent on again (and revive the item In Progress on the other services): bring it up to date first
            if not (self._trakt_refresh('movie', ('playback_progress',)) and self._trakt_refresh('show', ('playback_progress',))):
                return
            movie_rows, ep_rows = self._trakt_progress_rows()
        # Every point Trakt holds belongs to the Trakt pass; the resume mirror (MDBList <-> Simkl) leaves those alone (read before the filter below)
        trakt_movies = {int(r[0]) for r in movie_rows}
        trakt_eps = {(int(r[0]), int(r[2]), int(r[3])) for r in ep_rows}
        # Trakt only marks an item watched from 80% while MDBList and Simkl do from 75%, so between the two Trakt keeps the
        # resume point of an item they already count as watched; sent on, it would put the item back In Progress on them
        failed = {'MDBList': 0, 'Simkl': 0}   # a post that got no usable answer: counted and logged, never silent
        total = len(movie_rows) + len(ep_rows)
        movie_rows = [r for r in movie_rows if not self._watched_since('movie', f'{int(r[0])}', r[2])]
        ep_rows = [r for r in ep_rows if not self._watched_since('episode', f'{int(r[0])}:{int(r[2])}:{int(r[3])}', r[4])]
        if total != len(movie_rows) + len(ep_rows):
            kodi_log(f'BridgeSync: {total - len(movie_rows) - len(ep_rows)} Trakt resume point(s) not sent - the item was watched '
                     f'on MDBList or Simkl after Trakt saved it', 1)

        if wetrakr:
            try:
                self._wetrakr_resume(movie_rows, ep_rows)
            except Exception as exc:
                kodi_log(f'BridgeSync: WeTrakr resume points error: {exc}', 2)

        # A resume point the target already holds, or holds newer, is left alone: sending it again would overwrite
        # progress made there since. A target whose resume points cannot be read is skipped this run.
        mdb_movies, mdb_eps, simkl_movies, simkl_newest = [], [], [], {}
        if self._mdb_ok:
            have = self._mdb_playback()
            if have is None:
                kodi_log('BridgeSync: MDBList resume points could not be read - none sent to it this cycle', 1)
            else:
                mdb_movies = [r for r in movie_rows if self._resume_due(have.get(('movie', str(int(r[0])))), r[2])]
                mdb_eps = [r for r in ep_rows if self._resume_due(have.get(('episode', str(int(r[0])), int(r[2]), int(r[3]))), r[4])]
        if self._simkl_ok:
            have = self._simkl_playback()
            if have is None:
                kodi_log('BridgeSync: Simkl resume points could not be read - none sent to it this cycle', 1)
            else:
                simkl_movies = [r for r in movie_rows if self._resume_due(have.get(('movie', str(int(r[0])))), r[2])]
                # Simkl keeps one resume point per show, so of a show's episodes only the one Trakt saved last goes to it
                # (sent in turn, the last of them would win whichever was newest)
                for r in ep_rows:
                    if r[0] not in simkl_newest or (r[4] or '') > (simkl_newest[r[0]][4] or ''):
                        simkl_newest[r[0]] = r
                simkl_newest = {k: r for k, r in simkl_newest.items() if self._resume_due(have.get(('show', str(int(r[0])))), r[4])}
        if self._mdb_ok and self._simkl_ok:
            try:
                to_simkl_movies, to_simkl_eps, to_mdb_movies, to_mdb_eps, text = self._mirror_resume(trakt_movies, trakt_eps)
            except Exception as exc:
                to_simkl_movies, to_simkl_eps, to_mdb_movies, to_mdb_eps, text = [], {}, [], [], ''
                kodi_log(f'BridgeSync: resume mirror error: {exc}', 2)
            if text:
                kodi_log(text, 1)
            simkl_movies += to_simkl_movies
            mdb_movies += to_mdb_movies
            mdb_eps += to_mdb_eps
            for show, row in to_simkl_eps.items():
                simkl_newest.setdefault(show, row)
        if self._mdb_ok:
            # A point MDBList answered with action 'skip' (it ignores a pause shortly after a watched mark and keeps nothing) on the
            # last runs, with Trakt's copy unchanged, is not posted again for a day (it was one useless POST on every launch)
            book = 'mdblist_resume_skipped'
            skipped = [r for r in mdb_movies if self._resume_refused(f'm:{int(r[0])}', r[2], book)]
            skipped += [r for r in mdb_eps if self._resume_refused(f'e:{int(r[0])}:{int(r[2])}:{int(r[3])}', r[4], book)]
            if skipped:
                mdb_movies = [r for r in mdb_movies if r not in skipped]
                mdb_eps = [r for r in mdb_eps if r not in skipped]
                kodi_log(f'BridgeSync: {len(skipped)} Trakt resume point(s) not sent to MDBList - it ignored them on the last runs '
                         f'(watched there just before) and they have not changed (tried again in a day)', 1)

            def mdb_skip(resp):
                try:
                    return (resp.json() or {}).get('action') == 'skip'
                except Exception:
                    return False
            for row in mdb_movies:
                try:
                    tmdb_id, progress = int(row[0]), round(float(row[1]), 2)  # MDBList 400s on more than 2 decimals
                    url = self.mdb.get_request_url('scrobble', 'pause')
                    resp = self.mdb.get_api_request(
                        url,
                        postdata={'movie': {'ids': {'tmdb': tmdb_id}}, 'progress': progress},
                        method='json', headers=self.mdb.auth_headers)
                    failed['MDBList'] += resp is None
                    if resp is not None:
                        self._resume_note(f'm:{tmdb_id}', row[2], not mdb_skip(resp), book)
                        if not mdb_skip(resp):
                            self._mirror_confirm(f'm:{tmdb_id}')
                except Exception:
                    failed['MDBList'] += 1
            for row in mdb_eps:
                try:
                    tmdb_id, progress, season, episode = int(row[0]), round(float(row[1]), 2), int(row[2]), int(row[3])
                    url = self.mdb.get_request_url('scrobble', 'pause')
                    resp = self.mdb.get_api_request(
                        url,
                        postdata={
                            'show': {'ids': {'tmdb': tmdb_id},
                                     'season': {'number': season, 'episode': {'number': episode}}},
                            'progress': progress},
                        method='json', headers=self.mdb.auth_headers)
                    failed['MDBList'] += resp is None
                    if resp is not None:
                        self._resume_note(f'e:{tmdb_id}:{season}:{episode}', row[4], not mdb_skip(resp), book)
                        if not mdb_skip(resp):
                            self._mirror_confirm(f'e:{tmdb_id}:{season}:{episode}')
                except Exception:
                    failed['MDBList'] += 1
            if mdb_movies or mdb_eps:
                kodi_log(f'BridgeSync →MDBList: {len(mdb_movies)} movie + {len(mdb_eps)} episode progress', 1)
            if failed['MDBList']:
                kodi_log(f'BridgeSync: {failed["MDBList"]} MDBList resume post(s) got no usable answer', 2)

        if self._simkl_ok:
            simkl_ep_rows = list(simkl_newest.values())
            refused = [r for r in simkl_movies if self._resume_refused(f'm:{int(r[0])}', r[2])]
            refused += [r for r in simkl_ep_rows if self._resume_refused(f'e:{int(r[0])}:{int(r[2])}:{int(r[3])}', r[4])]
            if refused:
                simkl_movies = [r for r in simkl_movies if r not in refused]
                simkl_ep_rows = [r for r in simkl_ep_rows if r not in refused]
                kodi_log(f'BridgeSync: {len(refused)} Trakt resume point(s) not sent to Simkl - it refused them on the last runs '
                         f'and they have not changed (tried again in a day)', 1)
            if simkl_movies or simkl_ep_rows:
                self._simkl_pace()      # the bridge's own writes just above may still hold Simkl's per-user lock
            for row in simkl_movies:
                try:
                    tmdb_id, progress = int(row[0]), float(row[1])
                    url = f'{_SIMKL_BASE}/scrobble/pause?client_id={self.simkl.client_id}'
                    resp = self.simkl.get_api_request(
                        url,
                        postdata={'progress': progress, 'movie': {'ids': {'tmdb': tmdb_id}}},
                        method='json', headers=self.simkl.auth_headers)
                    failed['Simkl'] += resp is None
                    self._resume_note(f'm:{tmdb_id}', row[2], resp is not None)
                    if resp is not None:
                        self._mirror_confirm(f'm:{tmdb_id}')
                except Exception:
                    failed['Simkl'] += 1
            for row in simkl_ep_rows:
                try:
                    tmdb_id, progress, season, episode = int(row[0]), float(row[1]), int(row[2]), int(row[3])
                    url = f'{_SIMKL_BASE}/scrobble/pause?client_id={self.simkl.client_id}'
                    resp = self.simkl.get_api_request(
                        url,
                        postdata={
                            'progress': progress,
                            'show': {'ids': {'tmdb': tmdb_id}},
                            'episode': {'season': season, 'number': episode}},
                        method='json', headers=self.simkl.auth_headers)
                    failed['Simkl'] += resp is None
                    self._resume_note(f'e:{tmdb_id}:{season}:{episode}', row[4], resp is not None)
                    if resp is not None:
                        self._mirror_confirm(f'e:{tmdb_id}:{season}:{episode}')
                except Exception:
                    failed['Simkl'] += 1
            if simkl_movies or simkl_ep_rows:
                kodi_log(f'BridgeSync →Simkl: {len(simkl_movies)} movie + {len(simkl_ep_rows)} episode progress', 1)
            if failed['Simkl']:
                kodi_log(f'BridgeSync: {failed["Simkl"]} Simkl resume post(s) got no usable answer', 2)
