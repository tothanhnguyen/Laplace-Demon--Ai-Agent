"""Bounded, deterministic extraction for trusted document attachments."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from laplace.tools.base import ToolFailure

if TYPE_CHECKING:
    from laplace.services.budget import TurnBudget

DocumentFormat = Literal["txt", "pdf", "docx"]

_PDF_SIGNATURE = b"%PDF-"
_ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_MAX_PROTOCOL_BYTES = 2 * 1024 * 1024
_DEFAULT_PARSE_TIMEOUT_SECONDS = 10.0
_DEFAULT_MEMORY_LIMIT_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class DocumentLimits:
    """Resource limits applied before and during document extraction."""

    max_bytes: int
    max_units: int
    max_unit_chars: int
    max_page_chars: int
    max_expanded_bytes: int | None = None
    max_zip_entries: int = 1_000
    parse_timeout_seconds: float = _DEFAULT_PARSE_TIMEOUT_SECONDS
    memory_limit_bytes: int = _DEFAULT_MEMORY_LIMIT_BYTES

    def __post_init__(self) -> None:
        for name in (
            "max_bytes",
            "max_units",
            "max_unit_chars",
            "max_page_chars",
            "max_zip_entries",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")
        if self.max_expanded_bytes is not None and self.max_expanded_bytes < 1:
            raise ValueError("max_expanded_bytes must be positive")
        if not math.isfinite(self.parse_timeout_seconds) or self.parse_timeout_seconds <= 0:
            raise ValueError("parse_timeout_seconds must be finite and positive")
        if self.memory_limit_bytes < 1:
            raise ValueError("memory_limit_bytes must be positive")

    @property
    def expanded_bytes(self) -> int:
        if self.max_expanded_bytes is not None:
            return self.max_expanded_bytes
        return min(self.max_bytes * 10, 100 * 1024 * 1024)


@dataclass(frozen=True, slots=True)
class AttachmentMetadata:
    """Model-safe attachment facts; deliberately contains no filesystem path."""

    attachment_id: str
    filename: str
    format: DocumentFormat
    size_bytes: int


@dataclass(frozen=True, slots=True)
class LocatorUnit:
    locator: str
    text: str


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    metadata: AttachmentMetadata
    unit_kind: str
    units: tuple[LocatorUnit, ...]
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ResolvedAttachment:
    metadata: AttachmentMetadata
    path: Path


class _ParseFailure(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def extract_document(
    paths: Mapping[str, Path],
    attachment_id: str,
    limits: DocumentLimits,
    *,
    budget: TurnBudget,
) -> ExtractedDocument:
    """Resolve an opaque ID and extract its document without returning its path."""

    budget.check()
    attachment = _resolve_attachment(paths, attachment_id, limits)
    budget.check()
    if attachment.metadata.format == "txt":
        units = _extract_txt(attachment.path, limits)
        budget.check()
        unit_kind = "line"
        limitations: tuple[str, ...] = ()
    else:
        if attachment.metadata.format == "docx":
            _preflight_docx(attachment.path, limits)
            budget.check()
        units = _parse_in_subprocess(
            attachment.path,
            attachment.metadata.format,
            limits,
            budget=budget,
        )
        budget.check()
        if attachment.metadata.format == "pdf":
            unit_kind = "page"
            limitations = (
                "Only the embedded PDF text layer is extracted; images and OCR are not processed.",
            )
        else:
            unit_kind = "paragraph/table row"
            limitations = (
                "Only body paragraphs and tables are extracted; headers, footers, "
                "comments, and images are not processed.",
            )

    if not units:
        raise ToolFailure("unsupported_content", "The document contains no readable text.")
    if len(units) > limits.max_units:
        raise ToolFailure(
            "unsupported_content",
            f"The document exceeds the extraction limit of {limits.max_units} locator units.",
        )
    return ExtractedDocument(
        metadata=attachment.metadata,
        unit_kind=unit_kind,
        units=units,
        limitations=limitations,
    )


def _resolve_attachment(
    paths: Mapping[str, Path],
    attachment_id: str,
    limits: DocumentLimits,
) -> _ResolvedAttachment:
    raw_path = paths.get(attachment_id)
    if raw_path is None:
        raise ToolFailure("not_found", "Attachment was not found.")
    try:
        path = Path(raw_path)
        stat = path.stat()
        if not path.is_file():
            raise OSError
    except (OSError, TypeError, ValueError):
        # Missing, stale, and unknown IDs intentionally have one disclosure-safe response.
        raise ToolFailure("not_found", "Attachment was not found.") from None

    if stat.st_size > limits.max_bytes:
        raise ToolFailure(
            "unsupported_content",
            f"The attachment exceeds the {limits.max_bytes}-byte document limit.",
        )

    suffix = path.suffix.lower()
    try:
        with path.open("rb") as stream:
            signature = stream.read(8)
    except OSError:
        raise ToolFailure("not_found", "Attachment was not found.") from None

    if suffix == ".txt":
        document_format: DocumentFormat = "txt"
    elif suffix == ".pdf":
        if not signature.startswith(_PDF_SIGNATURE):
            raise ToolFailure(
                "unsupported_content", "The attachment does not have a valid PDF signature."
            )
        document_format = "pdf"
    elif suffix == ".docx":
        if signature.startswith(_OLE_SIGNATURE):
            raise ToolFailure(
                "unsupported_content",
                "Encrypted or legacy Word documents are not supported.",
            )
        if not signature.startswith(_ZIP_SIGNATURES):
            raise ToolFailure(
                "unsupported_content", "The attachment does not have a valid DOCX signature."
            )
        document_format = "docx"
    else:
        raise ToolFailure(
            "unsupported_content", "Only UTF-8 TXT, PDF, and DOCX attachments are supported."
        )

    metadata = AttachmentMetadata(
        attachment_id=attachment_id,
        filename=path.name,
        format=document_format,
        size_bytes=stat.st_size,
    )
    return _ResolvedAttachment(metadata=metadata, path=path)


def _extract_txt(path: Path, limits: DocumentLimits) -> tuple[LocatorUnit, ...]:
    try:
        payload = path.read_bytes()
    except OSError:
        raise ToolFailure("not_found", "Attachment was not found.") from None
    if b"\x00" in payload:
        raise ToolFailure("unsupported_content", "The TXT attachment contains binary data.")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ToolFailure("unsupported_content", "The TXT attachment is not valid UTF-8.") from None

    suspicious_controls = sum(character < " " and character not in "\n\r\t" for character in text)
    if suspicious_controls and suspicious_controls * 100 > max(1, len(text)):
        raise ToolFailure("unsupported_content", "The TXT attachment contains binary data.")

    raw_units = (
        (f"line:{line_number}", line)
        for line_number, line in enumerate(StringIO(text), start=1)
        if line.strip()
    )
    return _bounded_units(raw_units, limits)


def _preflight_docx(path: Path, limits: DocumentLimits) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > limits.max_zip_entries:
                raise ToolFailure(
                    "unsupported_content", "The DOCX archive contains too many entries."
                )
            names = {member.filename for member in members}
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise ToolFailure("unsupported_content", "The DOCX package is incomplete.")
            expanded_size = 0
            for member in members:
                normalized = member.filename.replace("\\", "/")
                parts = normalized.split("/")
                if normalized.startswith("/") or ".." in parts:
                    raise ToolFailure(
                        "unsupported_content", "The DOCX package contains an unsafe entry."
                    )
                if member.flag_bits & 0x1:
                    raise ToolFailure(
                        "unsupported_content", "Encrypted DOCX attachments are not supported."
                    )
                expanded_size += member.file_size
                if expanded_size > limits.expanded_bytes:
                    raise ToolFailure(
                        "unsupported_content",
                        "The DOCX archive exceeds the expanded-size limit.",
                    )
    except ToolFailure:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        raise ToolFailure("unsupported_content", "The DOCX attachment is corrupt.") from None


def _bounded_units(
    raw_units: Iterable[tuple[str, str]], limits: DocumentLimits
) -> tuple[LocatorUnit, ...]:
    chunk_size = min(limits.max_unit_chars, limits.max_page_chars)
    bounded: list[LocatorUnit] = []
    for locator, raw_text in raw_units:
        text = _normalize_text(raw_text)
        if not text:
            continue
        multipart = len(text) > chunk_size
        for index, offset in enumerate(range(0, len(text), chunk_size), start=1):
            if len(bounded) >= limits.max_units:
                raise ToolFailure(
                    "unsupported_content",
                    f"The document exceeds the extraction limit of "
                    f"{limits.max_units} locator units.",
                )
            part_locator = f"{locator}/part:{index}" if multipart else locator
            bounded.append(
                LocatorUnit(
                    locator=part_locator,
                    text=text[offset : offset + chunk_size],
                )
            )
    return tuple(bounded)


def _normalize_text(text: str) -> str:
    return "\n".join(line.strip() for line in text.splitlines() if line.strip()).strip()


def _parse_in_subprocess(
    path: Path,
    document_format: Literal["pdf", "docx"],
    limits: DocumentLimits,
    *,
    budget: TurnBudget,
) -> tuple[LocatorUnit, ...]:
    request = json.dumps(
        {
            "path": os.fspath(path),
            "format": document_format,
            "max_units": limits.max_units,
            "max_unit_chars": limits.max_unit_chars,
            "max_page_chars": limits.max_page_chars,
            "memory_limit_bytes": limits.memory_limit_bytes,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(request) > 64 * 1024:
        raise ToolFailure("runtime_error", "The document parser request is too large.")

    budget.check()
    process = subprocess.Popen(
        [sys.executable, "-m", "laplace.services.documents", "--worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=os.fspath(Path(__file__).resolve().parents[2]),
    )
    try:
        budget.check()
        stdout, _ = process.communicate(
            request, timeout=min(limits.parse_timeout_seconds, budget.remaining_seconds())
        )
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        budget.check()
        raise ToolFailure("timeout", "Document parsing timed out.", retryable=True) from None
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.communicate()
        raise
    budget.check()
    if len(stdout) > _MAX_PROTOCOL_BYTES:
        raise ToolFailure("runtime_error", "The document parser returned too much data.")
    if process.returncode != 0:
        raise ToolFailure("runtime_error", "The document parser failed unexpectedly.")
    try:
        response = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ToolFailure(
            "runtime_error", "The document parser returned an invalid response."
        ) from None
    if not isinstance(response, dict) or not isinstance(response.get("status"), str):
        raise ToolFailure("runtime_error", "The document parser returned an invalid response.")
    if response["status"] != "ok":
        _raise_worker_failure(response["status"])

    raw_units = response.get("units")
    if not isinstance(raw_units, list):
        raise ToolFailure("runtime_error", "The document parser returned an invalid response.")
    parsed: list[LocatorUnit] = []
    for item in raw_units:
        if (
            not isinstance(item, dict)
            or set(item) != {"locator", "text"}
            or not isinstance(item["locator"], str)
            or not isinstance(item["text"], str)
        ):
            raise ToolFailure("runtime_error", "The document parser returned an invalid response.")
        parsed.append(LocatorUnit(locator=item["locator"], text=item["text"]))
    if len(parsed) > limits.max_units:
        raise ToolFailure("unsupported_content", "The document exceeds the locator-unit limit.")
    return tuple(parsed)


def _raise_worker_failure(status: str) -> None:
    messages = {
        "encrypted": "Encrypted documents are not supported.",
        "image_only": "The PDF has no extractable text layer; image-only PDFs require OCR.",
        "corrupt": "The document is corrupt and could not be parsed.",
        "limit": "The document exceeds the configured extraction limits.",
        "unsupported": "The document content is not supported.",
    }
    if status in messages:
        raise ToolFailure("unsupported_content", messages[status])
    raise ToolFailure("runtime_error", "The document parser failed unexpectedly.")


def _apply_memory_cap(limit_bytes: int) -> None:
    if os.name != "posix":
        return
    try:
        import resource

        current_soft, current_hard = resource.getrlimit(resource.RLIMIT_AS)
        hard = current_hard if current_hard != resource.RLIM_INFINITY else limit_bytes
        cap = min(limit_bytes, hard)
        if current_soft == resource.RLIM_INFINITY or current_soft > cap:
            resource.setrlimit(resource.RLIMIT_AS, (cap, hard))
    except (ImportError, OSError, ValueError):
        return


def _worker_extract(request: dict[str, Any]) -> dict[str, Any]:
    required = {
        "path",
        "format",
        "max_units",
        "max_unit_chars",
        "max_page_chars",
        "memory_limit_bytes",
    }
    if set(request) != required:
        raise _ParseFailure("invalid", "invalid request")
    path = request["path"]
    document_format = request["format"]
    numeric = (
        request["max_units"],
        request["max_unit_chars"],
        request["max_page_chars"],
        request["memory_limit_bytes"],
    )
    if (
        not isinstance(path, str)
        or document_format not in {"pdf", "docx"}
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in numeric
        )
    ):
        raise _ParseFailure("invalid", "invalid request")

    _apply_memory_cap(request["memory_limit_bytes"])
    limits = DocumentLimits(
        max_bytes=1,
        max_units=request["max_units"],
        max_unit_chars=request["max_unit_chars"],
        max_page_chars=request["max_page_chars"],
        memory_limit_bytes=request["memory_limit_bytes"],
    )
    try:
        raw_units = (
            _worker_extract_pdf(Path(path))
            if document_format == "pdf"
            else _worker_extract_docx(Path(path))
        )
        units = _bounded_units(raw_units, limits)
    except ToolFailure as exc:
        if "limit" in exc.message:
            raise _ParseFailure("limit", exc.message) from None
        raise _ParseFailure("unsupported", exc.message) from None
    return {
        "status": "ok",
        "units": [{"locator": unit.locator, "text": unit.text} for unit in units],
    }


def _worker_extract_pdf(path: Path) -> list[tuple[str, str]]:
    try:
        from pypdf import PdfReader
        from pypdf.errors import FileNotDecryptedError, PdfReadError

        reader = PdfReader(path, strict=True)
        if reader.is_encrypted:
            raise _ParseFailure("encrypted", "encrypted")
        units: list[tuple[str, str]] = []
        for page_number, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            if text.strip():
                units.append((f"page:{page_number}", text))
        if not units:
            raise _ParseFailure("image_only", "no text layer")
        return units
    except _ParseFailure:
        raise
    except (FileNotDecryptedError, PdfReadError):
        raise _ParseFailure("corrupt", "corrupt PDF") from None
    except Exception:
        raise _ParseFailure("corrupt", "corrupt PDF") from None


def _worker_extract_docx(path: Path) -> list[tuple[str, str]]:
    try:
        from docx import Document
        from docx.table import Table

        document = Document(path)
        units: list[tuple[str, str]] = []
        paragraph_number = 0
        table_number = 0
        for block in document.iter_inner_content():
            if isinstance(block, Table):
                table_number += 1
                for row_number, row in enumerate(block.rows, start=1):
                    cells = [_normalize_text(cell.text) for cell in row.cells]
                    text = " | ".join(cells).strip(" |")
                    if text:
                        units.append((f"table:{table_number}/row:{row_number}", text))
            else:
                paragraph_number += 1
                if block.text.strip():
                    units.append((f"paragraph:{paragraph_number}", block.text))
        if not units:
            raise _ParseFailure("unsupported", "no readable text")
        return units
    except _ParseFailure:
        raise
    except Exception:
        raise _ParseFailure("corrupt", "corrupt DOCX") from None


def _worker_main() -> int:
    try:
        request_bytes = sys.stdin.buffer.read(64 * 1024 + 1)
        if len(request_bytes) > 64 * 1024:
            raise _ParseFailure("invalid", "request too large")
        request = json.loads(request_bytes)
        if not isinstance(request, dict):
            raise _ParseFailure("invalid", "invalid request")
        response = _worker_extract(request)
    except _ParseFailure as exc:
        response = {"status": exc.kind}
    except (UnicodeDecodeError, json.JSONDecodeError):
        response = {"status": "invalid"}
    except Exception:
        response = {"status": "crash"}

    encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_PROTOCOL_BYTES:
        encoded = b'{"status":"limit"}'
    sys.stdout.buffer.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(_worker_main())
