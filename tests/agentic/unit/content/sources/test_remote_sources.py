# Copyright 2026 Firefly Software Foundation
# Licensed under the Apache License, Version 2.0.
"""Provider HTTP contract tests; these do not establish live delegated acceptance."""

import hashlib

import httpx
import pytest

from pyfly.agentic.content.sources.dtos import Account, Cursor, SourceScope
from pyfly.agentic.content.sources.errors import SourceError
from pyfly.agentic.content.sources.google_drive import GoogleDriveSource
from pyfly.agentic.content.sources.microsoft import OneDriveSource, SharePointSource


class Tokens:
    def __init__(self):
        self.accounts = []

    async def access_token(self, account):
        self.accounts.append(account)
        return "synthetic-delegated-token"


class Sink:
    def __init__(self):
        self.chunks = []

    async def write(self, chunk):
        self.chunks.append(chunk)


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, fail=False):
        self.chunks = chunks
        self.fail = fail
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.fail:
            raise httpx.ReadError("sensitive URL and token must not escape")

    async def aclose(self):
        self.closed = True


def graph_item(**changes):
    return {
        "id": "f1",
        "name": "report.docx",
        "eTag": '"v1"',
        "size": 6,
        "file": {"mimeType": "application/octet-stream"},
        "parentReference": {"driveId": "drive", "id": "folder"},
        **changes,
    }


def google_item(**changes):
    return {
        "id": "f1",
        "name": "report.docx",
        "version": "1",
        "size": "6",
        "mimeType": "application/octet-stream",
        "parents": ["folder"],
        "capabilities": {"canDownload": True},
        **changes,
    }


@pytest.mark.parametrize("cls,provider", [(OneDriveSource, "onedrive"), (SharePointSource, "sharepoint")])
async def test_graph_ordered_pages_tombstone_and_bound_checkpoint(cls, provider):
    scope = SourceScope(Account(provider, "person", "tenant"), "drive")
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.params.get("token") == "next":
            return httpx.Response(
                200,
                json={
                    "value": [{"id": "f1", "deleted": {}}],
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/drives/drive/root/delta?token=done",
                },
            )
        return httpx.Response(
            200,
            json={
                "value": [graph_item(), graph_item(name="renamed.docx")],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/drives/drive/root/delta?token=next",
            },
        )

    async with cls(scope, Tokens(), transport=httpx.MockTransport(handler)) as source:
        page = await source.changes()
        assert [change.item.name for change in page.changes] == ["report.docx", "renamed.docx"]
        assert page.checkpoint is None and page.next_cursor is not None
        final = await source.changes(page.next_cursor)
        assert final.changes[0].kind == "deleted" and final.changes[0].item is None
        assert final.next_cursor is None and final.checkpoint is not None
        with pytest.raises(SourceError, match="invalid_cursor"):
            await source.changes(
                Cursor(SourceScope(Account(provider, "other", "tenant"), "drive"), "changes", final.checkpoint.token)
            )
    assert len(seen) == 2 and all(request.method == "GET" for request in seen)


async def test_graph_stream_redirect_strips_bearer_and_measures_receipt():
    scope = SourceScope(Account("onedrive", "reader", "tenant"), "drive")
    data = Stream([b"abc", b"def"])
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.host == "tenant.sharepoint.com":
            assert "authorization" not in request.headers
            return httpx.Response(200, stream=data, headers={"content-length": "6"})
        if request.url.path.endswith("/content"):
            return httpx.Response(302, headers={"location": "https://tenant.sharepoint.com/content?secret=signed"})
        return httpx.Response(200, json=graph_item())

    sink = Sink()
    async with OneDriveSource(
        scope, Tokens(), transport=httpx.MockTransport(handler), download_hosts=frozenset({"tenant.sharepoint.com"})
    ) as source:
        item = await source.get_item("f1")
        receipt = await source.download(item, sink, max_bytes=10)
    assert b"".join(sink.chunks) == b"abcdef" and data.closed
    assert receipt.sha256 == hashlib.sha256(b"abcdef").hexdigest() and receipt.size_bytes == 6
    assert receipt.version == '"v1"'
    assert all(r.method == "GET" for r in seen)


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "reconnect_required"),
        (403, "access_denied"),
        (404, "unavailable"),
        (410, "resync_required"),
        (429, "throttled"),
    ],
)
async def test_provider_failures_never_become_deletions(status, code):
    scope = SourceScope(Account("sharepoint", "reader", "tenant"), "drive")
    async with SharePointSource(
        scope,
        Tokens(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json={"secret": "must-not-escape"}, headers={"retry-after": "12"})
        ),
    ) as source:
        with pytest.raises(SourceError, match=code) as caught:
            await source.changes()
    assert "must-not-escape" not in str(caught.value)
    assert caught.value.retry_after == (12 if status == 429 else None)


