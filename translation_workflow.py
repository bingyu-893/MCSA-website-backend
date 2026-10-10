"""Persistent English candidates and review workflow; public copy is a projection.

Content revision protects CMS edits. Each translation has its own version so a
worker cannot overwrite a newer source, draft, or review decision.
"""
from contextlib import closing
from copy import deepcopy
import json
import logging
import os
import re
import threading
import time
import uuid

from translations import TRADITIONAL, translate_text, TranslationError


def fields(value, path=()):
    if isinstance(value, dict):
        if {"zh", "en", "hant"}.issubset(value):
            yield path, value
        else:
            for key, child in value.items():
                yield from fields(child, path + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from fields(child, path + (index,))


def write_record(c, record):
    c.execute("INSERT OR REPLACE INTO translation_fields VALUES (?,?)",
              (record["id"], json.dumps(record, ensure_ascii=False)))


def records(c):
    return {key: json.loads(value) for key, value in c.execute("SELECT id,data FROM translation_fields")}


def glossary(c):
    return json.loads(c.execute("SELECT data FROM translation_config WHERE id=1").fetchone()[0])


def public_english(record):
    if not record["source"].strip():
        return "", ""
    if record["protected"]:
        return record["protected"], "stale" if record["protected_source"] != record["source"] else ""
    if record["candidate"] and record["candidate_source"] == record["source"]:
        return record["candidate"], "machine"
    return "", "pending"


class Workflow:
    def __init__(self, connect):
        self.connect = connect
        self.stop_event = threading.Event()
        self.thread = None

    def initialize(self, migrate):
        with closing(self.connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("CREATE TABLE IF NOT EXISTS translation_fields (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            c.execute("""CREATE TABLE IF NOT EXISTS translation_jobs (
                id TEXT PRIMARY KEY, token TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_at REAL NOT NULL DEFAULT 0, lease TEXT, lease_until REAL NOT NULL DEFAULT 0)""")
            c.execute("CREATE TABLE IF NOT EXISTS translation_config (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)")
            c.execute("INSERT OR IGNORE INTO translation_config VALUES (1, ?)",
                      (json.dumps({"version": 1, "terms": []}),))
            data = migrate(json.loads(c.execute("SELECT data FROM site WHERE id=1").fetchone()[0]))
            self.sync(c, data, bootstrap=True)
            config = glossary(c)
            if not config.get("seeded"):
                suggestions = {}
                for collection in ("departments", "merchants", "sponsors", "posts"):
                    for item in data.get(collection, []):
                        name = item.get("name", item.get("title", {}))
                        if name.get("zh") and name.get("en"):
                            suggestions.setdefault(name["zh"], name["en"])
                config.update(seeded=True, terms=[{"zh": zh, "en": en, "enabled": False}
                                                  for zh, en in suggestions.items()])
                c.execute("UPDATE translation_config SET data=? WHERE id=1", (json.dumps(config),))
            c.execute("UPDATE site SET data=? WHERE id=1", (json.dumps(data, ensure_ascii=False),))

    def enqueue(self, c, record):
        record["token"] = uuid.uuid4().hex
        record["error"] = ""
        record["status"] = "pending" if record["source"].strip() else "empty"
        c.execute("DELETE FROM translation_jobs WHERE id=?", (record["id"],))
        if record["source"].strip():
            c.execute("INSERT INTO translation_jobs (id,token) VALUES (?,?)", (record["id"], record["token"]))

    def sync(self, c, data, bootstrap=False):
        known = records(c)
        seen = set()
        for path, value in fields(data):
            identifier = value.get("_translationId")
            if identifier and (identifier in seen or (identifier not in known and not bootstrap)):
                raise ValueError("invalid_translation_id")
            identifier = identifier or uuid.uuid4().hex
            value["_translationId"] = identifier
            seen.add(identifier)
            record = known.get(identifier)
            if record is None:
                old_english = value["en"] if bootstrap else ""
                record = dict(id=identifier, source=value["zh"], version=1, candidate="",
                              candidate_source="", draft=None, draft_source="", protected=old_english,
                              protected_source=value["zh"] if old_english else "", reviewed_by=None,
                              reviewed_at=None, legacy=bool(old_english), error="", token="",
                              status="legacy" if old_english else "empty")
                if not old_english:
                    self.enqueue(c, record)
            elif record["source"] != value["zh"]:
                record["source"] = value["zh"]
                record["version"] += 1
                # Keep previous candidates and drafts for the reviewer, but never
                # serve them as the machine translation of the new source.
                self.enqueue(c, record)
            record["path"] = list(path)
            value["hant"] = TRADITIONAL.convert(value["zh"])
            value["en"] = public_english(record)[0]  # Ignore submitted English.
            write_record(c, record)
        for identifier in known.keys() - seen:
            c.execute("DELETE FROM translation_jobs WHERE id=?", (identifier,))
            c.execute("DELETE FROM translation_fields WHERE id=?", (identifier,))

    def project(self, c, data, public=False):
        result = deepcopy(data)
        known = records(c)
        for _, value in fields(result):
            record = known.get(value.get("_translationId"))
            if record:
                value["en"], status = public_english(record)
                if public and status:
                    value["_translationStatus"] = status
            if public:
                value.pop("_translationId", None)
        return result

    def snapshot(self, c):
        return {"fields": records(c), "glossary": glossary(c),
                "configured": bool(os.environ.get("AZURE_TRANSLATOR_KEY"))}

    def mutate(self, c, body, user):
        action = body.get("action")
        entries = body.get("entries")
        if not isinstance(action, str) or action not in {"draft", "approve", "retry"} or not isinstance(entries, list) or not 1 <= len(entries) <= 1000:
            raise ValueError("invalid_translation")
        known = records(c)
        selected = set()
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or type(entry.get("version")) is not int:
                raise ValueError("invalid_translation")
            record = known.get(entry.get("id"))
            if not record or record["id"] in selected or entry.get("version") != record["version"]:
                raise ValueError("translation_conflict")
            selected.add(record["id"])
            if action in {"draft", "approve"}:
                english = entry.get("text")
                if not isinstance(english, str) or len(english) > 12000 or (action == "approve" and record["source"].strip() and not english.strip()):
                    raise ValueError("invalid_translation")
                if sorted(re.findall(r"\{[A-Za-z_][\w]*\}", english)) != sorted(re.findall(r"\{[A-Za-z_][\w]*\}", record["source"])):
                    raise ValueError("invalid_placeholders")
                record["draft"] = english
                record["draft_source"] = record["source"]
                if action == "approve":
                    record.update(protected=english, protected_source=record["source"],
                                  draft=None, legacy=False, reviewed_by=user["username"],
                                  reviewed_at=time.time(), status="approved", error="")
                    # Invalidate in-flight translation as well as queued work.
                    record["token"] = uuid.uuid4().hex
                    c.execute("DELETE FROM translation_jobs WHERE id=?", (record["id"],))
            else:
                self.enqueue(c, record)
            record["version"] += 1
            write_record(c, record)

    def save_glossary(self, c, body):
        config = glossary(c)
        if type(body.get("version")) is not int or body.get("version") != config["version"]:
            raise ValueError("translation_conflict")
        terms = body.get("terms")
        if not isinstance(terms, list) or len(terms) > 500:
            raise ValueError("invalid_glossary")
        seen = set()
        clean = []
        for term in terms:
            if not isinstance(term, dict) or set(term) != {"zh", "en", "enabled"} or type(term["enabled"]) is not bool:
                raise ValueError("invalid_glossary")
            if not all(isinstance(term[k], str) and 0 < len(term[k].strip()) <= 200 for k in ("zh", "en")):
                raise ValueError("invalid_glossary")
            zh, en = term["zh"].strip(), term["en"].strip()
            if zh in seen or re.search(r"[{}]", zh + en):
                raise ValueError("invalid_glossary")
            seen.add(zh)
            clean.append(dict(zh=zh, en=en, enabled=term["enabled"]))
        before = {t["zh"]: t["en"] for t in config["terms"] if t["enabled"]}
        after = {t["zh"]: t["en"] for t in clean if t["enabled"]}
        changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
        config.update(version=config["version"] + 1, terms=clean)
        c.execute("UPDATE translation_config SET data=? WHERE id=1", (json.dumps(config, ensure_ascii=False),))
        for record in records(c).values():
            if any(term in record["source"] for term in changed):
                self.enqueue(c, record)
                record["version"] += 1
                write_record(c, record)

    def run_once(self):
        """Claim one job with a lease; network calls never hold a SQLite lock."""
        now = time.time()
        with closing(self.connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            job = c.execute("SELECT id,token,attempts FROM translation_jobs WHERE next_at<=? AND lease_until<=? ORDER BY next_at LIMIT 1", (now, now)).fetchone()
            if not job:
                return False
            identifier, token, attempts = job
            record = records(c).get(identifier)
            if not record or record["token"] != token:
                c.execute("DELETE FROM translation_jobs WHERE id=?", (identifier,))
                return True
            lease = uuid.uuid4().hex
            c.execute("UPDATE translation_jobs SET lease=?,lease_until=? WHERE id=?", (lease, now + 90, identifier))
            terms = glossary(c)["terms"]
        try:
            english = translate_text(record["source"], terms)
            error = None
        except TranslationError as exc:
            english, error = "", exc
        except Exception:
            english, error = "", TranslationError("invalid_response", retryable=False)
        with closing(self.connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            job = c.execute("SELECT token,lease FROM translation_jobs WHERE id=?", (identifier,)).fetchone()
            current = records(c).get(identifier)
            if not current or not job or job != (token, lease) or current["token"] != token:
                return True
            if error:
                current["status"], current["error"] = "failed", error.code
                if error.code == "not_configured":
                    # Recheck configuration without consuming attempts.
                    c.execute("UPDATE translation_jobs SET next_at=?,lease=NULL,lease_until=0 WHERE id=?", (time.time() + 60, identifier))
                elif error.retryable and attempts < 3:
                    c.execute("UPDATE translation_jobs SET attempts=attempts+1,next_at=?,lease=NULL,lease_until=0 WHERE id=?", (time.time() + 5 * 2 ** attempts, identifier))
                else:
                    c.execute("DELETE FROM translation_jobs WHERE id=?", (identifier,))
            else:
                current.update(candidate=english, candidate_source=current["source"], status="review", error="")
                c.execute("DELETE FROM translation_jobs WHERE id=?", (identifier,))
            # The worker updates only candidate/status. Drafts and approved copy
            # are taken from the current row, never from the pre-request snapshot.
            current["version"] += 1
            write_record(c, current)
        return True

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        def work():
            while not self.stop_event.is_set():
                try:
                    active = self.run_once()
                except Exception:
                    logging.getLogger(__name__).exception("Translation queue processing failed")
                    active = False  # Lease makes interrupted jobs recoverable.
                self.stop_event.wait(0.1 if active else 2)
        self.thread = threading.Thread(target=work, name="mcsa-translations", daemon=True)
        self.thread.start()

    def export(self, c):
        return {"version": 1, "fields": records(c), "glossary": glossary(c)}

    def restore(self, c, data, bundle):
        c.execute("DELETE FROM translation_jobs")
        c.execute("DELETE FROM translation_fields")
        if not bundle:
            c.execute("UPDATE translation_config SET data=? WHERE id=1", (json.dumps({"version": 1, "terms": [], "seeded": False}),))
        if bundle:
            if bundle.get("version") != 1:
                raise ValueError("Unsupported translation backup")
            allowed = {v.get("_translationId"): v["zh"] for _, v in fields(data)}
            for identifier, record in bundle["fields"].items():
                if identifier in allowed and record["id"] == identifier:
                    record = deepcopy(record)
                    write_record(c, record)
            c.execute("UPDATE translation_config SET data=? WHERE id=1", (json.dumps(bundle["glossary"]),))
        self.sync(c, data, bootstrap=True)
        for record in records(c).values():
            if record["status"] in {"pending", "failed"}:
                self.enqueue(c, record)
                write_record(c, record)
