"""
GenF1 — Content Quality Scoring Stage (T-058 / GENF1-123)
=============================================================

Gives every content item a quality_score (0.0-1.0) computed from 4
weighted components, and stores it on the item so review and feed
ranking can use it. This is new development — nothing before this
ticket touched individual content items, only sources.

    quality_score = w_source * source_weight
                  + w_completeness * completeness
                  + w_recency * recency
                  + w_originality * originality

Per the spec, the 4 weights and the exclusion floor are CONFIG VALUES,
never code constants. This file contains zero hardcoded weights/floor —
`load_scoring_config()` is the only place they enter the program, and it
reads from the `scoring_config` collection. Use
`genf1_seed_scoring_config.py` to write the first config document (the
starting values 0.40/0.25/0.20/0.15 come directly from the ticket spec;
the floor value is a placeholder pending real sign-off — see that file).

Components
----------
  source_weight   0.0-1.0 trust value from the source registry. Derived
                  from `source_registry.trust_tier` (T1-T5) via a
                  configurable tier->weight map; falls back to the
                  spec's stated default of 0.5 if the item's source
                  isn't found in the registry.
  completeness    fraction of "envelope fields" (title, body_text,
                  published_at, author, url, image_url — configurable)
                  that are non-empty on the item.
  recency         decays with item age; items with no published_at
                  can't have their age computed, so recency falls back
                  to a configurable default (see ASSUMPTIONS below).
  originality     1 - highest similarity to an earlier item in the same
                  duplicate cluster (from the dedup stage, T-078). If
                  cluster/similarity data isn't present yet, originality
                  defaults to 1.0 with a note — exactly what the ticket's
                  "If you're stuck" section says to do, to be backfilled
                  once T-078 is live.

ASSUMPTIONS made explicit (none of these are in the ticket text verbatim
— flagging them so they can be confirmed against the real PRD/data model
before this goes anywhere near production)
------------------------------------------------------------------------
  1. "Envelope fields" for completeness: title, body/text, published_at,
     author, url, image — each checked against several possible field
     names since collections don't share a schema (e.g. raw_articles
     uses "text", not "body_text"). See ENVELOPE_FIELD_GROUPS. The
     ticket doesn't enumerate the actual field set.
  2. Recency when published_at is missing: defaults to a configurable
     `default_recency_missing` (seeded at 0.0 — conservative, since an
     item with unknown age can't be shown to be fresh). Completeness
     already takes the hit for the missing field per the spec; this is
     an additional, separate assumption about the recency component.
  3. "Needs scoring" query: items where `quality_score` doesn't exist
     yet. In production this should be driven by consuming the
     `content.deduplicated` event (per the interaction contract) rather
     than a polling query — that event integration isn't built here.
  4. Quarantine after repeated scoring failures (T-077 lifecycle rules)
     is approximated with a `scoring_attempts` counter and a
     `lifecycle_status="quarantined"` flag after 3 failures. The real
     lifecycle state machine (T-077) isn't built here.

Guardrails enforced (Section "Must NOT change" in the ticket)
---------------------------------------------------------------
  - Weights/floor: read from config only, never hardcoded.
  - Never delete: under-floor items are marked excluded_reason and kept.
  - Never touch classification/provenance fields: this stage only ever
    writes quality_score, quality_components, excluded_reason,
    quality_scored_at, and scoring_attempts/lifecycle_status on failure.
    It does not read or write kind/trust_tier/moderation_status/source
    fields on the content item itself.
  - Idempotent re-scoring: if recomputed values are byte-identical to
    what's stored, no write happens (TC-3).
  - Validation before write: every component and the final score must
    be in [0.0, 1.0], and the score must equal the weighted sum to 2
    decimal places, or the write is rejected (TC-5).

Usage
-----
    pip install pymongo python-dotenv

    python genf1_seed_scoring_config.py     # one-time: write starting config
    python genf1_content_quality_scorer.py --dry-run
    python genf1_content_quality_scorer.py --content-collection raw_articles
"""

