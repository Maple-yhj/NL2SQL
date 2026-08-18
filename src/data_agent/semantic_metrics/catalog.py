"""Effective metric catalog and deterministic term resolution."""

from __future__ import annotations

from enum import StrEnum

from pydantic import model_validator

from .digest import semantic_digest
from .errors import SemanticMetricError, SemanticMetricErrorCode
from .models import SemanticMetricDefinitionV2
from .ast import MetricAstModel
from .types import NonBlankText


class MetricCatalogOrigin(StrEnum):
    GOVERNED = "governed"
    OVERLAY = "overlay"
    LEGACY = "legacy"


class MetricCatalogEntry(MetricAstModel):
    definition: SemanticMetricDefinitionV2
    origin: MetricCatalogOrigin
    authority_ref: NonBlankText
    definition_digest: NonBlankText

    @model_validator(mode="after")
    def validate_digest(self) -> "MetricCatalogEntry":
        if self.definition_digest != semantic_digest(self.definition):
            raise ValueError("metric catalog entry digest does not match definition")
        return self

    @classmethod
    def create(
        cls,
        *,
        definition: SemanticMetricDefinitionV2,
        origin: MetricCatalogOrigin,
        authority_ref: str,
    ) -> "MetricCatalogEntry":
        return cls(
            definition=definition,
            origin=origin,
            authority_ref=authority_ref,
            definition_digest=semantic_digest(definition),
        )


class MetricResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"


class MetricResolutionMatch(MetricAstModel):
    metric_ref: NonBlankText
    matched_token: NonBlankText
    origin: MetricCatalogOrigin
    definition_digest: NonBlankText


class MetricResolutionResult(MetricAstModel):
    status: MetricResolutionStatus
    requested_term: NonBlankText
    matches: tuple[MetricResolutionMatch, ...] = ()

    @model_validator(mode="after")
    def validate_result_shape(self) -> "MetricResolutionResult":
        if self.status == MetricResolutionStatus.RESOLVED and len(self.matches) != 1:
            raise ValueError("resolved metric terms require exactly one match")
        if self.status == MetricResolutionStatus.UNRESOLVED and self.matches:
            raise ValueError("unresolved metric terms cannot include matches")
        if self.status == MetricResolutionStatus.AMBIGUOUS and len(self.matches) < 2:
            raise ValueError("ambiguous metric terms require at least two matches")
        return self


def normalize_metric_term(value: str) -> str:
    return " ".join(value.strip().casefold().split())


