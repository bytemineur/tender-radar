"""API 数据契约（OpenAPI / JSON Schema 由 FastAPI 自动生成）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class TenderOut(BaseModel):
    """公告输出。"""

    id: int
    title: str
    source_id: str
    source_name: str = ""
    source_url: str = ""
    notice_type: str = ""
    is_result: bool = False
    purchaser: str = ""
    agency: str = ""
    budget_amount: float | None = None
    currency: str = ""
    province: str = ""
    industry: str = ""
    publish_time: datetime | None = None
    deadline: datetime | None = None
    deadline_raw: str = ""
    deadline_confident: bool = False
    cluster_id: int | None = None
    is_primary: bool = True
    # 冗余：前端直接判断紧急程度，不用自己算
    days_left: int | None = None
    expired: bool = False
    mirrors: int = 0

    class Config:
        from_attributes = True


class TenderDetail(TenderOut):
    content_text: str = ""
    raw_path: str = ""
    attachments: list | None = None
    extract_meta: dict | None = None
    mirrors_list: list[dict] = Field(default_factory=list)


class Paged(BaseModel):
    total: int
    page: int
    size: int
    items: list[TenderOut]


class SourceOut(BaseModel):
    id: str
    name: str
    base_url: str = ""
    enabled: bool = True
    mode: str = "http"
    health: str = "idle"
    fail_count: int = 0
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    rule_version: str = ""
    schedule: str = ""

    class Config:
        from_attributes = True


class SourcePatch(BaseModel):
    enabled: bool | None = None
    name: str | None = None


class CrawlRequest(BaseModel):
    site_id: str | None = None
    max_pages: int = 3
    keyword: str | None = None
    fetch_detail: bool = False


class CrawlResult(BaseModel):
    ok: bool = True
    results: dict[str, Any] = Field(default_factory=dict)


class HealthOut(BaseModel):
    status: str = "ok"
    version: str = "0.1.0"
    db: str = ""
    total: int = 0
    sources: int = 0
    scheduler: bool = False
    last_run_at: datetime | None = None