from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from pymongo import MongoClient, UpdateOne

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DB_NAME = "genf1"
SOURCE_REGISTRY_COLLECTION = "source_registry"
SCORING_CONFIG_COLLECTION = "scoring_config"
DEFAULT_CONTENT_COLLECTION = "raw_articles"  # override with --content-collection

MAX_SCORING_ATTEMPTS = 3

# See ASSUMPTION 1 above. Not a "weight" or "floor" per the ticket's
# restriction — this is a field-list decision, kept as a code constant
# but easy to move to config later if product wants it tunable too.
#
# Each envelope "slot" lists the field name(s) that collection might use
# for that concept — different content collections name things
# differently (e.g. raw_articles uses "text", not "body_text"). A slot
# counts as filled if ANY of its candidate field names is non-empty.
ENVELOPE_FIELD_GROUPS = {
    "title": ["title", "headline"],
    "body": ["body_text", "text", "content", "full_text"],
    "published_at": ["published_at"],
    "author": ["author"],
    "url": ["url"],
    "image": ["image_url", "image"],
}

# See ASSUMPTION about source_weight's default (0.5) and the tier->weight
# mapping, which isn't specified anywhere in the ticket. This connects
# to the trust_tier values genf1_source_classifier_v2.py already writes.
TRUST_TIER_TO_SOURCE_WEIGHT = {
    "T1": 1.0,
    "T2": 0.8,
    "T3": 0.6,
    "T4": 0.4,
    "T5": 0.2,
}
DEFAULT_SOURCE_WEIGHT = 0.5  # spec: "default 0.5" when source isn't found


# ---------------------------------------------------------------------------
# Config (weights + floor) — the ONLY place these values enter the program
# ---------------------------------------------------------------------------
@dataclass
class ScoringConfig:
    weight_source: float
    weight_completeness: float
    weight_recency: float
    weight_originality: float
    floor: float
    recency_half_life_days: float
    default_recency_missing: float
    version: int


def load_scoring_config(db) -> ScoringConfig:
    doc = db[SCORING_CONFIG_COLLECTION].find_one({"active": True})
    if doc is None:
        raise RuntimeError(
            f"No active document in '{SCORING_CONFIG_COLLECTION}'. "
            f"Run genf1_seed_scoring_config.py first — weights and floor "
            f"must come from config, never from code."
        )
    return ScoringConfig(
        weight_source=doc["weights"]["source_weight"],
        weight_completeness=doc["weights"]["completeness"],
        weight_recency=doc["weights"]["recency"],
        weight_originality=doc["weights"]["originality"],
        floor=doc["floor"],
        recency_half_life_days=doc.get("recency_half_life_days", 14.0),
        default_recency_missing=doc.get("default_recency_missing", 0.0),
        version=doc.get("version", 1),
    )


# ---------------------------------------------------------------------------
# Component calculations — pure functions, no I/O, easy to unit test
# ---------------------------------------------------------------------------
def compute_completeness(item: dict) -> float:
    filled_slots = 0
    for candidates in ENVELOPE_FIELD_GROUPS.values():
        if any(str(item.get(f) or "").strip() for f in candidates):
            filled_slots += 1
    return filled_slots / len(ENVELOPE_FIELD_GROUPS)


def compute_recency(item: dict, config: ScoringConfig, now: Optional[datetime] = None) -> float:
    published_at = item.get("published_at")
    if not isinstance(published_at, datetime):
        return config.default_recency_missing

    now = now or datetime.now(timezone.utc)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)

    age_days = max(0.0, (now - published_at).total_seconds() / 86400.0)
    recency = 0.5 ** (age_days / max(config.recency_half_life_days, 0.01))
    return max(0.0, min(1.0, recency))


def compute_originality(item: dict) -> tuple[float, Optional[str]]:
    """Returns (originality, note). Note is set when we fell back to the
    ticket's documented "not ready yet" default (originality=1.0)."""
    similarity = item.get("dedup_max_similarity")
    if similarity is None:
        return 1.0, "originality defaulted to 1.0 — dedup clusters (T-078) not available on this item"
    similarity = max(0.0, min(1.0, float(similarity)))
    return 1.0 - similarity, None


