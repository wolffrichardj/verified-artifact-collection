"""Fail-closed orchestration for authenticated document collection.

The runner owns the mandatory sequence: manifest route -> provider connector
-> injected transport -> injected verifier -> injected sink. Browser, Gmail,
and other authenticated transports stay outside this package and are supplied
by the calling agent/runtime.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .connectors import connector_for
from .core import EndpointRequest
from .manifest import CollectorManifest, Route, load_manifest


class CollectionError(RuntimeError):
    """Raised when collection cannot safely complete."""


@dataclass(frozen=True)
class TransportResponse:
    """Response returned by an injected authenticated transport."""

    body: bytes
    content_type: str
    metadata: Mapping[str, str] = field(default_factory=dict)
    payload: object | None = None


@dataclass(frozen=True)
class VerifiedArtifact:
    """Provider artifact accepted by the injected verifier."""

    body: bytes
    content_type: str
    identity: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SinkResult:
    """Durable landing result returned by the injected destination sink."""

    landed: bool
    destination: str
    record_id: str | None = None


class AuthenticatedTransport(Protocol):
    def execute(self, plan: EndpointRequest | str, *, route_kind: str) -> TransportResponse: ...


class ArtifactVerifier(Protocol):
    def verify(
        self,
        response: TransportResponse,
        *,
        provider_id: str,
        route: Route,
        plan: EndpointRequest | str,
    ) -> VerifiedArtifact: ...


class DocumentSink(Protocol):
    def land(
        self,
        artifact: VerifiedArtifact,
        *,
        provider_id: str,
        route: Route,
    ) -> SinkResult: ...


@dataclass(frozen=True)
class CollectionResult:
    provider_id: str
    route_id: str
    route_kind: str
    plan: EndpointRequest | str
    artifact: VerifiedArtifact
    sink: SinkResult


def _resolve_manifest(manifest: CollectorManifest | str | Path) -> CollectorManifest:
    if isinstance(manifest, CollectorManifest):
        return manifest
    return load_manifest(manifest)


def _route_by_id(provider, route_id: str) -> Route:
    for route in provider.routes:
        if route.id == route_id:
            return route
    raise CollectionError(f"route {route_id!r} is not declared for provider {provider.id!r}")


def _select_route(manifest: CollectorManifest, provider_id: str, route_id: str | None, capabilities: set[str] | None) -> Route:
    provider = manifest.provider(provider_id)
    if route_id is not None:
        route = _route_by_id(provider, route_id)
        if route.deferred:
            raise CollectionError(f"route {route.id!r} is explicitly deferred")
        if route.status not in {"tested", "conditional"}:
            raise CollectionError(f"route {route.id!r} is not executable in manifest state {route.status!r}")
        if capabilities is not None and route.kind not in capabilities:
            raise CollectionError(f"route {route.id!r} requires unavailable capability {route.kind!r}")
        return route
    try:
        return manifest.choose_route(provider_id, available_kinds=capabilities)
    except (KeyError, LookupError) as exc:
        raise CollectionError(f"no usable manifest route for provider {provider_id!r}") from exc


def collect(
    manifest: CollectorManifest | str | Path,
    provider_id: str,
    values: Mapping[str, Any],
    *,
    transport: AuthenticatedTransport,
    verifier: ArtifactVerifier,
    sink: DocumentSink,
    route_id: str | None = None,
    capabilities: set[str] | None = None,
) -> CollectionResult:
    """Execute one connector-backed collection and require durable landing.

    A successful return proves that a manifest route was selected, its
    provider connector built the plan, the injected authenticated transport
    returned a response, the injected verifier accepted provider artifact
    bytes, and the injected sink reported a destination.
    """

    manifest_obj = _resolve_manifest(manifest)
    try:
        provider = manifest_obj.provider(provider_id)
    except KeyError as exc:
        raise CollectionError(f"provider {provider_id!r} is not declared in the manifest") from exc
    route = _select_route(manifest_obj, provider.id, route_id, capabilities)
    try:
        connector = connector_for(provider.id)
    except KeyError as exc:
        raise CollectionError(f"provider {provider.id!r} has no registered connector") from exc
    if route.id not in connector.route_ids():
        raise CollectionError(f"route {route.id!r} has no registered connector builder")

    try:
        plan = connector.plan(route.id, **dict(values))
    except Exception as exc:
        raise CollectionError(f"connector could not build route plan {route.id!r}") from exc
    try:
        response = transport.execute(plan, route_kind=route.kind)
    except Exception as exc:
        raise CollectionError(f"authenticated transport failed for route {route.id!r}") from exc
    if not isinstance(response, TransportResponse) or not response.body:
        raise CollectionError("transport returned no document response bytes")

    try:
        artifact = verifier.verify(response, provider_id=provider.id, route=route, plan=plan)
    except Exception as exc:
        raise CollectionError(f"artifact verification failed for route {route.id!r}") from exc
    if not isinstance(artifact, VerifiedArtifact) or not artifact.body:
        raise CollectionError("verifier did not return a non-empty verified artifact")

    try:
        landed = sink.land(artifact, provider_id=provider.id, route=route)
    except Exception as exc:
        raise CollectionError(f"destination sink failed for route {route.id!r}") from exc
    if not isinstance(landed, SinkResult) or not landed.landed or not landed.destination:
        raise CollectionError("sink did not report a durable destination")

    return CollectionResult(provider.id, route.id, route.kind, plan, artifact, landed)
