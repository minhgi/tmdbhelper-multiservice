""" Kodi's own resume points under the watch services (GitHub issue #13, 7.0.110).

Kodi keeps its own watch history for every item it played, keyed by the item's path (for TMDb Helper that is the
plugin://plugin.video.themoviedb.helper/?info=play&... URL), and draws the stored resume point on top of a list item. A skin
then shows that old Kodi bar (a play stopped part-way on this device) next to what the watch services say, e.g. 60% on an
episode the services show as watched.

While a watch service with its progress indicator on is connected and has synced, the lists show that service's progress
only: for a movie or episode no service holds a resume point for, a resume point Kodi stored for the same play URL is reset.
The reset is Kodi's own Files.SetFileDetails (resume position 0); nothing else of Kodi's data is touched and Kodi's database is
only read here (read-only), to find which play URLs hold a resume point, because one Files.GetFileDetails call takes about
0.45 s. Items that are Kodi library items (they have a dbid) are never touched. An item no service shows as watched keeps its Kodi
resume point for 24 hours after Kodi last played it (the services may not have received that play's pause yet). """
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta

SETTING = 'kodi_marks_follow_services'
URL_PREFIX = 'plugin://plugin.video.themoviedb.helper/?'
BEFORE_LIST = 20    # resets done before the list is handed to Kodi (Files.SetFileDetails takes about 5 ms; the 0.45 s one costs is GetFileDetails, not used)
AFTER_LIST = 40     # resets done after the list is shown, at most (only reached when more than BEFORE_LIST items hold a stale mark)
COOL_OFF_HOURS = 24  # an unwatched item Kodi played more recently than this keeps its Kodi resume point: the service may not have the pause yet
SERVICES = (        # (service setting, progress setting, token settings, sync database)
    ('service_trakt', 'trakt_playprogress', ('trakt_token',), 'TraktSync_v2.db'),
    ('service_mdblist', 'mdblist_playprogress', ('mdblist_token', 'mdblist_apikey'), 'MDbListSync.db'),
    ('service_simkl', 'simkl_playprogress', ('simkl_token',), 'SimklSync.db'),
    ('service_wetrakr', 'wetrakr_playprogress', ('wetrakr_token', 'wetrakr_refresh_token'), 'WeTrakrSync.db'),
)