async def test_google_removed_stays_ambiguous_and_shared_drive_cursor_is_explicit():
    scope = SourceScope(Account("google_drive", "reader"), "shared")
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("startPageToken"):
            return httpx.Response(200, json={"startPageToken": "before-enumeration"})
        return httpx.Response(
            200,
            json={
                "changes": [{"fileId": "f1", "removed": True, "changeType": "file"}],
                "newStartPageToken": "after-observations",
            },
        )

    async with GoogleDriveSource(scope, Tokens(), transport=httpx.MockTransport(handler)) as source:
        start = await source.start_cursor()
        page = await source.changes(start)
    assert page.changes[0].kind == "removed" and page.changes[0].item is None
    assert page.checkpoint.token == "after-observations" and page.next_cursor is None
    assert all(r.url.params["supportsAllDrives"] == "true" and r.url.params["driveId"] == "shared" for r in seen)


async def test_google_download_permission_is_current_and_not_importer_owned():
    scope = SourceScope(Account("google_drive", "reader"), "my-drive")
    tokens = Tokens()
    async with GoogleDriveSource(
        scope,
        tokens,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=google_item(capabilities={"canDownload": False}))
        ),
    ) as source:
        with pytest.raises(SourceError, match="access_denied"):
            await source.check_access("f1")
    assert tokens.accounts == [scope.account]


async def test_partial_stream_has_no_receipt_and_closes_response():
    scope = SourceScope(Account("google_drive", "reader"), "my-drive")
    data = Stream([b"abc"], fail=True)

    def handler(request):
        if request.url.params.get("alt") == "media":
            return httpx.Response(200, stream=data)
        return httpx.Response(200, json=google_item())

    async with GoogleDriveSource(scope, Tokens(), transport=httpx.MockTransport(handler)) as source:
        item = await source.get_item("f1")
        sink = Sink()
        with pytest.raises(SourceError, match="transport_error"):
            await source.download(item, sink, max_bytes=10)
    assert data.closed


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/data",
        "https://127.0.0.1/data",
        "http://graph.microsoft.com/v1.0/drives/drive/root/delta?x=1",
        "https://graph.microsoft.com/v1.0/me/messages?x=1",
        "https://graph.microsoft.com/v1.0/drives/drive/root/delta(token='%2527%2529%252F..%252Fother')",
    ],
)
async def test_graph_rejects_unbound_continuations_without_second_request(target):
    scope = SourceScope(Account("onedrive", "person"), "drive")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"value": [], "@odata.nextLink": target})

    async with OneDriveSource(scope, Tokens(), transport=httpx.MockTransport(handler)) as source:
        with pytest.raises(SourceError, match="unsafe_url"):
            await source.changes()
    assert len(seen) == 1


async def test_signed_download_urls_are_not_emitted_by_http_client_logging(caplog):
    scope = SourceScope(Account("onedrive", "reader"), "drive")

    def handler(request):
        if request.url.host == "tenant.sharepoint.com":
            return httpx.Response(200, stream=Stream([b"abcdef"]))
        if request.url.path.endswith("/content"):
            return httpx.Response(
                302, headers={"location": "https://tenant.sharepoint.com/path?secret=private-signed-value"}
            )
        return httpx.Response(200, json=graph_item())

    with caplog.at_level("DEBUG"):
        async with OneDriveSource(
            scope, Tokens(), transport=httpx.MockTransport(handler), download_hosts=frozenset({"tenant.sharepoint.com"})
        ) as source:
            await source.download(await source.get_item("f1"), Sink(), max_bytes=10)
    assert "private-signed-value" not in caplog.text
    assert "synthetic-delegated-token" not in caplog.text


@pytest.mark.parametrize(
    "chunks,max_bytes,expected", [([b"abcdefg"], 6, "size_exceeded"), ([b"ab"], 10, "invalid_response")]
)
async def test_stream_bytes_are_bounded_and_short_stream_has_no_receipt(chunks, max_bytes, expected):
    scope = SourceScope(Account("google_drive", "reader"), "my-drive")
    data = Stream(chunks)

    def handler(request):
        return (
            httpx.Response(200, stream=data)
            if request.url.params.get("alt")
            else httpx.Response(200, json=google_item())
        )

    sink = Sink()
    async with GoogleDriveSource(scope, Tokens(), transport=httpx.MockTransport(handler)) as source:
        with pytest.raises(SourceError, match=expected):
            await source.download(await source.get_item("f1"), sink, max_bytes=max_bytes)
    assert data.closed and sum(map(len, sink.chunks)) <= max_bytes