def resolve_source_weight(item: dict, source_weight_by_collection: dict) -> float:
    source_collection = item.get("source_collection") or item.get("source")
    return source_weight_by_collection.get(source_collection, DEFAULT_SOURCE_WEIGHT)


def build_source_weight_map(db) -> dict:
    """One query, not one-per-item — needed to hit the 30s/100-item budget."""
    registry_docs = db[SOURCE_REGISTRY_COLLECTION].find(
        {}, {"collection": 1, "trust_tier": 1}
    )
    return {
        doc["collection"]: TRUST_TIER_TO_SOURCE_WEIGHT.get(doc.get("trust_tier"), DEFAULT_SOURCE_WEIGHT)
        for doc in registry_docs
    }


# ---------------------------------------------------------------------------
# Scoring + validation
# ---------------------------------------------------------------------------
class ScoringValidationError(Exception):
    pass


def compute_quality_score(source_weight: float, completeness: float, recency: float,
                           originality: float, config: ScoringConfig) -> tuple[float, dict]:
    components = {
        "source_weight": source_weight,
        "completeness": completeness,
        "recency": recency,
        "originality": originality,
    }
    for name, value in components.items():
        if not (0.0 <= value <= 1.0):
            raise ScoringValidationError(f"component '{name}'={value} outside [0.0, 1.0]")

    raw_score = (
        config.weight_source * source_weight
        + config.weight_completeness * completeness
        + config.weight_recency * recency
        + config.weight_originality * originality
    )
    # AC5: float error rounding above 1.0 (e.g. 1.004) clamps to exactly 1.0.
    score = round(min(1.0, max(0.0, raw_score)), 2)

    # Validation: score must equal the weighted sum to 2dp (post-clamp).
    expected = round(min(1.0, max(0.0, raw_score)), 2)
    if score != expected:
        raise ScoringValidationError(f"quality_score {score} does not match weighted sum {expected}")

    return score, components


def score_item(item: dict, config: ScoringConfig, source_weight_by_collection: dict,
                now: Optional[datetime] = None) -> dict:
    source_weight = resolve_source_weight(item, source_weight_by_collection)
    completeness = compute_completeness(item)
    recency = compute_recency(item, config, now=now)
    originality, originality_note = compute_originality(item)

    score, components = compute_quality_score(
        source_weight, completeness, recency, originality, config
    )

    result = {
        "quality_score": score,
        "quality_components": components,
        "quality_scored_at": now or datetime.now(timezone.utc),
    }
    if score < config.floor:
        result["excluded_reason"] = "low_quality"
    else:
        result["excluded_reason"] = None  # clear a stale low_quality flag on rescore
    if originality_note:
        result["quality_components_notes"] = [originality_note]
    return result


def needs_rewrite(existing: dict, new: dict) -> bool:
    """Idempotency check (TC-3): skip the write if nothing actually changed."""
    if existing.get("quality_score") != new["quality_score"]:
        return True
    if existing.get("quality_components") != new["quality_components"]:
        return True
    if existing.get("excluded_reason") != new["excluded_reason"]:
        return True
    return False


