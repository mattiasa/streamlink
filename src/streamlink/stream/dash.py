import itertools
import logging
import datetime
import os.path

from streamlink import StreamError, PluginError
from streamlink.compat import urlparse, urlunparse
from streamlink.stream.stream import Stream
from streamlink.stream.wrappers import StreamIOIterWrapper
from streamlink.stream.dash_manifest import MPD, sleeper, sleep_until, utc
from streamlink.stream.ffmpegmux import FFMPEGMuxer
from streamlink.stream.segmented import SegmentedStreamReader, SegmentedStreamWorker, SegmentedStreamWriter

log = logging.getLogger(__name__)


class DASHStreamWriter(SegmentedStreamWriter):
    def __init__(self, reader, *args, **kwargs):
        options = reader.stream.session.options
        kwargs["retries"] = options.get("dash-segment-attempts")
        kwargs["threads"] = options.get("dash-segment-threads")
        kwargs["timeout"] = options.get("dash-segment-timeout")
        SegmentedStreamWriter.__init__(self, reader, *args, **kwargs)

    def fetch(self, segment, retries=None):
        if self.closed or not retries:
            return

        try:
            now = datetime.datetime.now(tz=utc)
            if segment.available_at > now:
                time_to_wait = (segment.available_at - now).total_seconds()
                fname = os.path.basename(urlparse(segment.url).path)
                log.debug("Waiting for segment: {fname} ({wait:.01f}s)".format(fname=fname, wait=time_to_wait))

                sleep_until(segment.available_at)
            return self.session.http.get(segment.url,
                                         timeout=self.timeout,
                                         exception=StreamError)
        except StreamError as err:
            log.error("Failed to open segment {0}: {1}", segment.url, err)
            return self.fetch(segment, retries - 1)

    def write(self, segment, res, chunk_size=8192):
        for chunk in StreamIOIterWrapper(res.iter_content(chunk_size)):
            if not self.closed:
                self.reader.buffer.write(chunk)
            else:
                log.warning("Download of segment: {} aborted".format(segment.url))
                return

        log.debug("Download of segment: {} complete".format(segment.url))


class DASHStreamWorker(SegmentedStreamWorker):
    def __init__(self, *args, **kwargs):
        SegmentedStreamWorker.__init__(self, *args, **kwargs)
        self.mpd = self.stream.mpd
        self.period = self.stream.period

    def iter_segments(self):
        init = True
        back_off_factor = 1
        while not self.closed:
            # find the representation by ID
            representation = None
            for aset in self.mpd.periods[0].adaptationSets:
                for rep in aset.representations:
                    if rep.id == self.reader.representation_id:
                        representation = rep
            refresh_wait = max(self.mpd.minimumUpdatePeriod.total_seconds(),
                               self.mpd.periods[0].duration.total_seconds()) or 5
            with sleeper(refresh_wait * back_off_factor):
                if representation:
                    for segment in representation.segments(init=init):
                        if self.closed:
                            break
                        yield segment
                        # log.debug("Adding segment {0} to queue", segment.url)

                    if self.mpd.type == "dynamic":
                        if not self.reload():
                            back_off_factor = max(back_off_factor * 1.3, 10.0)
                        else:
                            back_off_factor = 1
                    else:
                        return
                    init = False

    def reload(self):
        if self.closed:
            return

        self.reader.buffer.wait_free()
        log.debug("Reloading manifest ({0})".format(self.reader.representation_id))
        res = self.session.http.get(self.mpd.url, exception=StreamError)

        new_mpd = MPD(self.session.http.xml(res, ignore_ns=True),
                      base_url=self.mpd.base_url,
                      url=self.mpd.url,
                      timelines=self.mpd.timelines)

        changed = new_mpd.publishTime > self.mpd.publishTime
        if changed:
            self.mpd = new_mpd

        return changed


class DASHStreamReader(SegmentedStreamReader):
    __worker__ = DASHStreamWorker
    __writer__ = DASHStreamWriter

    def __init__(self, stream, representation_id, *args, **kwargs):
        SegmentedStreamReader.__init__(self, stream, *args, **kwargs)
        self.representation_id = representation_id
        log.debug("Opening DASH reader for: {0}".format(self.representation_id))



class DASHStream(Stream):
    __shortname__ = "dash"

    def __init__(self,
                 session,
                 mpd,
                 video_representation=None,
                 audio_representation=None,
                 period=0):
        super(DASHStream, self).__init__(session)
        self.mpd = mpd
        self.video_representation = video_representation
        self.audio_representation = audio_representation
        self.period = period

    @classmethod
    def parse_manifest(cls, session, url):
        """
        Attempt to parse a DASH manifest file and return its streams

        :param session: Streamlink session instance
        :param url: URL of the manifest file
        :return: a dict of name -> DASHStream instances
        """
        ret = {}
        res = session.http.get(url)
        url = res.url

        urlp = list(urlparse(url))
        urlp[2], _ = urlp[2].rsplit("/", 1)

        mpd = MPD(session.http.xml(res, ignore_ns=True), base_url=urlunparse(urlp), url=url)

        video, audio = [], []

        # Search for suitable video and audio representations
        for aset in mpd.periods[0].adaptationSets:
            if aset.contentProtection:
                raise PluginError("{} is protected by DRM".format(url))
            for rep in aset.representations:
                if rep.mimeType.startswith("video"):
                    video.append(rep)
                elif rep.mimeType.startswith("audio"):
                    audio.append(rep)

        if not video:
            video = [None]

        if not audio:
            audio = [None]

        for vid, aud in itertools.product(video, audio):
            stream = DASHStream(session, mpd, vid, aud)
            stream_name = []

            if vid:
                stream_name.append("{:0.0f}{}".format(vid.height or vid.bandwidth, "p" if vid.height else "k"))
            if audio and len(audio) > 1:
                stream_name.append("a{:0.0f}k".format(aud.bandwidth))
            ret['+'.join(stream_name)] = stream
        return ret

    def open(self):
        if self.video_representation:
            video = DASHStreamReader(self, self.video_representation.id)
            video.open()

        if self.audio_representation:
            audio = DASHStreamReader(self, self.audio_representation.id)
            audio.open()

        if self.video_representation and self.audio_representation:
            return FFMPEGMuxer(self.session, video, audio, copyts=True).open()
        elif self.video_representation:
            return video
        elif self.audio_representation:
            return audio

    def to_url(self):
        return self.mpd.url
