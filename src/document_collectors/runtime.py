"""Concrete runtime bridges for the connector-backed collection runner.

The Codex agent supplies a browser or Gmail tool bridge. This module keeps the
transport selection and durable filesystem landing deterministic without
embedding credentials or MCP calls in provider connectors.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .collection import CollectionError, SinkResult, TransportResponse, VerifiedArtifact
from .core import EndpointRequest
from .manifest import Route


class BrowserToolBridge(Protocol):
    """Agent/runtime bridge for Chrome, page-context, and UI execution."""

    def execute_browser(self, plan: EndpointRequest | str, *, route_kind: str) -> TransportResponse: ...


class GmailToolBridge(Protocol):
    """Agent/runtime bridge for bounded provider-email retrieval."""

    def execute_gmail(self, plan: EndpointRequest | str, *, route_kind: str) -> TransportResponse: ...


@dataclass(frozen=True)
class ToolTransport:
    """Dispatch a connector plan to the correct live Codex tool bridge."""

    browser: BrowserToolBridge
    gmail: GmailToolBridge

    def execute(self, plan: EndpointRequest | str, *, route_kind: str) -> TransportResponse:
        if route_kind == "email" or (isinstance(plan, EndpointRequest) and plan.method == "GMAIL_METADATA"):
            return self.gmail.execute_gmail(plan, route_kind=route_kind)
        return self.browser.execute_browser(plan, route_kind=route_kind)


@dataclass(frozen=True)
class ArtifactContract:
    """Sanitized response contract for one route."""

    content_type: str
    magic: bytes
    identity_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class SignatureVerifier:
    """Verify provider bytes and runtime identity metadata before landing.

    The live bridge must set ``identity_verified=true`` only after matching
    visible provider identity to the selected document. It must also provide a
    safe ``relative_path`` for the sink and a ``filename`` for auditability.
    """

    contracts: Mapping[str, ArtifactContract]

    def verify(self, response: TransportResponse, *, provider_id: str, route: Route, plan: EndpointRequest | str) -> VerifiedArtifact:
        try:
            contract = self.contracts[route.id]
        except KeyError as exc:
            raise CollectionError(f"no artifact contract registered for route {route.id!r}") from exc
        if response.content_type.split(";", 1)[0].lower() != contract.content_type.lower():
            raise CollectionError(f"route {route.id!r} returned unexpected content type")
        if not response.body.startswith(contract.magic):
            raise CollectionError(f"route {route.id!r} returned unexpected document signature")
        if response.metadata.get("identity_verified", "").lower() != "true":
            raise CollectionError(f"route {route.id!r} lacks same-content identity verification")
        if not response.metadata.get("relative_path") or not response.metadata.get("filename"):
            raise CollectionError(f"route {route.id!r} lacks a safe destination filename/path")
        identity = {key: response.metadata[key] for key in contract.identity_keys if response.metadata.get(key)}
        missing = [key for key in contract.identity_keys if not response.metadata.get(key)]
        if missing:
            raise CollectionError(f"route {route.id!r} lacks identity metadata: {', '.join(missing)}")
        return VerifiedArtifact(response.body, response.content_type, identity, dict(response.metadata))


def _safe_relative_path(value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or not candidate.parts or any(part in {"", ".", ".."} for part in candidate.parts):
        raise CollectionError("sink relative_path must be a non-empty safe relative path")
    return candidate


@dataclass(frozen=True)
class FileSystemSink:
    """Atomically land verified bytes under an explicitly chosen root.

    Existing identical bytes are treated as an idempotent duplicate. A
    destination collision with different bytes fails closed.
    """

    root: Path

    def land(self, artifact: VerifiedArtifact, *, provider_id: str, route: Route) -> SinkResult:
        relative = _safe_relative_path(artifact.metadata["relative_path"])
        target = self.root / relative
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise CollectionError("sink target escaped its configured root") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(artifact.body).hexdigest()
        if target.exists():
            if target.read_bytes() != artifact.body:
                raise CollectionError(f"destination collision contains different bytes: {target}")
            return SinkResult(True, str(target), digest)
        fd, temporary_name = tempfile.mkstemp(prefix=".collector-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as temporary:
                temporary.write(artifact.body)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, target)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
        return SinkResult(True, str(target), digest)