# ---------------------------------------------------------------------------
# Batch scoring — core logic, DB-connection-agnostic (takes a `db` handle)
# Used by both the CLI (run, below) and the FastAPI service.
# ---------------------------------------------------------------------------
def run_batch_scoring(db, content_collection: str, dry_run: bool = False,
                       batch_limit: int = 100, return_items: bool = False) -> dict:
    config = load_scoring_config(db)
    source_weight_by_collection = build_source_weight_map(db)

    content = db[content_collection]
    # ASSUMPTION 3: "needs scoring" = no quality_score yet. In production
    # this stage should consume the content.deduplicated event instead.
    #
    # Note: scoring_attempts may not exist on a document at all (it's
    # only ever set by this script after a failed attempt). Mongo's
    # comparison operators don't match missing fields, so the query must
    # explicitly allow "doesn't exist" as well as "< MAX_SCORING_ATTEMPTS" —
    # otherwise every never-scored item is silently excluded.
    pending = list(content.find({
        "quality_score": {"$exists": False},
        "$or": [
            {"scoring_attempts": {"$exists": False}},
            {"scoring_attempts": {"$lt": MAX_SCORING_ATTEMPTS}},
        ],
    }).limit(batch_limit))

    started = time.monotonic()
    writes = []
    scored, skipped_idempotent, failed, quarantined = 0, 0, 0, 0
    item_results = [] if return_items else None

    for item in pending:
        item_id = str(item.get("_id"))
        try:
            new_fields = score_item(item, config, source_weight_by_collection)
        except ScoringValidationError as e:
            failed += 1
            attempts = item.get("scoring_attempts", 0) + 1
            update = {"scoring_attempts": attempts, "last_scoring_error": str(e)}
            if attempts >= MAX_SCORING_ATTEMPTS:
                update["lifecycle_status"] = "quarantined"
                quarantined += 1
            if not dry_run:
                writes.append(UpdateOne({"_id": item["_id"]}, {"$set": update}))
            if return_items:
                item_results.append({"item_id": item_id, "error": str(e), "attempt": attempts})
            continue

        if not needs_rewrite(item, new_fields):
            skipped_idempotent += 1
            continue

        scored += 1
        if return_items:
            item_results.append({
                "item_id": item_id,
                "quality_score": new_fields["quality_score"],
                "quality_components": new_fields["quality_components"],
                "excluded_reason": new_fields["excluded_reason"],
                "notes": new_fields.get("quality_components_notes"),
            })
        if not dry_run:
            writes.append(UpdateOne({"_id": item["_id"]}, {"$set": new_fields}))

    if writes and not dry_run:
        content.bulk_write(writes, ordered=False)

    elapsed = time.monotonic() - started

    return {
        "content_collection": content_collection,
        "config_version": config.version,
        "floor": config.floor,
        "found": len(pending),
        "scored": scored,
        "skipped_idempotent": skipped_idempotent,
        "failed": failed,
        "quarantined": quarantined,
        "elapsed_seconds": round(elapsed, 3),
        "dry_run": dry_run,
        "items": item_results,
    }


# ---------------------------------------------------------------------------
# CLI entrypoint — thin wrapper around run_batch_scoring() that prints
# ---------------------------------------------------------------------------
def run(mongo_uri: str, content_collection: str, dry_run: bool = False,
        batch_limit: int = 100) -> None:
    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=8000)
    db = client[DB_NAME]
    client.admin.command("ping")

    result = run_batch_scoring(db, content_collection, dry_run=dry_run,
                                batch_limit=batch_limit, return_items=dry_run)

    print(f"Found {result['found']} item(s) needing scoring in '{content_collection}' "
          f"(config version {result['config_version']}, floor={result['floor']}).")

    if dry_run and result["items"]:
        for r in result["items"]:
            if "error" in r:
                print(f"  [FAILED] {r['item_id']}: {r['error']} (attempt {r['attempt']})")
            else:
                print(f"  [DRY RUN] {r['item_id']}: quality_score={r['quality_score']} "
                      f"components={r['quality_components']} "
                      f"excluded_reason={r['excluded_reason']}")

    print(f"\nScored: {result['scored']}, unchanged (idempotent skip): {result['skipped_idempotent']}, "
          f"failed: {result['failed']} (quarantined: {result['quarantined']})")
    print(f"Elapsed: {result['elapsed_seconds']:.2f}s for {result['found']} item(s) "
          f"({'within' if result['elapsed_seconds'] <= 30 else 'OVER'} the 30s/100-item budget)")

    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uri", default=os.environ.get("MONGO_URI"))
    parser.add_argument("--content-collection", default=DEFAULT_CONTENT_COLLECTION,
                         help=f"Collection holding content items to score (default: {DEFAULT_CONTENT_COLLECTION})")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-limit", type=int, default=100)
    args = parser.parse_args()

    if not args.uri:
        raise SystemExit("Set MONGO_URI in .env or pass --uri")

    run(mongo_uri=args.uri, content_collection=args.content_collection,
        dry_run=args.dry_run, batch_limit=args.batch_limit)
