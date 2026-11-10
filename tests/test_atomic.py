from __future__ import annotations

import base64
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from python_multipart import multipart as mp
from python_multipart.exceptions import FileConflictError, FileError, FormParserError
from python_multipart.multipart import File, FormParser


def atomic_config(upload_dir: str | bytes, **extra: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "UPLOAD_DIR": upload_dir,
        "UPLOAD_KEEP_FILENAME": True,
        "UPLOAD_ATOMIC": True,
        "MAX_MEMORY_FILE_SIZE": 1024,
    }
    config.update(extra)
    return config


def make_multipart(boundary: bytes, parts: list[tuple[bytes, bytes, bytes]]) -> bytes:
    """Build a multipart/form-data body from (field_name, file_name, data) parts."""
    chunks: list[bytes] = []
    for field_name, file_name, data in parts:
        chunks.append(b"--" + boundary + b"\r\n")
        chunks.append(b'Content-Disposition: form-data; name="' + field_name + b'"; filename="' + file_name + b'"\r\n')
        chunks.append(b"Content-Type: application/octet-stream\r\n\r\n")
        chunks.append(data)
        chunks.append(b"\r\n")
    chunks.append(b"--" + boundary + b"--\r\n")
    return b"".join(chunks)


def make_base64_multipart(boundary: bytes, field_name: bytes, file_name: bytes, data: bytes) -> bytes:
    encoded = base64.b64encode(data)
    return (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="' + field_name + b'"; filename="' + file_name + b'"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\n" + encoded + b"\r\n--" + boundary + b"--\r\n"
    )


def feed_in_chunks(parser: FormParser, body: bytes, chunk_size: int) -> None:
    for index in range(0, len(body), chunk_size):
        parser.write(body[index : index + chunk_size])
    parser.finalize()


def list_entries(path: Path) -> set[str]:
    return {entry.name for entry in path.iterdir()}


class TestAtomicConfigAndNames:
    def test_requires_upload_dir(self) -> None:
        with pytest.raises(FileError, match="UPLOAD_DIR"):
            File(b"a.txt", config={"UPLOAD_ATOMIC": True, "UPLOAD_KEEP_FILENAME": True})

    def test_requires_keep_filename(self, tmp_path: Path) -> None:
        with pytest.raises(FileError, match="UPLOAD_KEEP_FILENAME"):
            File(b"a.txt", config={"UPLOAD_ATOMIC": True, "UPLOAD_DIR": str(tmp_path)})

    def test_requires_non_empty_name(self, tmp_path: Path) -> None:
        with pytest.raises(FileError, match="non-empty"):
            File(b"", config=atomic_config(tmp_path))
        with pytest.raises(FileError, match="non-empty"):
            File(None, config=atomic_config(tmp_path))

    @pytest.mark.parametrize(
        "file_name", [b".", b"..", b"a/b.txt", b"a\\b.txt", b"/etc/passwd", b"a\x00b", b"/", b"\\"]
    )
    def test_rejects_unsafe_names(self, tmp_path: Path, file_name: bytes) -> None:
        with pytest.raises(FileError):
            File(file_name, config=atomic_config(tmp_path))
        # Nothing was created in the trusted upload directory.
        assert list_entries(tmp_path) == set()

    def test_accepts_plain_single_name(self, tmp_path: Path) -> None:
        file = File(b"plain name.txt", config=atomic_config(tmp_path))
        file.write(b"data")
        file.finalize()
        assert (tmp_path / "plain name.txt").read_bytes() == b"data"
        file.close()


