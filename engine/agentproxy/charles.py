import asyncio
import ctypes
import logging
from ctypes import wintypes

from .device import DeviceError

log = logging.getLogger("agentproxy.charles")

CHARLES_PROCESSES = ("charles.exe",)
CHECK_INTERVAL_SECONDS = 5
RESUME_WAIT_SECONDS = 10
RESUME_POLL_SECONDS = 0.5
PAUSED_FOR = "charles"
SNAPSHOT_PROCESSES = 0x2
INVALID_HANDLE = ctypes.c_void_p(-1).value


class ProcessEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


def process_names():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel32.CreateToolhelp32Snapshot(SNAPSHOT_PROCESSES, 0)
    if not snapshot or snapshot == INVALID_HANDLE:
        raise OSError("process snapshot failed, error {0}".format(ctypes.get_last_error()))
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(ProcessEntry)
        names = set()
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            names.add(entry.szExeFile.lower())
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return names
    finally:
        kernel32.CloseHandle(snapshot)


def charles_running(names=process_names):
    return any(name in CHARLES_PROCESSES for name in names())


class CharlesYield:
    def __init__(self, service, set_listening, is_listening, detect=charles_running,
                 interval=CHECK_INTERVAL_SECONDS, resume_wait=RESUME_WAIT_SECONDS):
        self.service = service
        self.set_listening = set_listening
        self.is_listening = is_listening
        self.detect = detect
        self.interval = interval
        self.resume_wait = resume_wait
        self.released = []
        self.detect_failed = False

    async def run(self):
        starting = bool(self.service.paused_for)
        while True:
            try:
                if starting:
                    starting = False
                    await self.pause()
                await self._step()
            except Exception:
                log.exception("charles check step failed; next check in %s s", self.interval)
            await asyncio.sleep(self.interval)

    async def _step(self):
        running = await self._check()
        if running is not None and running != bool(self.service.paused_for):
            if running:
                await self.pause()
            else:
                await self.resume()

    async def _check(self):
        try:
            running = await asyncio.to_thread(self.detect)
        except OSError as error:
            if not self.detect_failed:
                log.warning("charles check failed, proxy state kept: %s", error)
            self.detect_failed = True
            return None
        self.detect_failed = False
        return running

    async def pause(self):
        log.info("charles running: proxy paused")
        self.service.paused_for = PAUSED_FOR
        self.service.listen_epoch += 1
        self.set_listening(False)
        self.service.proxy_running = False
        released = await self._device("release", self.service.device.release)
        if released is not None:
            self.released = released

    async def resume(self):
        log.info("charles closed: proxy resumed")
        self.service.paused_for = None
        self.service.listen_epoch += 1
        self.set_listening(True)
        waited = 0.0
        while waited < self.resume_wait and not await asyncio.to_thread(self.is_listening):
            await asyncio.sleep(RESUME_POLL_SECONDS)
            waited += RESUME_POLL_SECONDS
        if not await asyncio.to_thread(self.is_listening):
            log.warning("proxy did not start listening after charles closed; devices left off")
            self.released = []
            return
        self.service.listen_epoch += 1
        self.service.proxy_running = True
        released, self.released = self.released, []
        if released:
            await self._device("restore", lambda: self.service.device.restore(released))

    def forget(self, serial):
        if serial in self.released:
            self.released.remove(serial)

    async def _device(self, step, action):
        try:
            done = await asyncio.to_thread(action)
        except DeviceError as error:
            log.info("charles device %s skipped: %s", step, error.kind)
            return None
        if done:
            log.info("charles device %s: %s device(s)", step, len(done))
        return done
