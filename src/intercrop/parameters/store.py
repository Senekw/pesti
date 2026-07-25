"""Versioned, content-hashed agronomic parameter sets.

The brief's second rule is that the agent may never fabricate a coefficient at inference
time — if the parameter is not there, it says so. That is enforced structurally: there is no
API on this module that returns a fallback. :meth:`ParameterSet.get` raises
:class:`MissingParameter` with a message written to be reported to the grower verbatim, and
:meth:`ParameterSet.require` additionally refuses values used outside their validity range.

Files are TOML: comments are supported (a parameter file without commentary is unmaintainable)
and ``tomllib`` is in the standard library, so there is no YAML dependency to pin.

The content hash covers parsed semantic content only, so reformatting a comment does not
invalidate every plan revision that referenced the set — while any change to a value, status,
citation, or validity range does.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from intercrop.domain.interactions import InteractionCoefficient
from intercrop.provenance import ParameterRecord, ParameterStatus


class MissingParameter(KeyError):
    """A required agronomic parameter is not in the set.

    Deliberately loud. This is the exception whose message the agent surfaces instead of
    inventing a number, so it names the key and says what would fix it.
    """

    def __init__(self, key: str, set_version: str) -> None:
        self.key = key
        super().__init__(
            f"parameter {key!r} is not in parameter set {set_version}. It has not been "
            "sourced yet. Report the gap — do not substitute a plausible value, and do not "
            "proceed as though the effect were quantified."
        )


class ParameterSetMeta(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str
    maintainer: str | None = None


class ParameterSet(BaseModel):
    """One immutable, hashed collection of agronomic parameters and interactions."""

    model_config = ConfigDict(frozen=True)

    meta: ParameterSetMeta
    records: dict[str, ParameterRecord] = Field(default_factory=dict)
    interactions: tuple[InteractionCoefficient, ...] = ()
    source_path: str | None = None

    @model_validator(mode="after")
    def _keys_match(self) -> ParameterSet:
        for key, record in self.records.items():
            if record.key != key:
                raise ValueError(f"record under {key!r} declares key {record.key!r}")
        seen: set[str] = set()
        for interaction in self.interactions:
            if interaction.key in seen:
                raise ValueError(f"duplicate interaction key {interaction.key!r}")
            seen.add(interaction.key)
        return self

    # -- lookup -----------------------------------------------------------------------

    def get(self, key: str) -> ParameterRecord:
        """Fetch a parameter or raise. There is no default-returning variant, by design."""
        try:
            return self.records[key]
        except KeyError:
            raise MissingParameter(key, self.meta.version) from None

    def require(self, key: str, context: dict[str, Any] | None = None) -> ParameterRecord:
        """Fetch a parameter and assert it applies to ``context``.

        Prefer this over :meth:`get` everywhere the value feeds a model. ``get`` is for
        display and provenance reporting; ``require`` is for computation.
        """
        record = self.get(key)
        record.check_applicable(context or {})
        return record

    def value(
        self, key: str, context: dict[str, Any] | None = None
    ) -> float | int | str | bool | list[float]:
        return self.require(key, context).value

    def find_interactions(
        self,
        *,
        source_crop: str | None = None,
        target_crop: str | None = None,
        pest: str | None = None,
        mechanism: str | None = None,
    ) -> tuple[InteractionCoefficient, ...]:
        """Filter interactions. An empty result is a real answer: no evidence on file."""
        return tuple(
            i
            for i in self.interactions
            if (source_crop is None or i.source_crop_slug == source_crop)
            and (target_crop is None or i.target_crop_slug == target_crop)
            and (pest is None or i.pest_slug == pest)
            and (mechanism is None or str(i.mechanism) == mechanism)
        )

    # -- provenance reporting ---------------------------------------------------------

    @property
    def provisional_keys(self) -> tuple[str, ...]:
        """Every provisional entry. Any output touching one of these must be labelled."""
        keys = [k for k, r in self.records.items() if r.is_provisional]
        keys += [i.key for i in self.interactions if i.is_provisional]
        return tuple(sorted(keys))

    @property
    def unsourced_count(self) -> int:
        return len(self.provisional_keys)

    def citations_for(self, keys: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
        """Key -> citation titles, for the provenance footer on any numeric output."""
        by_key: dict[str, tuple[str, ...]] = {}
        interactions = {i.key: i for i in self.interactions}
        for key in keys:
            entry = self.records.get(key) or interactions.get(key)
            if entry is None:
                raise MissingParameter(key, self.meta.version)
            by_key[key] = tuple(c.title for c in entry.citations)
        return by_key

    # -- hashing ----------------------------------------------------------------------

    def content_hash(self) -> str:
        payload = {
            "version": self.meta.version,
            "records": {
                k: self.records[k].model_dump(mode="json", exclude={"key"})
                for k in sorted(self.records)
            },
            "interactions": [
                i.model_dump(mode="json", exclude={"id"})
                for i in sorted(self.interactions, key=lambda i: i.key)
            ],
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()

    # -- loading ----------------------------------------------------------------------

    @classmethod
    def from_toml(cls, path: str | Path) -> ParameterSet:
        path = Path(path)
        with path.open("rb") as handle:
            raw = tomllib.load(handle)

        meta = ParameterSetMeta.model_validate(raw.get("meta", {}))
        records = {}
        for entry in raw.get("parameter", []):
            record = ParameterRecord.model_validate(entry)
            if record.key in records:
                raise ValueError(f"duplicate parameter key {record.key!r} in {path}")
            records[record.key] = record
        interactions = tuple(
            InteractionCoefficient.model_validate(entry)
            for entry in raw.get("interaction", [])
        )
        return cls(
            meta=meta, records=records, interactions=interactions, source_path=str(path)
        )


def load_default(params_dir: str | Path = "params") -> ParameterSet:
    """Load the highest-versioned parameter file in ``params_dir``.

    Version is taken from the filename (``parameters.v0.1.0.toml``) so the intended set is
    obvious from a directory listing rather than requiring every file to be parsed.
    """
    directory = Path(params_dir)
    candidates = sorted(directory.glob("parameters.v*.toml"))
    if not candidates:
        raise FileNotFoundError(
            f"no parameter file in {directory}. The system cannot make any quantitative "
            "agronomic claim without one."
        )

    def sort_key(path: Path) -> tuple[int, ...]:
        stem = path.stem.removeprefix("parameters.v")
        return tuple(int(part) for part in stem.split("."))

    return ParameterSet.from_toml(max(candidates, key=sort_key))


def assert_no_deprecated_in_use(pset: ParameterSet, keys: tuple[str, ...]) -> None:
    """Guard for new solves: old revisions may reference deprecated parameters, new ones may not."""
    bad = [
        k
        for k in keys
        if (r := pset.records.get(k)) is not None and r.status is ParameterStatus.DEPRECATED
    ]
    if bad:
        raise ValueError(
            "new solves must not use deprecated parameters: " + ", ".join(sorted(bad))
        )
