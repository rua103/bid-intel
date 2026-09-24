from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ItemCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package_code: str = Field(default="default", min_length=1)
    product_name: str | None = None
    category: str | None = None
    brand: str | None = None
    model: str | None = None
    quantity: Decimal | None = None
    quantity_unit: str | None = None
    unit_price: Decimal | None = None
    total_price: Decimal | None = None
    source_file: str
    source_location: str
    source_evidence: str | None = None
    extraction_method: str = "table_header_mapping"
    confidence: float = Field(default=0.7, ge=0, le=1)


class NoticeMetadata(BaseModel):
    project_name: str | None = None
    project_number: str | None = None
    procurement_unit: str | None = None
    project_budget: Decimal | None = None
    announced_total_award: Decimal | None = None


class ParticipantCandidate(BaseModel):
    package_code: str = Field(default="default", min_length=1)
    consortium_members: list[str] = Field(default_factory=list)
    organization_name: str
    outcome: Literal["winner", "nonwinner", "unknown"] = "unknown"
    award_amount: Decimal | None = None
    source_file: str
    source_location: str
    source_evidence: str | None = None
    extraction_method: str = "qwen_deepseek_structured"
    confidence: float = Field(default=0.55, ge=0, le=1)


class ImportResult(BaseModel):
    notice_id: int
    source_files: list[str]
    items_found: int
    items: list[ItemCandidate]
    metadata: NoticeMetadata
    participants: list[ParticipantCandidate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class BatchNoticeSummary(BaseModel):
    notice_id: int
    source_files: list[str]
    items_found: int
    participants_found: int
    warnings: list[str] = Field(default_factory=list)


class BatchImportResult(BaseModel):
    notices_found: int
    notices_imported: int
    total_items_found: int
    total_participants_found: int
    elapsed_seconds: float
    orphan_files: list[str] = Field(default_factory=list)
    notices: list[BatchNoticeSummary] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class ExtractionPayload(BaseModel):
    """Strict model response shape; missing values must remain null."""

    metadata: NoticeMetadata = Field(default_factory=NoticeMetadata)
    items: list[ItemCandidate] = Field(default_factory=list)


class ModelItemPayload(BaseModel):
    package_code: str = Field(default="default", min_length=1)
    product_name: str | None = None
    category: str | None = None
    brand: str | None = None
    model: str | None = None
    quantity: Decimal | None = None
    quantity_unit: str | None = None
    unit_price: Decimal | None = None
    total_price: Decimal | None = None
    source_evidence: str


class ModelParticipantPayload(BaseModel):
    package_code: str = Field(default="default", min_length=1)
    consortium_members: list[str] = Field(default_factory=list)
    organization_name: str
    outcome: Literal["winner", "nonwinner", "unknown"] = "unknown"
    award_amount: Decimal | None = None
    source_evidence: str


class ModelExtractionPayload(BaseModel):
    metadata: NoticeMetadata = Field(default_factory=NoticeMetadata)
    participants: list[ModelParticipantPayload] = Field(default_factory=list)
    items: list[ModelItemPayload] = Field(default_factory=list)


class OrganizationSelection(BaseModel):
    organization_ids: list[int] = Field(min_length=2, max_length=50)


class ItemRecord(ItemCandidate):
    id: int
    notice_id: int


class ModelConfigPayload(BaseModel):
    model_base_url: str = ""
    model_api_key: str = ""
    model_name: str = ""


class ModelConfigResponse(BaseModel):
    model_base_url: str
    model_name: str
    model_api_key_masked: str
    api_key_configured: bool
    configured: bool


class ModelTestResponse(BaseModel):
    ok: bool
    message: str
