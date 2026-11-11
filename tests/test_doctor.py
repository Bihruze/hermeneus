from hermeneus_engine import components, doctor


class Backend:
    def __init__(self, name, ok, reason="r"):
        self.name, self._ok, self._reason = name, ok, reason

    def available(self):
        return self._ok, self._reason


def run(monkeypatch, backends, verify=lambda: (True, "audio arrived"), secret="k", mac="15.1",
        multi_output=None):
    monkeypatch.setattr(components, "capture_backends", lambda on_error=None: backends)

    def mo():
        if multi_output is None:
            raise components.ComponentUnavailable("not here")
        return multi_output
    monkeypatch.setattr(components, "detect_multi_output_setup", mo)
    monkeypatch.setattr(doctor.sys, "platform", "darwin")
    return doctor.run(get_secret=lambda name: secret, aux_config={"provider": "nous", "model": "m"},
                      mac_ver=lambda: (mac, "", ""), which=lambda name: None, verify_tap_audio=verify)


def states(report):
    return {c["id"]: c["state"] for c in report.to_dict()["checks"]}


def test_process_tap_working_passes_without_driver_or_multi_output(monkeypatch):
    report = run(monkeypatch, [Backend("virtual-device", False, "no BlackHole"), Backend("process-tap", True)])
    s = states(report)
    assert report.exit_code == 0
    assert s["permission-system-audio-recording"] == "ok"
    assert s["capture-virtual-device"] == "warn"
    assert s["system-sound-reaches-the-virtual-device"] == "ok"


def test_silent_tap_fails_with_the_settings_path(monkeypatch):
    report = run(monkeypatch, [Backend("process-tap", True)], verify=lambda: (False, "only silence"))
    check = next(c for c in report.to_dict()["checks"] if c["id"] == "permission-system-audio-recording")
    assert report.exit_code == 1 and check["state"] == "fail"
    assert "Screen & System Audio Recording" in check["fix"]


def test_fallback_needs_multi_output_and_missing_key_fails_without_printing_it(monkeypatch):
    mo = type("R", (), {"ok": False, "explanation": "system output is MacBook Speakers"})()
    report = run(monkeypatch, [Backend("virtual-device", True), Backend("process-tap", False, "macOS 13")],
                 secret=None, mac="13.6", multi_output=mo)
    s = states(report)
    assert report.exit_code == 1
    assert s["system-sound-reaches-the-virtual-device"] == "fail"
    assert s["elevenlabs-api-key"] == "fail" and s["macos"] == "warn"
    assert "permission-microphone-virtual-device" in s
    text = report.render()
    assert "Audio MIDI Setup" in text


def test_secret_value_is_never_printed(monkeypatch):
    report = run(monkeypatch, [Backend("process-tap", True)], secret="sk_live_secret_value")
    assert "sk_live_secret_value" not in report.render() and "sk_live" not in str(report.to_dict())


def test_the_key_fix_never_puts_the_key_on_a_command_line():
    """A6-14: the key goes in through Tools & Keys or the masked install prompt, never `config set <key>`."""
    def unreadable(name):
        raise RuntimeError("scope unavailable")

    for getter in (unreadable, lambda name: None):
        check = doctor._secret(getter)
        assert check.ok is False and "config set" not in check.fix
    assert "Tools & Keys" in doctor._secret(unreadable).fix