class TestAtomicFileLifecycle:
    def test_small_file_still_publishes_through_temp(self, tmp_path: Path) -> None:
        # Under the memory threshold: no disk file may exist before finalize,
        # but finalize must still go through the temp-file publish flow.
        file = File(b"small.bin", config=atomic_config(tmp_path, MAX_MEMORY_FILE_SIZE=1024))
        file.write(b"tiny")
        assert file.in_memory
        assert file.actual_file_name is None
        assert list_entries(tmp_path) == set()

        file.finalize()
        assert file.actual_file_name == b"small.bin"
        assert not file.in_memory
        assert file.size == 4
        assert (tmp_path / "small.bin").read_bytes() == b"tiny"
        # The temporary name is gone; only the final name is visible.
        assert list_entries(tmp_path) == {"small.bin"}
        file.close()
        # close() must not remove a published file.
        assert (tmp_path / "small.bin").exists()

    def test_memory_spill_uses_exclusive_temp(self, tmp_path: Path) -> None:
        file = File(b"big.bin", config=atomic_config(tmp_path, MAX_MEMORY_FILE_SIZE=4))
        file.write(b"abcde")
        assert not file.in_memory
        assert file.actual_file_name is None
        entries = list_entries(tmp_path)
        assert len(entries) == 1
        (tmp_name,) = entries
        assert tmp_name.startswith(".python-multipart-")
        assert (tmp_path / tmp_name).read_bytes() == b"abcde"

        file.finalize()
        assert list_entries(tmp_path) == {"big.bin"}
        assert (tmp_path / "big.bin").read_bytes() == b"abcde"
        file.close()

    def test_bytes_upload_dir(self, tmp_path: Path) -> None:
        file = File(b"x.dat", config=atomic_config(os.fsencode(tmp_path)))
        file.write(b"bytes-dir")
        file.finalize()
        assert (tmp_path / "x.dat").read_bytes() == b"bytes-dir"
        file.close()

    @pytest.mark.parametrize("entry", ["existing.txt", "a_dir", "link.txt"])
    def test_existing_entries_are_conflicts(self, tmp_path: Path, entry: str) -> None:
        if entry == "existing.txt":
            (tmp_path / entry).write_bytes(b"original")
        elif entry == "a_dir":
            (tmp_path / entry).mkdir()
        else:
            target = tmp_path / "target.txt"
            target.write_bytes(b"linked")
            os.symlink(target, tmp_path / entry)

        file = File(entry.encode(), config=atomic_config(tmp_path))
        file.write(b"new contents")
        with pytest.raises(FileConflictError, match="already exists"):
            file.finalize()

        # No temporary file remains, and the original entry is untouched.
        temp_leftovers = [name for name in list_entries(tmp_path) if name.startswith(".python-multipart-")]
        assert temp_leftovers == []
        if entry == "existing.txt":
            assert (tmp_path / entry).read_bytes() == b"original"
        elif entry == "a_dir":
            assert (tmp_path / entry).is_dir()
        else:
            assert os.path.islink(tmp_path / entry)
            assert (tmp_path / "target.txt").read_bytes() == b"linked"

        # State is self-consistent after the failure.
        assert file.actual_file_name is None
        assert file.size == len(b"new contents")
        assert file.file_object.closed

    def test_concurrent_same_name_only_one_wins(self, tmp_path: Path) -> None:
        barrier = threading.Barrier(2)
        outcomes: list[object] = []

        def worker() -> None:
            file = File(b"race.bin", config=atomic_config(tmp_path))
            file.write(b"payload")
            barrier.wait()
            try:
                file.finalize()
            except FileConflictError as exc:
                outcomes.append(exc)
            else:
                outcomes.append("ok")
                file.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sorted(type(item).__name__ for item in outcomes) == ["FileConflictError", "str"]
        assert list_entries(tmp_path) == {"race.bin"}
        assert (tmp_path / "race.bin").read_bytes() == b"payload"

    def test_concurrent_distinct_names_both_succeed(self, tmp_path: Path) -> None:
        files = [File(b"one.bin", config=atomic_config(tmp_path)), File(b"two.bin", config=atomic_config(tmp_path))]
        for file in files:
            file.write(b"data")
        for file in files:
            file.finalize()
            file.close()
        assert list_entries(tmp_path) == {"one.bin", "two.bin"}

    def test_close_before_finalize_removes_temp_only(self, tmp_path: Path) -> None:
        foreign = tmp_path / "foreign.tmp"
        foreign.write_bytes(b"foreign")

        file = File(b"spill.bin", config=atomic_config(tmp_path, MAX_MEMORY_FILE_SIZE=1))
        file.write(b"overflow")
        assert len(list_entries(tmp_path)) == 2
        file.close()

        # Our temp is gone; the unrelated file is untouched.
        assert list_entries(tmp_path) == {"foreign.tmp"}
        assert foreign.read_bytes() == b"foreign"
        assert file.actual_file_name is None

    def test_repeated_close_is_idempotent(self, tmp_path: Path) -> None:
        file = File(b"close.bin", config=atomic_config(tmp_path))
        file.write(b"data")
        file.close()
        file.close()
        assert list_entries(tmp_path) == set()

    def test_repeated_finalize_is_idempotent_after_success(self, tmp_path: Path) -> None:
        file = File(b"once.bin", config=atomic_config(tmp_path))
        file.write(b"data")
        file.finalize()
        file.finalize()
        assert file.actual_file_name == b"once.bin"
        file.close()

    def test_finalize_after_failure_raises_and_leaves_no_residue(self, tmp_path: Path) -> None:
        (tmp_path / "taken.bin").write_bytes(b"original")
        file = File(b"taken.bin", config=atomic_config(tmp_path))
        file.write(b"new")
        with pytest.raises(FileConflictError):
            file.finalize()
        with pytest.raises(FileError, match="already failed"):
            file.finalize()
        assert (tmp_path / "taken.bin").read_bytes() == b"original"
        assert list_entries(tmp_path) == {"taken.bin"}

    def test_write_after_finalize_is_rejected(self, tmp_path: Path) -> None:
        file = File(b"sealed.bin", config=atomic_config(tmp_path))
        file.write(b"data")
        file.finalize()
        with pytest.raises(FileError, match="finalized"):
            file.write(b"more")
        file.close()

    def test_state_is_consistent_after_spill_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        file = File(b"x.bin", config=atomic_config(tmp_path, MAX_MEMORY_FILE_SIZE=1))

        def boom_copy(src: Any, dst: Any) -> None:
            raise OSError("copy boom")

        monkeypatch.setattr(mp.shutil, "copyfileobj", boom_copy)
        with pytest.raises(OSError, match="copy boom"):
            file.write(b"overflow")

        # Nothing was published, the bytes counter is truthful, and the
        # in-memory object remains open and readable.
        assert file.actual_file_name is None
        assert file.size == 8
        assert file.in_memory
        assert not file.file_object.closed
        file.file_object.seek(0)
        assert file.file_object.read() == b"overflow"

    def test_legacy_finalize_and_close_remain_uncoupled(self) -> None:
        file = File(b"legacy.txt")
        file.write(b"data")
        file.finalize()
        # Legacy finalize does not close, and can be called repeatedly.
        assert not file.file_object.closed
        file.finalize()
        assert not file.file_object.closed
        file.close()
        assert file.file_object.closed


