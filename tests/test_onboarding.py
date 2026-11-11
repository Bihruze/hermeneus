"""First-run setup for the Desktop pane (onboarding.py): defaults, the audio check and its failures."""

import asyncio
import json
import threading

import pytest

from pipeline_fakes import remove_tmpdir, short_tmpdir
from plugin_loader import load_plugin_package

from hermeneus_engine.capture import tap_doctor

pkg = load_plugin_package()
import importlib
onboarding = importlib.import_module(f"{pkg.__name__}.onboarding")


@pytest.fixture
def data_dir():
    path = short_tmpdir()
    yield path
    remove_tmpdir(path)


# -- language ------------------------------------------------------------------------

@pytest.mark.parametrize("tag,code", [("tr-TR", "tr"), ("pt_BR.UTF-8", "pt"), ("de", "de"), ("fil", "fil"),
                                      ("zh-Hans", "zh"), ("C", None), ("POSIX", None), ("", None),
                                      ("xx-YY", None), (None, None)])
def test_language_code_reads_locale_tags(tag, code):
    assert onboarding.language_code(tag) == code


def test_default_language_order_setting_then_hermes_then_system_then_english():
    pick = onboarding.default_language
    assert pick(saved="de", hermes_language="tr", client_locales=["fr-FR"], system="es_ES") == \
        {"code": "de", "from": "setting"}
    assert pick(saved="", hermes_language="tr", client_locales=["fr-FR"], system="es_ES") == \
        {"code": "tr", "from": "hermes"}
    assert pick(saved="", hermes_language=None, client_locales=["xx", "fr-FR"], system="es_ES") == \
        {"code": "fr", "from": "system"}
    assert pick(saved="", hermes_language="", client_locales=[], system="es_ES.UTF-8") == \
        {"code": "es", "from": "system"}
    assert pick(saved="", hermes_language="", client_locales=None, system="C") == \
        {"code": "en", "from": "fallback"}
    # display.language defaults to en, so en gives way to a known system language.
    assert pick(saved="", hermes_language="en", client_locales=["tr-TR"], system="C") == \
        {"code": "tr", "from": "system"}
    assert pick(saved="", hermes_language="en", client_locales=[], system="C") == {"code": "en", "from": "hermes"}


# -- model ---------------------------------------------------------------------------

def test_model_route_falls_back_to_the_main_model_without_a_live_translate_pin():
    route = onboarding.model_route({}, {"model": {"default": "big-model", "provider": "openrouter"}})
    assert route == {"route": "main", "provider": "openrouter", "model": "big-model", "suggest_faster": True,
                     "ready": True}
    assert onboarding.model_route(None, {"model": "plain-string-model"})["model"] == "plain-string-model"
    assert onboarding.model_route(None, {})["ready"] is False


def test_model_route_uses_the_pin_and_suggests_nothing():
    route = onboarding.model_route({"provider": "anthropic", "model": "small"}, {"model": {"default": "big"}})
    assert route == {"route": "auxiliary", "provider": "anthropic", "model": "small", "suggest_faster": False}


# -- secret --------------------------------------------------------------------------

def test_key_is_set_never_returns_the_value():
    assert onboarding.key_is_set(lambda name: "sk-secret") is True
    assert onboarding.key_is_set(lambda name: "") is False
    assert onboarding.key_is_set(lambda name: None) is False

    def broken(name):
        raise RuntimeError("scope unavailable")

    assert onboarding.key_is_set(broken) is None


def test_setup_state_is_complete_only_with_a_key_and_working_audio():
    model = onboarding.model_route({}, {"model": "m"})
    lang = {"code": "tr", "from": "hermes"}
    state = onboarding.setup_state(key_set=True, audio={"state": "ok"}, model=model, language=lang)
    assert state["complete"] is True
    assert state["key"]["url"] == "https://elevenlabs.io/app/settings/api-keys"
    assert [p["label"] for p in state["key"]["permissions"]] == ["Speech to Text", "Text to Speech", "Voices"]
    assert "sk-" not in json.dumps(state)
    assert onboarding.setup_state(key_set=False, audio={"state": "ok"}, model=model, language=lang)["complete"] is False
    assert onboarding.setup_state(key_set=None, audio={"state": "ok"}, model=model, language=lang)["complete"] is False
    for audio in ("unknown", "running", "denied", "failed"):
        assert onboarding.setup_state(key_set=True, audio={"state": audio}, model=model,
                                      language=lang)["complete"] is False
    # Off macOS (Windows, Linux) there is no System Audio Recording permission to ask for.
    assert onboarding.setup_state(key_set=True, audio={"state": "not_needed"}, model=model,
                                  language=lang, platform="win32")["complete"] is True


