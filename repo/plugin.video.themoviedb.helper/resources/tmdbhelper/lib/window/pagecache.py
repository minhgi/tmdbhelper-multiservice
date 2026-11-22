import time
import threading
import xbmc
from tmdbhelper.lib.addon.logger import kodi_log

""" The built pages of a calendar window, and the background prefetch that fills them ahead of the user (the TV calendars by day, the movie
calendar by month). After the window lands on a page the neighbours it asks for are built in the background, so paging there shows a finished page.
"""

PAGE_SECONDS = 600  # a built page is reused this long (the window is modal: only a background sync can change what it shows)
PAGE_MAX = 7  # built pages kept
JOIN_SECONDS = 15  # on close: how long the script waits for a prefetch that is running (the window is already gone; a cold page takes 3-6 s)
REFRESH_DELAY = 1.5  # landed on a page whose stored copy is already out of date: build it again this many seconds later (before the neighbours)
WAIT_SECONDS = 90  # a page the prefetch is building right now: how long the window waits for it before building the page itself


SYNC_DATABASES = ('TraktSync_v2.db', 'MDbListSync.db', 'SimklSync.db', 'WeTrakrSync.db')  # where the watched ticks of a page come from
_sync_paths = None


def _database_paths():
    """ The sync databases' files, found the way DataBase finds them (nothing is created: make_dir=False) """
    global _sync_paths
    if _sync_paths is None:
        import jurialmunkey.scache
        from tmdbhelper.lib.files.database import DataBase
        folder = jurialmunkey.scache.DATABASE_NAME
        basefolder = f'{DataBase._basefolder}{folder}'
        _sync_paths = [DataBase._fileutils.get_file_path(basefolder, name, join_addon_data=basefolder == folder, make_dir=False) for name in SYNC_DATABASES]
    return _sync_paths


def sync_stamp():
    """ Changes when a background sync (Trakt / MDBList / Simkl / WeTrakr) wrote to its local database, so a stored page that shows watched ticks is rebuilt.
    Per database: the file's time and size, and the time and size of its write-ahead log while that holds data (a write to a database in WAL mode
    changes the log first; an empty log is just an open connection). Building a page writes to none of these (measured), so a page stored after its
    build keeps its stamp. A file that cannot be read gives None: it never changes the stamp over and over. """
    import os
    stamp = []
    try:
        paths = _database_paths()
    except Exception:
        return None
    for path in paths:
        parts = []
        for suffix in ('', '-wal'):
            try:
                found = os.stat(path + suffix)
            except OSError:
                parts.append(0 if suffix else None)  # a missing -wal is the same as an empty one: the last connection to close removes it, the first to open creates it (7.0.120)
                continue
            parts.append((found.st_mtime_ns, found.st_size) if not suffix or found.st_size else 0)
        stamp.append(tuple(parts))
    return tuple(stamp)


STAMP_NAMES = ('Trakt', 'MDBList', 'Simkl', 'WeTrakr')  # the order of SYNC_DATABASES


def _stamp_part(part):
    if part is None:
        return 'missing'
    if not part:
        return 'empty'
    return f'{time.strftime("%H:%M:%S", time.localtime(part[0] / 1e9))}.{int(part[0] % 1000000000 / 1000000):03d}/{part[1]}'


def stamp_text(stamp):
    """ A stamp as text for the log: per database the file's modified time / size and the write-ahead log's (7.0.119, to find what changes it) """
    if not stamp:
        return 'none'
    try:
        return ' | '.join(f'{name} db {_stamp_part(parts[0])} wal {_stamp_part(parts[1])}' for name, parts in zip(STAMP_NAMES, stamp))
    except Exception:  # a log line must never stop a page from being stored
        return str(stamp)[:120]


def stamp_changes(old, new):
    """ Which parts of two stamps differ, as text for the log """
    if not old or not new:
        return 'a stamp could not be read'
    found = []
    try:
        for name, before, after in zip(STAMP_NAMES, old, new):
            for label, x, y in zip(('db', 'wal'), before, after):
                if x != y:
                    found.append(f'{name} {label}')
    except Exception:
        return 'a stamp could not be compared'
    return ', '.join(found) or 'none'


