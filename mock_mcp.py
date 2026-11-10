"""In-process mock MCP boundary, not a server or real subprocess invocation."""
import json
import re
from dataclasses import dataclass
from typing import Any

from sample import CollectionError, LandingUncertain, Request, collect_sample, receipt


@dataclass
class InvokeResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int
    parsed: Any | None = None

    def as_tool_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }
        if self.parsed is not None:
            payload["result"] = self.parsed
        return payload


def run(arguments, *, root):
    """Simulate tool routing with one fixed host function and scope attestation."""
    allowed = {"record_id", "version", "approval_name", "approved_scope"}
    valid = (type(arguments) is dict and set(arguments) == allowed
             and arguments["record_id"] == "statement-0001"
             and type(arguments["version"]) is int and arguments["version"] in (1, 2)
             and isinstance(arguments["approval_name"], str)
             and bool(arguments["approval_name"].strip())
             and type(arguments["approved_scope"]) is dict
             and set(arguments["approved_scope"]) == {"record_id", "version"}
             and type(arguments["approved_scope"]["version"]) is int
             and arguments["approved_scope"] == {
                 "record_id": arguments["record_id"], "version": arguments["version"]})
    if not valid:
        return InvokeResult(False, "", "scope_rejected", 2).as_tool_payload()
    try:
        result = receipt(collect_sample(root, Request(version=arguments["version"])), root)
    except LandingUncertain as exc:
        result = {"domain_ok": False, "error": "landing_uncertain", "destination": exc.destination}
    except (CollectionError, ValueError, OSError):
        result = {"domain_ok": False, "error": "collection_rejected"}
    # Deliberately simulate a completed invocation even when the domain refused.
    return InvokeResult(True, json.dumps(result), "", 0, result).as_tool_payload()


def accept_tool_payload(payload):
    """Outer success must never erase a domain refusal or missing evidence."""
    if (type(payload) is not dict or payload.get("ok") is not True
            or type(payload.get("returncode")) is not int or payload["returncode"] != 0
            or type(payload.get("stdout")) is not str or type(payload.get("stderr")) is not str):
        raise CollectionError("invocation_rejected")
    result = payload.get("result")
    if (type(result) is not dict or set(result) != {
            "domain_ok", "record_id", "landing", "physical_verified"}
            or result["domain_ok"] is not True or result["physical_verified"] is not True
            or result["record_id"] != "statement-0001"):
        raise CollectionError("domain_result_rejected")
    landing = result["landing"]
    if (type(landing) is not dict or set(landing) != {
            "destination", "sha256", "size", "disposition", "landed", "readback_verified"}
            or landing["landed"] is not True or landing["readback_verified"] is not True
            or type(landing["sha256"]) is not str
            or not re.fullmatch(r"[a-f0-9]{64}", landing["sha256"])
            or type(landing["size"]) is not int or not 0 < landing["size"] <= 65536
            or landing["disposition"] not in ("created", "duplicate", "variant")):
        raise CollectionError("landing_result_rejected")
    from runtime import _safe_relative_path
    _safe_relative_path(landing["destination"])
    try:
        if json.loads(payload["stdout"]) != result:
            raise CollectionError("serialized_result_mismatch")
    except (ValueError, TypeError) as exc:
        raise CollectionError("serialized_result_invalid") from exc
    return result
