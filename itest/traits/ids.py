"""Trait ids: the readable slug, its identifier form, and the old ids it replaced.

A trait is addressed by a family-prefixed slug — ``authority.anonymous`` — in the
table, the manifest, the ledger and every generated name. The table's ``code``
(``AUTH-1``) is a display label for narrow columns and never an identity.

Two rules live here, once, with no import of anything else in the engine, so the
manifest loader, the planner, sync, verify and the report can all apply them
without pulling in the trait table:

- :func:`trait_ident` — where a name cannot hold a dot (a Python function, a
  fixture, a file), the dot becomes a double underscore.
- :data:`LEGACY_TRAIT_IDS` — the AN-style ids the first tables used (``A1``,
  ``B2``, …). They read as OWASP ids to a security reader and were ours, so they
  were renamed. This is a **rename map, not a trait rule**: which checks a tool
  gets is still only ``traits.yaml``. Anything written with an old id — a
  manifest, a ledger, a declaration's ``traits:`` list, a binding's call — is
  mapped to the new id on read (:func:`migrate_trait_id`), and the next sync
  writes the new one.
"""

from __future__ import annotations

#: Old id -> slug. Every value is a row of the shipped table (a test pins it).
LEGACY_TRAIT_IDS: dict[str, str] = {
    "A1": "authority.anonymous",
    "A2": "authority.tenant_isolation",
    "A3": "authority.backing_least_privilege",
    "A4": "authority.delegation",
    "B1": "blast.mutation_class",
    "B2": "blast.destructive_gating",
    "B3": "blast.egress",
    "B4": "blast.audit",
    "C1": "containment.parameter_scope",
    "C2": "containment.expression_passthrough",
    "C3": "containment.output_hygiene",
    "D1": "change.inventory",
    "D2": "change.schema_drift",
    "D3": "change.description_drift",
}

#: Slug -> old id: how a legacy name on disk (``test_a1_get_guide``,
#: ``t-<point>-a1``) is recognised as covering the renamed trait.
LEGACY_BY_TRAIT: dict[str, str] = {new: old for old, new in LEGACY_TRAIT_IDS.items()}

#: Old family letter -> family id, for a ledger written before the rename.
LEGACY_FAMILY_IDS: dict[str, str] = {
    "A": "authority",
    "B": "blast",
    "C": "containment",
    "D": "change",
}


def trait_ident(trait_id: str) -> str:
    """The trait as a Python identifier fragment: ``a.b`` -> ``a__b``."""
    return trait_id.replace(".", "__")


def migrate_trait_id(trait_id: str) -> str:
    """The current id for ``trait_id``: an old id maps to its slug; anything
    else — a current slug, an id this build does not know — is returned as is."""
    return LEGACY_TRAIT_IDS.get(trait_id, trait_id)


def migrate_trait_ids(trait_ids: list[str] | None) -> list[str] | None:
    """:func:`migrate_trait_id` over a list, preserving ``None``."""
    if trait_ids is None:
        return None
    return [migrate_trait_id(trait_id) for trait_id in trait_ids]


def migrate_family_id(family_id: str) -> str:
    return LEGACY_FAMILY_IDS.get(family_id, family_id)
