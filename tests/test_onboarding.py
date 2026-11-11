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
    assert onboarding.setup_state(key_set=True, audio={"state": "not_needed"}, model=model,
                                  language=lang)["complete"] is True


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
    assert state["state"] == "failed" and "Quit and reopen Hermes" in state["message"] and "objc" in state["message"]

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