class PageCache:
    """ build(key) -> the page (any object), run in the window and in the prefetch thread; it must not hold the window (a Timer that did would
    keep the window alive past the opener's `del window`): a module function or a functools.partial of plain values.
    neighbours(key) -> [(seconds to wait, key to build), ...] after landing on key; None for a window that pages nowhere.
    stamp() -> a value that changes when the data a page shows changed behind the window's back (sync_stamp): a page stored under another value is
    not reused. Read BEFORE the build: a sync that writes while the page is being built then marks that page stale instead of hiding behind a newer
    stamp (building writes to none of these databases, so a page does not make itself stale). """

    def __init__(self, build, neighbours=None, label='Calendar', stamp=None):
        self._build = build
        self._neighbours = neighbours
        self._label = label
        self._stamp = stamp
        self._lock = threading.Lock()
        self._pages = {}  # key -> (time built, page)
        self._building = {}  # key -> Event set when the build of that page ends (window or prefetch)
        self._foreground = 0  # builds the window waits for right now: a prefetch does not start next to one
        self._generation = 0  # bumped by clear(): a build that began before it must not store its (stale) page
        self._prefetch_lock = threading.Lock()  # one prefetch build at a time
        self._timers = []
        self._closed = False

    def _read_stamp(self):
        try:
            return self._stamp() if self._stamp else None
        except Exception:
            return None

    def _fresh(self, key):  # the caller holds the lock
        entry = self._pages.get(key)
        if entry:
            age = time.time() - entry[0]
            if age >= PAGE_SECONDS:
                kodi_log(f'{self._label}: {key} stored page dropped, {age:.0f} s old', 2)
            else:
                now = self._read_stamp()
                if entry[2] == now:
                    return entry[1]
                kodi_log(f'{self._label}: {key} stored page dropped after {age:.0f} s, sync data changed ({stamp_changes(entry[2], now)}); stored {stamp_text(entry[2])} now {stamp_text(now)}', 1)
        self._pages.pop(key, None)
        return None

    def has(self, key):
        with self._lock:
            return self._fresh(key) is not None

    def clear(self):
        """ A row changed (Watched, Unwatched, Drop ...): every stored page may show it, and a build in progress must not store its old page """
        with self._lock:
            self._generation += 1
            self._pages.clear()

    def page(self, key, foreground=True):
        """ The page: the stored one, the one a prefetch is building right now (waited for) or one built here.
        A prefetch (foreground=False) builds only a page nobody has or is building, and not while the window itself is waiting for a build. """
        with self._lock:
            found = self._fresh(key)
            if found is not None:
                return found
            event = self._building.get(key)
            owner = event is None
            if owner:
                if not foreground and (self._closed or self._foreground):
                    return None
                event = self._building[key] = threading.Event()
                generation = self._generation
                if foreground:
                    self._foreground += 1
        if not owner:
            if not foreground:
                return None
            event.wait(WAIT_SECONDS)
            with self._lock:
                found = self._fresh(key)
            return found if found is not None else self._build(key)  # the prefetch failed or was cleared meanwhile: build the page here
        try:
            stamp = self._read_stamp()
            started = time.time()
            page = self._build(key)
            with self._lock:
                if generation == self._generation and not self._closed:
                    self._pages[key] = (time.time(), page, stamp)
                    kodi_log(f'{self._label}: {key} stored, built in {time.time() - started:.1f} s ({"window" if foreground else "prefetch"}), stamp read before the build {stamp_text(stamp)}', 2)
                    while len(self._pages) > PAGE_MAX:
                        self._pages.pop(min(self._pages, key=lambda k: self._pages[k][0]))
            return page
        finally:
            with self._lock:
                self._building.pop(key, None)
                if foreground:
                    self._foreground -= 1
            event.set()

    def schedule(self, key):
        """ The window landed on this page: prefetch the pages around it (and stop waiting to prefetch the ones around the page just left).
        The page itself is built again in the background when its stored copy is already out of date, which happens when the build of the
        first page of a window starts its own sync of the connected services, which can write a sync database while the page is built (a Simkl playback sync was seen, 7.0.120): the
        window shows the copy it has, and the next visit to this page finds a current one instead of waiting for a rebuild. """
        self.cancel()
        with self._lock:
            if self._closed:
                return
            timers = [(REFRESH_DELAY, self._refresh, key)]
            if self._neighbours:
                timers += [(delay, self._prefetch, neighbour) for delay, neighbour in self._neighbours(key)]
            for delay, call, target in timers:
                timer = threading.Timer(delay, call, args=(target,))
                timer.daemon = True
                self._timers.append(timer)
                timer.start()

    def _refresh(self, key):
        """ Build the page again only when what is stored for it is out of date by its stamp (nothing stored, or still current: nothing to do) """
        if self._closed:
            return
        with self._lock:
            entry = self._pages.get(key)
            if entry is None or entry[2] == self._read_stamp():
                return
        kodi_log(f'{self._label}: {key} is out of date right after it was built, building it again in the background', 2)
        self._prefetch(key)

    def _prefetch(self, key):
        if self._closed or xbmc.Monitor().abortRequested():
            return
        with self._prefetch_lock:
            if self._closed:
                return
            started = time.time()
            try:
                found = self.page(key, foreground=False)
            except Exception as exc:
                return kodi_log(f'{self._label}: could not prefetch {key}: {exc}', 1)
        if found is not None:
            kodi_log(f'{self._label}: {key} ready ({time.time() - started:.1f} s)', 2)

    def cancel(self):
        """ Drop the prefetches that have not started; returns those timers (one that is running goes on to the end) """
        with self._lock:
            timers, self._timers = self._timers, []
        for timer in timers:
            timer.cancel()
        return timers

    def close(self):
        """ The window is closing: nothing new starts, a running prefetch is given a few seconds to end, its page is dropped """
        with self._lock:
            self._closed = True
            self._pages.clear()
        deadline = time.time() + JOIN_SECONDS
        monitor = xbmc.Monitor()
        for timer in self.cancel():
            while timer.is_alive() and time.time() < deadline and not monitor.abortRequested():  # in short steps: Kodi quitting must not wait for it
                timer.join(0.25)
