"""Slash command ``/translate`` and CLI command ``hermes hermeneus``.

Nothing here prompts. A missing choice ends the command with a message that
names the flag to pass (catalog rule 12).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Callable, Optional, TextIO

from . import glue
from .hermeneus_engine import components, doctor, overlay_page, stop_signals
from .hermeneus_engine.platform_words import this_machine
from .hermeneus_engine.contracts import FeedbackRiskError
from .hermeneus_engine.control import Controller
from .hermeneus_engine.ipc import SettingsRefused
from .hermeneus_engine.options import (Choices, UsageError, add_session_arguments, add_set_arguments,
                                       changes_from_namespace, parse_slash_set, parse_slash_start, resolve,
                                       resolve_source_change, split_args)
from .hermeneus_engine.tts.voice_catalog import VoiceCatalog, VoiceListError
from .hermeneus_engine.runtime import SessionBusy, SessionRuntime, lock_holder
from .hermeneus_engine.ipc import SessionClient, SessionUnavailable
from .hermeneus_engine.runtime import socket_path
from .hermeneus_engine.overlay.service import OverlayService, OverlayUnavailable, describe as describe_window

SLASH_USAGE = ("usage: /translate start [--to LANG] [--source NAME] [--speak] "
               "[--device OUTPUT] [--voice VOICE_ID] | /translate set [--to LANG] [--mode subtitles|both|voice] "
               "[--speak on|off] [--voice VOICE_ID] [--device OUTPUT] [--source NAME] "
               "| /translate stop | /translate status | /translate voices "
               "| /translate overlay show|hide|status")

# Errors a start can raise that carry a message meant for the user.
START_ERRORS = (UsageError, SessionBusy, FeedbackRiskError, components.ComponentUnavailable, RuntimeError,
                TimeoutError, ValueError)


class Plugin:
    """Per-process plugin state: the Hermes ctx (when there is one) and the session controller."""

    def __init__(self, ctx: Any = None, *, owner: str = "hermes",
                 services_factory: Optional[Callable[[], components.HermesServices]] = None,
                 settings: Optional[Callable[[], dict]] = None,
                 data_dir: Optional[Callable[[], Any]] = None,
                 build: Optional[Callable] = None,
                 list_sources: Optional[Callable] = None,
                 save_setting: Optional[Callable[[str, Any], None]] = None) -> None:
        self.ctx = ctx
        self._services_factory = services_factory
        self._settings = settings
        self.controller = Controller(data_dir or glue.data_dir, owner=owner)
        self._build = build
        self._list_sources = list_sources or components.list_sources
        self.voice_catalog = VoiceCatalog(lambda: glue.get_secret(components.SECRET_NAME))
        self._save_setting = save_setting
        self.overlay = OverlayService(lambda: self.controller.data_dir, self.settings, self.controller.status)

    # -- wiring ---------------------------------------------------------------

    def services(self) -> components.HermesServices:
        if self._services_factory is not None:
            return self._services_factory()
        llm = self.ctx.llm if self.ctx is not None else glue.standalone_llm()
        return glue.services(llm)

    def settings(self) -> dict:
        if self._settings is not None:
            return self._settings()
        return glue.settings(self.ctx)

    def save_setting(self, key: str, value: Any) -> None:
        """Write one plugin setting through Hermes (``ctx.set_config``)."""
        if self._save_setting is not None:
            self._save_setting(key, value)
        elif self.ctx is not None:
            self.ctx.set_config(key, value)
        else:
            raise UsageError("settings can only be changed from a Hermes command")

    def overlay_page(self) -> overlay_page.OverlayPage:
        """The overlay page switch for a session this process owns."""
        return overlay_page.OverlayPage(lambda: self.controller.data_dir, self.settings)

    def builder(self) -> Callable:
        """The component builder for a session; secrets and the LLM are bound when the session starts."""
        if self._build is not None:
            return self._build
        services = self.services()
        return lambda options, on_capture_error: components.build(options, services, on_capture_error)

    async def resolve_options(self, choices: Choices):
        sources, problems = await asyncio.to_thread(self._list_sources)
        try:
            return resolve(choices, self.settings(), sources)
        except UsageError as exc:
            if not sources and problems:
                raise UsageError(f"{exc} ({'; '.join(problems)})") from None
            raise

    async def start(self, choices: Choices, *, platform: Optional[str] = None) -> dict:
        options = await self.resolve_options(choices)
        if self._build is None:
            await asyncio.to_thread(glue.model_preflight)
        status = await self.controller.start(options, self.builder(), overlay_page=self.overlay_page())
        note = await self.overlay.auto(platform)
        return {**status, "overlay_message": note} if note else status

    async def set(self, changes: dict) -> dict:
        """Change the running session. Answers at once; raises UsageError or SettingsRefused.

        A source name is matched against the sources this Mac offers now, so
        the session receives the full source and never lists devices itself.
        """
        if not changes:
            raise UsageError("nothing to change. Pass at least one of --to, --mode, --speak, --voice, "
                             "--device, --source.")
        changes = dict(changes)
        if changes.get("source") is not None:
            sources, problems = await asyncio.to_thread(self._list_sources)
            try:
                changes["source"] = resolve_source_change(changes["source"], sources)
            except UsageError as exc:
                if not sources and problems:
                    raise UsageError(f"{exc} ({'; '.join(problems)})") from None
                raise
        return await self.controller.set(changes)

    async def voices(self, refresh: bool = False) -> dict:
        """The account's voices (cached briefly). Raises VoiceListError with a plain message."""
        result = await asyncio.to_thread(self.voice_catalog.list, refresh=refresh)
        return result.to_dict()

    def unload(self) -> None:
        """on_unload: stop a session this process owns and remove its socket and lock."""
        self.controller.stop_local(timeout=2.0)

    # -- slash command --------------------------------------------------------

    async def slash(self, raw_args: str = "", **context: Any) -> str:
        try:
            argv = split_args(raw_args)
        except UsageError as exc:
            return f"{exc}\n{SLASH_USAGE}"
        sub, rest = (argv[0].lower(), argv[1:]) if argv else ("status", [])
        if sub == "start":
            try:
                status = await self.start(Choices.from_namespace(parse_slash_start(rest)),
                                          platform=context.get("platform"))
            except START_ERRORS as exc:
                return f"Could not start translation: {exc}"
            note = status.get("overlay_message")
            return describe_status(status) + (f" {note}" if note else "")
        if sub == "set":
            try:
                result = await self.set(changes_from_namespace(parse_slash_set(rest)))
            except (UsageError, SettingsRefused) as exc:
                return f"Not changed: {exc}"
            return describe_set(result)
        if sub == "overlay":
            return await self.slash_overlay(rest, context.get("platform"))
        if rest:
            return f"/translate {sub} takes no arguments.\n{SLASH_USAGE}"
        if sub == "stop":
            return describe_status(await self.controller.stop())
        if sub == "status":
            return describe_status(await self.controller.status())
        if sub == "voices":
            try:
                return describe_voices(await self.voices())
            except VoiceListError as exc:
                return str(exc)
        return SLASH_USAGE

    async def slash_overlay(self, rest: list, platform: Optional[str]) -> str:
        action = rest[0].lower() if rest else "status"
        if len(rest) > 1 or action not in ("show", "hide", "status"):
            return SLASH_USAGE
        try:
            if action == "show":
                return describe_window(await self.overlay.show(platform))
            if action == "hide":
                return describe_window(await self.overlay.hide())
        except OverlayUnavailable as exc:
            return f"The subtitle overlay did not open: {exc}."
        return describe_window(self.overlay.status())


