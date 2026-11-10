"""Source-derived verifier and local landing mechanisms; new hardening is explicit."""
import hashlib
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from sample import (CollectionError, LandingUncertain, SinkResult, VerifiedArtifact,
                    fixture_bytes)

MAX_BYTES = 64 * 1024


@dataclass(frozen=True)
class ArtifactContract:
    content_type: str
    magic: bytes
    identity_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class SignatureVerifier:
    request: object

    def verify(self, response, *, provider_id, route, plan):
        contract = ArtifactContract("text/plain", b"OFFLINE-SAMPLE v1\n",
                                    tuple(self.request.identity()))
        if response.content_type.split(";", 1)[0].lower() != contract.content_type.lower():
            raise CollectionError(f"route {route.id!r} returned unexpected content type")
        if not isinstance(response.body, bytes) or not response.body.startswith(contract.magic):
            raise CollectionError(f"route {route.id!r} returned unexpected document signature")
        if response.metadata.get("identity_verified", "").lower() != "true":
            raise CollectionError(f"route {route.id!r} lacks same-content identity verification")
        identity = {key: response.metadata[key] for key in contract.identity_keys
                    if response.metadata.get(key)}
        # New: a host attestation is insufficient without request and byte equality.
        if (provider_id != self.request.provider_id or route.id != self.request.route_id
                or identity != self.request.identity() or len(response.body) > MAX_BYTES
                or response.body != fixture_bytes(self.request)):
            raise CollectionError("request_content_mismatch")
        metadata = {"relative_path": f"{self.request.record_id}.txt"}
        return VerifiedArtifact(response.body, response.content_type, identity, metadata)


def _safe_relative_path(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value)
            or value.endswith(".")):
        raise CollectionError("sink requires a flat safe basename")
    return value


def _open_root(root):
    if os.name != "posix" or not all(hasattr(os, k) for k in
                                    ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")):
        raise CollectionError("platform_unsupported")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    if os.fstat(fd).st_uid != os.getuid():
        os.close(fd)
        raise CollectionError("root_not_owned")
    return fd


def _read_regular(rootfd, name):
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=rootfd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise CollectionError("target_not_bounded_regular_file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            body = stream.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise CollectionError("target_exceeds_size_bound")
        return body
    finally:
        os.close(fd)


def _readback(rootfd, name, expected):
    actual = _read_regular(rootfd, name)
    if len(actual) != len(expected) or hashlib.sha256(actual).digest() != hashlib.sha256(expected).digest():
        raise CollectionError("final_readback_mismatch")


def _write_temporary(rootfd, body):
    name = ".collector-" + secrets.token_hex(12)
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=rootfd)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        os.unlink(name, dir_fd=rootfd)
        raise
    return name


@dataclass(frozen=True)
class FileSystemSink:
    root: Path

    def land(self, artifact, *, provider_id, route):
        base = _safe_relative_path(artifact.metadata["relative_path"])
        body = artifact.body
        if not isinstance(body, bytes) or not 0 < len(body) <= MAX_BYTES:
            raise CollectionError("artifact_not_bounded_bytes")
        digest = hashlib.sha256(body).hexdigest()
        suffix = Path(base).suffix
        variant = f"{Path(base).stem}__variant-{digest}{suffix}"
        rootfd = _open_root(self.root)
        temporary = None
        published = None
        try:
            try:
                existing = _read_regular(rootfd, base)
            except FileNotFoundError:
                existing = None
            target = variant if existing is not None and existing != body else base
            disposition = "variant" if target == variant else "created"
            try:
                current = _read_regular(rootfd, target)
            except FileNotFoundError:
                current = None
            if target == base and current is not None and current != body:
                # Another writer may publish between the two initial observations.
                target, disposition = variant, "variant"
                try:
                    current = _read_regular(rootfd, target)
                except FileNotFoundError:
                    current = None
            if current is not None:
                if current != body:
                    raise CollectionError("variant_collision")
                disposition = "duplicate"
            else:
                temporary = _write_temporary(rootfd, body)
                try:
                    os.link(temporary, target, src_dir_fd=rootfd, dst_dir_fd=rootfd,
                            follow_symlinks=False)
                    published = target
                except FileExistsError:
                    winner = _read_regular(rootfd, target)
                    if winner == body:
                        disposition = "duplicate"
                    elif target == base:
                        # Existing approved behavior: race loser advances once to hash variant.
                        target = variant
                        disposition = "variant"
                        try:
                            os.link(temporary, target, src_dir_fd=rootfd, dst_dir_fd=rootfd,
                                    follow_symlinks=False)
                            published = target
                        except FileExistsError:
                            if _read_regular(rootfd, target) != body:
                                raise CollectionError("variant_collision")
                            disposition = "duplicate"
                    else:
                        raise CollectionError("variant_collision")
                os.unlink(temporary, dir_fd=rootfd)
                temporary = None
                if published is not None:
                    os.fsync(rootfd)
            _readback(rootfd, target, body)
            return SinkResult(True, target, digest, disposition, digest, len(body), True)
        except Exception as exc:
            if published is not None:
                raise LandingUncertain(published) from exc
            raise
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=rootfd)
                except OSError:
                    pass  # Cleanup is limited to our temporary name, never the destination.
            os.close(rootfd)
