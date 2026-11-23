import re
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


def sync_snapshot():
    """ What run_context_option() compares around an option, for a window that opens something else that can change the data (the information dialog) """
    return sync_stamp(), activity_marks()


def sync_changed_since(snapshot):
    """ True when the Trakt / MDBList / Simkl / WeTrakr data may have changed since the snapshot: a stamp or a mark that was unreadable counts as changed """
    stamp_before, marks_before = snapshot
    return stamp_before is None or marks_before is None or sync_stamp() != stamp_before or activity_marks() != marks_before