def describe_status(status: dict) -> str:
    state = status.get("state", "idle")
    if state == "idle":
        msg = status.get("message") or ""
        return "Translation is off." + (f" ({msg})" if msg and msg != "stopped" else "")
    parts = [f"Translation is {state}"]
    source = status.get("source") or {}
    if source.get("label"):
        parts.append(f"from {source['label']}")
    if status.get("target_language"):
        parts.append(f"into {status['target_language']}")
    text = " ".join(parts) + "."
    if status.get("speak"):
        voice = f" (voice {status['voice_id']}" if status.get("voice_id") else ""
        device = (f", output {status['output_device']})" if status.get("output_device") else ")") if voice else ""
        text += (" Spoken translation is on, subtitles hidden" if status.get("mode") == "voice"
                 else " Spoken translation is on") + voice + device + "."
    if status.get("applying"):
        text += " Applying: " + ", ".join(_FIELD_NAMES.get(f, f) for f in status["applying"]) + "."
    if status.get("message") and status.get("message") not in ("live", state):
        text += f" {status['message']}"
    return text


_FIELD_NAMES = {"source": "source", "speak": "spoken translation", "voice_id": "voice",
                "output_device": "output device", "target_language": "subtitle language", "mode": "mode"}
_MODE_TEXT = {"subtitles": "subtitles only", "both": "subtitles and voice", "voice": "voice only (subtitles hidden)"}


