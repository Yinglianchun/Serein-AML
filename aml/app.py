"""FastAPI surface for the Agent Memory Leaderboard Textual Memory track."""

from __future__ import annotations

from typing import Any
import logging
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from pydantic import BaseModel, Field, field_validator

from .engine import AddConflict, SearchInputError, add_memory, api_key_matches, search_memory
from .delivery import DeliveryError
from .runtime import ProfileConflict


app = FastAPI(title="Serein-AML", version="0.1.0")
logger = logging.getLogger(__name__)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    if request.url.path == "/search":
        # Never log input, context, error messages, or the request body.
        fields = {"body", "query", "options", "user_id", "top_k"}
        errors = [{"loc": [part if isinstance(part, int) or part in fields else "other"
                           for part in error["loc"]], "type": error["type"]}
                  for error in exc.errors()]
        logger.warning("aml_search_validation errors=%s", errors)
    return await request_validation_exception_handler(request, exc)


def _log_search_failure(exc: Exception) -> str:
    error_id = uuid4().hex
    frames = []
    trace = exc.__traceback__
    while trace is not None:
        code = trace.tb_frame.f_code
        frames.append(f"{Path(code.co_filename).name}:{code.co_name}:{trace.tb_lineno}")
        trace = trace.tb_next
    # A traceback's message/source/locals can contain question or memory text.
    logger.error("aml_search_internal error_id=%s exception=%s frames=%s",
                 error_id, type(exc).__name__, frames)
    return error_id


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
    except (AddConflict, ProfileConflict) as exc:
        raise HTTPException(status_code=409, detail={"reason": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"reason": str(exc)}) from exc
    except RuntimeError:
        raise HTTPException(status_code=503, detail={"reason": "Public ingestion did not complete; retry the same request_id"}) from None
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
    try:
        data = search_memory(query=payload.query, options=payload.options, user_id=payload.user_id, top_k=payload.top_k)
        return SearchResponse(data=[SearchItem(**item) for item in data])
    except ProfileConflict as exc:
        raise HTTPException(status_code=409, detail={"reason": str(exc)}) from exc
    except SearchInputError as exc:
        logger.warning("aml_search_validation reason=input_budget")
        raise HTTPException(status_code=422, detail={"reason": str(exc)}) from exc
    except DeliveryError as exc:
        logger.warning("aml_search_delivery reason=%s", exc.reason)
        raise HTTPException(status_code=503, detail={"reason": exc.reason}) from None
    except Exception as exc:
        error_id = _log_search_failure(exc)
        raise HTTPException(status_code=500, detail={
            "reason": "Search failed internally", "error_id": error_id,
        }) from None
