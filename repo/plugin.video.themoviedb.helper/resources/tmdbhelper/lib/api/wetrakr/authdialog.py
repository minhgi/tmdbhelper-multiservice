""" The dialog of the WeTrakr authorization: a QR code of the activation page (with the code in the link), the address, the code to enter and the time left.
Built from plain Kodi window controls (no skin file), so it looks the same in every skin. If anything about it fails (no temp folder to draw the
picture in, a window that cannot be created) the old text dialog is used instead, so signing in never depends on the QR code. """
import os
import xbmcgui
from tmdbhelper.lib.addon.logger import kodi_log

ACTION_CLOSEWINDOW = (9, 10, 92, 216, 247, 257, 275, 61467, 61448)  # back, previous menu, close, ... (the same list the other windows use)
QR_SCALE = 16  # pixels per module in the picture; it is shown smaller (smooth), which scans better than a picture drawn at its shown size
TEXT = '0xFFFFFFFF'
MUTED = '0xFFB8B8B8'
ACCENT = '[COLOR FF4FC3F7]'


class _AuthWindow(xbmcgui.WindowDialog):
    """ A window that remembers that the user pressed Back (and goes away at once, not at the next poll) """

    def __init__(self):
        self.canceled = False

    def onAction(self, action):
        if action.getId() in ACTION_CLOSEWINDOW:
            self.canceled = True
            try:
                self.close()
            except Exception:
                pass


def _clock(seconds):
    seconds = max(int(seconds), 0)
    return f'{seconds // 60}:{seconds % 60:02d}'


class WeTrakrAuthDialog:
    """ create() / update(percent, seconds_left) / iscanceled() / close(), the calls DialogProgress had in the sign-in loop """

    def __init__(self, url, user_code):
        self._url = url
        self._code = user_code
        self._window = None
        self._status = None
        self._files = []
        self._fallback = None

    @property
    def uses_window(self):
        return self._window is not None

    def create(self):
        try:
            self._create_window()
        except Exception as exc:  # the QR dialog is a nicety: any failure means the text dialog
            kodi_log(f'WeTrakr: QR dialog not available ({type(exc).__name__}: {exc}), using the text dialog', 1)
            self._close_window()
            self._fallback = xbmcgui.DialogProgress()
            self._fallback.create('WeTrakr', f'Visit [B]{self._url}[/B]\nCode: [B]{self._code}[/B]')

    def _picture(self, name, data):
        import xbmcvfs
        path = os.path.join(xbmcvfs.translatePath('special://temp/'), name)
        with open(path, 'wb') as f:
            f.write(data)
        self._files.append(path)
        return path

    @property
    def qr_target(self):
        """ What the QR code opens: the activation page with the code already filled in (the link WeTrakr's own Kodi add-on shows), so scanning it needs
        no typing. The code stays on screen for anyone who types the address instead. """
        from urllib.parse import quote
        return f'{self._url}{"&" if "?" in self._url else "?"}code={quote(self._code, safe="")}&platform=kodi'

    def _qr_matrix(self):
        from tmdbhelper.lib.api.wetrakr import qrcode
        try:
            target = self.qr_target
            return target, qrcode.make_matrix(target)
        except ValueError:  # too long for the encoder: the plain address, the code is then typed
            return self._url, qrcode.make_matrix(self._url)

    def _create_window(self):
        from zlib import crc32
        from tmdbhelper.lib.api.wetrakr import qrcode
        target, matrix = self._qr_matrix()
        qr = self._picture(f'tmdbhelper_wetrakr_qr_{crc32(target.encode()) & 0xFFFFFFFF:08x}.png', qrcode.png_bytes(matrix, scale=QR_SCALE))
        panel = self._picture('tmdbhelper_wetrakr_panel.png', qrcode.solid_png((18, 18, 18, 235)))
        window = _AuthWindow()
        add = window.addControl
        add(xbmcgui.ControlImage(190, 110, 900, 500, panel))
        add(xbmcgui.ControlLabel(230, 135, 820, 40, '[B]Authorize TMDb Helper with WeTrakr[/B]', font='font13', textColor=TEXT))
        add(xbmcgui.ControlImage(230, 200, 320, 320, qr))
        add(xbmcgui.ControlLabel(590, 200, 470, 30, 'Scan the QR code with your phone:', font='font13', textColor=TEXT))
        add(xbmcgui.ControlLabel(590, 230, 470, 30, 'the page opens with the code filled in.', font='font13', textColor=MUTED))
        add(xbmcgui.ControlLabel(590, 280, 470, 30, 'Or open this page on any device:', font='font13', textColor=TEXT))
        add(xbmcgui.ControlLabel(590, 310, 470, 30, f'[B]{self._url}[/B]', font='font13', textColor=TEXT))
        add(xbmcgui.ControlLabel(590, 350, 470, 30, 'and enter this code:', font='font13', textColor=TEXT))
        add(xbmcgui.ControlLabel(590, 380, 470, 50, f'[B]{ACCENT}{self._code}[/COLOR][/B]', font='font30', textColor=TEXT))
        self._status = xbmcgui.ControlLabel(590, 440, 470, 30, 'Waiting for approval...', font='font13', textColor=MUTED)
        add(self._status)
        add(xbmcgui.ControlLabel(590, 480, 470, 30, 'Press Back to cancel', font='font13', textColor=MUTED))
        window.show()
        self._window = window

    def update(self, percent, seconds_left):
        if self._window is not None:
            try:
                self._status.setLabel(f'Waiting for approval... {_clock(seconds_left)} left')
            except Exception:
                pass
        elif self._fallback is not None:
            self._fallback.update(percent)

    def iscanceled(self):
        if self._window is not None:
            return bool(self._window.canceled)
        return bool(self._fallback and self._fallback.iscanceled())

    def _close_window(self):
        window, self._window = self._window, None
        if window is not None:
            try:
                window.close()
            except Exception:
                pass
        for path in self._files:
            try:
                os.remove(path)
            except OSError:
                pass
        self._files = []

    def close(self):
        self._close_window()
        fallback, self._fallback = self._fallback, None
        if fallback is not None:
            fallback.close()