def describe_set(result: dict) -> str:
    """One line for an accepted change: what is in effect now, what is still being applied."""
    accepted = result.get("accepted") or {}
    applying = result.get("applying") or []
    parts = []
    if "target_language" in accepted:
        parts.append(f"subtitles now go into {accepted['target_language']} from the next sentence")
    if "mode" in accepted:
        parts.append(f"mode is {_MODE_TEXT.get(accepted['mode'], accepted['mode'])}")
    text = ("Changed: " + "; ".join(parts) + ".") if parts else ""
    if applying:
        names = ", ".join(_FIELD_NAMES.get(f, f) for f in applying)
        text += (" " if text else "") + (f"Applying {names} in the background; "
                                         "/translate status shows the result.")
    if not text:
        text = "Nothing changed: the session already runs with these settings."
    return text


def describe_voices(payload: dict) -> str:
    voices = payload.get("voices") or []
    if not voices:
        return "No voices on this ElevenLabs account."
    rows = []
    for v in voices:
        langs = ", ".join(v.get("flash_languages") or []) or "no verified Flash v2.5 languages listed"
        default = " (default)" if v.get("is_default") else ""
        rows.append(f"  {v['name']}{default}  {v['voice_id']}  [{v.get('category') or 'unknown'}]  {langs}")
    return "Voices on this ElevenLabs account (pass one with --voice):\n" + "\n".join(rows)


# -- CLI ------------------------------------------------------------------------

def setup_cli(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="hermeneus_command", metavar="COMMAND")
    p_doc = sub.add_parser("doctor", help=f"check what live translation needs on {this_machine()}")
    p_doc.add_argument("--json", action="store_true", help="print the checks as JSON")
    p_doc.add_argument("--no-audio-check", action="store_true",
                       help=("skip the capture check, which plays a short quiet test tone (on BlackHole or "
                             "VB-Cable when installed, so you do not hear it)") if sys.platform == "darwin" else
                       "accepted for scripts; the Windows doctor has no capture check to skip")
    p_doc.add_argument("--test-model", action="store_true",
                       help="also make one tiny call on the live_translate model (spends a few tokens)")
    p_dev = sub.add_parser("devices", help="list audio sources and output devices")
    p_dev.add_argument("--json", action="store_true", help="print the devices as JSON")
    p_run = sub.add_parser("run", help="show live subtitles here; joins a running session or starts one")
    add_session_arguments(p_run)
    p_run.add_argument("--json", action="store_true", help="print every event as one JSON line")
    p_stop = sub.add_parser("stop", help="stop the running session")
    p_stop.add_argument("--json", action="store_true")
    p_status = sub.add_parser("status", help="show the session state")
    p_status.add_argument("--json", action="store_true")
    p_set = sub.add_parser("set", help="change the running session: language, mode, voice, output, source")
    add_set_arguments(p_set)
    p_set.add_argument("--json", action="store_true")
    p_voices = sub.add_parser("voices", help="list the voices on your ElevenLabs account")
    p_voices.add_argument("--json", action="store_true")
    p_voices.add_argument("--refresh", action="store_true", help="ask ElevenLabs again instead of the 60 s cache")
    p_overlay = sub.add_parser("overlay", help=("show or hide the floating subtitle overlay on this Mac" if sys.platform == "darwin" else
                                     "the floating subtitle overlay (macOS only; use the Desktop pane or overlay-page)"))
    p_overlay.add_argument("overlay_action", nargs="?", choices=("show", "hide", "status"), default="status")
    p_overlay.add_argument("--json", action="store_true")
    p_page = sub.add_parser("overlay-page", help="serve live subtitles on a 127.0.0.1 page for OBS and second "
                                                 "screens; with no argument, show its address")
    p_page.add_argument("switch", nargs="?", choices=("on", "off"),
                        help="on serves the page during every session (it is off by default); off stops it")
    p_page.add_argument("--port", type=int, default=None,
                        help=f"127.0.0.1 port for the page (default {overlay_page.DEFAULT_PORT})")
    p_page.add_argument("--json", action="store_true")


