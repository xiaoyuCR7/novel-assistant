"""Local, atomic API spending reservations. Never stores prompts or credentials."""

import json
import sqlite3
from contextlib import contextmanager
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from threading import RLock
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from novel_harness.ai.base import ProviderExecutionError, estimate_input_tokens

Money = Annotated[Decimal, Field(ge=0, le=1_000_000, max_digits=13, decimal_places=6)]


class ModelPrice(BaseModel):
    base_url: str = Field(max_length=1000)
    model: str = Field(min_length=1, max_length=200)
    input_per_million: Money
    output_per_million: Money


class SpendingSettings(BaseModel):
    revision: int = Field(ge=0)
    global_limit: Money | None = None
    project_limits: dict[str, Money] = Field(default_factory=dict, max_length=1000)
    prices: list[ModelPrice] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def unique_prices(self):
        identities = [(p.base_url, p.model) for p in self.prices]
        if len(set(identities)) != len(identities):
            raise ValueError("同一模型只能设置一组单价。")
        return self


def _micro(value):
    return int((Decimal(value) * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def _money(value):
    return f"{Decimal(value or 0) / 1_000_000:.6f}"


def _cost(input_tokens, output_tokens, input_price, output_price):
    if input_price is None or output_price is None:
        return None
    return int(
        (
            Decimal(input_tokens) * Decimal(input_price)
            + Decimal(output_tokens) * Decimal(output_price)
        ).to_integral_value(rounding=ROUND_CEILING)
    )


class SpendingLedger:
    def __init__(self, path: Path):
        self.path = path
        self.lock = RLock()
        self.active: set[str] = set()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS entries (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    project_id TEXT, job_id TEXT, stage TEXT, task TEXT NOT NULL,
                    base_url TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
                    input_price TEXT, output_price TEXT,
                    input_tokens INTEGER, output_tokens INTEGER,
                    reserve_micro INTEGER, cost_micro INTEGER, usage_source TEXT, note TEXT
                );
                CREATE INDEX IF NOT EXISTS spending_project ON entries(project_id, created_at);
            """)
            db.execute(
                "INSERT OR IGNORE INTO settings VALUES (1, ?)",
                (SpendingSettings(revision=0).model_dump_json(),),
            )

    @contextmanager
    def _db(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            try:
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def settings(self):
        with self._db() as db:
            return json.loads(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])

    def configure(self, value: SpendingSettings):
        with self._db() as db:
            old = json.loads(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])
            if old["revision"] != value.revision:
                raise ValueError("SPENDING_SETTINGS_CHANGED")
            result = value.model_copy(update={"revision": value.revision + 1})
            db.execute("UPDATE settings SET value=? WHERE id=1", (result.model_dump_json(),))
            return result.model_dump(mode="json")

    def meter(self, identity, *, project_id=None, job_id=None, stage=None):
        return SpendingMeter(self, identity, project_id, job_id, stage)

    @staticmethod
    def _totals(db, project_id=None):
        scope, params = (" AND project_id=?", (project_id,)) if project_id else ("", ())
        row = db.execute(
            """SELECT
            COALESCE(SUM(CASE WHEN status IN ('settled','reconciled')
                THEN cost_micro ELSE 0 END),0) AS settled,
            COALESCE(SUM(CASE WHEN status IN ('reserved','sent','unknown')
                THEN reserve_micro ELSE 0 END),0) AS reserved,
            COALESCE(SUM(CASE WHEN status IN ('reserved','sent','unknown')
                THEN 1 ELSE 0 END),0) AS unresolved,
            COALESCE(SUM(CASE WHEN COALESCE(cost_micro,reserve_micro) IS NULL
                THEN 1 ELSE 0 END),0) AS unpriced,
            COALESCE(SUM(CASE WHEN usage_source IN ('estimated','mixed')
                THEN 1 ELSE 0 END),0) AS estimated
            FROM entries WHERE status!='not_sent'"""
            + scope,
            params,
        ).fetchone()
        return dict(row)

    def report(self, project_id=None):
        with self._db() as db:
            totals = self._totals(db, project_id)
            where, params = ("WHERE project_id=?", (project_id,)) if project_id else ("", ())
            rows = db.execute(
                f"SELECT * FROM entries {where} ORDER BY "
                "CASE WHEN status IN ('reserved','sent','unknown') "
                "OR (status='settled' AND cost_micro IS NULL) THEN 0 ELSE 1 END, "
                "created_at DESC, rowid DESC LIMIT 100",
                params,
            )
            entries = []
            for row in rows:
                item = dict(row)
                for key in ("reserve", "cost"):
                    amount = item.pop(key + "_micro")
                    item[key + "_cny"] = None if amount is None else _money(amount)
                item["can_reconcile"] = item["id"] not in self.active and (
                    item["status"] in {"reserved", "sent", "unknown"}
                    or item["status"] == "settled"
                    and item["cost_cny"] is None
                )
                entries.append(item)
            return {
                "currency": "CNY",
                "totals": {
                    "settled_cny": _money(totals["settled"]),
                    "reserved_cny": _money(totals["reserved"]),
                    "committed_cny": _money(totals["settled"] + totals["reserved"]),
                    "unresolved_count": totals["unresolved"],
                    "unpriced_count": totals["unpriced"],
                    "estimated_count": totals["estimated"],
                },
                "entries": entries,
            }

    def reconcile(self, entry_id, actual_cny, note):
        with self._db() as db:
            row = db.execute(
                "SELECT status,cost_micro FROM entries WHERE id=?", (entry_id,)
            ).fetchone()
            if not row:
                raise KeyError(entry_id)
            if entry_id in self.active or not (
                row[0] in {"reserved", "sent", "unknown"} or row[0] == "settled" and row[1] is None
            ):
                raise ValueError("SPENDING_ENTRY_NOT_RECONCILABLE")
            db.execute(
                "UPDATE entries SET status='reconciled', cost_micro=?, "
                "usage_source='manual', note=? WHERE id=?",
                (_micro(actual_cny), note, entry_id),
            )


class SpendingMeter:
    def __init__(self, ledger, identity, project_id, job_id, stage):
        self.ledger, self.identity = ledger, identity
        self.project_id, self.job_id, self.stage = project_id, job_id, stage

    def reserve(self, request, schema=None):
        input_tokens = estimate_input_tokens(request, schema)
        with self.ledger._db() as db:
            settings = json.loads(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])
            price = next(
                (
                    p
                    for p in settings["prices"]
                    if p["base_url"] == self.identity["base_url"]
                    and p["model"] == self.identity["model"]
                ),
                None,
            )
            input_price = price["input_per_million"] if price else None
            output_price = price["output_per_million"] if price else None
            reserve = _cost(input_tokens, request.output_token_budget, input_price, output_price)
            checks = [(None, settings["global_limit"])]
            if self.project_id:
                checks.append((self.project_id, settings["project_limits"].get(self.project_id)))
            for scope, limit in checks:
                if limit is None:
                    continue
                totals = self.ledger._totals(db, scope)
                if reserve is None or totals["unpriced"]:
                    raise ProviderExecutionError(
                        "请设置模型单价并核对未定价记录后再启用费用限额。",
                        outcome="known",
                        code="SPENDING_PRICE_REQUIRED",
                    )
                if totals["settled"] + totals["reserved"] + reserve > _micro(limit):
                    raise ProviderExecutionError(
                        "本次请求预计超过本地费用限额；请在费用中心查看记录和调整预算。",
                        outcome="known",
                        code="SPENDING_LIMIT_REACHED",
                    )
            entry_id = str(uuid4())
            db.execute(
                """INSERT INTO entries (id,project_id,job_id,stage,task,base_url,model,status,
                input_price,output_price,input_tokens,reserve_micro)
                VALUES (?,?,?,?,?,?,?,'reserved',?,?,?,?)""",
                (
                    entry_id,
                    self.project_id,
                    self.job_id,
                    self.stage,
                    request.task,
                    self.identity["base_url"],
                    self.identity["model"],
                    input_price,
                    output_price,
                    input_tokens,
                    reserve,
                ),
            )
            self.ledger.active.add(entry_id)
            return entry_id

    def sent(self, entry_id):
        with self.ledger._db() as db:
            db.execute(
                "UPDATE entries SET status='sent' WHERE id=? AND status='reserved'", (entry_id,)
            )

    def not_sent(self, entry_id):
        with self.ledger._db() as db:
            db.execute("UPDATE entries SET status='not_sent',cost_micro=0 WHERE id=?", (entry_id,))
            self.ledger.active.discard(entry_id)

    def unknown(self, entry_id):
        with self.ledger._db() as db:
            db.execute(
                "UPDATE entries SET status='unknown' WHERE id=? AND status IN ('reserved','sent')",
                (entry_id,),
            )
            self.ledger.active.discard(entry_id)

    def settle(self, entry_id, usage, usage_source):
        with self.ledger._db() as db:
            row = db.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
            if not row or row["status"] not in {"reserved", "sent", "unknown"}:
                return
            incoming, outgoing = usage["input_tokens"], usage["output_tokens"]
            cost = _cost(incoming, outgoing, row["input_price"], row["output_price"])
            db.execute(
                """UPDATE entries SET status='settled',input_tokens=?,output_tokens=?,
                          cost_micro=?,usage_source=?
                          WHERE id=?""",
                (incoming, outgoing, cost, usage_source, entry_id),
            )
            self.ledger.active.discard(entry_id)