def test_on_macos_setup_never_completes_without_the_audio_step():
    """A6-1: on a Mac only a working check, or a macOS without process taps, finishes the audio step."""
    model = onboarding.model_route({}, {"model": "m"})
    lang = {"code": "tr", "from": "hermes"}

    def complete(audio):
        return onboarding.setup_state(key_set=True, audio=audio, model=model, language=lang,
                                      platform="darwin")["complete"]

    assert complete({"state": "ok"}) is True
    assert complete({"state": "not_needed",
                     "message": "capture backend 'process-tap' is not part of this build"}) is False
    assert complete({"state": "not_needed", "message": ""}) is False
    assert complete({"state": "not_needed", "message": f"{onboarding.OLD_MACOS}; this Mac runs 13.6"}) is True


# -- the audio check -----------------------------------------------------------------

def _check(results, data_dir, *, available=(True, ""), deadline_s=5.0):
    calls = []

    def run(timeout_s):
        calls.append(timeout_s)
        result = results.pop(0)
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            return result()
        return result

    check = onboarding.AudioCheck(run, lambda: data_dir, available=lambda: available, deadline_s=deadline_s,
                                  clock=lambda: 1000.0)
    return check, calls


async def test_audio_check_runs_in_the_background_and_reports_ok(data_dir):
    gate = threading.Event()

    def slow():
        gate.wait(2)
        return tap_doctor.DoctorResult(True, tap_doctor.OK, "System audio capture works.")

    check, calls = _check([slow], data_dir)
    assert check.state()["state"] == "unknown"
    started = check.start()
    assert started["state"] == "running"  # answered at once, the check still runs
    assert check.start()["state"] == "running"  # a second press does not start a second tap
    gate.set()
    await check.wait()
    assert check.state() == {"state": "ok", "message": "System audio capture works.", "at": 1000.0}
    assert calls == [onboarding.CHECK_TIMEOUT_S]
    # Kept across a restart.
    again, _ = _check([], data_dir)
    assert again.state()["state"] == "ok"


async def test_audio_check_denied_then_retry_succeeds(data_dir):
    denied = tap_doctor.DoctorResult(False, tap_doctor.PERMISSION_DENIED, "macOS is giving Hermeneus silence")
    ok = tap_doctor.DoctorResult(True, tap_doctor.OK, "works")
    check, calls = _check([denied, ok], data_dir)
    check.start()
    await check.wait()
    assert check.state()["state"] == "denied" and "silence" in check.state()["message"]
    check.start()
    await check.wait()
    assert check.state()["state"] == "ok" and len(calls) == 2


async def test_audio_check_failure_and_crash_are_plain_messages(data_dir):
    failed = tap_doctor.DoctorResult(False, tap_doctor.START_FAILED, "Capture could not start: busy")
    check, _ = _check([failed, RuntimeError("worker died")], data_dir)
    check.start()
    await check.wait()
    assert check.state() == {"state": "failed", "message": "Capture could not start: busy", "at": 1000.0}
    check.start()
    await check.wait()
    assert check.state()["state"] == "failed" and "worker died" in check.state()["message"]
    # A failure is not saved: after a restart the step asks again.
    again, _ = _check([], data_dir)
    assert again.state()["state"] == "unknown"


async def test_audio_check_that_hangs_ends_at_its_deadline(data_dir):
    gate = threading.Event()
    check, _ = _check([lambda: gate.wait(5)], data_dir, deadline_s=0.2)
    check.start()
    await check.wait()
    state = check.state()
    assert state["state"] == "failed" and "did not finish" in state["message"] and "Check again" in state["message"]
    gate.set()


