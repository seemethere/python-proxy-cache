from __future__ import annotations

import io
import struct
import zipfile

import httpx
import pytest

import app.wheel_metadata as wheel_metadata
from app.wheel_metadata import RangeNotSupportedError, WheelMetadataError, extract_wheel_metadata


def _wheel(
    metadata: bytes, compression: int = zipfile.ZIP_DEFLATED, *, comment: bytes = b""
) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        archive.writestr("example/__init__.py", b"")
        archive.writestr("example-1.0.dist-info/METADATA", metadata)
        archive.comment = comment
    return output.getvalue()


class RangeOrigin:
    def __init__(self, body: bytes):
        self.body = body
        self.ranges: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept-Encoding"] == "identity"
        value = request.headers["Range"]
        self.ranges.append(value)
        spec = value.removeprefix("bytes=")
        if spec.startswith("-"):
            length = int(spec[1:])
            start = max(0, len(self.body) - length)
            end = len(self.body) - 1
        else:
            raw_start, raw_end = spec.split("-", 1)
            start, end = int(raw_start), int(raw_end)
        part = self.body[start : end + 1]
        return httpx.Response(
            206,
            headers={"Content-Range": f"bytes {start}-{end}/{len(self.body)}"},
            content=part,
        )


async def _extract(body: bytes) -> tuple[bytes, RangeOrigin]:
    origin = RangeOrigin(body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(origin)) as client:
        result = await extract_wheel_metadata(
            client, "https://packages.test/example-1.0-py3-none-any.whl"
        )
    return result, origin


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
async def test_extracts_stored_and_deflated_metadata(compression: int) -> None:
    expected = b"Metadata-Version: 2.4\nName: example\nVersion: 1.0\n"
    result, origin = await _extract(
        _wheel(expected, compression, comment=b"comment containing fake PK\x05\x06 signature")
    )

    assert result == expected
    assert origin.ranges[0] == f"bytes=-{wheel_metadata._MAX_EOCD_SEARCH}"
    assert len(origin.ranges) == 5


