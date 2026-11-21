from tmdbhelper.lib.files.ftools import cached_property
from tmdbhelper.lib.addon.logger import kodi_log
import jurialmunkey.dialog as jurialmunkey_dialog
""" Top level module only import plugin/constants/logger """


BusyDialog = jurialmunkey_dialog.BusyDialog
busy_decorator = jurialmunkey_dialog.busy_decorator


class CalendarBusyDialog:
    """ BusyDialog of the calendar windows. It appears only when the block takes longer than SHOW_AFTER seconds: a load that is answered at
    once shows nothing and a load with a delay always shows the dialog (no guessing beforehand which one it will be). It is opened from a
    timer thread and closed with force: a plain Dialog.Close leaves a dialog that was on screen for about a second after the load is done
    (measured 1.05 s in a calendar window). The shared jurialmunkey BusyDialog is not changed, the other users of it keep it. """

    SHOW_AFTER = 0.5

    def __init__(self, is_enabled=True, delay=None):
        import threading
        self.is_enabled = is_enabled
        self._delay = self.SHOW_AFTER if delay is None else delay
        self._lock = threading.Lock()
        self._timer = None
        self._shown = False
        self._stopped = False

    def __enter__(self):
        if self.is_enabled:
            import threading
            timer = threading.Timer(self._delay, self._show)
            timer.daemon = True
            with self._lock:
                self._timer = timer
            timer.start()
        return self

    def _show(self):
        from xbmc import executebuiltin
        with self._lock:
            if self._stopped:
                return
            self._shown = True
            executebuiltin('ActivateWindow(busydialognocancel)')

    def __exit__(self, exc_type, exc_value, exc_traceback):
        with self._lock:
            self._stopped = True
            shown = self._shown
            timer = self._timer
        if timer is not None:
            timer.cancel()
        if shown:
            from xbmc import executebuiltin
            executebuiltin('Dialog.Close(busydialognocancel,true)')


def progress_bg(func):
    def wrapper(self, *args, **kwargs):
        self.dialog_progress_bg = DialogProgressSyncBG()
        self.dialog_progress_bg.heading = f'Updating {self.__class__.__name__}'
        self.dialog_progress_bg.create()
        data = func(self, *args, **kwargs)
        self.dialog_progress_bg.close()
        return data
    return wrapper


class DialogProgressSyncBG:
    max_value = 100
    now_value = 0
    heading = ''

    @cached_property
    def dialog_progress_bg_enabled(self):
        from tmdbhelper.lib.addon.plugin import get_setting
        if not get_setting('sync_notifications'):
            return False
        try:
            from xbmc import Player
            return not Player().isPlayingVideo()  # no sync pop-ups in the middle of a video
        except Exception:
            return True

    @cached_property
    def dialog_progress_bg(self):
        if not self.dialog_progress_bg_enabled:
            return
        from xbmcgui import DialogProgressBG
        return DialogProgressBG()

    def increment(self, x=1):
        self.now_value += x

    def update_or_create(self, progress, message, heading=None):
        if not self.dialog_progress_bg:
            return
        try:
            self.dialog_progress_bg.update(progress, message=message, heading=heading or self.heading)
        except RuntimeError:
            self.dialog_progress_bg.create(heading=heading or self.heading)
            self.dialog_progress_bg.update(progress, message=message)

    def update(self, now_value, message, heading=None):
        if not self.dialog_progress_bg:
            return
        self.now_value = now_value
        self.dialog_progress_bg.update(self.now_value, heading=heading or self.heading, message=message)

    def create(self):
        if not self.dialog_progress_bg:
            return
        self.dialog_progress_bg.create(heading=self.heading)

    def close(self):
        if not self.dialog_progress_bg:
            return
        try:
            self.dialog_progress_bg.close()
        except RuntimeError:
            pass
        del self.dialog_progress_bg

    @property
    def progress(self):
        return int((self.now_value / max(self.max_value, 1)) * 100)

    def set_message(self, message, heading=None):
        if not self.dialog_progress_bg:
            return
        self.update_or_create(self.progress, message=message, heading=heading)


class ProgressDialog(jurialmunkey_dialog.ProgressDialog):
    @staticmethod
    def kodi_log(msg, level=0):
        kodi_log(msg, level)
