"""FastAPI surface for the Agent Memory Leaderboard Textual Memory track."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

from .engine import AddConflict, add_memory, api_key_matches, search_memory


app = FastAPI(title="Serein-AML", version="0.1.0")


class Message(BaseModel):
    role: str
    timestamp: int | None = None
    content: str

    @field_validator("content")
    @classmethod
    def content_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be empty")
        return value


class AddRequest(BaseModel):
    request_id: str = Field(min_length=1)
    messages: list[Message] = Field(min_length=1)
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)


class AddResponse(BaseModel):
    success: bool
    request_id: str
    user_id: str
    session_id: str


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    options: list[str] | None = None
    user_id: str = Field(min_length=1)
    top_k: int = Field(ge=1, le=100)


class SearchItem(BaseModel):
    id: str
    content: str
    score: float | None = None
    created_at: str | None = None


class SearchResponse(BaseModel):
    data: list[SearchItem]


def _authorize(authorization: str | None, x_api_key: str | None) -> None:
    if not api_key_matches(authorization, x_api_key):
        raise HTTPException(status_code=401, detail={"reason": "invalid memory system key"})


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "serein-aml"}


@app.post("/add", response_model=AddResponse)
def add(
    payload: AddRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
) -> AddResponse:
    _authorize(authorization, x_api_key)
    try:
        add_memory(
            request_id=payload.request_id,
            messages=[message.model_dump() for message in payload.messages],
            user_id=payload.user_id,
            session_id=payload.session_id,
        )
    except AddConflict as exc:
        raise HTTPException(status_code=409, detail={"reason": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"reason": str(exc)}) from exc
    return AddResponse(
        success=True,
        request_id=payload.request_id,
        user_id=payload.user_id,
        session_id=payload.session_id,
    )


@app.post("/search", response_model=SearchResponse)
def search(
    payload: SearchRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
) -> SearchResponse:
    _authorize(authorization, x_api_key)
    data = search_memory(
        query=payload.query,
        options=payload.options,
        user_id=payload.user_id,
        top_k=payload.top_k,
    )
    return SearchResponse(data=[SearchItem(**item) for item in data])
