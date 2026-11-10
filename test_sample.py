"""Synthetic source-derived contracts and new integrity/race regressions."""
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from unittest.mock import patch

import mock_mcp
import runtime
from sample import (CollectionError, FakeTransport, LandingUncertain, Request,
                    Route, SinkResult, TransportResponse, VerifiedArtifact, collect,
                    collect_sample, fixture_bytes, main, receipt, simulated_session)
from source_identity import SourceVerifier

GOLDEN = (b"OFFLINE-SAMPLE v1\nprovider_id=example_archive\nsubject_id=demo_subject_01\n"
          b"record_id=statement-0001\nperiod=2026-01\nversion=1\nsynthetic=true\n")
GOLDEN_SHA = "3e440528685d2ebc76365f8ad54ecc9a76973813ea49db30a2f0eeb51c01d733"


class FakeSink:
    """Adapted recording fake from the original collection tests."""
    def __init__(self, result):
        self.result, self.calls = result, []

    def land(self, artifact, *, provider_id, route):
        self.calls.append((artifact, provider_id, route.id))
        return self.result


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.request = Request()
        self.transport = FakeTransport(self.request)

    def acquire(self, **overrides):
        options = dict(session=simulated_session(self.request), transport=self.transport,
                       verifier=runtime.SignatureVerifier(self.request),
                       sink=runtime.FileSystemSink(self.root))
        options.update(overrides)
        return collect(self.request, **options)

    def artifact(self, body=GOLDEN, name="statement-0001.txt"):
        return VerifiedArtifact(body, "text/plain", self.request.identity(), {"relative_path": name})

    def land(self, body=GOLDEN, name="statement-0001.txt"):
        return runtime.FileSystemSink(self.root).land(self.artifact(body, name),
                                                     provider_id="example_archive", route=Route())

    def test_literal_fixture_has_stable_bytes_and_digest(self):
        self.assertEqual(fixture_bytes(self.request), GOLDEN)
        self.assertEqual(hashlib.sha256(GOLDEN).hexdigest(), GOLDEN_SHA)

    def test_real_acquisition_and_separate_physical_verification(self):
        result = self.acquire()
        self.assertEqual((self.root / result.sink.destination).read_bytes(), GOLDEN)
        self.assertEqual(result.sink.sha256, GOLDEN_SHA)
        self.assertEqual(result.sink.size, len(GOLDEN))
        self.assertTrue(receipt(result, self.root)["physical_verified"])
        self.assertEqual(len(self.transport.calls), 1)

    def test_invalid_request_and_session_stop_before_transport(self):
        for request, session in [
                (replace(self.request, route_id="unknown"), simulated_session(self.request)),
                (replace(self.request, version=True), simulated_session(self.request)),
                (self.request, None), (self.request, {}),
                (self.request, {**simulated_session(self.request), "attested": 1}),
                (self.request, {**simulated_session(self.request), "subject_id": "wrong"})]:
            with self.subTest(request=request, session=session), self.assertRaises(CollectionError):
                collect(request, session=session, transport=self.transport,
                        verifier=runtime.SignatureVerifier(self.request),
                        sink=runtime.FileSystemSink(self.root))
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_content_failures_never_call_sink(self):
        valid = self.transport.response
        for response in [None, replace(valid, body=b""), replace(valid, body=b"not a document"),
                         replace(valid, content_type="application/pdf"),
                         replace(valid, metadata={**valid.metadata, "subject_id": "wrong"}),
                         replace(valid, metadata={**valid.metadata, "identity_verified": "false"}),
                         replace(valid, body=GOLDEN.replace(b"true", b"fake")),
                         replace(valid, body=GOLDEN + b"unknown=x\n"),
                         replace(valid, body=GOLDEN + b"\xff"),
                         replace(valid, body=GOLDEN + b"x" * 65536)]:
            sink = FakeSink(None)
            self.transport.response = response
            with self.subTest(response=response), self.assertRaises(CollectionError):
                self.acquire(sink=sink)
            self.assertEqual(sink.calls, [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_untyped_or_empty_verifier_and_incomplete_sink_results_refuse(self):
        for verified in [None, self.artifact(b"")]:
            sink = FakeSink(None)
            with patch.object(runtime.SignatureVerifier, "verify", return_value=verified):
                with self.assertRaises(CollectionError):
                    self.acquire(sink=sink)
            self.assertEqual(sink.calls, [])
        for result in [None, SinkResult(False, ""), SinkResult(True, "file.txt"),
                       SinkResult(True, "file.txt", sha256=GOLDEN_SHA,
                                  size=True, readback_verified=True)]:
            with self.subTest(result=result), self.assertRaises(CollectionError):
                self.acquire(sink=FakeSink(result))

    def test_created_duplicate_variant_and_variant_duplicate_preserve_original(self):
        first, second = self.land(), self.land()
        revised = fixture_bytes(Request(version=2))
        third, fourth = self.land(revised), self.land(revised)
        self.assertEqual([x.disposition for x in (first, second, third, fourth)],
                         ["created", "duplicate", "variant", "duplicate"])
        self.assertEqual((self.root / first.destination).read_bytes(), GOLDEN)
        self.assertEqual((self.root / third.destination).read_bytes(), revised)
        self.assertIn(hashlib.sha256(revised).hexdigest(), third.destination)
        self.assertEqual(len(list(self.root.iterdir())), 2)

    def test_wrong_byte_variant_is_explicit_collision(self):
        self.land()
        revised = fixture_bytes(Request(version=2))
        name = "statement-0001__variant-" + hashlib.sha256(revised).hexdigest() + ".txt"
        (self.root / name).write_bytes(b"occupied with different bytes")
        with self.assertRaisesRegex(CollectionError, "variant_collision"):
            self.land(revised)
        self.assertEqual((self.root / "statement-0001.txt").read_bytes(), GOLDEN)

    def test_invalid_paths_and_root_symlink_refuse(self):
        for name in ["../escape", "/absolute", "nested/file", "..", ".", "a\\b", "trailing."]:
            with self.subTest(name=name), self.assertRaises(CollectionError):
                self.land(name=name)
        with tempfile.TemporaryDirectory() as parent:
            link = Path(parent) / "link"
            link.symlink_to(self.root, target_is_directory=True)
            with self.assertRaises(OSError):
                runtime.FileSystemSink(link).land(self.artifact(), provider_id="example_archive", route=Route())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_base_variant_and_physical_symlinks_refuse_without_touching_outside(self):
        with tempfile.TemporaryDirectory() as outside:
            sentinel = Path(outside) / "sentinel"
            sentinel.write_bytes(GOLDEN)
            base = self.root / "statement-0001.txt"
            base.symlink_to(sentinel)
            with self.assertRaises(OSError):
                self.land()
            with self.assertRaises(ValueError):
                SourceVerifier(self.root).verify(base.name, GOLDEN_SHA)
            base.unlink()
            self.land()
            revised = fixture_bytes(Request(version=2))
            name = "statement-0001__variant-" + hashlib.sha256(revised).hexdigest() + ".txt"
            (self.root / name).symlink_to(sentinel)
            with self.assertRaises(OSError):
                self.land(revised)
            self.assertEqual(sentinel.read_bytes(), GOLDEN)

    def bounded_failure(self, function):
        outcomes = []
        def worker():
            try:
                function()
            except Exception as exc:
                outcomes.append(exc)
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive(), "special-file access blocked")
        self.assertEqual(len(outcomes), 1, "special file was accepted")

    def test_fifo_base_variant_readback_and_physical_checks_are_nonblocking(self):
        base = self.root / "statement-0001.txt"
        os.mkfifo(base)
        self.bounded_failure(self.land)
        self.bounded_failure(lambda: SourceVerifier(self.root).verify(base.name, GOLDEN_SHA))
        base.unlink()
        self.land()
        revised = fixture_bytes(Request(version=2))
        variant = self.root / ("statement-0001__variant-" + hashlib.sha256(revised).hexdigest() + ".txt")
        os.mkfifo(variant)
        self.bounded_failure(lambda: self.land(revised))
        fd = runtime._open_root(self.root)
        try:
            self.bounded_failure(lambda: runtime._readback(fd, variant.name, revised))
        finally:
            os.close(fd)

    def test_directory_and_oversize_existing_targets_refuse(self):
        target = self.root / "statement-0001.txt"
        target.mkdir()
        with self.assertRaises(CollectionError):
            self.land()
        target.rmdir()
        target.write_bytes(b"x" * 65537)
        with self.assertRaises(CollectionError):
            self.land()

    def race(self, bodies, target_name="statement-0001.txt"):
        barrier = threading.Barrier(len(bodies))
        original_link = os.link
        results, errors = [], []
        def synchronized(source, destination, **kwargs):
            if destination == target_name:
                barrier.wait(timeout=3)
            return original_link(source, destination, **kwargs)
        def worker(body):
            try:
                results.append(self.land(body))
            except Exception as exc:
                errors.append(exc)
        with patch("runtime.os.link", side_effect=synchronized):
            threads = [threading.Thread(target=worker, args=(body,), daemon=True) for body in bodies]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertFalse(any(t.is_alive() for t in threads), "writer blocked")
        self.assertEqual(errors, [])
        return results

    def test_simultaneous_same_byte_writers_reconcile_duplicate(self):
        results = self.race([GOLDEN, GOLDEN])
        self.assertCountEqual([r.disposition for r in results], ["created", "duplicate"])
        self.assertEqual((self.root / "statement-0001.txt").read_bytes(), GOLDEN)

    def test_simultaneous_different_byte_writers_preserve_both(self):
        revised = fixture_bytes(Request(version=2))
        results = self.race([GOLDEN, revised])
        self.assertCountEqual([r.disposition for r in results], ["created", "variant"])
        self.assertEqual({(self.root / r.destination).read_bytes() for r in results}, {GOLDEN, revised})

    def test_simultaneous_variant_writers_reconcile_duplicate(self):
        self.land()
        revised = fixture_bytes(Request(version=2))
        variant = "statement-0001__variant-" + hashlib.sha256(revised).hexdigest() + ".txt"
        results = self.race([revised, revised], variant)
        self.assertCountEqual([r.disposition for r in results], ["variant", "duplicate"])

    def test_link_and_temporary_write_failures_do_not_publish(self):
        for point in ["runtime.os.link", "runtime.os.fsync"]:
            with patch(point, side_effect=OSError("injected failure")), self.assertRaises(OSError):
                self.land()
            self.assertEqual(list(self.root.iterdir()), [])

    def test_actual_stream_write_failure_cleans_only_temporary_file(self):
        from contextlib import contextmanager
        real_fdopen = os.fdopen
        @contextmanager
        def faulty_stream(fd, mode, **kwargs):
            with real_fdopen(fd, mode, **kwargs) as stream:
                class Faulty:
                    def write(self, body):
                        raise OSError("injected stream-write fault")
                yield Faulty()
        with patch("runtime.os.fdopen", side_effect=faulty_stream), self.assertRaises(OSError):
            self.land()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_post_link_fsync_is_uncertain_and_preserves_bytes(self):
        real_fsync = os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("directory fsync failed")
            return real_fsync(fd)
        with patch("runtime.os.fsync", side_effect=fail_directory):
            with self.assertRaises(LandingUncertain) as failure:
                self.acquire()
        self.assertEqual(failure.exception.destination, "statement-0001.txt")
        self.assertEqual((self.root / failure.exception.destination).read_bytes(), GOLDEN)
        self.assertEqual(self.land().disposition, "duplicate")

    def test_actual_final_readback_mismatch_is_uncertain_not_success(self):
        real_readback = runtime._readback
        def corrupt(rootfd, name, expected):
            (self.root / name).write_bytes(expected.replace(b"true", b"fake"))
            real_readback(rootfd, name, expected)
        with patch("runtime._readback", side_effect=corrupt), self.assertRaises(LandingUncertain):
            self.acquire()
        self.assertEqual(list(p.name for p in self.root.iterdir()), ["statement-0001.txt"])

    def test_physical_verification_rechecks_same_size_tamper_and_missing(self):
        result = self.land()
        verifier = SourceVerifier(self.root)
        verifier.verify(result.destination, GOLDEN_SHA, len(GOLDEN))
        (self.root / result.destination).write_bytes(GOLDEN.replace(b"true", b"fake"))
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            verifier.verify(result.destination, GOLDEN_SHA, len(GOLDEN))
        (self.root / result.destination).unlink()
        with self.assertRaises(FileNotFoundError):
            verifier.verify(result.destination, GOLDEN_SHA)

    def test_physical_path_digest_and_size_rejections(self):
        self.land()
        verifier = SourceVerifier(self.root)
        for path, digest, size in [("../escape", GOLDEN_SHA, None), ("/absolute", GOLDEN_SHA, None),
                                   ("x\\y", GOLDEN_SHA, None), ("x#record=0001", GOLDEN_SHA, None),
                                   ("statement-0001.txt", "not-a-hash", None),
                                   ("statement-0001.txt", GOLDEN_SHA, 1)]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                verifier.verify(path, digest, size)

    def test_mock_end_to_end_without_network_or_subprocess(self):
        arguments = {"record_id": "statement-0001", "version": 1,
                     "approval_name": "SIMULATED_APPROVAL", "approved_scope": {
                         "record_id": "statement-0001", "version": 1}}
        with patch.object(socket, "socket", side_effect=AssertionError("network forbidden")), \
                patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden")), \
                patch.object(subprocess, "Popen", side_effect=AssertionError("process forbidden")):
            payload = mock_mcp.run(arguments, root=self.root)
            accepted = mock_mcp.accept_tool_payload(payload)
        self.assertTrue(accepted["physical_verified"])

    def test_mock_bad_scope_never_calls_collection(self):
        good = {"record_id": "statement-0001", "version": 1,
                "approval_name": "SIMULATED_APPROVAL", "approved_scope": {
                    "record_id": "statement-0001", "version": 1}}
        invalid = [None, {}, {**good, "version": True}, {**good, "root": "/wrong"},
                   {**good, "approval_name": ""}, {**good, "approved_scope": {
                       "record_id": "statement-0001", "version": 2}}]
        with patch("mock_mcp.collect_sample") as collect_mock:
            for arguments in invalid:
                payload = mock_mcp.run(arguments, root=self.root)
                with self.assertRaises(CollectionError):
                    mock_mcp.accept_tool_payload(payload)
            collect_mock.assert_not_called()

    def test_strict_mock_result_validation_rejects_outer_inner_disagreement(self):
        result = receipt(self.acquire(), self.root)
        valid = mock_mcp.InvokeResult(True, json.dumps(result), "", 0, result).as_tool_payload()
        bad = []
        for key, value in [("ok", False), ("ok", 1), ("returncode", True),
                           ("returncode", 1), ("result", None), ("result", "not-object"),
                           ("stdout", "malformed")]:
            bad.append({**valid, key: value})
        for location, key, value in [("result", "domain_ok", False),
                                      ("result", "physical_verified", False),
                                      ("landing", "readback_verified", False),
                                      ("landing", "landed", False), ("landing", "sha256", "bad"),
                                      ("landing", "size", True),
                                      ("landing", "destination", "../escape")]:
            payload = copy.deepcopy(valid)
            target = payload["result"] if location == "result" else payload["result"]["landing"]
            target[key] = value
            payload["stdout"] = json.dumps(payload["result"])
            bad.append(payload)
        missing = copy.deepcopy(valid)
        del missing["result"]["landing"]["readback_verified"]
        bad.append(missing)
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(CollectionError):
                mock_mcp.accept_tool_payload(payload)

    def test_completed_mock_invocation_can_contain_domain_failure(self):
        arguments = {"record_id": "statement-0001", "version": 1,
                     "approval_name": "SIMULATED_APPROVAL", "approved_scope": {
                         "record_id": "statement-0001", "version": 1}}
        with patch("mock_mcp.collect_sample", side_effect=CollectionError("refused")):
            payload = mock_mcp.run(arguments, root=self.root)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["returncode"], 0)
        self.assertFalse(payload["result"]["domain_ok"])
        with self.assertRaises(CollectionError):
            mock_mcp.accept_tool_payload(payload)

    def test_cli_sequence_and_safe_negative_demonstrations(self):
        import io
        for scenario, expected in [("happy", 0), ("happy", 0), ("revised", 0),
                                   ("wrong-identity", 1), ("tampered-source", 1)]:
            before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
            stdout = io.StringIO()
            with patch("sys.argv", ["sample.py", "--root", str(self.root), "--scenario", scenario]), \
                    patch("sys.stdout", stdout):
                status = main()
            self.assertEqual(status, expected)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["domain_ok"], expected == 0)
            self.assertNotIn(str(self.root), stdout.getvalue())
            if expected:
                self.assertEqual(payload["error"], "tamper_rejected" if
                                 scenario == "tampered-source" else "collection_rejected")
                after = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
                self.assertEqual(before, after)

    def test_tamper_demo_does_not_mislabel_failed_acquisition_as_detection(self):
        import io
        stdout = io.StringIO()
        with patch("sys.argv", ["sample.py", "--root", str(self.root), "--scenario", "tampered-source"]), \
                patch("sample.collect_sample", side_effect=CollectionError("preflight failed")), \
                patch("sys.stdout", stdout):
            self.assertEqual(main(), 1)
        self.assertEqual(json.loads(stdout.getvalue())["error"], "collection_rejected")


if __name__ == "__main__":
    unittest.main()
