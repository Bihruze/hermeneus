"""Fake sounddevice pieces for playback tests. Nothing here touches PortAudio."""

from types import SimpleNamespace

import numpy as np

DEVICES = [
    {"name": "Test Built-in Microphone", "max_input_channels": 1, "max_output_channels": 0},
    {"name": "Test Built-in Speakers", "max_input_channels": 0, "max_output_channels": 2},
    {"name": "BlackHole 2ch", "max_input_channels": 2, "max_output_channels": 2},
    {"name": "Test Mono Out", "max_input_channels": 0, "max_output_channels": 1},
]


class FakeClock:
    def __init__(self, start_ns=1_000_000_000):
        self.now = start_ns

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += int(seconds * 1e9)


class FakeStream:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.callback = kwargs["callback"]
        self.channels = kwargs["channels"]
        self.started = False
        self.stopped = False
        self.closed = False
        FakeStream.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True

    def pull(self, frames, dac_offset_s=0.0):
        """Run one callback like PortAudio would and return the block."""
        out = np.full((frames, self.channels), 12345, dtype=np.int16)  # garbage the callback must overwrite
        t = SimpleNamespace(currentTime=10.0, outputBufferDacTime=10.0 + dac_offset_s)
        self.callback(out, frames, t, None)
        return out


def fake_factory(**kwargs):
    return FakeStream(**kwargs)
