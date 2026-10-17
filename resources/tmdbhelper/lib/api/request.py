import jurialmunkey.reqapi
from tmdbhelper.lib.addon.plugin import get_setting
from tmdbhelper.lib.addon.logger import kodi_log
from tmdbhelper.lib.files.bcache import BasicCache


class RequestAPI(jurialmunkey.reqapi.RequestAPI):
    error_notification = get_setting('connection_notifications')
    _basiccache = BasicCache

    @staticmethod
    def kodi_log(msg, level=0):
        kodi_log(msg, level)

    def get_api_request_json(self, request=None, postdata=None, headers=None, is_xml=False, method=None):
        """ As the jurialmunkey version, but an empty body is "no data" rather than a JSONDecodeError.
        Trakt answers e.g. users/me/stats with HTTP 204 and an empty body under a JSON content-type. """
        request = self.get_api_request(request=request, postdata=postdata, headers=headers, method=method)
        if not request:
            return {}
        try:
            if not is_xml and not request.content:
                return {}
            return self.translate_xml(request) if is_xml else request.json()
        finally:
            request.close()