class UnreadableBody(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.was_read = False

    async def __aiter__(self):
        self.was_read = True
        raise AssertionError("full wheel response must not be consumed")
        yield b""  # pragma: no cover

    async def aclose(self) -> None:
        pass


async def test_range_ignored_does_not_read_full_response() -> None:
    stream = UnreadableBody()

    def ignore_range(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Length": str(wheel_metadata._MAX_EOCD_SEARCH + 1)},
            stream=stream,
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(ignore_range)) as client:
        with pytest.raises(RangeNotSupportedError, match="ignored"):
            await extract_wheel_metadata(client, "https://packages.test/huge.whl")

    assert stream.was_read is False


async def test_extracts_small_wheel_when_origin_returns_full_response() -> None:
    expected = b"Metadata-Version: 2.4\nName: example\nVersion: 1.0\n"
    body = _wheel(expected)
    requests: list[str] = []

    def ignore_range(request: httpx.Request) -> httpx.Response:
        requests.append(request.headers["Range"])
        return httpx.Response(200, content=body, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(ignore_range)) as client:
        result = await extract_wheel_metadata(
            client, "https://packages.test/example-1.0-py3-none-any.whl"
        )

    assert result == expected
    assert requests == [f"bytes=-{wheel_metadata._MAX_EOCD_SEARCH}"]


class OversizedBody(httpx.AsyncByteStream):
    def __init__(self, limit: int) -> None:
        self.chunks = [b"x" * limit, b"y", b"must not be read"]
        self.yielded = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk

    async def aclose(self) -> None:
        pass


async def test_range_ignored_without_length_stops_after_bounded_probe() -> None:
    limit = wheel_metadata._MAX_EOCD_SEARCH
    stream = OversizedBody(limit)

    def ignore_range(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(ignore_range)) as client:
        with pytest.raises(RangeNotSupportedError, match="safe limit"):
            await extract_wheel_metadata(
                client, "https://packages.test/example-1.0-py3-none-any.whl"
            )

    assert stream.yielded == 2


async def test_redirect_is_not_followed_or_read() -> None:
    stream = UnreadableBody()
    requests: list[str] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == "storage.test":
            raise AssertionError("redirect must not be followed")
        return httpx.Response(
            307,
            headers={"Location": "https://storage.test/full-wheel"},
            stream=stream,
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(redirect), follow_redirects=True
    ) as client:
        with pytest.raises(WheelMetadataError, match="HTTP 307"):
            await extract_wheel_metadata(
                client, "https://packages.test/example-1.0-py3-none-any.whl"
            )

    assert requests == ["https://packages.test/example-1.0-py3-none-any.whl"]
    assert stream.was_read is False


@pytest.mark.parametrize(
    ("headers", "message"),
    [
        ({}, "Content-Range"),
        ({"Content-Range": "bytes 0-5/*"}, "Content-Range"),
        ({"Content-Range": "bytes 0-5/6", "Content-Length": "7"}, "Content-Length"),
    ],
)
async def test_rejects_invalid_range_headers(headers: dict[str, str], message: str) -> None:
    def invalid_range(request: httpx.Request) -> httpx.Response:
        return httpx.Response(206, headers=headers, content=b"123456", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(invalid_range)) as client:
        with pytest.raises(WheelMetadataError, match=message):
            await extract_wheel_metadata(client, "https://packages.test/example.whl")


async def test_rejects_crc_mismatch() -> None:
    body = bytearray(_wheel(b"Metadata-Version: 2.4\nName: example\n"))
    central = body.rindex(b"PK\x01\x02")
    struct.pack_into("<I", body, central + 16, 0x12345678)

    with pytest.raises(WheelMetadataError, match="CRC"):
        await _extract(bytes(body))


async def test_rejects_encrypted_metadata() -> None:
    body = bytearray(_wheel(b"Metadata-Version: 2.4\nName: example\n"))
    # There are two entries; find the central record whose name is METADATA.
    central = body.rindex(b"PK\x01\x02")
    flags = struct.unpack_from("<H", body, central + 8)[0]
    struct.pack_into("<H", body, central + 8, flags | 1)

    with pytest.raises(WheelMetadataError, match="encrypted"):
        await _extract(bytes(body))


async def test_rejects_unsupported_metadata_compression() -> None:
    body = bytearray(_wheel(b"Metadata-Version: 2.4\nName: example\n"))
    central = body.rindex(b"PK\x01\x02")
    local = struct.unpack_from("<I", body, central + 42)[0]
    struct.pack_into("<H", body, central + 10, 12)
    struct.pack_into("<H", body, local + 8, 12)

    with pytest.raises(WheelMetadataError, match="compression method"):
        await _extract(bytes(body))


async def test_rejects_multidisk_archive() -> None:
    body = bytearray(_wheel(b"Metadata-Version: 2.4\nName: example\n"))
    eocd = body.rindex(b"PK\x05\x06")
    struct.pack_into("<H", body, eocd + 4, 1)

    with pytest.raises(WheelMetadataError, match="multi-disk"):
        await _extract(bytes(body))


async def test_rejects_zip64_archive() -> None:
    body = bytearray(_wheel(b"Metadata-Version: 2.4\nName: example\n"))
    eocd = body.rindex(b"PK\x05\x06")
    struct.pack_into("<HH", body, eocd + 8, 0xFFFF, 0xFFFF)

    with pytest.raises(WheelMetadataError, match="ZIP64"):
        await _extract(bytes(body))


async def test_enforces_uncompressed_metadata_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wheel_metadata, "MAX_METADATA_SIZE", 32)
    body = _wheel(b"Metadata-Version: 2.4\nName: example\nVersion: 1.0\n" * 20)

    with pytest.raises(WheelMetadataError, match="size limit"):
        await _extract(body)


async def test_rejects_duplicate_metadata_members() -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("one-1.0.dist-info/METADATA", b"Name: one\n")
        archive.writestr("two-1.0.dist-info/METADATA", b"Name: two\n")

    with pytest.raises(WheelMetadataError, match="exactly one"):
        await _extract(output.getvalue())


@pytest.mark.parametrize(
    "member_name",
    [
        "different-1.0.dist-info/METADATA",
        "example-2.0.dist-info/METADATA",
    ],
)
async def test_rejects_dist_info_mismatched_with_wheel_filename(member_name: str) -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member_name, b"Metadata-Version: 2.4\n")

    with pytest.raises(WheelMetadataError, match="does not match wheel filename"):
        await _extract(output.getvalue())


async def test_decodes_wheel_filename_and_ignores_query() -> None:
    body = _wheel(b"Metadata-Version: 2.4\nName: example-pkg\n")
    origin = RangeOrigin(body)
    url = "https://packages.test/example%2D1.0-py3-none-any.whl?token=opaque"
    async with httpx.AsyncClient(transport=httpx.MockTransport(origin)) as client:
        result = await extract_wheel_metadata(client, url)

    assert result == b"Metadata-Version: 2.4\nName: example-pkg\n"
