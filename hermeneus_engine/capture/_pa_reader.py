"""Read a tap's private aggregate device through PortAudio (sounddevice) in blocking mode.

With blocking reads PortAudio's own C callback fills a ring buffer on the
realtime thread, and Python only drains it, so the GIL never sits in the
realtime path. Measured in the W02 spike: 0 overflows in 600 s at
``latency="high"`` with a tap-only aggregate. ``latency="low"`` overflowed
1329 times in 20 s.

PortAudio lists devices once, when it initialises, so a freshly created
private aggregate is visible only after re-initialising it
(``sd._terminate(); sd._initialize()``, the route the sounddevice
documentation gives for refreshing the device list). Re-initialising closes
every open PortAudio stream in the process, which is why this reader runs only
inside the capture worker process and never in the engine process.
"""

from __future__ import annotations

from typing import Optional

import numpy as np

LATENCY = "high"
BLOCK_FRAMES = 512


class PortAudioUnavailable(RuntimeError):
    pass


class PortAudioStream:
    def __init__(self, stream, rate: int) -> None:
        self._stream = stream
        self.rate = rate
        self.latency_s = float(stream.latency)

    def read(self, frames: int = BLOCK_FRAMES) -> tuple[np.ndarray, bool]:
        data, overflowed = self._stream.read(frames)
        return data[:, 0].copy(), bool(overflowed)

    def close(self) -> None:
        try:
            self._stream.abort()
        finally:
            self._stream.close()


class PortAudioReader:
    def __init__(self) -> None:
        try:
            import sounddevice
        except Exception as exc:  # PortAudio missing or failing to load
            raise PortAudioUnavailable(f"sounddevice is not usable: {exc}") from exc
        self._sd = sounddevice

    def open(self, device_name: str, rate: int) -> PortAudioStream:
        sd = self._sd
        sd._terminate()
        sd._initialize()
        index: Optional[int] = None
        for i, dev in enumerate(sd.query_devices()):
            if dev["name"] == device_name and dev["max_input_channels"] > 0:
                index = i
                break
        if index is None:
            raise PortAudioUnavailable(f"PortAudio does not list the capture device {device_name!r}")
        stream = sd.InputStream(device=index, channels=1, samplerate=rate, dtype="float32",
                                blocksize=0, latency=LATENCY)
        stream.start()
        return PortAudioStream(stream, rate)