async def test_google_explicit_native_export_keeps_version_and_measured_size():
    scope = SourceScope(Account("google_drive", "reader"), "my-drive")
    target = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/export"):
            assert request.url.params["mimeType"] == target
            return httpx.Response(200, stream=Stream([b"original-export"]))
        return httpx.Response(200, json=google_item(mimeType="application/vnd.google-apps.document", size=None))

    async with GoogleDriveSource(scope, Tokens(), transport=httpx.MockTransport(handler)) as source:
        item = await source.get_item("f1")
        assert item.size_bytes is None
        with pytest.raises(SourceError, match="unsupported"):
            await source.download(item, Sink(), max_bytes=100)
        receipt = await source.download(item, Sink(), max_bytes=100, export_mime=target)
    assert receipt.size_bytes == 15 and receipt.version == "1" and receipt.mime_type == target
    assert all(r.method == "GET" for r in seen)


async def test_google_version_change_after_bytes_never_produces_receipt():
    scope = SourceScope(Account("google_drive", "reader"), "my-drive")
    reads = 0

    def handler(request):
        nonlocal reads
        if request.url.params.get("alt"):
            return httpx.Response(200, stream=Stream([b"abcdef"]))
        reads += 1
        return httpx.Response(200, json=google_item(version="2" if reads == 3 else "1"))

    async with GoogleDriveSource(scope, Tokens(), transport=httpx.MockTransport(handler)) as source:
        with pytest.raises(SourceError, match="version_changed"):
            await source.download(await source.get_item("f1"), Sink(), max_bytes=10)


@pytest.mark.parametrize(
    "provider,cls,body",
    [
        ("google_drive", GoogleDriveSource, google_item(mimeType="application/vnd.google-apps.shortcut")),
        ("onedrive", OneDriveSource, graph_item(remoteItem={"id": "remote"})),
    ],
)
async def test_shortcuts_are_never_followed(provider, cls, body):
    scope = SourceScope(Account(provider, "reader"), "my-drive" if provider == "google_drive" else "drive")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=body)

    async with cls(scope, Tokens(), transport=httpx.MockTransport(handler)) as source:
        with pytest.raises(SourceError, match="unsupported"):
            await source.download(await source.get_item("f1"), Sink(), max_bytes=10)
    assert all("remote" not in str(r.url) for r in seen)


async def test_google_page_token_is_not_a_url_and_incomplete_listing_is_not_absence():
    scope = SourceScope(Account("google_drive", "reader"), "shared")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"files": [], "incompleteSearch": True})

    async with GoogleDriveSource(scope, Tokens(), transport=httpx.MockTransport(handler)) as source:
        with pytest.raises(SourceError, match="resync_required"):
            await source.children("folder", Cursor(scope, "children:folder", "https://evil.example/"))
    assert seen[0].url.host == "www.googleapis.com" and seen[0].url.params["corpora"] == "drive"


@pytest.mark.parametrize("bad_id", ["..", ".", "../other", "a/b", "a%2Fb"])
async def test_remote_path_segments_cannot_change_endpoint(bad_id):
    scope = SourceScope(Account("onedrive", "reader"), "drive")
    seen = []
    async with OneDriveSource(
        scope,
        Tokens(),
        transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200, json=graph_item())),
    ) as source:
        with pytest.raises(SourceError):
            await source.get_item(bad_id)
    assert not seen


async def test_personal_google_changes_exclude_shared_drive_feed():
    scope = SourceScope(Account("google_drive", "reader"), "my-drive")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"changes": [], "newStartPageToken": "next"})

    async with GoogleDriveSource(scope, Tokens(), transport=httpx.MockTransport(handler)) as source:
        await source.changes(Cursor(scope, "changes", "start"))
    assert seen[0].url.params["includeItemsFromAllDrives"] == "false"
    assert seen[0].url.params["restrictToMyDrive"] == "true"


async def test_bounded_json_stops_stream_and_closes_before_full_body():
    from pyfly.agentic.content.sources.transport import SourceTransport

    data = Stream([b"{" * 20, b"not-consumed"])
    transport = SourceTransport(
        Account("onedrive", "reader"),
        Tokens(),
        host="graph.microsoft.com",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=data)),
        max_json_bytes=10,
    )
    try:
        with pytest.raises(SourceError, match="size_exceeded"):
            await transport.json("https://graph.microsoft.com/v1.0/drives/drive/root/delta")
    finally:
        await transport.aclose()
    assert data.closed