def test_audio_check_not_needed_without_a_process_tap(data_dir):
    check, _ = _check([], data_dir, available=(False, "process taps need macOS 14.2"))
    assert check.state() == {"state": "not_needed", "message": "process taps need macOS 14.2", "at": None}


def test_classify_accepts_the_doctor_pair_too():
    assert onboarding.classify((True, "fine"))["state"] == "ok"
    assert onboarding.classify((False, "nope"))["state"] == "failed"


def test_open_privacy_settings_uses_open_with_the_privacy_url(monkeypatch):
    seen = []
    monkeypatch.setattr(onboarding.sys, "platform", "darwin")
    onboarding.open_privacy_settings(run=seen.append)
    assert seen == [["/usr/bin/open", onboarding.PRIVACY_URL]]
    assert "Privacy_ScreenCapture" in onboarding.PRIVACY_URL


async def test_missing_plugin_libraries_ask_for_one_restart_not_a_skip(data_dir):
    def unavailable():
        raise ModuleNotFoundError("No module named 'objc'", name="objc")

    check = onboarding.AudioCheck(lambda t: None, lambda: data_dir, available=unavailable)
    state = check.state()
    assert state["state"] == "failed" and "Restart gateway" in state["message"] and "objc" in state["message"]
    assert "status bar" in state["message"] and "Quit and reopen" not in state["message"]

    check, _ = _check([ModuleNotFoundError("No module named 'numpy'", name="numpy")], data_dir)
    check.start()
    await check.wait()
    assert check.state()["state"] == "failed" and "numpy" in check.state()["message"]
    assert "Check again" in check.state()["message"]


def test_a_backend_check_that_raises_is_a_failure_not_a_skip(data_dir):
    def broken():
        raise RuntimeError("HAL busy")

    state = onboarding.AudioCheck(lambda t: None, lambda: data_dir, available=broken).state()
    assert state["state"] == "failed" and "HAL busy" in state["message"]


# -- A6-1: a capture module that cannot load after a fresh install ------------------

def _fake_components(monkeypatch, *, load_error=None, available=(True, "ok"), version=(15, 1)):
    components = importlib.import_module(f"{pkg.__name__}.hermeneus_engine.components")
    process_tap = importlib.import_module(f"{pkg.__name__}.hermeneus_engine.capture.process_tap")
    monkeypatch.setattr(onboarding.sys, "platform", "darwin")
    monkeypatch.setattr(process_tap, "macos_version", lambda: version)

    class Backend:
        name = "process-tap"

        def available(self):
            return available

    def capture_backend(name):
        if load_error:
            raise components.ComponentUnavailable(load_error)
        return Backend()

    monkeypatch.setattr(components, "capture_backend", capture_backend)


def test_a_capture_module_missing_after_install_is_failed_with_the_restart_action(monkeypatch, data_dir):
    """The audit's case: Hermes put numpy and pyobjc into a new environment the running backend cannot see."""
    _fake_components(monkeypatch, load_error="capture backend 'process-tap' is not part of this build")
    check = onboarding.AudioCheck(lambda t: None, lambda: data_dir, available=onboarding.process_tap_available)
    state = check.state()
    assert state["state"] == "failed" and state["state"] != "not_needed"
    assert "Restart gateway" in state["message"] and "Check again" in state["message"]
    setup = onboarding.setup_state(key_set=True, audio=state, model={}, language={"code": "en"}, platform="darwin")
    assert setup["complete"] is False


def test_a_missing_module_name_is_named_in_the_restart_message(monkeypatch, data_dir):
    _fake_components(monkeypatch, load_error="capture.process-tap is not part of this build (numpy is missing)")
    state = onboarding.AudioCheck(lambda t: None, lambda: data_dir, available=onboarding.process_tap_available).state()
    assert state["state"] == "failed" and "(numpy)" in state["message"]


