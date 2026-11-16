from jurialmunkey.parser import del_empty_keys, merge_two_dicts
from jurialmunkey.tmdate import get_timestamp, set_timestamp
from jurialmunkey.window import get_property
from tmdbhelper.lib.api.request import RequestAPI
from tmdbhelper.lib.api.omdb.mapping import ItemMapper
from tmdbhelper.lib.api.api_keys.omdb import API_KEY


def translate_xml(request):
    """ Workaround wrapper for broken ElementTree in Python 3.11.1 """

    if not request:
        return

    from xml.dom.minidom import parseString
    from xml.parsers.expat import ExpatError

    try:
        r = parseString(request.text)
        d = {k: v for k, v in r.firstChild.firstChild.attributes.items() if k and v}
    except AttributeError:
        return
    except ExpatError:
        return

    return {'root': {'movie': [d]}}


OMDB_BLOCK_SECONDS = 1800  # pause after a 401 (rejected key or used-up daily quota), as Seren does


class OMDb(RequestAPI):

    api_key = API_KEY

    def __init__(self, api_key=None):
        api_key = api_key or self.api_key

        super(OMDb, self).__init__(
            req_api_key=f'apikey={api_key}',
            req_api_name='OMDb',
            req_api_url='https://www.omdbapi.com/')
        self.translate_xml = translate_xml  # Temp monkey patch bandaid for broken ElementTree. Remove after upstream fix.
        self._error_notification = False  # Override user settings and always suppress OMDb error notifications since it times-out a lot.
        OMDb.api_key = api_key

    @property
    def unauthorized_property(self):
        """ Window property holding the time until which OMDb is not asked. Named by the key so a new key starts clean. """
        from hashlib import md5
        key_hash = md5(str(OMDb.api_key).encode('utf-8')).hexdigest()[:12]
        return f'OMDb.Unauthorized.{key_hash}'

    def get_simple_api_request(self, request=None, postdata=None, headers=None, method=None):
        """ OMDb answers 401 for a rejected key and for a used-up daily quota, and the same key fails again on the next
        item. The base class only logs the 401 and returns nothing, and the empty result is never cached, so a list of
        items asked OMDb once per item (222 requests for 59 titles in one Shield log). Remember the 401 for a while,
        shared by every plugin call until Kodi restarts, and send nothing to OMDb in the meantime. """
        prop = self.unauthorized_property
        if get_timestamp(get_property(prop, is_type=float)):
            return
        response = super(OMDb, self).get_simple_api_request(request=request, postdata=postdata, headers=headers, method=method)
        if response is not None and response.status_code == 401:
            get_property(prop, set_timestamp(OMDB_BLOCK_SECONDS))
            self.kodi_log(
                f'OMDb rejected the request (401), the API key is invalid or its daily limit is used up.\n'
                f'Pausing OMDb lookups for {OMDB_BLOCK_SECONDS // 60} minutes', 1)
        return response

    def get_request_item(self, imdb_id=None, title=None, year=None, tomatoes=True, fullplot=True, cache_only=False):
        kwparams = {}
        kwparams['i'] = imdb_id
        kwparams['t'] = title
        kwparams['y'] = year
        kwparams['plot'] = 'full' if fullplot else 'short'
        kwparams['tomatoes'] = 'True' if tomatoes else None
        kwparams = del_empty_keys(kwparams)
        request = self.get_request_lc(is_xml=True, cache_only=cache_only, r='xml', **kwparams)
        try:
            request = request['root']['movie'][0]
        except (KeyError, TypeError, AttributeError):
            request = {}
        return request

    def get_ratings_awards(self, imdb_id=None, title=None, year=None, cache_only=False, base_item=None):
        request = self.get_request_item(imdb_id=imdb_id, title=title, year=year, cache_only=cache_only)
        return ItemMapper().get_info(request, base_item=base_item)

    def get_item_ratings(self, item, cache_only=False):
        """ Get ratings for an item using IMDb lookup """
        if not item:
            return

        def _get_item_value(item, key_pairs: list = None, starts_with: str = None):
            for i, j in key_pairs:
                try:
                    value = item[i][j]
                    if not value:
                        continue
                    if starts_with and not value.startswith(starts_with):
                        continue
                    return value
                except (KeyError, AttributeError):
                    continue

        imdb_id = _get_item_value(item, key_pairs=[('infolabels', 'imdbnumber'), ('unique_ids', 'imdb')], starts_with='tt')
        if not imdb_id:
            return item
        ratings = self.get_ratings_awards(imdb_id=imdb_id, cache_only=cache_only)
        imdb_tv_id = _get_item_value(item, key_pairs=[('unique_ids', 'tvshow.tvshow.imdb'), ('unique_ids', 'tvshow.imdb')], starts_with='tt')
        if imdb_tv_id and imdb_tv_id != imdb_id:
            # Also merge base tv show details
            tv_ratings = self.get_ratings_awards(imdb_id=imdb_tv_id, cache_only=cache_only)
            ratings['infoproperties'] = merge_two_dicts(tv_ratings.get('infoproperties', {}), ratings.get('infoproperties', {}))
        item['infoproperties'] = merge_two_dicts(item.get('infoproperties', {}), ratings.get('infoproperties', {}))
        return item
