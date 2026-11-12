"""Offline prototype tests. No registry, application modules, browser, or real inbox."""

import concurrent.futures
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

PROTOTYPE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROTOTYPE))
import host
import prepare_host

ORIGIN = "chrome-extension://" + "a" * 32 + "/"


def temporary_directory():
    root = PROTOTYPE / "tests" / ".runtime"
    root.mkdir(exist_ok=True)
    return tempfile.TemporaryDirectory(dir=root)


def capture(**changes):
    result = {"version": 1, "type": "capture", "capture_id": str(uuid.uuid4()),
              "text": "  勇者 ÉTÉ 🌸\nRe:Zero\t  ", "captured_at": "2026-11-07T12:34:56.789Z"}
    result.update(changes)
    return result


def frame(message):
    stream = io.BytesIO()
    host.write_message(stream, message)
    return stream.getvalue()


class ShortReads(io.BytesIO):
    def read(self, size=-1):
        return super().read(min(size, 2))


class ProtocolTests(unittest.TestCase):
    def test_unicode_byte_length_and_partial_reads(self):
        message = capture()
        raw = frame(message)
        self.assertEqual(len(raw) - 4, host.HEADER.unpack(raw[:4])[0])
        self.assertGreater(len(raw[4:]), len(raw[4:].decode("utf-8")))
        self.assertEqual(host.read_message(ShortReads(raw)), message)

    def test_empty_input_is_clean_eof(self):
        self.assertIs(host.read_message(io.BytesIO()), host.END_OF_INPUT)

    def test_json_null_and_unsupported_fields_are_rejected_not_eof(self):
        with temporary_directory() as directory:
            inbox = Path(directory) / "inbox"
            for message in (None, [], capture(path="not-allowed"), capture(text="\ud800")):
                output = io.BytesIO()
                # ensure_ascii encodes the deliberately invalid lone surrogate.
                raw = json.dumps(message, ensure_ascii=True).encode("utf-8")
                result = host.serve(io.BytesIO(host.HEADER.pack(len(raw)) + raw), output, inbox)
                self.assertEqual(result, 1)
                self.assertEqual(host.read_message(io.BytesIO(output.getvalue()))["error"], "invalid_message")
                self.assertFalse(inbox.exists())

    def test_complete_maximum_unicode_round_trip(self):
        with temporary_directory() as directory:
            inbox = Path(directory) / "inbox"
            message = capture(text="🌸" * 4096)
            output = io.BytesIO()
            self.assertEqual(host.serve(ShortReads(frame(message)), output, inbox), 0)
            self.assertEqual(host.read_message(io.BytesIO(output.getvalue()))["capture_id"], message["capture_id"])
            self.assertEqual(json.loads(next(inbox.glob("*.json")).read_text(encoding="utf-8")), message)

    def test_invalid_frames_and_json(self):
        invalid = [b"\x01", host.HEADER.pack(10) + b"{}", host.HEADER.pack(0),
                   host.HEADER.pack(host.MAX_FRAME_BYTES + 1),
                   host.HEADER.pack(1) + b"\xff", host.HEADER.pack(1) + b"{",
                   host.HEADER.pack(13) + b'{"a":1,"a":2}', host.HEADER.pack(3) + b"NaN"]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(host.CaptureError):
                host.read_message(io.BytesIO(raw))

    def test_invalid_schema_values_and_unsupported_fields(self):
        invalid = [None, [], {}, capture(path="C:\\not-allowed"), capture(command="cmd.exe"),
                   capture(version=True), capture(version=2), capture(type="lookup"),
                   capture(capture_id="../../escape"), capture(capture_id="A" * 36),
                   capture(capture_id=12), capture(capture_id=str(uuid.uuid1())),
                   capture(text=" \t\n"), capture(text=None), capture(text="a\x00b"),
                   capture(text="a\x1bb"), capture(text="\ud800"),
                   capture(text="a" * 4097), capture(text="🌸" * 4097),
                   capture(captured_at="yesterday"), capture(captured_at="2026-02-30T00:00:00Z")]
        for message in invalid:
            with self.subTest(message=repr(message)[:100]), self.assertRaises(host.CaptureError):
                host.validate_capture(message)
        missing = capture()
        del missing["text"]
        with self.assertRaises(host.CaptureError):
            host.validate_capture(missing)

    def test_maximum_unicode_text_and_exact_whitespace_preserved(self):
        self.assertEqual(host.validate_capture(capture(text="🌸" * 4096))["text"], "🌸" * 4096)
        message = capture()
        self.assertEqual(host.validate_capture(message), message)

    def test_error_response_is_framed_not_plaintext(self):
        with temporary_directory() as directory:
            destination = io.BytesIO()
            diagnostic = io.StringIO()
            with contextlib.redirect_stderr(diagnostic):
                result = host.serve(io.BytesIO(host.HEADER.pack(999999)), destination, Path(directory) / "inbox")
            self.assertEqual(result, 1)
            self.assertEqual(host.read_message(io.BytesIO(destination.getvalue()))["error"], "protocol_error")
            self.assertFalse((Path(directory) / "inbox").exists())


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory()
        self.addCleanup(self.temporary.cleanup)
        self.inbox = Path(self.temporary.name) / "test inbox 勇者" / "inbox"

    def test_write_and_retry_one_file(self):
        message = capture()
        for expected in ("saved", "already_saved", "already_saved"):
            self.assertEqual(host.save_capture(self.inbox, message), expected)
        files = list(self.inbox.iterdir())
        self.assertEqual([file.name for file in files], [message["capture_id"] + ".json"])
        self.assertEqual(json.loads(files[0].read_text(encoding="utf-8")), message)

    def test_conflicting_uuid_never_overwrites(self):
        message = capture()
        host.save_capture(self.inbox, message)
        with self.assertRaises(host.CaptureError) as error:
            host.save_capture(self.inbox, {**message, "text": "Different title"})
        self.assertEqual(error.exception.code, "id_conflict")
        self.assertEqual(json.loads(next(self.inbox.glob("*.json")).read_text(encoding="utf-8")), message)

    def test_corrupt_existing_file_not_overwritten(self):
        message = capture()
        self.inbox.mkdir(parents=True)
        path = self.inbox / (message["capture_id"] + ".json")
        path.write_bytes(b"broken")
        with self.assertRaises(host.CaptureError):
            host.save_capture(self.inbox, message)
        self.assertEqual(path.read_bytes(), b"broken")

    def test_concurrent_duplicate_publication(self):
        message = capture()
        # Exercise atomic publication, not Windows' transient access errors when
        # multiple threads create a fresh nested fixture directory simultaneously.
        self.inbox.mkdir(parents=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(lambda _: host.save_capture(self.inbox, message), range(16)))
        self.assertEqual(statuses.count("saved"), 1)
        self.assertEqual(statuses.count("already_saved"), 15)
        self.assertEqual(len(list(self.inbox.iterdir())), 1)

    def test_distinct_captures_of_identical_text_remain_distinct(self):
        for _ in range(2):
            host.save_capture(self.inbox, capture(text="Same title"))
        self.assertEqual(len(list(self.inbox.glob("*.json"))), 2)

    def test_write_failure_not_acknowledged_and_no_partial_files(self):
        for target, operation in ((host.os, "fsync"), (host, "_publish")):
            with self.subTest(operation=operation), patch.object(target, operation, side_effect=OSError("test failure")):
                output = io.BytesIO()
                self.assertEqual(host.serve(io.BytesIO(frame(capture())), output, self.inbox), 1)
                self.assertEqual(host.read_message(io.BytesIO(output.getvalue()))["error"], "write_failed")
            self.assertEqual(list(self.inbox.iterdir()), [])

    def test_directory_write_failure(self):
        blocker = Path(self.temporary.name) / "not-a-directory"
        blocker.write_text("keep", encoding="utf-8")
        output = io.BytesIO()
        host.serve(io.BytesIO(frame(capture())), output, blocker / "inbox")
        self.assertEqual(host.read_message(io.BytesIO(output.getvalue()))["error"], "write_failed")
        self.assertEqual(blocker.read_text(encoding="utf-8"), "keep")

    def test_file_is_fsynced_before_publication(self):
        events = []
        actual_fsync, actual_publish = host.os.fsync, host._publish
        def fsync(fd):
            events.append("flush")
            actual_fsync(fd)
        def publish(source, target):
            events.append("publish")
            self.assertEqual(json.loads(Path(source).read_text(encoding="utf-8"))["text"], "Atomic")
            actual_publish(source, target)
        with patch.object(host.os, "fsync", fsync), patch.object(host, "_publish", publish):
            host.save_capture(self.inbox, capture(text="Atomic"))
        self.assertEqual(events, ["flush", "publish"])


