# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: Pydantic schemas for all API request/response models.
# Date: 2026-01-20
# ---------------------------------------------------------------------------
"""
Pydantic schemas for all API request/response models.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── Enums ─────────────────────────────────────────────────────

class LLMProvider(str, Enum):
    ollama = "ollama"
    openai = "openai"


class FeedbackRating(int, Enum):
    thumbs_down = 1
    neutral = 2
    thumbs_up = 3


# ── Settings ──────────────────────────────────────────────────

class SettingsUpdate(BaseModel):
    llm_provider: LLMProvider
    ollama_model: Optional[str] = None
    ollama_base_url: Optional[str] = None
    openai_api_key: Optional[str] = None
    openai_model: Optional[str] = None


class SettingsResponse(BaseModel):
    llm_provider: LLMProvider
    ollama_model: str
    ollama_base_url: str
    openai_model: str
    openai_configured: bool


# ── Agent Query ───────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str  # "human" | "assistant"
    content: str


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=100000)
    chat_history: Optional[List[ChatMessage]] = []
    llm_provider: Optional[LLMProvider] = None  # overrides global setting


class SourceDoc(BaseModel):
    source: str
    type: str
    relevance: float


class QueryResponse(BaseModel):
    answer: str
    sources: List[SourceDoc]
    confidence: float
    context_used: bool
    mode: str = "chat"
    session_id: Optional[str] = None


class SyntheticTicketRequest(BaseModel):
    llm_provider: Optional[LLMProvider] = LLMProvider.ollama


class SyntheticTicketResponse(BaseModel):
    ticket_text: str
    model_provider: str
    sources: List[SourceDoc]


# ── ServiceNow ────────────────────────────────────────────────

class ServiceNowTicketRequest(BaseModel):
    ticket_number: str = Field(..., pattern=r"^[A-Za-z0-9]+$")
    additional_context: Optional[str] = None
    llm_provider: Optional[LLMProvider] = None


class ServiceNowConnectionRequest(BaseModel):
    base_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    encrypted_password: Optional[str] = None
    # OAuth (resource owner password credentials grant) — set both to authenticate
    # via bearer token instead of basic auth, for instances with basic auth disabled.
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    auth_type: str = "basic"


class ServiceNowSyncRequest(BaseModel):
    base_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    encrypted_password: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    auth_type: str = "basic"
    query: str = "active=true"
    limit: int = Field(default=500, ge=1, le=100000)
    batch_size: int = Field(default=100, ge=10, le=500)


class ServiceNowSyncStatusResponse(BaseModel):
    can_chat: bool
    requires_sync: bool
    is_fresh: bool
    max_age_hours: int
    last_sync_at: Optional[str] = None
    hours_since_sync: Optional[float] = None
    has_indexed_data: bool
    total_chunks: int
    source_name: Optional[str] = None
    tickets_fetched: int
    chunks_indexed: int
    reason: Optional[str] = None


class ManualTicketRequest(BaseModel):
    """Manually entered ticket details (when no live SN connection)."""
    incident_number: str
    short_description: str
    description: str
    priority: Optional[str] = None
    category: Optional[str] = None
    affected_user: Optional[str] = None
    additional_context: Optional[str] = None
    llm_provider: Optional[LLMProvider] = None


# ── Feedback ──────────────────────────────────────────────────

class FeedbackRequest(BaseModel):
    session_id: str
    question: str
    answer: str
    rating: FeedbackRating
    comment: Optional[str] = None
    sources: Optional[List[str]] = []


class FeedbackResponse(BaseModel):
    feedback_id: str
    message: str


# ── Admin ─────────────────────────────────────────────────────

class IndexRequest(BaseModel):
    reindex: bool = False  # if True, wipe and re-index all


class IndexResponse(BaseModel):
    status: str
    chunks_indexed: int
    files_processed: int


class KnowledgeDocument(BaseModel):
    doc_id: str
    source: str
    type: str
    indexed_at: datetime
    chunk_count: int


class DeleteDocRequest(BaseModel):
    source_name: str


# ── Ticket Update ─────────────────────────────────────────────

class TicketUpdateRequest(BaseModel):
    ticket_number: str = Field(..., pattern=r"^[A-Za-z0-9]+$")
    resolution_notes: str = Field(..., min_length=10)
    state: Optional[str] = "resolved"   # resolved | in_progress | closed


class TicketUpdateResponse(BaseModel):
    ticket_number: str
    status: str
    message: str


# ── Incident Workbench ───────────────────────────────────────

class IncidentFeatureRequest(BaseModel):
    feature: str = Field(
        ...,
        pattern=r"^(classify_incident|anomaly_analysis|event_correlation|incident_summarize|similar_incidents|remediation_playbook|script_generator)$",
    )
    incident_number: Optional[str] = None
    short_description: str
    description: str
    priority: Optional[str] = None
    category: Optional[str] = None
    additional_context: Optional[str] = None
    llm_provider: Optional[LLMProvider] = None


class SimilarIncident(BaseModel):
    incident_number: str
    summary: str
    source: str
    relevance: float


class IncidentFeatureResponse(BaseModel):
    feature: str
    answer: str
    confidence: float
    context_used: bool = False
    mode: str = "analysis"
    sources: List[SourceDoc]
    similar_incidents: Optional[List[SimilarIncident]] = None
