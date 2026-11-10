"""Load the plugin directory as a package, the way Hermes's loader and validate probe do
(``importlib.util.spec_from_file_location`` with ``submodule_search_locations``).

The tests' fakes import the engine as top-level ``hermeneus_engine``. So that
the plugin package and the fakes share one engine (one ``SttStatus`` class,
one ``LineState`` enum), the engine's modules are registered under the
package name too before the package runs.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "hermeneus_plugin_under_test"


def share_engine(package_name: str) -> None:
    """Make ``<package_name>.hermeneus_engine.*`` the same modules as top-level ``hermeneus_engine.*``."""
    import hermeneus_engine
    sys.modules[f"{package_name}.hermeneus_engine"] = hermeneus_engine
    for info in pkgutil.walk_packages(hermeneus_engine.__path__, "hermeneus_engine."):
        try:
            module = importlib.import_module(info.name)
        except ImportError:
            continue  # a part whose optional dependency is not installed here
        sys.modules[package_name + "." + info.name] = module


def load_plugin_package():
    if NAME in sys.modules:
        return sys.modules[NAME]
    share_engine(NAME)
    spec = importlib.util.spec_from_file_location(NAME, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[NAME] = module
    spec.loader.exec_module(module)
    importlib.import_module(f"{NAME}.commands")
    importlib.import_module(f"{NAME}.glue")
    return module
