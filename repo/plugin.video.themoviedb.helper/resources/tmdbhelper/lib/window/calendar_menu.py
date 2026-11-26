import re
import threading
import xbmc
import xbmcgui
from tmdbhelper.lib.addon.logger import kodi_log
from tmdbhelper.lib.window.pagecache import sync_stamp

""" The Context Menu of the calendar windows (7.5.23: shared by the TV calendar and the Movie Calendar, so the rule for "did the option change anything"
exists once). A window shows the row's options (Trakt / MDBList / Simkl / WeTrakr options, Play with ...) and runs the chosen one;
it rebuilds its page only when run_context_option() says something changed.
"""


def activity_marks():
    """ The Trakt / MDBList / Simkl / WeTrakr last-activities marks kept in the Home window: a sync action clears its mark, which is how a change shows before the local data is pulled (7.5.21: WeTrakr's mark too, a WeTrakr option clears it and pulls nothing at once). None when unreadable """
    try:
        from jurialmunkey.window import get_property
        marks = tuple(get_property(name) for name in ('TraktNewSyncLastActivities', 'MDbList.LastActivities', 'Simkl.LastActivities', 'WeTrakr.LastActivities'))
        return marks if any(marks) else None  # no mark at all: a clear could not be seen, so treat as unknown (the page is rebuilt)
    except Exception:
        return None


def run_context_option(entries, name):
    """ entries: the row's context menu [(label, command)]. Shows them, runs the chosen one and returns True when the window has to rebuild its page.
    name: the window's name for the log line. """
    entries = [entry for entry in entries or [] if 'related_lists' not in str(entry[1])]  # Browse lists opens a list in the window under this one: nothing happens here
    if not entries:
        return False
    choice = xbmcgui.Dialog().contextmenu([xbmc.getInfoLabel(label) or label for label, _ in entries])  # names are $ADDON[...] / $LOCALIZE[...] strings
    if choice < 0:
        return False
    command = str(entries[choice][1])
    script = re.match(r'(?is)^\s*runscript\(\s*plugin\.video\.themoviedb\.helper\s*,(.*)\)\s*$', command)
    if not script:  # Play with ... and the like: nothing on the page changes
        xbmc.executebuiltin(command)
        return False
    # RunScript would start the script in its own thread and return at once, before its dialogs and the sync are done: run it here instead,
    # so the page reloads only after the Trakt / MDBList / Simkl / WeTrakr option (Watched, Unwatched, Drop ...) has finished and the row shows the new state
    stamp_before, marks_before = sync_stamp(), activity_marks()
    try:
        from tmdbhelper.lib.script.router import Script
        Script(*[arg.strip() for arg in script.group(1).split(',')]).router()
    except Exception as exc:
        kodi_log(f'{name}: the context menu option {command} failed: {exc}', 1)
    # Skip the rebuild only when nothing at all changed: the sync databases are the same AND none of the last-activities marks moved. The Trakt option
    # (Watched, Unwatched ...) only clears its mark here and the local Trakt database is pulled at the next page build, so the databases alone cannot tell
    if stamp_before is not None and marks_before is not None and sync_stamp() == stamp_before and activity_marks() == marks_before:  # backed out of the options window, or an option that changed nothing: no rebuild (and no busy dialog)
        kodi_log(f'{name}: the context menu option changed no Trakt / MDBList / Simkl / WeTrakr data, the page is left as it is', 1)
        return False
    return True


# The information dialog a calendar opened (Dialog().info, which blocks the window) is parked under the fullscreen video and comes back when the video
# stops (seen on Bingie: 'Video info' left on screen, Estuary ends on Home). Dialog.Close does nothing while it is parked, so this waits for the stop and
# closes it then. It runs in its own thread: the window's close handler joins the watcher thread (stop_watching) and must not wait for a whole movie.
INFO_ACTIVE = 'Window.IsActive(movieinformation) | Window.IsActive(12003)'


def close_info_after_stop(log, name):
    def _run():
        monitor = xbmc.Monitor()
        while xbmc.getCondVisibility('Player.HasVideo'):  # the video plays (started from the calendar), wait for its end
            if monitor.waitForAbort(0.5):
                return
        for _ in range(10):  # the dialog comes back a moment after the stop
            if xbmc.getCondVisibility(INFO_ACTIVE):
                xbmc.executebuiltin('Dialog.Close(movieinformation,true)')
                log(f'{name}: the video ended, the information dialog that came back is closed')
                return
            if monitor.waitForAbort(0.5):
                return
    threading.Thread(target=_run, name=f'{name} close info after stop', daemon=True).start()


class CloseOnPlay(threading.Thread):
    """ 7.5.26: a video started from a calendar (Play in the information dialog, Play with ... in the context menu) opens behind the window, which is
    modal, so the video was out of sight until the calendar was closed by hand. This watches for a video that starts after the window opened and
    closes the window, which shows the video. TMDb Helper's own splash video (dummy.mp4) and a video that was already playing do not count; a
    start that never comes (the player pick is cancelled, the stream fails) leaves the calendar open. """

    def __init__(self, window, name):
        super().__init__(name=f'{name} close on play', daemon=True)
        self._window = window
        self._log_name = name
        self._stop_event = threading.Event()
        self._monitor = xbmc.Monitor()
        self._player = xbmc.Player()
        self._first = self._playing_file()  # whatever already plays when the window opens
        self.fired = False  # True once a video started and the window was closed for it: a window code that follows must not reload its page

    def _playing_file(self):
        """ Path of the file that plays now, '' when no video plays (or it ended while this asked) """
        if not xbmc.getCondVisibility('Player.HasVideo'):
            return ''
        try:
            return self._player.getPlayingFile() or ''
        except RuntimeError:  # Kodi is not playing any file: the splash video just ended
            return ''

    def stop_watching(self):
        self._stop_event.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=1)

    def run(self):
        while not self._stop_event.is_set() and not self._monitor.waitForAbort(0.5):
            path = self._playing_file()
            if not path or path == self._first or path.endswith('dummy.mp4'):
                continue
            window, self._window = self._window, None
            if window is not None and not self._stop_event.is_set():
                self.fired = True
                kodi_log(f'{self._log_name}: a video started, the window is closed so it shows', 1)
                try:
                    window.close()
                except Exception as exc:
                    kodi_log(f'{self._log_name}: could not close the window - {exc}', 1)
                close_info_after_stop(lambda text: kodi_log(text, 1), self._log_name)  # 7.5.29
            return


def sync_snapshot():
    """ What run_context_option() compares around an option, for a window that opens something else that can change the data (the information dialog) """
    return sync_stamp(), activity_marks()


def sync_changed_since(snapshot):
    """ True when the Trakt / MDBList / Simkl / WeTrakr data may have changed since the snapshot: a stamp or a mark that was unreadable counts as changed """
    stamp_before, marks_before = snapshot
    return stamp_before is None or marks_before is None or sync_stamp() != stamp_before or activity_marks() != marks_before
