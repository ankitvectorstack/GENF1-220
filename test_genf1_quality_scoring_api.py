"""
GenF1 — Automated tests for genf1_quality_scoring_api.py
============================================================

Runs the FastAPI app against an in-memory mongomock database — no real
MongoDB connection needed, no risk to real data. This is the same
approach used to validate the API before delivery.

Usage
-----
    pip install pytest httpx mongomock "pymongo<4.7"

    (mongomock's bulk_write emulation lags behind newer pymongo releases;
    pin pymongo below 4.7 in your test environment only. Your real
    service can run whatever pymongo version you like — this pin is
    for these tests only, e.g. in a separate requirements-test.txt or a
    tox/venv used only for testing.)

    pytest test_genf1_quality_scoring_api.py -v
"""

import os
from datetime import datetime, timezone

import mongomock
import pymongo
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("MONGO_URI", "mongodb://fake:27017")

# Patch MongoClient everywhere it's referenced BEFORE importing the app,
# so the app's lifespan connects to an in-memory mock instead of a real server.
pymongo.MongoClient = mongomock.MongoClient
import genf1_content_quality_scorer as core  # noqa: E402
core.MongoClient = mongomock.MongoClient
import genf1_quality_scoring_api as api_mod  # noqa: E402
api_mod.MongoClient = mongomock.MongoClient


@pytest.fixture()
def client():
    with TestClient(api_mod.app) as c:
        yield c


@pytest.fixture()
def seeded_client(client):
    """A client with a config already set and two sample articles loaded."""
    client.post("/config", json={
        "weights": {"source_weight": 0.40, "completeness": 0.25,
                    "recency": 0.20, "originality": 0.15},
        "floor": 0.4,
    })
    db = api_mod.app.state.db
    db["source_registry"].insert_one({"collection": "f1_news_org", "trust_tier": "T2"})
    ins = db["raw_articles"].insert_many([
        {"title": "Verstappen wins", "text": "long report", "author": "A", "url": "http://x",
         "published_at": datetime.now(timezone.utc), "source_collection": "f1_news_org"},
        {"title": "Sparse item", "source_collection": "f1_news_org"},
    ])
    return client, [str(i) for i in ins.inserted_ids]


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_config_missing_before_seed(client):
    r = client.get("/config")
    assert r.status_code == 404


def test_set_config_success(client):
    r = client.post("/config", json={
        "weights": {"source_weight": 0.40, "completeness": 0.25,
                    "recency": 0.20, "originality": 0.15},
        "floor": 0.4,
    })
    assert r.status_code == 201
    body = r.json()
    assert body["version"] == 1
    assert body["active"] is True
    assert body["floor"] == 0.4


def test_set_config_rejects_bad_weights(client):
    r = client.post("/config", json={
        "weights": {"source_weight": 0.5, "completeness": 0.5,
                    "recency": 0.5, "originality": 0.5},
        "floor": 0.4,
    })
    assert r.status_code == 422  # weights must sum to 1.0


def test_config_versions_and_changelog(client):
    client.post("/config", json={
        "weights": {"source_weight": 0.40, "completeness": 0.25,
                    "recency": 0.20, "originality": 0.15},
        "floor": 0.4,
    })
    r2 = client.post("/config", json={
        "weights": {"source_weight": 0.35, "completeness": 0.30,
                    "recency": 0.20, "originality": 0.15},
        "floor": 0.45,
    })
    assert r2.json()["version"] == 2

    changelog = client.get("/config/changelog").json()
    assert len(changelog) == 2

    current = client.get("/config").json()
    assert current["version"] == 2  # only the latest is active


def test_pending_count_before_and_after_scoring(seeded_client):
    client, item_ids = seeded_client

    before = client.get("/score/pending-count", params={"content_collection": "raw_articles"})
    assert before.json()["pending"] == 2

    client.post("/score/batch", json={"content_collection": "raw_articles", "dry_run": False})

    after = client.get("/score/pending-count", params={"content_collection": "raw_articles"})
    assert after.json()["pending"] == 0


def test_batch_dry_run_does_not_write(seeded_client):
    client, item_ids = seeded_client

    r = client.post("/score/batch", json={"content_collection": "raw_articles", "dry_run": True})
    body = r.json()
    assert body["found"] == 2
    assert body["scored"] == 2
    assert body["dry_run"] is True
    assert len(body["items"]) == 2

    # Nothing should actually be written — pending count unchanged.
    pending = client.get("/score/pending-count", params={"content_collection": "raw_articles"})
    assert pending.json()["pending"] == 2


def test_batch_real_run_scores_within_range(seeded_client):
    client, item_ids = seeded_client

    r = client.post("/score/batch", json={"content_collection": "raw_articles", "dry_run": False})
    body = r.json()
    assert body["scored"] == 2
    assert body["within_budget"] is True

    db = api_mod.app.state.db
    for oid in item_ids:
        from bson import ObjectId
        doc = db["raw_articles"].find_one({"_id": ObjectId(oid)})
        assert 0.0 <= doc["quality_score"] <= 1.0
        assert set(doc["quality_components"].keys()) == {
            "source_weight", "completeness", "recency", "originality"
        }


def test_idempotent_rerun_finds_nothing_new(seeded_client):
    client, item_ids = seeded_client
    client.post("/score/batch", json={"content_collection": "raw_articles", "dry_run": False})

    r2 = client.post("/score/batch", json={"content_collection": "raw_articles", "dry_run": False})
    assert r2.json()["found"] == 0  # already-scored items no longer match "needs scoring"


def test_score_single_item_dry_run(seeded_client):
    client, item_ids = seeded_client
    r = client.post(f"/score/item/{item_ids[0]}", params={"dry_run": True})
    assert r.status_code == 200
    body = r.json()
    assert body["written"] is False
    assert 0.0 <= body["quality_score"] <= 1.0


def test_score_single_item_writes_when_not_dry_run(seeded_client):
    client, item_ids = seeded_client
    r = client.post(f"/score/item/{item_ids[0]}", params={"dry_run": False})
    assert r.status_code == 200
    assert r.json()["written"] is True

    db = api_mod.app.state.db
    from bson import ObjectId
    doc = db["raw_articles"].find_one({"_id": ObjectId(item_ids[0])})
    assert "quality_score" in doc


def test_score_item_invalid_object_id(seeded_client):
    client, _ = seeded_client
    r = client.post("/score/item/not-a-valid-id")
    assert r.status_code == 400


def test_score_item_not_found(seeded_client):
    client, _ = seeded_client
    r = client.post("/score/item/000000000000000000000000")
    assert r.status_code == 404


def test_sparse_item_gets_reduced_completeness(seeded_client):
    """AC2: item missing most envelope fields still gets scored, never skipped."""
    client, item_ids = seeded_client
    sparse_item_id = item_ids[1]  # the "Sparse item" doc has almost no fields
    r = client.post(f"/score/item/{sparse_item_id}", params={"dry_run": True})
    body = r.json()
    assert body["quality_components"]["completeness"] < 0.5
    assert body["quality_score"] is not None  # never skipped