async def test_stream_cancellation_closes_response_without_receipt():
    import asyncio

    scope = SourceScope(Account("google_drive", "reader"), "my-drive")
    data = Stream([b"abcdef"])
    reached = asyncio.Event()

    class BlockingSink:
        async def write(self, chunk):
            reached.set()
            await asyncio.Event().wait()

    def handler(request):
        return (
            httpx.Response(200, stream=data)
            if request.url.params.get("alt")
            else httpx.Response(200, json=google_item())
        )

    async with GoogleDriveSource(scope, Tokens(), transport=httpx.MockTransport(handler)) as source:
        item = await source.get_item("f1")
        task = asyncio.create_task(source.download(item, BlockingSink(), max_bytes=10))
        await asyncio.wait_for(reached.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert data.closed


async def test_cancellation_during_completed_stream_close_waits_for_cleanup():
    import asyncio

    scope = SourceScope(Account("google_drive", "reader"), "my-drive")
    closing = asyncio.Event()
    release = asyncio.Event()

    class SlowClose(Stream):
        async def aclose(self):
            closing.set()
            await release.wait()
            self.closed = True

    data = SlowClose([b"abcdef"])

    def handler(request):
        return (
            httpx.Response(200, stream=data)
            if request.url.params.get("alt")
            else httpx.Response(200, json=google_item())
        )

    async with GoogleDriveSource(scope, Tokens(), transport=httpx.MockTransport(handler)) as source:
        item = await source.get_item("f1")
        task = asyncio.create_task(source.download(item, Sink(), max_bytes=10))
        await asyncio.wait_for(closing.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert data.closed


async def test_graph_can_capture_future_changes_cursor():
    scope = SourceScope(Account("onedrive", "reader"), "drive")

    def handler(request):
        assert request.url.params["token"] == "latest"
        return httpx.Response(
            200,
            json={
                "value": [],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/drives/drive/root/delta?token=checkpoint",
            },
        )

    async with OneDriveSource(scope, Tokens(), transport=httpx.MockTransport(handler)) as source:
        cursor = await source.start_cursor()
    assert cursor.scope == scope and cursor.operation == "changes"


async def test_download_receipt_is_bound_to_verified_account_and_drive():
    scope = SourceScope(Account("google_drive", "reader"), "my-drive")

    def handler(request):
        return (
            httpx.Response(200, stream=Stream([b"abcdef"]))
            if request.url.params.get("alt")
            else httpx.Response(200, json=google_item())
        )

    async with GoogleDriveSource(scope, Tokens(), transport=httpx.MockTransport(handler)) as source:
        receipt = await source.download(await source.get_item("f1"), Sink(), max_bytes=10)
    assert receipt.scope == scope


async def test_google_export_has_its_own_limit_before_streaming():
    scope = SourceScope(Account("google_drive", "reader"), "my-drive")
    data = Stream([b"must-not-stage"])

    def handler(request):
        if request.url.path.endswith("/export"):
            return httpx.Response(200, stream=data, headers={"content-length": "10000001"})
        return httpx.Response(200, json=google_item(mimeType="application/vnd.google-apps.document", size=None))

    sink = Sink()
    async with GoogleDriveSource(scope, Tokens(), transport=httpx.MockTransport(handler)) as source:
        with pytest.raises(SourceError, match="size_exceeded"):
            await source.download(
                await source.get_item("f1"),
                sink,
                max_bytes=20_000_000,
                export_mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
    assert not sink.chunks and data.closed


async def test_graph_documented_function_token_cursor_preserves_exact_drive_endpoint():
    scope = SourceScope(Account("sharepoint", "reader"), "drive")
    link = "https://graph.microsoft.com/v1.0/drives/drive/root/delta(token='opaque-token')"
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"value": [], "@odata.deltaLink": link})

    async with SharePointSource(scope, Tokens(), transport=httpx.MockTransport(handler)) as source:
        page = await source.changes()
        await source.changes(page.checkpoint)
    assert len(seen) == 2
    assert seen[1].url.path == "/v1.0/drives/drive/root/delta(token='opaque-token')"


async def test_malformed_download_redirect_is_a_typed_failure_before_external_io():
    scope = SourceScope(Account("onedrive", "reader"), "drive")
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/content"):
            return httpx.Response(302, headers={"location": "https://[broken"})
        return httpx.Response(200, json=graph_item())

    async with OneDriveSource(scope, Tokens(), transport=httpx.MockTransport(handler)) as source:
        with pytest.raises(SourceError, match="unsafe_url"):
            await source.download(await source.get_item("f1"), Sink(), max_bytes=10)
    assert all(request.url.host == "graph.microsoft.com" for request in seen)