def cli_handler(plugin: Plugin) -> Callable[[argparse.Namespace], int]:
    def handle(args: argparse.Namespace) -> int:
        command = getattr(args, "hermeneus_command", None)
        if command == "doctor":
            return cmd_doctor(args, plugin=plugin)
        if command == "devices":
            return cmd_devices(args)
        if command == "run":
            return asyncio.run(cmd_run(plugin, args))
        if command == "stop":
            return _print_status(asyncio.run(plugin.controller.stop()), args)
        if command == "status":
            return _print_status(asyncio.run(plugin.controller.status()), args)
        if command == "set":
            return cmd_set(plugin, args)
        if command == "voices":
            return cmd_voices(plugin, args)
        if command == "overlay":
            return asyncio.run(cmd_overlay(plugin, args))
        if command == "overlay-page":
            return cmd_overlay_page(plugin, args)
        print("usage: hermes hermeneus {doctor,devices,run,stop,status,set,voices,overlay,overlay-page}",
              file=sys.stderr)
        return 2
    return handle


def cmd_set(plugin: "Plugin", args: argparse.Namespace, *, out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    as_json = bool(getattr(args, "json", False))
    try:
        result = asyncio.run(plugin.set(changes_from_namespace(args)))
    except (UsageError, SettingsRefused) as exc:
        if as_json:
            print(json.dumps({"ok": False, "message": str(exc)}), file=out)
        else:
            print(f"hermeneus: not changed: {exc}", file=err)
        return 2 if isinstance(exc, UsageError) else 1
    print(json.dumps(result) if as_json else describe_set(result), file=out)
    return 0


def cmd_voices(plugin: "Plugin", args: argparse.Namespace, *, out: TextIO = sys.stdout,
               err: TextIO = sys.stderr) -> int:
    try:
        payload = asyncio.run(plugin.voices(refresh=bool(getattr(args, "refresh", False))))
    except VoiceListError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "message": str(exc)}), file=out)
        else:
            print(f"hermeneus: {exc}", file=err)
        return 1
    print(json.dumps(payload, indent=2) if getattr(args, "json", False) else describe_voices(payload), file=out)
    return 0


async def cmd_overlay(plugin: Plugin, args: argparse.Namespace, *, out: TextIO = sys.stdout,
                      err: TextIO = sys.stderr) -> int:
    action = getattr(args, "overlay_action", "status")
    try:
        if action == "show":
            result = await plugin.overlay.show("cli")
        elif action == "hide":
            result = await plugin.overlay.hide()
        else:
            result = plugin.overlay.status()
    except OverlayUnavailable as exc:
        print(f"hermeneus: the subtitle overlay did not open: {exc}", file=err)
        return 1
    print(json.dumps(result) if getattr(args, "json", False) else describe_window(result), file=out)
    return 0


def _print_status(status: dict, args: argparse.Namespace) -> int:
    print(json.dumps(status) if getattr(args, "json", False) else describe_status(status))
    return 1 if status.get("state") == "failed" else 0


