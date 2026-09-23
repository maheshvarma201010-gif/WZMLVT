from .... import LOGGER
from ...ext_utils.status_utils import (
    get_readable_file_size,
    EngineStatus,
    MirrorStatus,
)


class TrackManagerStatus:
    def __init__(self, listener, gid):
        self.listener = listener
        self._gid = gid
        self.engine = EngineStatus().STATUS_FFMPEG

    def speed(self):
        return "0B/s"

    def processed_bytes(self):
        return "0B"

    def progress(self):
        return "0%"

    def gid(self):
        return self._gid

    def name(self):
        return self.listener.name

    def size(self):
        return get_readable_file_size(self.listener.size)

    def eta(self):
        return "-"

    def status(self):
        return MirrorStatus.STATUS_TRACKMGR

    def task(self):
        return self

    async def cancel_task(self):
        LOGGER.info(f"Cancelling Track Manager: {self.listener.name}")
        self.listener.is_cancelled = True
        await self.listener.on_upload_error("Track Manager cancelled by user!")
