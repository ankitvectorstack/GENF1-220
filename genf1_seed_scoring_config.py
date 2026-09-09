"""
GenF1 — Seed / Update Scoring Config (T-058 / GENF1-123)
============================================================

Writes the 4 component weights and the exclusion floor to the
`scoring_config` collection. This is the ONLY place these values should
ever be set — the scoring stage (genf1_content_quality_scorer.py) only
ever reads them, never hardcodes them.

Every write here also appends a row to `scoring_config_changelog`
(old values, new values, timestamp) per the ticket's requirement that
config changes have an audit trail.

*** THE FLOOR VALUE BELOW IS A PLACEHOLDER, NOT A SIGNED-OFF NUMBER ***
The ticket specifies the 4 weights explicitly (0.40 / 0.25 / 0.20 / 0.15)
— those are real, from the spec. It does NOT specify a floor value
anywhere; the ticket says only that the floor must be a config value
that gets signed off. DEFAULT_FLOOR here (0.35) is a reasonable engineering
guess for testing this code end-to-end — it must be replaced with the
actual approved number before this runs against real content.

Usage
-----
    python genf1_seed_scoring_config.py                    # seed with defaults below
    python genf1_seed_scoring_config.py --floor 0.4         # seed/update with a specific floor
    python genf1_seed_scoring_config.py --weights 0.4 0.25 0.2 0.15 --floor 0.4
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone

from pymongo import MongoClient

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DB_NAME = "genf1"
SCORING_CONFIG_COLLECTION = "scoring_config"
CHANGELOG_COLLECTION = "scoring_config_changelog"

# Weights are from the ticket spec directly.
DEFAULT_WEIGHTS = {
    "source_weight": 0.40,
    "completeness": 0.25,
    "recency": 0.20,
    "originality": 0.15,
}
# NOT from the spec — placeholder pending real sign-off. See module docstring.
DEFAULT_FLOOR = 0.35
DEFAULT_RECENCY_HALF_LIFE_DAYS = 14.0
DEFAULT_RECENCY_MISSING = 0.0


def set_config(mongo_uri: str, weights: dict, floor: float,
                recency_half_life_days: float, default_recency_missing: float,
                dry_run: bool = False) -> None:
    if abs(sum(weights.values()) - 1.0) > 1e-9:
        raise ValueError(f"Weights must sum to 1.0, got {sum(weights.values())}: {weights}")
    if not (0.0 <= floor <= 1.0):
        raise ValueError(f"Floor must be in [0.0, 1.0], got {floor}")

    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=8000)
    db = client[DB_NAME]
    client.admin.command("ping")

    config_col = db[SCORING_CONFIG_COLLECTION]
    changelog_col = db[CHANGELOG_COLLECTION]

    previous = config_col.find_one({"active": True})
    new_version = (previous.get("version", 0) + 1) if previous else 1

    new_doc = {
        "active": True,
        "version": new_version,
        "weights": weights,
        "floor": floor,
        "recency_half_life_days": recency_half_life_days,
        "default_recency_missing": default_recency_missing,
        "updated_at": datetime.now(timezone.utc),
    }

    changelog_entry = {
        "version": new_version,
        "previous_config": {k: v for k, v in (previous or {}).items() if k != "_id"},
        "new_config": {k: v for k, v in new_doc.items() if k != "_id"},
        "changed_at": datetime.now(timezone.utc),
    }

    print(f"{'[DRY RUN] Would set' if dry_run else 'Setting'} scoring_config "
          f"version {new_version}: weights={weights}, floor={floor}")

    if dry_run:
        return

    if previous:
        config_col.update_one({"_id": previous["_id"]}, {"$set": {"active": False}})
    config_col.insert_one(new_doc)
    changelog_col.insert_one(changelog_entry)

    print(f"Done. '{SCORING_CONFIG_COLLECTION}' now at version {new_version}; "
          f"logged to '{CHANGELOG_COLLECTION}'.")

    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uri", default=os.environ.get("MONGO_URI"))
    parser.add_argument("--weights", nargs=4, type=float,
                         metavar=("SOURCE", "COMPLETENESS", "RECENCY", "ORIGINALITY"),
                         default=[DEFAULT_WEIGHTS["source_weight"], DEFAULT_WEIGHTS["completeness"],
                                  DEFAULT_WEIGHTS["recency"], DEFAULT_WEIGHTS["originality"]])
    parser.add_argument("--floor", type=float, default=DEFAULT_FLOOR,
                         help="NOT specified in the ticket — replace with the real signed-off value")
    parser.add_argument("--recency-half-life-days", type=float, default=DEFAULT_RECENCY_HALF_LIFE_DAYS)
    parser.add_argument("--default-recency-missing", type=float, default=DEFAULT_RECENCY_MISSING)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.uri:
        raise SystemExit("Set MONGO_URI in .env or pass --uri")

    weights = {
        "source_weight": args.weights[0],
        "completeness": args.weights[1],
        "recency": args.weights[2],
        "originality": args.weights[3],
    }

    set_config(
        mongo_uri=args.uri,
        weights=weights,
        floor=args.floor,
        recency_half_life_days=args.recency_half_life_days,
        default_recency_missing=args.default_recency_missing,
        dry_run=args.dry_run,
    )
