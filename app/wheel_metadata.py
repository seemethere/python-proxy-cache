"""Extract wheel core metadata using bounded HTTP range requests.

This deliberately implements only the small, well-defined subset of ZIP that
normal wheels need.  Anything ambiguous (ZIP64, spanning, encryption, corrupt
sizes) fails closed rather than falling back to downloading the wheel.
"""

from __future__ import annotations

import binascii
import re
import struct
import zlib
from urllib.parse import unquote, urlparse

import httpx
from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

MAX_ARCHIVE_SIZE = 32 * 1024 * 1024 * 1024
MAX_CENTRAL_DIRECTORY_SIZE = 32 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_COMPRESSED_METADATA_SIZE = 8 * 1024 * 1024
MAX_METADATA_SIZE = 8 * 1024 * 1024

_EOCD_SIZE = 22
_MAX_EOCD_SEARCH = _EOCD_SIZE + 65_535
_EOCD_SIGNATURE = b"PK\x05\x06"
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_CENTRAL_SIGNATURE = b"PK\x01\x02"
_LOCAL_SIGNATURE = b"PK\x03\x04"
_CONTENT_RANGE_RE = re.compile(r"bytes (\d+)-(\d+)/(\d+)\Z", re.IGNORECASE)


class WheelMetadataError(Exception):
    """The wheel metadata could not be extracted safely."""


class RangeNotSupportedError(WheelMetadataError):
    """The server did not honor a byte range request."""


async def _range_response(
    client: httpx.AsyncClient,
    url: str,
    range_value: str,
    *,
    allow_full_response_up_to: int | None = None,
) -> tuple[bytes, int, int, int, bool]:
    request = client.build_request(
        "GET",
        url,
        headers={"Range": range_value, "Accept-Encoding": "identity"},
    )
    response = await client.send(request, stream=True, follow_redirects=False)
    try:
        content_encoding = response.headers.get("Content-Encoding", "identity").lower()
        if content_encoding not in ("", "identity"):
            raise WheelMetadataError("range response used content encoding")

        if response.status_code == 200:
            if allow_full_response_up_to is None:
                raise RangeNotSupportedError("server ignored the Range request")
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except ValueError as exc:
                    raise WheelMetadataError("invalid Content-Length") from exc
                if declared_length > allow_full_response_up_to:
                    raise RangeNotSupportedError(
                        "server ignored the Range request and response exceeds safe limit"
                    )

            # Some origins return the complete object when a suffix range is
            # larger than the object. Read no more than the permitted body plus
            # one byte, so a missing or dishonest Content-Length stays bounded.
            chunks: list[bytes] = []
            received = 0
            async for chunk in response.aiter_bytes(chunk_size=allow_full_response_up_to + 1):
                received += len(chunk)
                if received > allow_full_response_up_to:
                    raise RangeNotSupportedError(
                        "server ignored the Range request and response exceeds safe limit"
                    )
                chunks.append(chunk)
            if content_length is not None and received != declared_length:
                raise WheelMetadataError("Content-Length does not match response body")
            if received <= 0 or received > MAX_ARCHIVE_SIZE:
                raise WheelMetadataError("invalid archive size")
            return b"".join(chunks), 0, received - 1, received, True

        if response.status_code != 206:
            raise WheelMetadataError(f"range request returned HTTP {response.status_code}")

        raw_content_range = response.headers.get("Content-Range", "")
        match = _CONTENT_RANGE_RE.fullmatch(raw_content_range.strip())
        if match is None:
            raise WheelMetadataError("missing or invalid Content-Range")
        start, end, total = (int(value) for value in match.groups())
        if total <= 0 or total > MAX_ARCHIVE_SIZE or start > end or end >= total:
            raise WheelMetadataError("invalid Content-Range bounds")

        expected_length = end - start + 1
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                if int(content_length) != expected_length:
                    raise WheelMetadataError("Content-Length does not match Content-Range")
            except ValueError as exc:
                raise WheelMetadataError("invalid Content-Length") from exc

        chunks: list[bytes] = []
        received = 0
        async for chunk in response.aiter_bytes():
            received += len(chunk)
            if received > expected_length:
                raise WheelMetadataError("range response exceeded declared length")
            chunks.append(chunk)
        if received != expected_length:
            raise WheelMetadataError("truncated range response")
        return b"".join(chunks), start, end, total, False
    finally:
        await response.aclose()


async def _fetch_suffix(
    client: httpx.AsyncClient, url: str, length: int
) -> tuple[bytes, int, bool]:
    data, start, end, total, is_full_response = await _range_response(
        client,
        url,
        f"bytes=-{length}",
        allow_full_response_up_to=length,
    )
    expected_start = max(0, total - length)
    if start != expected_start or end != total - 1:
        raise WheelMetadataError("server returned the wrong suffix range")
    return data, total, is_full_response


