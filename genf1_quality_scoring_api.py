"""
GenF1 — Content Quality Scoring API (T-058 / GENF1-123)
===========================================================

FastAPI wrapper around genf1_content_quality_scorer.py. All scoring
logic (weights/floor from config, completeness/recency/originality/
source_weight components, validation, idempotency, exclusion floor)
is unchanged from that module — this file only adds HTTP endpoints on
top of the same tested functions. Keep both files in the same directory;
this one imports the other directly rather than duplicating logic.

Endpoints
---------
  GET    /health                      liveness check
  GET    /config                      current active scoring config
  POST   /config                      set new weights/floor (versioned + logged)
  GET    /config/changelog            history of config changes
  POST   /score/batch                 run the batch scoring stage
  POST   /score/item/{item_id}        score one item on demand
  GET    /score/pending-count         how many items still need scoring

Usage
-----
    pip install fastapi uvicorn pymongo python-dotenv

    .env file (same as the other scripts):
        MONGO_URI=mongodb://user:password@host:port/genf1?authSource=admin

    uvicorn genf1_quality_scoring_api:app --reload --port 8000

Then, e.g.:
    curl http://localhost:8000/health
    curl -X POST http://localhost:8000/score/batch \\
         -H "Content-Type: application/json" \\
         -d '{"content_collection": "raw_articles", "dry_run": true}'
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from genf1_content_quality_scorer import (
    DB_NAME,
    SCORING_CONFIG_COLLECTION,
    ScoringConfig,
    ScoringValidationError,
    build_source_weight_map,
    load_scoring_config,
    run_batch_scoring,
    score_item,
)

CHANGELOG_COLLECTION = "scoring_config_changelog"
MONGO_URI = os.environ.get("MONGO_URI")


# ---------------------------------------------------------------------------
# App lifecycle — one shared MongoClient for the life of the process
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    if not MONGO_URI:
        raise RuntimeError("Set MONGO_URI in .env or the environment before starting the API")
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)
    client.admin.command("ping")
    app.state.client = client
    app.state.db = client[DB_NAME]
    yield
    client.close()


app = FastAPI(
    title="GenF1 Content Quality Scoring API",
    description="T-058 / GENF1-123 — quality_score computation and config management.",
    version="1.0.0",
    lifespan=lifespan,
)


def get_db():
    return app.state.db


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class WeightsIn(BaseModel):
    source_weight: float = Field(ge=0.0, le=1.0)
    completeness: float = Field(ge=0.0, le=1.0)
    recency: float = Field(ge=0.0, le=1.0)
    originality: float = Field(ge=0.0, le=1.0)


class ScoringConfigIn(BaseModel):
    weights: WeightsIn
    floor: float = Field(ge=0.0, le=1.0, description="Exclusion floor — must be the real, signed-off value.")
    recency_half_life_days: float = Field(default=14.0, gt=0.0)
    default_recency_missing: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("weights")
    @classmethod
    def weights_must_sum_to_one(cls, v: WeightsIn) -> WeightsIn:
        total = v.source_weight + v.completeness + v.recency + v.originality
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"weights must sum to 1.0, got {total}")
        return v


class ScoringConfigOut(ScoringConfigIn):
    version: int
    active: bool
    updated_at: datetime


class ChangelogEntry(BaseModel):
    version: int
    previous_config: dict
    new_config: dict
    changed_at: datetime


class QualityComponentsOut(BaseModel):
    source_weight: float
    completeness: float
    recency: float
    originality: float


class ItemScoreOut(BaseModel):
    item_id: str
    quality_score: float
    quality_components: QualityComponentsOut
    excluded_reason: Optional[str]
    notes: Optional[list[str]] = None
    written: bool


class BatchScoreRequest(BaseModel):
    content_collection: str = "raw_articles"
    dry_run: bool = False
    batch_limit: int = Field(default=100, ge=1, le=1000)
    include_items: bool = Field(
        default=False,
        description="Include per-item results in the response. Always true for dry_run.",
    )


class BatchScoreResponse(BaseModel):
    content_collection: str
    config_version: int
    floor: float
    found: int
    scored: int
    skipped_idempotent: int
    failed: int
    quarantined: int
    elapsed_seconds: float
    within_budget: bool
    dry_run: bool
    items: Optional[list[dict]] = None


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Config endpoints
# ---------------------------------------------------------------------------
@app.get("/config", response_model=ScoringConfigOut)
def get_config():
    db = get_db()
    doc = db[SCORING_CONFIG_COLLECTION].find_one({"active": True})
    if doc is None:
        raise HTTPException(status_code=404, detail="No active scoring config. POST /config to set one.")
    doc.pop("_id", None)
    return doc


@app.post("/config", response_model=ScoringConfigOut, status_code=201)
def set_config(payload: ScoringConfigIn):
    db = get_db()
    config_col = db[SCORING_CONFIG_COLLECTION]
    changelog_col = db[CHANGELOG_COLLECTION]

    previous = config_col.find_one({"active": True})
    new_version = (previous.get("version", 0) + 1) if previous else 1

    new_doc = {
        "active": True,
        "version": new_version,
        "weights": payload.weights.model_dump(),
        "floor": payload.floor,
        "recency_half_life_days": payload.recency_half_life_days,
        "default_recency_missing": payload.default_recency_missing,
        "updated_at": datetime.now(timezone.utc),
    }

    changelog_col.insert_one({
        "version": new_version,
        "previous_config": {k: v for k, v in (previous or {}).items() if k != "_id"},
        "new_config": {k: v for k, v in new_doc.items() if k != "_id"},
        "changed_at": datetime.now(timezone.utc),
    })

    if previous:
        config_col.update_one({"_id": previous["_id"]}, {"$set": {"active": False}})
    config_col.insert_one(dict(new_doc))

    new_doc.pop("_id", None)
    return new_doc


@app.get("/config/changelog", response_model=list[ChangelogEntry])
def get_changelog(limit: int = Query(default=20, ge=1, le=200)):
    db = get_db()
    entries = list(
        db[CHANGELOG_COLLECTION].find({}).sort("changed_at", -1).limit(limit)
    )
    for e in entries:
        e.pop("_id", None)
    return entries


# ---------------------------------------------------------------------------
# Scoring endpoints
# ---------------------------------------------------------------------------
@app.post("/score/batch", response_model=BatchScoreResponse)
def score_batch(payload: BatchScoreRequest):
    db = get_db()
    try:
        result = run_batch_scoring(
            db,
            content_collection=payload.content_collection,
            dry_run=payload.dry_run,
            batch_limit=payload.batch_limit,
            return_items=payload.dry_run or payload.include_items,
        )
    except RuntimeError as e:
        # e.g. no active scoring config yet
        raise HTTPException(status_code=409, detail=str(e))

    result["within_budget"] = result["elapsed_seconds"] <= 30
    return result


@app.post("/score/item/{item_id}", response_model=ItemScoreOut)
def score_single_item(
    item_id: str,
    content_collection: str = Query(default="raw_articles"),
    dry_run: bool = Query(default=False),
):
    db = get_db()
    try:
        oid = ObjectId(item_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail=f"'{item_id}' is not a valid ObjectId")

    content = db[content_collection]
    item = content.find_one({"_id": oid})
    if item is None:
        raise HTTPException(status_code=404,
                             detail=f"Item {item_id} not found in '{content_collection}'")

    try:
        config = load_scoring_config(db)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))

    source_weight_by_collection = build_source_weight_map(db)

    try:
        new_fields = score_item(item, config, source_weight_by_collection)
    except ScoringValidationError as e:
        raise HTTPException(status_code=422, detail=f"Scoring validation failed: {e}")

    written = False
    if not dry_run:
        content.update_one({"_id": oid}, {"$set": new_fields})
        written = True

    return {
        "item_id": item_id,
        "quality_score": new_fields["quality_score"],
        "quality_components": new_fields["quality_components"],
        "excluded_reason": new_fields["excluded_reason"],
        "notes": new_fields.get("quality_components_notes"),
        "written": written,
    }


@app.get("/score/pending-count")
def pending_count(content_collection: str = Query(default="raw_articles")):
    db = get_db()
    from genf1_content_quality_scorer import MAX_SCORING_ATTEMPTS
    count = db[content_collection].count_documents({
        "quality_score": {"$exists": False},
        "$or": [
            {"scoring_attempts": {"$exists": False}},
            {"scoring_attempts": {"$lt": MAX_SCORING_ATTEMPTS}},
        ],
    })
    return {"content_collection": content_collection, "pending": count}
