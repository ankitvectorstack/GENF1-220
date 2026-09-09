# GenF1 — Content Quality Scoring

Scores individual content items (not sources) for quality on a 0.0–1.0
scale, so review and feed ranking can use the score. This is separate,
newer work from the source-classification pipeline — see
`README_source_classification.md` for that.

## Contents

| File | What it does |
|---|---|
| `genf1_seed_scoring_config.py` | Writes the content-quality scoring weights + floor to `scoring_config`, with a changelog entry per change. |
| `genf1_content_quality_scorer.py` | Computes `quality_score` for individual content items and writes it back onto them. CLI + importable core logic. |
| `genf1_quality_scoring_api.py` | FastAPI service wrapping the scorer — HTTP endpoints for config management and scoring. |
| `test_genf1_quality_scoring_api.py` | Automated pytest suite for the API, run against an in-memory mock database. |

## Setup

```bash
pip install pymongo python-dotenv fastapi uvicorn pydantic
```

Uses the same `.env` file as the source-classification pipeline:

```
MONGO_URI=mongodb://user:password@host:port/genf1?authSource=admin
```

## The formula

```
quality_score = 0.40 · source_weight
              + 0.25 · completeness
              + 0.20 · recency
              + 0.15 · originality
```

The 4 weights and the exclusion floor are **config values, never code
constants** — this is a hard guardrail from the ticket, enforced by
reading them from the `scoring_config` collection at runtime.

| Component | What it measures |
|---|---|
| `source_weight` | 0.0–1.0 trust value, derived from `source_registry.trust_tier` (T1→1.0 ... T5→0.2), defaulting to 0.5 if the source isn't found |
| `completeness` | fraction of "envelope fields" (title, body/text, published_at, author, url, image) that are filled in |
| `recency` | decays with item age; falls back to a configurable default if `published_at` is missing |
| `originality` | 1 − highest similarity to an earlier item in its duplicate cluster; defaults to 1.0 with a note if dedup data (T-078) isn't available yet |

## First-time setup

```bash
python genf1_seed_scoring_config.py --floor <REAL_SIGNED_OFF_VALUE>
```

⚠️ **The floor value is not specified in the ticket.** The 4 weights
(0.40/0.25/0.20/0.15) come directly from the spec; the exclusion floor
needs real sign-off before this runs against production content. Don't
ship the placeholder default (0.35) as-is.

Every config write also logs an entry to `scoring_config_changelog`
(old values, new values, timestamp) for the audit trail the ticket
requires.

## Running it — CLI

```bash
python genf1_content_quality_scorer.py --dry-run                          # preview, uses raw_articles by default
python genf1_content_quality_scorer.py --content-collection raw_articles  # write for real
```

## Running it — API

```bash
uvicorn genf1_quality_scoring_api:app --reload --port 8000
```

Browse to `http://localhost:8000/docs` for an interactive Swagger UI, or:

```bash
curl http://localhost:8000/health

curl -X POST http://localhost:8000/config \
  -H "Content-Type: application/json" \
  -d '{"weights": {"source_weight":0.40,"completeness":0.25,"recency":0.20,"originality":0.15}, "floor": 0.4}'

curl "http://localhost:8000/score/pending-count?content_collection=raw_articles"

curl -X POST http://localhost:8000/score/batch \
  -H "Content-Type: application/json" \
  -d '{"content_collection": "raw_articles", "dry_run": true, "batch_limit": 10}'
```