async def _fetch_exact(
    client: httpx.AsyncClient, url: str, start: int, length: int, archive_size: int
) -> bytes:
    if length <= 0 or start < 0 or start + length > archive_size:
        raise WheelMetadataError("invalid ZIP byte range")
    end = start + length - 1
    data, actual_start, actual_end, total, _ = await _range_response(
        client, url, f"bytes={start}-{end}"
    )
    if total != archive_size or actual_start != start or actual_end != end:
        raise WheelMetadataError("server returned the wrong byte range")
    return data


def _has_zip64_extra(extra: bytes) -> bool:
    offset = 0
    while offset < len(extra):
        if len(extra) - offset < 4:
            raise WheelMetadataError("malformed ZIP extra field")
        field_id, size = struct.unpack_from("<HH", extra, offset)
        offset += 4
        if offset + size > len(extra):
            raise WheelMetadataError("malformed ZIP extra field")
        if field_id == 0x0001:
            return True
        offset += size
    return False


def _decode_name(raw_name: bytes, flags: int) -> str:
    try:
        return raw_name.decode("utf-8" if flags & 0x800 else "cp437")
    except UnicodeDecodeError as exc:
        raise WheelMetadataError("invalid ZIP member name") from exc


def _is_metadata_name(name: str) -> bool:
    parts = name.split("/")
    return (
        len(parts) == 2
        and parts[0].endswith(".dist-info")
        and parts[0] != ".dist-info"
        and parts[1] == "METADATA"
    )


def _validate_metadata_member(member_name: str, url: str) -> None:
    wheel_filename = unquote(urlparse(url).path).rsplit("/", 1)[-1]
    try:
        wheel_distribution, wheel_version, _, _ = parse_wheel_filename(wheel_filename)
    except InvalidWheelFilename as exc:
        raise WheelMetadataError("artifact URL does not contain a valid wheel filename") from exc

    dist_info = member_name.split("/", 1)[0].removesuffix(".dist-info")
    metadata_distribution, separator, metadata_version = dist_info.rpartition("-")
    if not separator or not metadata_distribution or not metadata_version:
        raise WheelMetadataError("invalid dist-info directory name")
    try:
        parsed_metadata_version = Version(metadata_version)
    except InvalidVersion as exc:
        raise WheelMetadataError("invalid dist-info version") from exc
    if (
        canonicalize_name(metadata_distribution) != wheel_distribution
        or parsed_metadata_version != wheel_version
    ):
        raise WheelMetadataError("dist-info directory does not match wheel filename")


def _parse_eocd(tail: bytes, tail_start: int, archive_size: int) -> tuple[int, int, int]:
    offset = -1
    for candidate in range(len(tail) - _EOCD_SIZE, -1, -1):
        if tail[candidate : candidate + 4] != _EOCD_SIGNATURE:
            continue
        comment_length = struct.unpack_from("<H", tail, candidate + 20)[0]
        if candidate + _EOCD_SIZE + comment_length == len(tail):
            offset = candidate
            break
    if offset < 0:
        raise WheelMetadataError("ZIP end record not found")
    (
        disk,
        central_disk,
        disk_entries,
        total_entries,
        central_size,
        central_offset,
        comment_length,
    ) = struct.unpack_from("<HHHHIIH", tail, offset + 4)
    eocd_absolute = tail_start + offset
    if eocd_absolute + _EOCD_SIZE + comment_length != archive_size:
        raise WheelMetadataError("invalid ZIP end record or trailing data")
    if disk != 0 or central_disk != 0 or disk_entries != total_entries:
        raise WheelMetadataError("multi-disk ZIP archives are unsupported")
    if (
        total_entries == 0xFFFF
        or central_size == 0xFFFFFFFF
        or central_offset == 0xFFFFFFFF
        or (offset >= 20 and tail[offset - 20 : offset - 16] == _ZIP64_LOCATOR_SIGNATURE)
    ):
        raise WheelMetadataError("ZIP64 archives are unsupported")
    if total_entries == 0 or total_entries > MAX_ENTRIES:
        raise WheelMetadataError("invalid ZIP entry count")
    if central_size <= 0 or central_size > MAX_CENTRAL_DIRECTORY_SIZE:
        raise WheelMetadataError("central directory exceeds size limit")
    if central_offset + central_size != eocd_absolute:
        raise WheelMetadataError("invalid central directory bounds")
    return central_offset, central_size, total_entries


