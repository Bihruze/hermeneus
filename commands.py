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
from .hermeneus_engine import components, doctor, stop_signals
from .hermeneus_engine.contracts import FeedbackRiskError
from .hermeneus_engine.control import Controller
from .hermeneus_engine.options import Choices, UsageError, add_session_arguments, parse_slash_start, resolve, split_args
from .hermeneus_engine.runtime import SessionBusy, SessionRuntime, lock_holder
from .hermeneus_engine.ipc import SessionClient, SessionUnavailable
from .hermeneus_engine.runtime import socket_path

SLASH_USAGE = ("usage: /translate start [--to LANG] [--source NAME] [--speak] "
               "[--device OUTPUT] [--voice VOICE_ID] | /translate stop | /translate status")

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
                 list_sources: Optional[Callable] = None) -> None:
        self.ctx = ctx
        self._services_factory = services_factory
        self._settings = settings
        self.controller = Controller(data_dir or glue.data_dir, owner=owner)
        self._build = build
        self._list_sources = list_sources or components.list_sources

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

    async def start(self, choices: Choices) -> dict:
        options = await self.resolve_options(choices)
        return await self.controller.start(options, self.builder())

    def unload(self) -> None:
        """on_unload: stop a session this process owns and remove its socket and lock."""
        self.controller.stop_local(timeout=2.0)

    # -- slash command --------------------------------------------------------

    async def slash(self, raw_args: str = "", **_context: Any) -> str:
        try:
            argv = split_args(raw_args)
        except UsageError as exc:
            return f"{exc}\n{SLASH_USAGE}"
        sub, rest = (argv[0].lower(), argv[1:]) if argv else ("status", [])
        if sub == "start":
            try:
                status = await self.start(Choices.from_namespace(parse_slash_start(rest)))
            except START_ERRORS as exc:
                return f"Could not start translation: {exc}"
            return describe_status(status)
        if rest:
            return f"/translate {sub} takes no arguments.\n{SLASH_USAGE}"
        if sub == "stop":
            return describe_status(await self.controller.stop())
        if sub == "status":
            return describe_status(await self.controller.status())
        return SLASH_USAGE


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
        text += " Spoken translation is on."
    if status.get("message") and status.get("message") not in ("live", state):
        text += f" {status['message']}"
    return text


# -- CLI ------------------------------------------------------------------------

def setup_cli(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="hermeneus_command", metavar="COMMAND")
    p_doc = sub.add_parser("doctor", help="check what live translation needs on this Mac")
    p_doc.add_argument("--json", action="store_true", help="print the checks as JSON")
    p_dev = sub.add_parser("devices", help="list audio sources and output devices")
    p_dev.add_argument("--json", action="store_true", help="print the devices as JSON")
    p_run = sub.add_parser("run", help="show live subtitles here; joins a running session or starts one")
    add_session_arguments(p_run)
    p_run.add_argument("--json", action="store_true", help="print every event as one JSON line")
    p_stop = sub.add_parser("stop", help="stop the running session")
    p_stop.add_argument("--json", action="store_true")
    p_status = sub.add_parser("status", help="show the session state")
    p_status.add_argument("--json", action="store_true")


def cli_handler(plugin: Plugin) -> Callable[[argparse.Namespace], int]:
    def handle(args: argparse.Namespace) -> int:
        command = getattr(args, "hermeneus_command", None)
        if command == "doctor":
            return cmd_doctor(args)
        if command == "devices":
            return cmd_devices(args)
        if command == "run":
            return asyncio.run(cmd_run(plugin, args))
        if command == "stop":
            return _print_status(asyncio.run(plugin.controller.stop()), args)
        if command == "status":
            return _print_status(asyncio.run(plugin.controller.status()), args)
        print("usage: hermes hermeneus {doctor,devices,run,stop,status}", file=sys.stderr)
        return 2
    return handle


def _print_status(status: dict, args: argparse.Namespace) -> int:
    print(json.dumps(status) if getattr(args, "json", False) else describe_status(status))
    return 1 if status.get("state") == "failed" else 0


def cmd_doctor(args: argparse.Namespace, *, out: TextIO = sys.stdout) -> int:
    report = doctor.run(get_secret=glue.get_secret, aux_config=glue.aux_config())
    print(json.dumps(report.to_dict(), indent=2) if args.json else report.render(), file=out)
    return report.exit_code


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


def _format_line(event: dict) -> Optional[str]:
    line = event.get("line") or {}
    state = line.get("state")
    speaker = line.get("speaker_id")
    prefix = f"[{speaker}] " if speaker else ""
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
            text = _format_line(event)
            if text:
                print(text, file=self.out, flush=True)
        elif kind == "relabel" and not self.as_json:
            print(f"-- {event.get('from')} is now {event.get('to')}", file=self.err, flush=True)


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
        runtime = SessionRuntime(data_dir, options, plugin.builder(), owner="cli")
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

        async def pump() -> None:
            async for event in sub:
                printer(event)

        pump_task = asyncio.create_task(pump())
        stop_task = asyncio.create_task(stop.wait())
        closed_task = asyncio.create_task(runtime.session.wait_closed())
        await asyncio.wait({stop_task, closed_task}, return_when=asyncio.FIRST_COMPLETED)
        stop_task.cancel()
        closed_task.cancel()
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
