"""The README's language lists are generated from hermeneus_engine/languages.py and must not drift."""

import importlib.util
from pathlib import Path

from hermeneus_engine import languages

ROOT = Path(__file__).resolve().parents[1]


def _generator():
    spec = importlib.util.spec_from_file_location("readme_languages", ROOT / "scripts" / "readme_languages.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_readme_language_block_matches_the_code():
    gen = _generator()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert gen.current(readme) is not None, "README.md has no language markers"
    assert gen.current(readme) == gen.block(), "run: python scripts/readme_languages.py --write"


def test_block_lists_every_language_by_name_in_alphabetical_order():
    gen = _generator()
    block = gen.block()
    for code in languages.stt_codes():
        assert languages.display_name(code) in block
    listen, speak = gen._names(languages.stt_codes()), gen._names(languages.tts_codes())
    assert len(listen) == 100 and len(speak) == 32
    for names in (listen, speak):
        assert names == sorted(names, key=gen._sort_key)
        assert ", ".join(names) + "." in block


def test_check_detects_a_changed_list(tmp_path, monkeypatch):
    gen = _generator()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    edited = tmp_path / "README.md"
    edited.write_text(readme.replace("Afrikaans, ", "", 1), encoding="utf-8")
    monkeypatch.setattr(gen, "README", edited)
    assert gen.main(["--check"]) == 1
    monkeypatch.setattr(gen, "README", ROOT / "README.md")
    assert gen.main(["--check"]) == 0