def _parse_central_directory(
    data: bytes, entry_count: int
) -> tuple[str, int, int, int, int, int, int]:
    offset = 0
    metadata: list[tuple[str, int, int, int, int, int, int]] = []
    for _ in range(entry_count):
        if offset + 46 > len(data) or data[offset : offset + 4] != _CENTRAL_SIGNATURE:
            raise WheelMetadataError("malformed central directory")
        fields = struct.unpack_from("<4s6H3I5H2I", data, offset)
        flags, method = fields[3], fields[4]
        crc32, compressed_size, uncompressed_size = fields[7:10]
        name_length, extra_length, comment_length = fields[10:13]
        disk_start, local_offset = fields[13], fields[16]
        record_end = offset + 46 + name_length + extra_length + comment_length
        if record_end > len(data):
            raise WheelMetadataError("truncated central directory")
        raw_name = data[offset + 46 : offset + 46 + name_length]
        extra_start = offset + 46 + name_length
        extra = data[extra_start : extra_start + extra_length]
        if (
            compressed_size == 0xFFFFFFFF
            or uncompressed_size == 0xFFFFFFFF
            or local_offset == 0xFFFFFFFF
            or disk_start == 0xFFFF
            or _has_zip64_extra(extra)
        ):
            raise WheelMetadataError("ZIP64 archives are unsupported")
        if disk_start != 0:
            raise WheelMetadataError("multi-disk ZIP archives are unsupported")
        name = _decode_name(raw_name, flags)
        if _is_metadata_name(name):
            metadata.append(
                (name, flags, method, crc32, compressed_size, uncompressed_size, local_offset)
            )
        offset = record_end
    if offset != len(data):
        raise WheelMetadataError("central directory entry count mismatch")
    if len(metadata) != 1:
        raise WheelMetadataError("wheel must contain exactly one dist-info/METADATA member")
    return metadata[0]


def _decompress_metadata(data: bytes, method: int, expected_size: int) -> bytes:
    if method == 0:
        if len(data) != expected_size:
            raise WheelMetadataError("stored metadata size mismatch")
        return data
    if method != 8:
        raise WheelMetadataError(f"unsupported metadata compression method {method}")
    decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
    try:
        result = decompressor.decompress(data, MAX_METADATA_SIZE + 1)
        if len(result) > MAX_METADATA_SIZE or decompressor.unconsumed_tail:
            raise WheelMetadataError("deflated metadata exceeds size limit")
        result += decompressor.flush(MAX_METADATA_SIZE + 1 - len(result))
    except zlib.error as exc:
        raise WheelMetadataError("invalid deflate stream") from exc
    if (
        len(result) > MAX_METADATA_SIZE
        or len(result) != expected_size
        or not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
    ):
        raise WheelMetadataError("deflated metadata size mismatch")
    return result


async def extract_wheel_metadata(client: httpx.AsyncClient, url: str) -> bytes:
    """Return the wheel's ``.dist-info/METADATA`` using bounded range reads."""

    tail, archive_size, is_full_response = await _fetch_suffix(client, url, _MAX_EOCD_SEARCH)
    full_archive = tail if is_full_response else None

    async def fetch_exact(start: int, length: int) -> bytes:
        if full_archive is None:
            return await _fetch_exact(client, url, start, length, archive_size)
        if length <= 0 or start < 0 or start + length > archive_size:
            raise WheelMetadataError("invalid ZIP byte range")
        return full_archive[start : start + length]

    central_offset, central_size, entry_count = _parse_eocd(
        tail, archive_size - len(tail), archive_size
    )
    central = await fetch_exact(central_offset, central_size)
    (
        member_name,
        flags,
        method,
        expected_crc,
        compressed_size,
        uncompressed_size,
        local_offset,
    ) = _parse_central_directory(central, entry_count)
    _validate_metadata_member(member_name, url)
    if flags & 0x41:
        raise WheelMetadataError("encrypted ZIP members are unsupported")
    if compressed_size > MAX_COMPRESSED_METADATA_SIZE or uncompressed_size > MAX_METADATA_SIZE:
        raise WheelMetadataError("metadata exceeds size limit")
    if compressed_size == 0:
        raise WheelMetadataError("empty wheel metadata")

    local_header = await fetch_exact(local_offset, 30)
    if local_header[:4] != _LOCAL_SIGNATURE:
        raise WheelMetadataError("invalid local file header")
    local_flags, local_method = struct.unpack_from("<HH", local_header, 6)
    name_length, extra_length = struct.unpack_from("<HH", local_header, 26)
    if local_flags != flags or local_method != method:
        raise WheelMetadataError("local and central ZIP headers disagree")
    if local_flags & 0x41:
        raise WheelMetadataError("encrypted ZIP members are unsupported")
    variable = await fetch_exact(local_offset + 30, name_length + extra_length)
    raw_local_name = variable[:name_length]
    if _decode_name(raw_local_name, local_flags) != member_name:
        raise WheelMetadataError("local and central member names disagree")
    if _has_zip64_extra(variable[name_length:]):
        raise WheelMetadataError("ZIP64 archives are unsupported")

    data_offset = local_offset + 30 + name_length + extra_length
    compressed = await fetch_exact(data_offset, compressed_size)
    result = _decompress_metadata(compressed, method, uncompressed_size)
    if binascii.crc32(result) & 0xFFFFFFFF != expected_crc:
        raise WheelMetadataError("metadata CRC mismatch")
    return result