def _seconds(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _has_synced(filename):
    """True when the service's sync database holds at least one activity stamp, i.e. a sync has completed once."""
    from tmdbhelper.lib.files.futils import FileUtils
    import jurialmunkey.scache
    from tmdbhelper.lib.addon.plugin import get_setting
    folder = jurialmunkey.scache.DATABASE_NAME
    base = f"{get_setting('cache_location', 'str') or ''}{folder}"
    path = FileUtils().get_file_path(base, filename, join_addon_data=base == folder)
    if not path or not os.path.isfile(path):
        return False
    conn = sqlite3.connect('file:' + path.replace('\\', '/') + '?mode=ro', uri=True, timeout=1.0)
    try:
        return conn.execute('SELECT 1 FROM lactivities LIMIT 1').fetchone() is not None
    finally:
        conn.close()


def services_ready():
    """True when at least one watch service is on, connected, has its progress indicator on and has synced once."""
    from tmdbhelper.lib.addon.plugin import get_setting
    for service, progress, tokens, database in SERVICES:
        try:
            if get_setting(service) and get_setting(progress) and any(get_setting(t, 'str') for t in tokens) and _has_synced(database):
                return True
        except Exception:
            continue
    return False


def find_targets(items):
    """[(url, title, watched)] of the list's movies and episodes that no service holds a resume point for and that are not Kodi library
    items. Must run after the services have set their resume points and before the items are added to the directory."""
    from tmdbhelper.lib.addon.plugin import get_setting
    if not get_setting(SETTING):
        return []
    targets = []
    for li in items or []:
        try:
            if not li or li.is_folder or li.infolabels.get('mediatype') not in ('movie', 'episode'):
                continue
            if li.infolabels.get('dbid') or _seconds(li.infoproperties.get('ResumeTime')) > 0:
                continue
            url = li.get_url()
            watched = _seconds(li.infolabels.get('playcount')) >= 1
        except Exception:
            continue
        if url and url.startswith(URL_PREFIX):
            targets.append((url, li.infolabels.get('title') or url, watched))
    return targets


def _video_db():
    import xbmcvfs
    folder = xbmcvfs.translatePath('special://database/')
    best = None
    for name in os.listdir(folder):
        match = re.fullmatch(r'MyVideos(\d+)\.db', name)
        path = os.path.join(folder, name)
        if match and os.path.getsize(path) > 0 and (best is None or int(match.group(1)) > best[0]):
            best = (int(match.group(1)), path)
    return best[1] if best else None


def kodi_resume_urls():
    """{play URL: (seconds, lastPlayed text)} of the resume points Kodi holds for TMDb Helper play URLs (read-only)."""
    path = _video_db()
    if not path:
        return {}
    conn = sqlite3.connect('file:' + path.replace('\\', '/') + '?mode=ro', uri=True, timeout=1.0)
    try:
        rows = conn.execute(
            "SELECT p.strPath, f.strFilename, b.timeInSeconds, f.lastPlayed FROM bookmark b JOIN files f ON f.idFile = b.idFile "
            "JOIN path p ON p.idPath = f.idPath WHERE b.type = 1 AND b.timeInSeconds > 0 AND "
            "(p.strPath LIKE 'plugin://plugin.video.themoviedb.helper/%' OR f.strFilename LIKE 'plugin://plugin.video.themoviedb.helper/%')").fetchall()
    finally:
        conn.close()
    held = {}
    for strpath, strfile, seconds, played in rows:
        joined = f'{strpath or ""}{strfile or ""}'
        start = joined.rfind(URL_PREFIX)
        if start >= 0:
            held[joined[start:]] = (seconds, played)
    return held


def _is_cool(played):
    """True when Kodi played the item longer ago than COOL_OFF_HOURS (or has no play date): a play stopped seconds ago may not have
    reached the services yet, and its Kodi resume point is then the only record of where it stopped."""
    try:
        return datetime.now() - datetime.strptime(str(played)[:19], '%Y-%m-%d %H:%M:%S') > timedelta(hours=COOL_OFF_HOURS)
    except ValueError:
        return True


def stale_marks(items):
    """[(url, title, seconds)] of the list's items whose Kodi resume point is to be reset; [] in the normal case (setting off,
    no service ready, nothing in the list holds a Kodi resume point). Never raises."""
    from tmdbhelper.lib.addon.logger import kodi_log
    try:
        targets = find_targets(items)
        if not targets or not services_ready():
            return []
        held = kodi_resume_urls()
        marks = []
        for url, title, watched in targets:
            if url in held and (watched or _is_cool(held[url][1])):
                marks.append((url, title, held[url][0]))
        return marks
    except Exception as exc:
        kodi_log(f'Kodi marks: not checked - {type(exc).__name__}: {exc}', 2)
        return []


def reset_marks(marks, limit):
    """Reset Kodi's resume point for the first `limit` marks. Returns the marks not handled. Never raises."""
    if not marks:
        return []
    from tmdbhelper.lib.addon.logger import kodi_log
    import xbmc
    for url, title, seconds in marks[:limit]:
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'Files.SetFileDetails',
                   'params': {'file': url, 'media': 'video', 'resume': {'position': 0, 'total': 0}}}
        try:
            answer = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
        except Exception as exc:
            kodi_log(f'Kodi marks: reset of {title} failed - {type(exc).__name__}: {exc}', 2)
            continue
        ok = answer.get('result') == 'OK'
        kodi_log(f'Kodi marks: {title}: no service holds a resume point, Kodi held {round(seconds)} s - '
                 f'{"reset" if ok else "reset FAILED " + json.dumps(answer.get("error"))[:120]} ({url})', 1)
    return marks[limit:]
