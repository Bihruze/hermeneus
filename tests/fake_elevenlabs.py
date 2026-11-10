"""A local stand-in for the ElevenLabs multi-context TTS WebSocket, on 127.0.0.1.

It follows the message shapes in
https://elevenlabs.io/docs/api-reference/text-to-speech/v-1-text-to-speech-voice-id-multi-stream-input
and synthesizes deterministic PCM: every sample of a context's audio carries
that context's number, so tests can check routing and order.
"""

import asyncio
import base64
import json
import logging

import numpy as np
from websockets.asyncio.server import serve


_QUIET = logging.getLogger("tests.fake_elevenlabs")
_QUIET.setLevel(logging.WARNING)


class FakeTtsServer:
    def __init__(self, *, chunks_per_context=3, samples_per_chunk=480, audio_delay_s=0.0,
                 delays=None, drop_after_text=0, split_odd=False, idle_close_s=None, reject_status=None,
                 unknown_voices=()):
        self.chunks_per_context = chunks_per_context
        self.samples_per_chunk = samples_per_chunk
        self.audio_delay_s = audio_delay_s
        self.delays = delays or {}          # context_id -> seconds before its audio
        self.drop_after_text = drop_after_text  # abort the socket after this many text messages (once)
        self.split_odd = split_odd          # split audio at an odd byte offset
        self.idle_close_s = idle_close_s
        self.reject_status = reject_status
        self.unknown_voices = set(unknown_voices)  # voice ids answered like the live server on 2026-11-11
        self.connections = []               # list of dicts: path, headers, messages
        self._server = None
        self.port = None
        self._texts_seen = 0
        self._dropped = False

    async def __aenter__(self):
        self._server = await serve(self._handler, "127.0.0.1", 0, process_request=self._process_request,
                                   logger=_QUIET)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    @property
    def base_url(self):
        return f"ws://127.0.0.1:{self.port}"

    def _process_request(self, connection, request):
        if self.reject_status:
            return connection.respond(self.reject_status, "rejected\n")
        return None

    @staticmethod
    def value_for(context_id):
        return int("".join(ch for ch in context_id if ch.isdigit()) or "0")

    def _audio_messages(self, context_id):
        value = self.value_for(context_id)
        out = []
        for _ in range(self.chunks_per_context):
            out.append(np.full(self.samples_per_chunk, value, dtype="<i2").tobytes())
        if self.split_odd:
            joined = b"".join(out)
            out = [joined[:7], joined[7:]]
        return [json.dumps({"audio": base64.b64encode(b).decode(), "contextId": context_id,
                            "alignment": None, "normalizedAlignment": None}) for b in out]

    async def _generate(self, ws, context_id, closing: asyncio.Event):
        await asyncio.sleep(self.delays.get(context_id, self.audio_delay_s))
        for m in self._audio_messages(context_id):
            await ws.send(m)
        await closing.wait()
        await ws.send(json.dumps({"isFinal": True, "contextId": context_id}))

    async def _handler(self, ws):
        conn = {"path": ws.request.path, "headers": dict(ws.request.headers), "messages": []}
        self.connections.append(conn)
        voice = ws.request.path.split("/")[3]
        if voice in self.unknown_voices:
            conn["messages"].append(json.loads(await ws.recv()))
            text = f"A voice with voice_id {voice} does not exist."
            await ws.send(json.dumps({"message": text, "error": "voice_id_does_not_exist", "code": 1008}))
            await ws.close(1008, text)
            return
        closing = {}
        tasks = []
        try:
            while True:
                try:
                    timeout = self.idle_close_s
                    raw = await (asyncio.wait_for(ws.recv(), timeout) if timeout else ws.recv())
                except asyncio.TimeoutError:
                    await ws.close(1008, "inactivity")  # code is a test choice; the docs name none
                    return
                msg = json.loads(raw)
                conn["messages"].append(msg)
                if msg.get("close_socket"):
                    for ev in closing.values():
                        ev.set()
                    await asyncio.gather(*tasks)
                    await ws.close()
                    return
                cid = msg.get("context_id")
                if msg.get("close_context"):
                    closing.setdefault(cid, asyncio.Event()).set()
                    continue
                if msg.get("flush") and msg.get("text", "").strip():
                    self._texts_seen += 1
                    if self.drop_after_text and not self._dropped and self._texts_seen >= self.drop_after_text:
                        self._dropped = True
                        ws.transport.abort()
                        return
                    ev = closing.setdefault(cid, asyncio.Event())
                    tasks.append(asyncio.create_task(self._generate(ws, cid, ev)))
        except Exception:
            return
        finally:
            for t in tasks:
                t.cancel()
