"""Interfaces for FUTURE attribution providers (diarisation, A/V synchrony, visual context, continuity).

No provider exists, no model is installed or downloaded, and this package does no voice biometrics or face
recognition. What exists is the contract future providers must obey and an honest "unavailable" path:

  * a provider can only produce SUPPORTING evidence (never a human/metadata method);
  * a diarisation provider produces cluster MEMBERSHIPS (a grouping), never an identity;
  * every run is recorded in the store, including "unavailable" runs, so the log states plainly that
    nothing was attributed by software;
  * with no provider, attribution stays `unknown` / `requires_review`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol, Sequence, runtime_checkable

from media_analysis.models import CapabilityError, MediaRef

from .models import AUTHORITATIVE, AttributionError, AttributionEvidence, AttributionMethod, Channel
from .store import AttributionStore


@dataclass(frozen=True)
class ProviderCapabilities:
    available: bool
    reason: str | None = None
    methods: tuple[AttributionMethod, ...] = ()      # which supporting methods this provider can produce
    channels: tuple[Channel, ...] = ()


@dataclass(frozen=True)
class AttributionRequest:
    media: MediaRef                                  # the ORIGINAL (times are on its timeline)
    audio_path: str | None = None                    # an analysis copy, if the provider needs audio
    video_path: str | None = None
    settings: Mapping = field(default_factory=dict)


@runtime_checkable
class AttributionProvider(Protocol):
    name: str
    version: str
    kind: str                                        # "ml" | "deterministic" | "synthetic_fixture"
    def capabilities(self) -> ProviderCapabilities: ...
    def attribute(self, request: AttributionRequest) -> Sequence[AttributionEvidence]: ...


class UnavailableProvider:
    """Placeholder used until a real provider is chosen. Raises CapabilityError; attributes nothing."""
    kind = "ml"

    def __init__(self, name: str = "none", reason: str = "no speaker-attribution provider is installed or selected"):
        self.name, self.version, self._reason = name, "0", reason

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(False, self._reason)

    def attribute(self, request):
        raise CapabilityError(self._reason)


class FixtureProvider:
    """Replays prepared supporting evidence. SYNTHETIC: exercises the plumbing, identifies nobody."""
    kind = "synthetic_fixture"
    version = "1"

    def __init__(self, evidence: Sequence[AttributionEvidence], name: str = "fixture"):
        self.name, self._evidence = name, tuple(evidence)
        methods = tuple(sorted({e.method for e in self._evidence}, key=lambda m: m.value))
        channels = tuple(sorted({e.channel for e in self._evidence}, key=lambda c: c.value))
        self._caps = ProviderCapabilities(True, None, methods, channels)

    def capabilities(self) -> ProviderCapabilities:
        return self._caps

    def attribute(self, request):
        return [e for e in self._evidence if e.media_id == request.media.media_id]


def run_provider(store: AttributionStore, provider: AttributionProvider, request: AttributionRequest) -> dict:
    """Run a provider and store what it returned, after validating it. The run is always recorded.
    An unavailable provider records an 'unavailable' run and raises CapabilityError: nothing is invented."""
    caps = provider.capabilities()
    media_id = request.media.media_id
    if not caps.available:
        store.record_provider_run(provider.name, provider.version, "unavailable", caps.reason or "", media_id)
        raise CapabilityError(f"attribution provider {provider.name!r} unavailable: {caps.reason}")
    try:
        produced = list(provider.attribute(request))
        for e in produced:
            if e.method in AUTHORITATIVE:
                raise AttributionError("a provider cannot produce authoritative (human/metadata) attribution")
            if e.method not in caps.methods or e.channel not in caps.channels:
                raise AttributionError(f"provider produced {e.method.value}/{e.channel.value} without declaring it")
            if e.media_id != media_id:
                raise AttributionError("provider returned evidence for a different recording")
            if e.actor.startswith("human:"):
                raise AttributionError("provider evidence cannot claim a human actor")
            if request.media.sha256 and e.media_sha256 and e.media_sha256 != request.media.sha256:
                raise AttributionError("provider evidence names a different original (checksum mismatch)")
    except AttributionError as exc:
        store.record_provider_run(provider.name, provider.version, "failed", str(exc), media_id)
        raise
    added = sum(1 for e in produced if store.add_evidence(e) is not None)
    store.record_provider_run(provider.name, provider.version, "ok", f"{len(produced)} produced", media_id, added)
    return {"produced": len(produced), "added": added, "duplicates": len(produced) - added}