| Endpoint | Purpose |
|---|---|
| `GET /health` | liveness check |
| `GET /config` | current active weights/floor |
| `POST /config` | set new weights/floor (rejects if weights don't sum to 1.0; versions + logs to changelog) |
| `GET /config/changelog` | audit trail of config changes |
| `POST /score/batch` | run the batch scoring stage |
| `POST /score/item/{item_id}` | score one item on demand (`?dry_run=true` to preview) |
| `GET /score/pending-count` | how many items still need scoring |

Recommended order against real data: `pending-count` → `score/batch` with
`dry_run: true` and a small `batch_limit` → eyeball the results → run
for real.

## Testing

```bash
pip install pytest httpx mongomock "pymongo<4.7"
python -m pytest test_genf1_quality_scoring_api.py -v
```

Runs against an in-memory mock database — no real Mongo connection
needed, safe to run anytime. 14 tests, covering the happy path, both
failure paths (invalid ObjectId → 400, bad config weights → 422),
idempotency, and the "never skip scoring" rule (AC2).

The `pymongo<4.7` pin is only for this test environment — mongomock's
`bulk_write` emulation hasn't caught up to newer pymongo internals yet.
The real API and CLI have no such pin and work with any current
pymongo version against a real MongoDB server.

## Guardrails enforced (from the ticket's "Must NOT change" section)

- Weights and floor are **config values only** — never hardcoded in the scoring code.
- Low-quality items are **never deleted** — kept and marked `excluded_reason="low_quality"`.
- Scoring **never modifies** `kind`, `trust_tier`, `moderation_status`, or any other classification/provenance field on the item.
- Re-scoring an unchanged item is **idempotent** — identical inputs produce identical output and skip the write entirely (TC-3).
- Every component and the final score are validated to `[0.0, 1.0]` before any write; out-of-range values reject the write instead of silently clamping (TC-5).
- A weighted sum that rounds above 1.0 due to float error (e.g. 1.004) is clamped to exactly 1.0 (AC5).

## Acceptance criteria coverage

| AC | Status |
|---|---|
| AC1 — score + 4 components on every item, weighted sum to 2dp, 30s/100-item budget | ✅ verified offline with synthetic data |
| AC2 — missing body/published_at still scored, never skipped | ✅ verified |
| AC3 — under-floor items stored, queryable, 0 in candidate pools | ✅ `excluded_reason` set; candidate-pool query itself is out of this scope (owned by the feed team, T-069) |
| AC4 — floor raised at runtime, no mass rewrite of already-scored items | ✅ by design — the "needs scoring" query only ever picks up unscored items |
| AC5 — float rounding clamp | ✅ verified |
| AC6 — config-only weights/floor, no deletion, no classification/provenance changes | ✅ by design |

## Assumptions made (not specified in the ticket — confirm before production)

1. **Envelope fields for completeness**: title, body/text, published_at, author, url, image — matched against multiple possible field names per collection (e.g. `raw_articles` uses `text`, not `body_text`).
2. **Recency when `published_at` is missing**: defaults to `0.0` (configurable via `default_recency_missing` in config).
3. **"Needs scoring" query**: items with no `quality_score` field yet. In production this should consume a `content.deduplicated` event instead of polling — that event integration isn't built here.
4. **Quarantine after repeated failures**: approximated with a `scoring_attempts` counter and `lifecycle_status="quarantined"` after 3 failures. The real T-077 lifecycle state machine isn't built here.
5. **`source_weight` mapping**: derived from `source_registry.trust_tier` via `T1→1.0, T2→0.8, T3→0.6, T4→0.4, T5→0.2`, falling back to the spec's stated default of `0.5` if the source isn't found in the registry. This mapping isn't in the ticket and needs sign-off — it's the connective tissue between this ticket and the source-classification pipeline (which produces `trust_tier`, not a numeric weight).

## A known, separate gap

`quality_score` measures completeness/freshness/originality/source
trust — **it does not check topical relevance**. An off-topic item that
happens to be complete and recent can still score well. In testing, an
Apple-CEO article that had ended up in an F1-tagged source scored
0.58 — correct given what this ticket measures, but worth flagging if
topical drift within otherwise-good sources turns out to be a real
problem. Item-level relevance checking, if needed, is separate work
from this ticket (distinct from the source-level relevance check in
the earlier pipeline, which operates on whole collections, not
individual items).

## Dependencies not yet built

- **T-078 (dedup / duplicate clusters)**: `originality` defaults to `1.0` with a note until this exists, per the ticket's own fallback instruction.
- **T-076 (source_weight field)**: the numeric `source_weight` this ticket needs doesn't formally exist as its own field yet — assumption 5 above is a stand-in.
- **T-025, T-027**: listed as blocking dependencies on the parent ticket; confirm current status directly in Jira rather than assuming from this README.