def model_checks(test_model: bool, llm: Any = None) -> list:
    """Doctor lines for the translation model: a missing Hermes extra (no tokens), then an opt-in live call.

    Hermes has no public way to resolve the auxiliary route without calling
    it: the resolvers in agent/auxiliary_client.py are private and that
    module is not on the plugin API list (public-api.md), so a live check
    needs one real call and runs only with --test-model.
    """
    checks = []
    provider = glue._aux_provider()
    extra = glue.missing_model_extra(provider)
    if extra:
        checks.append(doctor.Check("translation model feature", False,
                                   f"the live_translate provider ({provider}) needs Hermes' {extra!r} feature",
                                   f"run: {glue._install_hint(extra)}"))
    if test_model:
        ok, detail = asyncio.run(glue.test_model(llm if llm is not None else glue.standalone_llm()))
        checks.append(doctor.Check("translation model (live call)", ok, detail,
                                   "" if ok else "pick another model with: hermes model "
                                   "(Configure auxiliary models > Live translation)"))
    return checks


def cmd_doctor(args: argparse.Namespace, *, out: TextIO = sys.stdout, plugin: Optional["Plugin"] = None) -> int:
    report = doctor.run(get_secret=glue.get_secret, aux_config=glue.aux_config(),
                        audio_check=not bool(getattr(args, "no_audio_check", False)))
    llm = plugin.ctx.llm if plugin is not None and plugin.ctx is not None else None
    report.checks.extend(model_checks(bool(getattr(args, "test_model", False)), llm))
    if plugin is not None:
        report.checks.append(overlay_check(plugin.settings(), asyncio.run(plugin.controller.overlay())))
    print(json.dumps(report.to_dict(), indent=2) if args.json else report.render(), file=out)
    return report.exit_code


def overlay_check(settings: dict, info: Optional[dict]) -> doctor.Check:
    """The doctor line for the overlay page: off, waiting for a session, its address, or why it failed."""
    name = "overlay page"
    if info and info.get("running"):
        return doctor.Check(name, True, f"serving on {info['url']} (127.0.0.1 only; the token changes "
                                        "every session)", required=False)
    if info and info.get("error"):
        return doctor.Check(name, False, info["error"], "hermes hermeneus overlay-page on --port <port>",
                            required=False)
    if not overlay_page.enabled_in(settings):
        return doctor.Check(name, True, "off (turn it on with: hermes hermeneus overlay-page on)", required=False)
    try:
        port = overlay_page.port_from_settings(settings)
    except overlay_page.OverlayPageError as exc:
        return doctor.Check(name, False, str(exc), "hermes hermeneus overlay-page on --port <port>",
                            required=False)
    return doctor.Check(name, True, f"on; it starts with the next session on 127.0.0.1:{port}", required=False)


def describe_overlay(settings: dict, info: Optional[dict]) -> str:
    if info and info.get("running"):
        return (f"Overlay page: {info['url']}\n"
                f"Add it to OBS as a Browser source, or open it on {this_machine()}. It serves 127.0.0.1 only and "
                "the address changes every session.")
    if info and info.get("error"):
        return f"Overlay page: {info['error']}"
    if not overlay_page.enabled_in(settings):
        return "Overlay page: off. Turn it on with: hermes hermeneus overlay-page on"
    try:
        port = overlay_page.port_from_settings(settings)
    except overlay_page.OverlayPageError as exc:
        return f"Overlay page: {exc}"
    return (f"Overlay page: on. It starts with the next session on 127.0.0.1:{port}; "
            "run hermes hermeneus overlay-page then to see its address.")


def cmd_overlay_page(plugin: "Plugin", args: argparse.Namespace, *, out: TextIO = sys.stdout,
                     err: TextIO = sys.stderr) -> int:
    """``hermes hermeneus overlay-page [on|off] [--port N]``: save the setting and apply it to a running session."""
    switch = getattr(args, "switch", None)
    port = getattr(args, "port", None)
    try:
        if port is not None:
            port = overlay_page.check_port(port)
            if switch is None:
                raise UsageError("--port goes with on, for example: hermes hermeneus overlay-page on --port 47822")
        if switch is not None:
            if port is not None:
                plugin.save_setting(overlay_page.KEY_PORT, port)
            plugin.save_setting(overlay_page.KEY_ENABLED, switch == "on")
    except (UsageError, overlay_page.OverlayPageError, PermissionError) as exc:
        print(f"hermeneus: {exc}", file=err)
        return 2
    settings = plugin.settings()
    if switch is not None:
        settings = {**settings, overlay_page.KEY_ENABLED: switch == "on",
                    **({overlay_page.KEY_PORT: port} if port is not None else {})}
    on = None if switch is None else switch == "on"
    info = asyncio.run(plugin.controller.overlay(on, port))
    if getattr(args, "json", False):
        print(json.dumps({"enabled": overlay_page.enabled_in(settings), "session": info is not None,
                          **(info or {"running": False, "url": None, "port": None, "error": ""})}), file=out)
    else:
        print(describe_overlay(settings, info), file=out)
    return 1 if info and info.get("error") else 0


