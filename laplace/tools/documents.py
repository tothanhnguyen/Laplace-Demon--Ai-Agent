"""Bounded document-reading tool over trusted attachment IDs."""

from pydantic import Field

from laplace.config import Settings
from laplace.services.documents import DocumentLimits, extract_document
from laplace.tools.base import ToolContext, ToolFailure, ToolModel, ToolPayload, tool


class ReadDocumentArgs(ToolModel):
    attachment_id: str = Field(min_length=1, max_length=200)
    start_unit: int = Field(default=1, ge=1)


class DocumentMetadata(ToolModel):
    attachment_id: str
    filename: str
    format: str
    size_bytes: int
    unit_kind: str
    total_units: int


class DocumentUnit(ToolModel):
    unit: int
    locator: str
    text: str


class ReadDocumentOutput(ToolModel):
    document: DocumentMetadata
    units: list[DocumentUnit]
    next_unit: int | None
    complete: bool
    limitations: list[str]


@tool(
    name="read_document",
    purpose="Read bounded text from a TXT, PDF, or DOCX attachment by opaque ID.",
    when_to_use="Use when the user asks about a document attached to the current message.",
    when_not_to_use=(
        "Do not invent attachment IDs, access filesystem paths, or claim OCR/image understanding."
    ),
    risk_level="low",
    args_model=ReadDocumentArgs,
    result_model=ReadDocumentOutput,
)
def read_document(args: ReadDocumentArgs, context: ToolContext) -> ToolPayload:
    settings = Settings()
    limits = DocumentLimits(
        max_bytes=settings.document_max_bytes,
        max_units=settings.document_max_units,
        max_zip_entries=settings.document_max_zip_entries,
        max_unit_chars=settings.document_unit_max_chars,
        max_page_chars=settings.document_unit_max_chars,
        max_expanded_bytes=settings.document_max_expanded_bytes,
        parse_timeout_seconds=settings.document_parse_timeout_seconds,
        memory_limit_bytes=settings.document_parser_memory_mb * 1024 * 1024,
    )
    document = extract_document(context.attachments, args.attachment_id, limits)
    start_index = args.start_unit - 1
    if start_index >= len(document.units):
        raise ToolFailure("invalid_input", "start_unit is beyond the document.")

    selected = []
    consumed = 0
    for offset, unit in enumerate(document.units[start_index:], start=args.start_unit):
        cost = len(unit.text)
        if selected and consumed + cost > settings.document_full_summary_max_chars:
            break
        if len(selected) == 8:
            break
        selected.append({"unit": offset, "locator": unit.locator, "text": unit.text})
        consumed += cost

    next_unit = args.start_unit + len(selected)
    complete = next_unit > len(document.units)
    if complete:
        next_unit = None
    metadata = document.metadata
    data = {
        "document": {
            "attachment_id": metadata.attachment_id,
            "filename": metadata.filename,
            "format": metadata.format,
            "size_bytes": metadata.size_bytes,
            "unit_kind": document.unit_kind,
            "total_units": len(document.units),
        },
        "units": selected,
        "next_unit": next_unit,
        "complete": complete,
        "limitations": list(document.limitations),
    }
    sources = tuple(f"attachment:{metadata.attachment_id}#{item['locator']}" for item in selected)
    return ToolPayload(data=data, sources=sources)