class ConfigurationTests(unittest.TestCase):
    def test_isolated_default_location(self):
        with temporary_directory() as directory, patch.dict(os.environ, {"LOCALAPPDATA": directory}):
            self.assertEqual(host.prototype_dir(), Path(directory) / "ExternalDiskIndexer" / "NativeMessagingPrototype")
        with patch.dict(os.environ, {"LOCALAPPDATA": ""}), self.assertRaises(host.CaptureError):
            host.prototype_dir()

    @unittest.skipUnless(os.name == "nt", "Windows host preparation")
    def test_generation_only_and_origin_allowlist(self):
        with temporary_directory() as directory:
            root = Path(directory) / "prototype 勇者"
            generated = prepare_host.prepare(root, Path(sys.executable), {"chrome": "a" * 32, "edge": "b" * 32})
            self.assertEqual(len(generated), 9)
            config = json.loads((root / "host.json").read_text(encoding="utf-8"))
            self.assertEqual(config["name"], host.HOST_NAME)
            self.assertEqual(config["allowed_origins"], [ORIGIN, "chrome-extension://" + "b" * 32 + "/"])
            host.validate_origin(ORIGIN, root)
            for origin in ("", "https://example.com/", "chrome-extension://" + "c" * 32 + "/"):
                with self.assertRaises(host.CaptureError):
                    host.validate_origin(origin, root)
            for browser, vendor in (("chrome", "Google\\Chrome"), ("edge", "Microsoft\\Edge")):
                registration = (root / f"register-{browser}.reg").read_text(encoding="utf-16")
                self.assertIn("HKEY_CURRENT_USER\\Software\\" + vendor, registration)
                self.assertNotIn("HKEY_LOCAL_MACHINE", registration)
                self.assertIn(str(root / "host.json").replace("\\", "\\\\"), registration)
                self.assertIn("[-HKEY_CURRENT_USER", (root / f"unregister-{browser}.reg").read_text(encoding="utf-16"))
            self.assertFalse((root / "inbox").exists())
            self.assertNotIn("winreg", (root / "host.py").read_text(encoding="utf-8"))
            self.assertNotIn("winreg", (PROTOTYPE / "prepare_host.py").read_text(encoding="utf-8"))

    def test_destination_is_explicit_and_defaults_to_prototype(self):
        with temporary_directory() as directory:
            root = Path(directory) / "NativeMessagingPrototype"
            root.mkdir()
            self.assertEqual(host.capture_destination(root), (root / "inbox", "prototype"))
            (root / "destination.json").write_text('{"inbox":"production"}', encoding="utf-8")
            self.assertEqual(host.capture_destination(root), (root.parent / "imports" / "inbox", "production"))
            for invalid in ('{"inbox":"C:/arbitrary"}', '{"inbox":"production","path":"C:/bad"}', '{'):
                (root / "destination.json").write_text(invalid, encoding="utf-8")
                with self.assertRaises(host.CaptureError):
                    host.capture_destination(root)

    def test_bad_extension_configuration_does_not_create_files(self):
        with temporary_directory() as directory:
            root = Path(directory) / "unused"
            for ids in ({}, {"chrome": "*"}, {"edge": "../../escape"}, {"other": "a" * 32}):
                with self.assertRaises(ValueError):
                    prepare_host.prepare(root, Path(sys.executable), ids)
            self.assertFalse(root.exists())

    def test_unprepared_host_origin_denied(self):
        with temporary_directory() as directory, self.assertRaises(host.CaptureError):
            host.validate_origin(ORIGIN, Path(directory))


class ProcessTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows launcher and CREATE_NO_WINDOW")
    def test_standalone_cmd_launcher_hidden_process_and_retry(self):
        with temporary_directory() as directory:
            local = Path(directory) / "local app data 勇者"
            root = local / "ExternalDiskIndexer" / "NativeMessagingPrototype"
            prepare_host.prepare(root, Path(sys.executable), {"chrome": "a" * 32})
            env = {**os.environ, "LOCALAPPDATA": str(local)}
            command = '"' + os.environ["COMSPEC"] + '" /d /s /c ""' + str(root / "launch_host.cmd") + '" ' + ORIGIN + ' --parent-window=0"'
            message = capture()
            for expected in ("saved", "already_saved"):
                result = subprocess.run(command, input=frame(message), capture_output=True,
                                        env=env, cwd=directory, timeout=15,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
                output = io.BytesIO(result.stdout)
                self.assertEqual(host.read_message(output)["status"], expected)
                self.assertEqual(output.read(), b"")  # No stdout diagnostics/codepage output.
                self.assertIn(b"console attached=False visible=False", result.stderr)
            self.assertEqual(len(list((root / "inbox").iterdir())), 1)
            self.assertFalse((local / "ExternalDiskIndexer" / "index.db").exists())
            self.assertFalse((local / "ExternalDiskIndexer" / "imports").exists())

    @unittest.skipUnless(os.name == "nt", "Windows native host process")
    def test_unauthorized_origin_cannot_write(self):
        with temporary_directory() as directory:
            env = {**os.environ, "LOCALAPPDATA": directory}
            result = subprocess.run([sys.executable, "-I", "-B", "-u", str(PROTOTYPE / "host.py"), ORIGIN],
                                    input=frame(capture()), capture_output=True, env=env, timeout=15,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(host.read_message(io.BytesIO(result.stdout))["error"], "unauthorized")
            self.assertFalse((Path(directory) / "ExternalDiskIndexer").exists())

    @unittest.skipUnless(shutil.which("node"), "Node is optional for offline extension checks")
    def test_extension_outbox_and_manual_retry(self):
        result = subprocess.run([shutil.which("node"), str(Path(__file__).with_name("test_extension.cjs"))],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Extension offline checks passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