def devices_payload(backend: Optional[str] = None, source_id: Optional[str] = None) -> dict:
    """Sources, and the output devices that are safe for spoken translation.

    Outputs are checked against the named source, or against every listed
    source when none is named, so a device that would feed any of them is
    never offered.
    """
    sources, problems = components.list_sources()
    chosen = [s for s in sources if source_id and s.source_id == source_id and (not backend or s.backend == backend)]
    check_against = chosen or sources
    outputs: list = []
    note = ""
    try:
        if check_against:
            per_source = [components.safe_output_devices(s) for s in check_against]
            allowed = set.intersection(*({o.device_id for o in outs} for outs in per_source))
            outputs = [o for o in per_source[0] if o.device_id in allowed]
        else:
            outputs = components.safe_output_devices(None)
    except components.ComponentUnavailable as exc:
        note = str(exc)
    except Exception as exc:  # device inventory failures must not hide the source list
        note = f"could not list output devices: {exc}"
    return {"sources": [{"backend": s.backend, "source_id": s.source_id, "label": s.label, "detail": s.detail}
                        for s in sources],
            "outputs": [{"device_id": o.device_id, "label": o.label} for o in outputs],
            "unavailable": problems, "outputs_note": note}


def cmd_devices(args: argparse.Namespace, *, out: TextIO = sys.stdout,
                payload: Optional[Callable[[], dict]] = None) -> int:
    data = (payload or devices_payload)()
    if args.json:
        print(json.dumps(data, indent=2), file=out)
        return 0
    print("Audio sources (pass one with --source):", file=out)
    for s in data["sources"]:
        print(f"  {s['label']}  [{s['backend']}]  {s['detail']}".rstrip(), file=out)
    if not data["sources"]:
        print("  none", file=out)
    for p in data["unavailable"]:
        print(f"  not available: {p}", file=out)
    print("Output devices for spoken translation (pass one with --device):", file=out)
    for o in data["outputs"]:
        print(f"  {o['label']}", file=out)
    if not data["outputs"]:
        note = data.get("outputs_note")
        print(f"  none{': ' + note if note else ''}", file=out)
    return 0


class SpeakerNumbers:
    """Speaker 1, 2, ... by first appearance, the way the pane numbers them (desktop/plugin.js:110-116, 166-180).

    Scribe ids look like ``s1:speaker_0``; the ``s<N>`` part changes with every
    Scribe session, so the raw id means nothing to a reader.
    """

    def __init__(self) -> None:
        self._numbers: dict[str, int] = {}

    def label(self, speaker_id: Optional[str]) -> Optional[str]:
        if not speaker_id:
            return None
        if speaker_id not in self._numbers:
            self._numbers[speaker_id] = len(set(self._numbers.values())) + 1
        return f"Speaker {self._numbers[speaker_id]}"

    def relabel(self, from_id: str, to_id: str) -> None:
        """The surviving speaker keeps its number; a new id inherits the merged one's."""
        if to_id not in self._numbers and from_id in self._numbers:
            self._numbers[to_id] = self._numbers[from_id]


def _format_line(event: dict, speakers: Optional[SpeakerNumbers] = None) -> Optional[str]:
    line = event.get("line") or {}
    state = line.get("state")
    label = (speakers or SpeakerNumbers()).label(line.get("speaker_id"))
    prefix = f"[{label}] " if label else ""
    if state == "translated":
        return f"{prefix}{line.get('translation', '')}"
    if state == "failed":
        return f"{prefix}{line.get('original', '')}  (not translated: {line.get('error', '')})"
    return None


