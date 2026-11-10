"""One source-derived acquisition path, using generated records only."""
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path


class CollectionError(RuntimeError):
    """Raised when collection cannot safely complete."""


class LandingUncertain(CollectionError):
    """Publication occurred but final durability/readback could not be confirmed."""

    def __init__(self, destination):
        super().__init__("landing_uncertain")
        self.destination = destination


# Retained acquisition types, with additive readback evidence on SinkResult.
@dataclass(frozen=True)
class TransportResponse:
    body: bytes
    content_type: str
    metadata: Mapping[str, str] = field(default_factory=dict)
    payload: object | None = None


@dataclass(frozen=True)
class VerifiedArtifact:
    body: bytes
    content_type: str
    identity: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SinkResult:
    landed: bool
    destination: str
    record_id: str | None = None
    disposition: str = "created"
    sha256: str = ""
    size: int = 0
    readback_verified: bool = False


@dataclass(frozen=True)
class Request:
    provider_id: str = "example_archive"
    subject_id: str = "demo_subject_01"
    record_id: str = "statement-0001"
    period: str = "2026-01"
    route_id: str = "example.statement"
    version: int = 1

    def identity(self):
        return {key: getattr(self, key) for key in
                ("provider_id", "subject_id", "record_id", "period")}


@dataclass(frozen=True)
class Route:
    id: str = "example.statement"
    kind: str = "offline"


@dataclass(frozen=True)
class CollectionResult:
    provider_id: str
    route_id: str
    route_kind: str
    plan: str
    artifact: VerifiedArtifact
    sink: SinkResult


def validate_request(request):
    if (not isinstance(request, Request) or type(request.version) is not int
            or request.version not in (1, 2)
            or request != replace(Request(), version=request.version)):
        raise CollectionError("undeclared_request")


def fixture_bytes(request):
    validate_request(request)
    fields = {**request.identity(), "version": str(request.version), "synthetic": "true"}
    return ("OFFLINE-SAMPLE v1\n" + "".join(f"{k}={v}\n" for k, v in fields.items())).encode()


def simulated_session(request):
    return {**request.identity(), "synthetic": True, "attested": True}


class FakeTransport:
    def __init__(self, request, response=None):
        self.calls = []
        self.response = response if response is not None else TransportResponse(
            fixture_bytes(request), "text/plain",
            {**request.identity(), "identity_verified": "true"})

    def execute(self, plan, *, route_kind):
        self.calls.append((plan, route_kind))
        return self.response


def collect(request, *, session, transport, verifier, sink):
    """Adapted transport -> verifier -> sink core; no live provider registry."""
    validate_request(request)
    if (type(session) is not dict or session != simulated_session(request)
            or session.get("synthetic") is not True or session.get("attested") is not True):
        raise CollectionError("simulated_session_rejected")
    route = Route()
    provider_id = request.provider_id
    plan = f"/synthetic/{request.record_id}?version={request.version}"
    try:
        response = transport.execute(plan, route_kind=route.kind)
    except Exception as exc:
        raise CollectionError(f"authenticated transport failed for route {route.id!r}") from exc
    if not isinstance(response, TransportResponse) or not response.body:
        raise CollectionError("transport returned no document response bytes")

    try:
        artifact = verifier.verify(response, provider_id=provider_id, route=route, plan=plan)
    except Exception as exc:
        raise CollectionError(f"artifact verification failed for route {route.id!r}") from exc
    if not isinstance(artifact, VerifiedArtifact) or not artifact.body:
        raise CollectionError("verifier did not return a non-empty verified artifact")

    try:
        landed = sink.land(artifact, provider_id=provider_id, route=route)
    except LandingUncertain:
        raise  # Preserve post-publication uncertainty through the original boundary.
    except Exception as exc:
        raise CollectionError(f"destination sink failed for route {route.id!r}") from exc
    if not isinstance(landed, SinkResult) or landed.landed is not True or not landed.destination:
        raise CollectionError("sink did not report a durable destination")
    if (landed.readback_verified is not True
            or landed.sha256 != hashlib.sha256(artifact.body).hexdigest()
            or type(landed.size) is not int or landed.size != len(artifact.body)):
        raise CollectionError("sink did not report matching readback evidence")
    return CollectionResult(provider_id, route.id, route.kind, plan, artifact, landed)


def collect_sample(root, request=None, response=None):
    from runtime import FileSystemSink, SignatureVerifier
    request = request or Request()
    return collect(request, session=simulated_session(request),
                   transport=FakeTransport(request, response),
                   verifier=SignatureVerifier(request), sink=FileSystemSink(Path(root)))


def receipt(result, root):
    from source_identity import SourceVerifier
    # Expected values come from verified input bytes, not from hashing the landed file.
    digest = hashlib.sha256(result.artifact.body).hexdigest()
    size = len(result.artifact.body)
    SourceVerifier(root).verify(result.sink.destination, digest, size)
    return {"domain_ok": True, "record_id": result.artifact.identity["record_id"],
            "landing": {"destination": result.sink.destination, "sha256": digest,
                        "size": size, "disposition": result.sink.disposition,
                        "landed": result.sink.landed,
                        "readback_verified": result.sink.readback_verified},
            "physical_verified": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--scenario", choices=("happy", "revised", "wrong-identity", "tampered-source"),
                        default="happy")
    args = parser.parse_args()
    request = Request(version=2 if args.scenario == "revised" else 1)
    try:
        if args.scenario == "tampered-source":
            from source_identity import SourceVerifier
            with tempfile.TemporaryDirectory(prefix="owned-tamper-", dir=args.root) as own:
                result = collect_sample(own)
                path = Path(own) / result.sink.destination
                path.write_bytes(result.artifact.body.replace(b"true", b"fake"))
                try:
                    SourceVerifier(own).verify(result.sink.destination,
                                              hashlib.sha256(result.artifact.body).hexdigest(),
                                              len(result.artifact.body))
                except ValueError as exc:
                    if str(exc) != "Source hash mismatch":
                        raise
                    print(json.dumps({"domain_ok": False, "error": "tamper_rejected"}))
                    return 1
            raise CollectionError("tamper_was_not_detected")
        response = None
        if args.scenario == "wrong-identity":
            response = TransportResponse(fixture_bytes(request), "text/plain",
                                         {**request.identity(), "subject_id": "wrong",
                                          "identity_verified": "true"})
        print(json.dumps(receipt(collect_sample(args.root, request, response), args.root), sort_keys=True))
        return 0
    except LandingUncertain as exc:
        print(json.dumps({"domain_ok": False, "error": "landing_uncertain",
                          "destination": exc.destination}))
        return 1
    except (CollectionError, ValueError, OSError):
        print(json.dumps({"domain_ok": False, "error": "collection_rejected"}))
        return 1


if __name__ == "__main__":
    # Runtime imports canonical sample types even when this file is the CLI.
    import sample
    raise SystemExit(sample.main())