class TestAtomicFaultInjection:
    def test_mkstemp_failure_cleans_in_memory_buffer(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        missing = tmp_path / "missing"
        file = File(b"x.bin", config=atomic_config(missing, MAX_MEMORY_FILE_SIZE=1))
        with pytest.raises(FileError):
            file.write(b"overflow")
        # The in-memory buffer is still consistent, and nothing was created.
        assert file.size == 8
        assert file.in_memory
        assert list_entries(tmp_path) == set()

    def test_fdopen_failure_closes_fd_and_unlinks_temp(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        real_fdopen = os.fdopen
        closed_fds: list[int] = []
        unlinked: list[object] = []

        def boom_fdopen(fd: int, mode: str) -> Any:
            raise OSError("fdopen boom")

        monkeypatch.setattr(mp.os, "fdopen", boom_fdopen)
        monkeypatch.setattr(mp.os, "close", lambda fd: closed_fds.append(fd))
        monkeypatch.setattr(mp.os, "unlink", lambda path: unlinked.append(path))

        file = File(b"x.bin", config=atomic_config(tmp_path))
        with pytest.raises(OSError, match="fdopen boom"):
            file.finalize()

        assert len(closed_fds) == 1
        assert len(unlinked) == 1
        assert file.actual_file_name is None

        # The mocked unlink only recorded the call, so delete for real after
        # restoring the implementations, and confirm nothing was left behind.
        monkeypatch.undo()
        for leftover in tmp_path.iterdir():
            leftover.unlink()
        assert list_entries(tmp_path) == set()

    def test_disk_copy_failure_removes_temp(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        file = File(b"x.bin", config=atomic_config(tmp_path, MAX_MEMORY_FILE_SIZE=1))

        def boom_copy(src: Any, dst: Any) -> None:
            raise OSError("copy boom")

        monkeypatch.setattr(mp.shutil, "copyfileobj", boom_copy)
        with pytest.raises(OSError, match="copy boom"):
            file.write(b"overflow")
        assert list_entries(tmp_path) == set()
        # The failed spill leaves the in-memory state intact; after the fault
        # is cleared the same File can still publish all received data.
        monkeypatch.undo()
        file.finalize()
        assert (tmp_path / "x.bin").read_bytes() == b"overflow"
        file.close()

    def test_publish_oserror_rolls_back(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom_link(src: Any, dst: Any, follow_symlinks: bool = True) -> None:
            raise OSError(28, "no space")

        monkeypatch.setattr(mp.os, "link", boom_link)
        file = File(b"x.bin", config=atomic_config(tmp_path))
        file.write(b"data")
        with pytest.raises(FileError, match="publishing"):
            file.finalize()
        assert list_entries(tmp_path) == set()
        assert file.file_object.closed

    def test_temp_unlink_failure_removes_new_final_name(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        real_unlink = os.unlink
        call_count = 0

        def selective_unlink(path: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("unlink boom")
            real_unlink(path)

        monkeypatch.setattr(mp.os, "unlink", selective_unlink)
        file = File(b"x.bin", config=atomic_config(tmp_path))
        file.write(b"data")
        with pytest.raises(FileError, match="removing temporary"):
            file.finalize()

        # The just-created final name must be undone, and the directory empty.
        assert list_entries(tmp_path) == set()
        assert file.file_object.closed

    def test_rollback_never_touches_foreign_temp(self, tmp_path: Path) -> None:
        foreign = tmp_path / ".python-multipart-foreign"
        foreign.write_bytes(b"other request")
        (tmp_path / "final.bin").write_bytes(b"original")

        file = File(b"final.bin", config=atomic_config(tmp_path))
        file.write(b"new")
        with pytest.raises(FileConflictError):
            file.finalize()

        assert foreign.read_bytes() == b"other request"
        assert (tmp_path / "final.bin").read_bytes() == b"original"


class TestAtomicFormParser:
    def test_chunked_multipart_spill_publishes_and_hands_off(self, tmp_path: Path) -> None:
        boundary = b"----test-boundary"
        payload = b"x" * 5000
        body = make_multipart(boundary, [(b"upload", b"report.bin", payload)])
        handed: list[File] = []

        parser = FormParser(
            "multipart/form-data",
            None,
            handed.append,
            boundary=boundary,
            config=atomic_config(tmp_path, MAX_MEMORY_FILE_SIZE=128),
        )
        feed_in_chunks(parser, body, 7)

        assert len(handed) == 1
        uploaded = handed[0]
        assert uploaded.actual_file_name == b"report.bin"
        assert uploaded.size == len(payload)
        assert (tmp_path / "report.bin").read_bytes() == payload
        assert list_entries(tmp_path) == {"report.bin"}
        # Handoff means the caller now owns the handle and final file.
        assert not uploaded.file_object.closed
        uploaded.close()
        assert (tmp_path / "report.bin").exists()

    def test_chunked_small_file_goes_through_publish(self, tmp_path: Path) -> None:
        boundary = b"b"
        body = make_multipart(boundary, [(b"f", b"small.txt", b"hi")])
        handed: list[File] = []
        parser = FormParser(
            "multipart/form-data", None, handed.append, boundary=boundary, config=atomic_config(tmp_path)
        )
        feed_in_chunks(parser, body, 3)
        uploaded = handed[0]
        assert uploaded.actual_file_name == b"small.txt"
        assert (tmp_path / "small.txt").read_bytes() == b"hi"
        uploaded.close()

    def test_multiple_parts_each_get_own_temp_and_name(self, tmp_path: Path) -> None:
        boundary = b"b"
        body = make_multipart(boundary, [(b"f1", b"one.dat", b"a" * 2000), (b"f2", b"two.dat", b"bb")])
        handed: list[File] = []
        parser = FormParser(
            "multipart/form-data",
            None,
            handed.append,
            boundary=boundary,
            config=atomic_config(tmp_path, MAX_MEMORY_FILE_SIZE=64),
        )
        feed_in_chunks(parser, body, 13)
        assert [file.actual_file_name for file in handed] == [b"one.dat", b"two.dat"]
        assert list_entries(tmp_path) == {"one.dat", "two.dat"}
        for file in handed:
            file.close()

    def test_fields_and_files_coexist(self, tmp_path: Path) -> None:
        boundary = b"b"
        body = (
            b"--b\r\n"
            b'Content-Disposition: form-data; name="note"\r\n\r\n'
            b"hello\r\n"
            b"--b\r\n"
            b'Content-Disposition: form-data; name="f"; filename="f.bin"\r\n\r\n'
            b"payload\r\n"
            b"--b--\r\n"
        )
        fields: list[Any] = []
        files: list[File] = []
        parser = FormParser(
            "multipart/form-data", fields.append, files.append, boundary=boundary, config=atomic_config(tmp_path)
        )
        feed_in_chunks(parser, body, 5)
        assert fields[0].value == b"hello"
        assert files[0].actual_file_name == b"f.bin"
        files[0].close()

    def test_conflict_via_parser_keeps_original_and_cleans_temp(self, tmp_path: Path) -> None:
        (tmp_path / "dup.bin").write_bytes(b"original")
        boundary = b"b"
        body = make_multipart(boundary, [(b"f", b"dup.bin", b"new")])
        parser = FormParser(
            "multipart/form-data", None, lambda file: None, boundary=boundary, config=atomic_config(tmp_path)
        )
        with pytest.raises(FileConflictError):
            feed_in_chunks(parser, body, 11)
        assert (tmp_path / "dup.bin").read_bytes() == b"original"
        assert list_entries(tmp_path) == {"dup.bin"}
        parser.close()
        assert list_entries(tmp_path) == {"dup.bin"}

    def test_on_file_exception_withdraws_publication(self, tmp_path: Path) -> None:
        boundary = b"b"
        body = make_multipart(boundary, [(b"f", b"cb.bin", b"data")])

        def boom(_file: File) -> None:
            raise RuntimeError("callback boom")

        parser = FormParser("multipart/form-data", None, boom, boundary=boundary, config=atomic_config(tmp_path))
        with pytest.raises(RuntimeError, match="callback boom"):
            feed_in_chunks(parser, body, 5)
        assert list_entries(tmp_path) == set()

    def test_on_file_exception_does_not_delete_handoff_of_earlier_part(self, tmp_path: Path) -> None:
        boundary = b"b"
        body = make_multipart(boundary, [(b"f1", b"first.bin", b"one"), (b"f2", b"second.bin", b"two")])
        seen: list[File] = []

        def callback(file: File) -> None:
            seen.append(file)
            if len(seen) == 2:
                raise RuntimeError("second callback boom")

        parser = FormParser("multipart/form-data", None, callback, boundary=boundary, config=atomic_config(tmp_path))
        with pytest.raises(RuntimeError, match="second callback boom"):
            feed_in_chunks(parser, body, 7)
        # First upload was handed off and stays; second is withdrawn.
        assert list_entries(tmp_path) == {"first.bin"}
        assert (tmp_path / "first.bin").read_bytes() == b"one"

    def test_base64_decode_failure_cleans_everything(self, tmp_path: Path) -> None:
        boundary = b"b"
        body = (
            b"--b\r\n"
            b'Content-Disposition: form-data; name="f"; filename="b64.bin"\r\n'
            b"Content-Transfer-Encoding: base64\r\n\r\n"
            b"@@@bad\r\n--b--\r\n"
        )
        parser = FormParser(
            "multipart/form-data", None, lambda file: None, boundary=boundary, config=atomic_config(tmp_path)
        )
        with pytest.raises(FormParserError):
            feed_in_chunks(parser, body, 4)
        assert list_entries(tmp_path) == set()

    def test_base64_success_publishes_decoded_data(self, tmp_path: Path) -> None:
        boundary = b"b"
        payload = b"decoded payload \x00\x01"
        body = make_base64_multipart(boundary, b"f", b"b64.bin", payload)
        handed: list[File] = []
        parser = FormParser(
            "multipart/form-data", None, handed.append, boundary=boundary, config=atomic_config(tmp_path)
        )
        feed_in_chunks(parser, body, 5)
        assert (tmp_path / "b64.bin").read_bytes() == payload
        handed[0].close()

    def test_disk_write_failure_during_parse_leaves_no_residue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        boundary = b"b"
        body = make_multipart(boundary, [(b"f", b"x.bin", b"overflow-data")])

        def boom_copy(src: Any, dst: Any) -> None:
            raise OSError("copy boom")

        monkeypatch.setattr(mp.shutil, "copyfileobj", boom_copy)
        parser = FormParser(
            "multipart/form-data",
            None,
            lambda file: None,
            boundary=boundary,
            config=atomic_config(tmp_path, MAX_MEMORY_FILE_SIZE=1),
        )
        with pytest.raises(OSError, match="copy boom"):
            feed_in_chunks(parser, body, 3)
        assert list_entries(tmp_path) == set()

    def test_close_mid_request_cleans_unfinished_uploads(self, tmp_path: Path) -> None:
        boundary = b"b"
        body = make_multipart(boundary, [(b"f", b"unfinished.bin", b"partial")])
        parser = FormParser(
            "multipart/form-data",
            None,
            lambda file: None,
            boundary=boundary,
            config=atomic_config(tmp_path, MAX_MEMORY_FILE_SIZE=1),
        )
        parser.write(body[:20])
        parser.close()
        assert list_entries(tmp_path) == set()

    def test_unsafe_name_rejected_through_parser(self, tmp_path: Path) -> None:
        boundary = b"b"
        body = make_multipart(boundary, [(b"f", b"../evil.bin", b"x")])
        parser = FormParser(
            "multipart/form-data", None, lambda file: None, boundary=boundary, config=atomic_config(tmp_path)
        )
        with pytest.raises(FileError, match="Path separators"):
            feed_in_chunks(parser, body, 6)
        assert list_entries(tmp_path) == set()

    def test_octet_stream_atomic_flow(self, tmp_path: Path) -> None:
        handed: list[File] = []
        parser = FormParser(
            "application/octet-stream", None, handed.append, file_name=b"octet.bin", config=atomic_config(tmp_path)
        )
        feed_in_chunks(parser, b"octet payload", 4)
        assert (tmp_path / "octet.bin").read_bytes() == b"octet payload"
        handed[0].close()

    def test_octet_stream_rejects_unsafe_name_before_any_data(self, tmp_path: Path) -> None:
        parser = FormParser(
            "application/octet-stream",
            None,
            lambda file: None,
            file_name=b"../evil.bin",
            config=atomic_config(tmp_path),
        )
        with pytest.raises(FileError, match="Path separators"):
            parser.write(b"x")
        assert list_entries(tmp_path) == set()

    def test_parser_close_after_success_is_idempotent(self, tmp_path: Path) -> None:
        boundary = b"b"
        body = make_multipart(boundary, [(b"f", b"ok.bin", b"data")])
        handed: list[File] = []
        parser = FormParser(
            "multipart/form-data", None, handed.append, boundary=boundary, config=atomic_config(tmp_path)
        )
        feed_in_chunks(parser, body, 5)
        handed[0].close()
        # The parser has nothing left to manage; closing it must not touch
        # the handed-off file.
        parser.close()
        parser.close()
        assert (tmp_path / "ok.bin").read_bytes() == b"data"

    def test_concurrent_parsers_same_name_one_winner(self, tmp_path: Path) -> None:
        boundary = b"b"
        body = make_multipart(boundary, [(b"f", b"shared.bin", b"same")])
        barrier = threading.Barrier(2)
        results: list[object] = []

        def worker() -> None:
            handed: list[File] = []
            parser = FormParser(
                "multipart/form-data", None, handed.append, boundary=boundary, config=atomic_config(tmp_path)
            )
            barrier.wait()
            try:
                feed_in_chunks(parser, body, 3)
            except FileConflictError as exc:
                results.append(exc)
            else:
                results.append("ok")
                handed[0].close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sorted(type(item).__name__ for item in results) == ["FileConflictError", "str"]
        assert list_entries(tmp_path) == {"shared.bin"}


class TestLegacyModeCompatibility:
    def test_atomic_disabled_overwrites_as_before(self, tmp_path: Path) -> None:
        (tmp_path / "legacy.bin").write_bytes(b"original")
        file = File(
            b"legacy.bin",
            config={
                "UPLOAD_DIR": str(tmp_path),
                "UPLOAD_KEEP_FILENAME": True,
                "UPLOAD_KEEP_EXTENSIONS": True,
                "MAX_MEMORY_FILE_SIZE": 1,
            },
        )
        file.write(b"new")
        file.finalize()
        assert (tmp_path / "legacy.bin").read_bytes() == b"new"
        assert file.actual_file_name == b"legacy.bin"
        file.close()

    def test_legacy_mode_strips_directory_components(self, tmp_path: Path) -> None:
        file = File(
            b"../escape.bin",
            config={
                "UPLOAD_DIR": str(tmp_path),
                "UPLOAD_KEEP_FILENAME": True,
                "UPLOAD_KEEP_EXTENSIONS": True,
                "MAX_MEMORY_FILE_SIZE": 1,
            },
        )
        file.write(b"data")
        file.finalize()
        assert file.actual_file_name == b"escape.bin"
        assert (tmp_path / "escape.bin").read_bytes() == b"data"
        file.close()

    def test_legacy_form_parser_unchanged(self, tmp_path: Path) -> None:
        boundary = b"b"
        body = make_multipart(boundary, [(b"f", b"../legacy.bin", b"data")])
        handed: list[File] = []
        parser = FormParser(
            "multipart/form-data",
            None,
            handed.append,
            boundary=boundary,
            config={
                "UPLOAD_DIR": str(tmp_path),
                "UPLOAD_KEEP_FILENAME": True,
                "UPLOAD_KEEP_EXTENSIONS": True,
                "UPLOAD_ATOMIC": False,
                "MAX_MEMORY_FILE_SIZE": 1,
            },
        )
        feed_in_chunks(parser, body, 9)
        assert (tmp_path / "legacy.bin").read_bytes() == b"data"
        handed[0].close()

    def test_legacy_nameless_temp_file_still_works(self) -> None:
        file = File(None, config={"UPLOAD_KEEP_EXTENSIONS": True, "MAX_MEMORY_FILE_SIZE": 10})
        file.write(b"123456789012")
        assert not file.in_memory
        assert file.actual_file_name is not None
        file.close()