class EffectiveMetricCatalog:
    """Immutable runtime view over governed, overlay, and legacy metrics."""

    __slots__ = ("_digest", "_entries", "_index")

    def __init__(
        self,
        entries: tuple[MetricCatalogEntry, ...],
        *,
        reject_alias_conflicts: bool = True,
    ) -> None:
        ordered = tuple(
            sorted(
                entries,
                key=lambda item: (
                    item.definition.metric_ref.casefold(),
                    item.origin.value,
                    item.authority_ref,
                ),
            )
        )
        metric_refs: dict[str, MetricCatalogEntry] = {}
        index: dict[str, list[tuple[MetricCatalogEntry, str]]] = {}
        for entry in ordered:
            ref_key = normalize_metric_term(entry.definition.metric_ref)
            if ref_key in metric_refs:
                raise SemanticMetricError(
                    SemanticMetricErrorCode.METRIC_CONFLICT,
                    f"duplicate metric ref: {entry.definition.metric_ref}",
                )
            metric_refs[ref_key] = entry
            tokens = (
                entry.definition.metric_ref,
                entry.definition.metric_ref.rsplit(".", 1)[-1],
                entry.definition.display_name,
                *entry.definition.synonyms,
            )
            for token in dict.fromkeys(tokens):
                key = normalize_metric_term(token)
                bucket = index.setdefault(key, [])
                if not any(candidate is entry for candidate, _ in bucket):
                    bucket.append((entry, token))
        conflicts = {
            key: values
            for key, values in index.items()
            if len({item.definition.metric_ref for item, _ in values}) > 1
        }
        if conflicts and reject_alias_conflicts:
            key = sorted(conflicts)[0]
            refs = sorted({item.definition.metric_ref for item, _ in conflicts[key]})
            raise SemanticMetricError(
                SemanticMetricErrorCode.METRIC_CONFLICT,
                f"metric alias {key!r} resolves to multiple refs: {', '.join(refs)}",
            )
        self._entries = ordered
        self._index = {key: tuple(value) for key, value in index.items()}
        self._digest = semantic_digest(
            tuple(
                {
                    "metric_ref": item.definition.metric_ref,
                    "origin": item.origin.value,
                    "authority_ref": item.authority_ref,
                    "definition_digest": item.definition_digest,
                }
                for item in ordered
            )
        )

    @property
    def entries(self) -> tuple[MetricCatalogEntry, ...]:
        return self._entries

    @property
    def digest(self) -> str:
        return self._digest

    def resolve(self, term: str) -> MetricResolutionResult:
        normalized = normalize_metric_term(term)
        if not normalized:
            raise ValueError("metric resolution term cannot be blank")
        candidates = self._index.get(normalized, ())
        matches = tuple(
            MetricResolutionMatch(
                metric_ref=entry.definition.metric_ref,
                matched_token=token,
                origin=entry.origin,
                definition_digest=entry.definition_digest,
            )
            for entry, token in candidates
        )
        if not matches:
            status = MetricResolutionStatus.UNRESOLVED
        elif len(matches) == 1:
            status = MetricResolutionStatus.RESOLVED
        else:
            status = MetricResolutionStatus.AMBIGUOUS
        return MetricResolutionResult(
            status=status,
            requested_term=term,
            matches=matches,
        )

    def require(self, term: str) -> MetricCatalogEntry:
        result = self.resolve(term)
        if result.status == MetricResolutionStatus.UNRESOLVED:
            raise SemanticMetricError(
                SemanticMetricErrorCode.METRIC_UNRESOLVED,
                f"metric term is not defined: {term}",
            )
        if result.status == MetricResolutionStatus.AMBIGUOUS:
            raise SemanticMetricError(
                SemanticMetricErrorCode.METRIC_AMBIGUOUS,
                f"metric term is ambiguous: {term}",
            )
        ref = result.matches[0].metric_ref
        return next(item for item in self._entries if item.definition.metric_ref == ref)

    @classmethod
    def build(
        cls,
        *,
        governed: tuple[MetricCatalogEntry, ...] = (),
        overlays: tuple[MetricCatalogEntry, ...] = (),
        legacy: tuple[MetricCatalogEntry, ...] = (),
        allow_overlay_shadow: bool = False,
        reject_alias_conflicts: bool = True,
    ) -> "EffectiveMetricCatalog":
        if any(item.origin != MetricCatalogOrigin.GOVERNED for item in governed):
            raise ValueError("governed catalog entries must use governed origin")
        if any(item.origin != MetricCatalogOrigin.OVERLAY for item in overlays):
            raise ValueError("overlay catalog entries must use overlay origin")
        if any(item.origin != MetricCatalogOrigin.LEGACY for item in legacy):
            raise ValueError("legacy catalog entries must use legacy origin")

        selected: list[MetricCatalogEntry] = list(governed)
        selected_refs = {
            normalize_metric_term(item.definition.metric_ref): item for item in selected
        }
        for entry in overlays:
            key = normalize_metric_term(entry.definition.metric_ref)
            if key in selected_refs and not allow_overlay_shadow:
                raise SemanticMetricError(
                    SemanticMetricErrorCode.METRIC_CONFLICT,
                    f"overlay cannot shadow governed metric: {entry.definition.metric_ref}",
                )
            if key in selected_refs:
                selected = [
                    item
                    for item in selected
                    if normalize_metric_term(item.definition.metric_ref) != key
                ]
            selected.append(entry)
            selected_refs[key] = entry
        for entry in legacy:
            key = normalize_metric_term(entry.definition.metric_ref)
            if key in selected_refs:
                continue
            selected.append(entry)
            selected_refs[key] = entry
        return cls(tuple(selected), reject_alias_conflicts=reject_alias_conflicts)


__all__ = [
    "EffectiveMetricCatalog",
    "MetricCatalogEntry",
    "MetricCatalogOrigin",
    "MetricResolutionMatch",
    "MetricResolutionResult",
    "MetricResolutionStatus",
    "normalize_metric_term",
]
