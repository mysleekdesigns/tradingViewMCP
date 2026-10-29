"""Feature registry: every feature a plan may name, all look-ahead-safe.

``REGISTRY`` maps a feature name to its ``FeatureSpec`` and is read-only. Each feature's
output at bar t uses candle rows at or before t only; ``tests/test_registry_lookahead.py``
runs the perturbation test on every entry, so a new feature is covered the moment it is
registered here.

Derivatives features (``lab.registry.derivatives``) read aligned ``drv_*`` columns that the
data layer attaches to the candles; they are risk filters, accepted by the plan validator only
as gating ``and`` clauses of a spot long-only entry in edge (signal) plans, and anywhere in the
``flat_when`` rule of a risk-overlay plan (PLAN.md Phase 15), since crash-risk filtering is
their stated role.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Mapping
from types import MappingProxyType

from lab.registry.derivatives import DERIVATIVES_FEATURE_SPECS, DERIVATIVES_FEATURES
from lab.registry.features import PRICE_FEATURES, fill_params
from lab.registry.regime import REGIME_FEATURES
from lab.types import FeatureSpec


def _build(specs: Iterable[FeatureSpec]) -> Mapping[str, FeatureSpec]:
    table: dict[str, FeatureSpec] = {}
    for spec in specs:
        if spec.name in table:
            raise ValueError(f"duplicate registry feature {spec.name!r}")
        table[spec.name] = spec
    return MappingProxyType(table)


REGISTRY: Mapping[str, FeatureSpec] = _build(
    (*PRICE_FEATURES, *REGIME_FEATURES, *DERIVATIVES_FEATURE_SPECS)
)


def get_feature(name: str) -> FeatureSpec:
    """The registered feature called ``name``.

    Raises ``ValueError`` naming the unknown feature, the closest registered names, and the
    full list so an agent can fix its plan from the message alone.
    """
    try:
        return REGISTRY[name]
    except (KeyError, TypeError):
        pass
    close = difflib.get_close_matches(str(name), list(REGISTRY), n=3, cutoff=0.5)
    hint = f" (did you mean {' or '.join(repr(c) for c in close)}?)" if close else ""
    raise ValueError(
        f"unknown feature {name!r}{hint}; available features: {', '.join(sorted(REGISTRY))}"
    )


__all__ = ["DERIVATIVES_FEATURES", "REGISTRY", "fill_params", "get_feature"]
