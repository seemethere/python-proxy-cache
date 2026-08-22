from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class File:
    filename: str
    url: str
    hashes: dict[str, str] = field(default_factory=dict)
    requires_python: str | None = None
    yanked: bool | str | None = None  # False, True, or reason string
    dist_info_metadata: bool | str | None = None  # PEP 658: true/false or hash dict
    core_metadata: bool | str | None = None  # PEP 714 rename
    size: int | None = None
    upload_time: str | None = None


@dataclass
class Project:
    name: str
    files: list[File] = field(default_factory=list)