def test_pyobjc_missing_on_a_new_mac_is_failed_not_skipped(monkeypatch, data_dir):
    _fake_components(monkeypatch, available=(False, "PyObjC is missing. Install pyobjc-framework-CoreAudio."))
    state = onboarding.AudioCheck(lambda t: None, lambda: data_dir, available=onboarding.process_tap_available).state()
    assert state["state"] == "failed" and "pyobjc-framework-CoreAudio" in state["message"]
    assert "Restart gateway" in state["message"]


def test_a_saved_ok_does_not_hide_libraries_that_went_missing(monkeypatch, data_dir):
    (data_dir / onboarding.STATE_FILE).write_text(json.dumps({"audio": {"state": "ok", "message": "", "at": 1}}))
    _fake_components(monkeypatch, load_error="capture backend 'process-tap' is not part of this build")
    state = onboarding.AudioCheck(lambda t: None, lambda: data_dir, available=onboarding.process_tap_available).state()
    assert state["state"] == "failed"


def test_only_a_macos_without_process_taps_needs_no_permission(monkeypatch, data_dir):
    _fake_components(monkeypatch, version=(13, 6))
    state = onboarding.AudioCheck(lambda t: None, lambda: data_dir, available=onboarding.process_tap_available).state()
    assert state["state"] == "not_needed" and state["message"].startswith(onboarding.OLD_MACOS)
    assert onboarding.audio_step_done(state, "darwin") is True
    _fake_components(monkeypatch)
    state = onboarding.AudioCheck(lambda t: None, lambda: data_dir, available=onboarding.process_tap_available).state()
    assert state["state"] == "unknown"


# -- A6-5: no overlapping checks after the deadline ----------------------------------

async def test_no_second_capture_while_a_timed_out_check_is_still_running(data_dir):
    running, most = [], [0]
    gate = threading.Event()
    ok = tap_doctor.DoctorResult(True, tap_doctor.OK, "works")

    def slow(timeout_s):
        running.append(1)
        most[0] = max(most[0], len(running))
        gate.wait(5)
        running.pop()
        return ok

    check = onboarding.AudioCheck(slow, lambda: data_dir, available=lambda: (True, ""), deadline_s=0.1)
    check.start()
    await check.wait()
    assert check.state()["state"] == "failed" and "did not finish" in check.state()["message"]
    assert check.busy() is True
    again = check.start()  # Check again while the first capture thread is alive
    assert again["state"] == "failed" and "still finishing" in again["message"]
    await asyncio.sleep(0.05)
    assert most[0] == 1
    gate.set()
    for _ in range(100):
        if not check.busy():
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.02)
    # The late answer (the user answered the prompt after the deadline) is kept.
    assert check.state()["state"] == "ok" and check.busy() is False
    assert most[0] == 1


async def test_the_running_marker_lives_exactly_as_long_as_the_capture_thread(data_dir):
    gate = threading.Event()
    seen = []

    def slow(timeout_s):
        seen.append(onboarding.audio_check_running(data_dir))
        gate.wait(5)
        return tap_doctor.DoctorResult(True, tap_doctor.OK, "works")

    check = onboarding.AudioCheck(slow, lambda: data_dir, available=lambda: (True, ""), deadline_s=5)
    assert onboarding.audio_check_running(data_dir) is False
    check.start()
    assert onboarding.audio_check_running(data_dir) is True  # set before the thread is even scheduled
    await asyncio.sleep(0.05)
    gate.set()
    await check.wait()
    assert seen == [True] and onboarding.audio_check_running(data_dir) is False


def test_a_stale_or_dead_marker_does_not_block_a_start(data_dir):
    import os
    path = data_dir / onboarding.MARKER_FILE
    path.write_text(json.dumps({"pid": os.getpid(), "at": 100.0}))
    assert onboarding.audio_check_running(data_dir, now=lambda: 100.0 + 5) is True
    assert onboarding.audio_check_running(data_dir, now=lambda: 100.0 + onboarding.MARKER_STALE_S + 1) is False
    path.write_text(json.dumps({"pid": 2 ** 22 + 12345, "at": 100.0}))
    assert onboarding.audio_check_running(data_dir, now=lambda: 101.0) is False
    path.write_text("not json")
    assert onboarding.audio_check_running(data_dir) is False
