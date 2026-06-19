"""
Puente de importación tras reorganize_ruteo.py.

El código físico vive en ``routing_engine/routing_engine/`` pero el resto del
proyecto importa ``ruteo.routing_engine.<módulo>``. Este finder redirige esos
imports al paquete interno sin duplicar archivos.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec

_ALIAS_PKG = "ruteo.routing_engine"
_INNER_PKG = f"{_ALIAS_PKG}.routing_engine"


class _RoutingEngineAliasLoader(Loader):
    def __init__(self, alias: str, target: str) -> None:
        self._alias = alias
        self._target = target

    def create_module(self, spec):  # noqa: ANN001
        return None

    def exec_module(self, module) -> None:  # noqa: ANN001
        target_mod = importlib.import_module(self._target)
        module.__dict__.update(target_mod.__dict__)
        module.__package__ = self._alias.rpartition(".")[0] or None
        module.__spec__ = getattr(target_mod, "__spec__", None)
        sys.modules[self._alias] = module


class _RoutingEngineBridgeFinder(MetaPathFinder):
    def find_spec(self, fullname, path, target=None):  # noqa: ANN001
        if fullname == _ALIAS_PKG:
            return None
        if not fullname.startswith(f"{_ALIAS_PKG}."):
            return None
        if fullname.startswith(f"{_INNER_PKG}.") or fullname == _INNER_PKG:
            return None

        suffix = fullname[len(f"{_ALIAS_PKG}.") :]
        inner_name = f"{_INNER_PKG}.{suffix}"
        inner_spec = importlib.util.find_spec(inner_name)
        if inner_spec is None:
            return None
        return ModuleSpec(
            fullname,
            _RoutingEngineAliasLoader(fullname, inner_name),
            is_package=inner_spec.submodule_search_locations is not None,
        )


if not any(isinstance(finder, _RoutingEngineBridgeFinder) for finder in sys.meta_path):
    sys.meta_path.insert(0, _RoutingEngineBridgeFinder())
