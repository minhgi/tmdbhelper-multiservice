from tmdbhelper.lib.files.ftools import cached_property


class TMDbImagePath:
    """ The quality-dependent paths are read live (plain properties, not cached): one instance lives as long as its mapper, and a long-running
    process (the service) must follow a change of the Artwork quality setting (GitHub issue #9). """

    @property
    def artwork_quality(self):
        from tmdbhelper.lib.addon.plugin import get_artwork_quality
        return get_artwork_quality()

    @property
    def artwork_quality_poster(self):  # poster (and the actor portraits drawn with it) follows the Poster image quality, else the default
        from tmdbhelper.lib.addon.consts import IMAGEPATH_QUALITY_POSTER
        from tmdbhelper.lib.addon.plugin import get_artwork_quality
        return IMAGEPATH_QUALITY_POSTER[get_artwork_quality('poster')]

    @property
    def artwork_quality_fanart(self):  # fanart follows the Fanart image quality, else the default
        from tmdbhelper.lib.addon.consts import IMAGEPATH_QUALITY_FANART
        from tmdbhelper.lib.addon.plugin import get_artwork_quality
        return IMAGEPATH_QUALITY_FANART[get_artwork_quality('fanart')]

    @property
    def artwork_quality_thumbs(self):
        from tmdbhelper.lib.addon.consts import IMAGEPATH_QUALITY_THUMBS
        return IMAGEPATH_QUALITY_THUMBS[self.artwork_quality]

    @property
    def artwork_quality_clogos(self):
        from tmdbhelper.lib.addon.consts import IMAGEPATH_QUALITY_CLOGOS
        return IMAGEPATH_QUALITY_CLOGOS[self.artwork_quality]

    @cached_property
    def artwork_quality_origin(self):
        from tmdbhelper.lib.addon.consts import IMAGEPATH_ORIGINAL
        return IMAGEPATH_ORIGINAL

    @cached_property
    def artwork_quality_negate(self):
        from tmdbhelper.lib.addon.consts import IMAGEPATH_NEGATE
        return IMAGEPATH_NEGATE

    def get_imagepath_poster(self, v):
        if not v:
            return ''
        return f'{self.artwork_quality_poster}{v}'

    def get_imagepath_fanart(self, v):
        if not v:
            return ''
        return f'{self.artwork_quality_fanart}{v}'

    def get_imagepath_thumbs(self, v):
        if not v:
            return ''
        return f'{self.artwork_quality_thumbs}{v}'

    def get_imagepath_clogos(self, v):
        if not v:
            return ''
        return f'{self.artwork_quality_clogos}{v}'

    def get_imagepath_negate(self, v):
        if not v:
            return ''
        return f'{self.artwork_quality_negate}{v}'

    def get_imagepath_origin(self, v):
        if not v:
            return ''
        return f'{self.artwork_quality_origin}{v}'