class _Printer:
    def __init__(self, as_json: bool, out: TextIO, err: TextIO) -> None:
        self.as_json = as_json
        self.out = out
        self.err = err
        self.last_state = ""
        self.speakers = SpeakerNumbers()

    def __call__(self, event: dict) -> None:
        if self.as_json:
            print(json.dumps(event, ensure_ascii=False), file=self.out, flush=True)
            return
        kind = event.get("type")
        if kind in ("hello", "status"):
            body = event.get("session", event) if kind == "hello" else event
            if body.get("state") != self.last_state:
                self.last_state = body.get("state", "")
                print(f"-- {describe_status(body)}", file=self.err, flush=True)
        elif kind == "line":
            text = _format_line(event, self.speakers)
            if text:
                print(text, file=self.out, flush=True)
        elif kind == "relabel":
            self.speakers.relabel(str(event.get("from")), str(event.get("to")))


async def cmd_run(plugin: Plugin, args: argparse.Namespace, *, out: TextIO = sys.stdout,
                  err: TextIO = sys.stderr, install_signals: bool = True) -> int:
    printer = _Printer(bool(args.json), out, err)
    data_dir = plugin.controller.data_dir
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def on_signal() -> None:
        stop.set()

    uninstall_signals = stop_signals.install(loop, on_signal) if install_signals else None
    try:
        if lock_holder(data_dir):
            return await _attach(data_dir, printer, stop, args)
        choices = Choices.from_namespace(args)
        try:
            options = await plugin.resolve_options(choices)
        except UsageError as exc:
            print(f"hermeneus: {exc}", file=err)
            return 2
        runtime = SessionRuntime(data_dir, options, plugin.builder(), owner="cli", overlay_page=plugin.overlay_page())
        sub = runtime.bus.subscribe()
        try:
            await runtime.start()
        except SessionBusy:
            sub.close()
            return await _attach(data_dir, printer, stop, args)
        except START_ERRORS as exc:
            sub.close()
            print(f"hermeneus: could not start: {exc}", file=err)
            return 1
        page = runtime.overlay_page.info()
        if page["running"] or page["error"]:
            print(f"-- {describe_overlay(plugin.settings(), page)}", file=err, flush=True)
        note = await plugin.overlay.auto("cli")
        if note:
            print(f"-- {note}", file=err, flush=True)

        async def pump() -> None:
            async for event in sub:
                printer(event)

        pump_task = asyncio.create_task(pump())
        # Three ways to end: Ctrl+C or SIGTERM here, a stop from another
        # process over the socket (hermes hermeneus stop, /translate stop, the
        # pane), or the session ending by itself.
        waits = {asyncio.create_task(stop.wait()), asyncio.create_task(runtime.wait_stop_requested()),
                 asyncio.create_task(runtime.session.wait_closed())}
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        for task in waits:
            task.cancel()
        failed = runtime.session.state == "failed"
        await runtime.close()
        await asyncio.wait({pump_task}, timeout=0.5)
        pump_task.cancel()
        return 1 if failed else 0
    finally:
        if uninstall_signals is not None:
            uninstall_signals()


async def _attach(data_dir, printer: _Printer, stop: asyncio.Event, args: argparse.Namespace) -> int:
    """Print another process's session. Ctrl+C detaches; the session keeps running."""
    if any(getattr(args, k, None) for k in ("to", "source", "speak", "device", "voice")):
        print("hermeneus: a session is already running; showing it (start flags ignored). "
              "Stop it first with: hermes hermeneus stop", file=printer.err)
    client = SessionClient(socket_path(data_dir))
    last_state = {"state": ""}

    async def follow() -> None:
        async for event in client.subscribe(after=None):
            if event.get("type") == "status":
                last_state["state"] = event.get("state", "")
            printer(event)

    task = asyncio.create_task(follow())
    stop_task = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop_task.cancel()
        task.cancel()
    if task.done() and not task.cancelled() and isinstance(task.exception(), SessionUnavailable):
        print("hermeneus: the running session is not answering yet; try again in a moment", file=printer.err)
        return 1
    return 1 if last_state["state"] == "failed" else 0
