"""SQLite 持久层：平台配置、原始挂网数据、归一品种、任务水位/断点、
变更历史、告警、任务运行记录。所有采集进度落库，进程被杀后可续跑。"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS platform_config (
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  base_url TEXT NOT NULL,
  mode TEXT NOT NULL,
  config_json TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS raw_drug (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform_code TEXT NOT NULL,
  item_code TEXT NOT NULL,
  generic_name TEXT NOT NULL,
  dosage_form TEXT NOT NULL,
  spec_text TEXT NOT NULL,
  manufacturer TEXT NOT NULL,
  price REAL,
  status TEXT NOT NULL,
  source_updated_at TEXT,
  version INTEGER,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  UNIQUE(platform_code, item_code)
);
CREATE TABLE IF NOT EXISTS drug (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  generic_name TEXT NOT NULL,
  dosage_form TEXT NOT NULL,
  spec_canonical TEXT NOT NULL,
  manufacturer TEXT NOT NULL,
  UNIQUE(generic_name, dosage_form, spec_canonical, manufacturer)
);
CREATE TABLE IF NOT EXISTS drug_mapping (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform_code TEXT NOT NULL,
  item_code TEXT NOT NULL,
  drug_id INTEGER,
  status TEXT NOT NULL,           -- auto / pending / confirmed / rejected
  confidence REAL,
  reason TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(platform_code, item_code),
  FOREIGN KEY(drug_id) REFERENCES drug(id)
);
CREATE TABLE IF NOT EXISTS price_change (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform_code TEXT NOT NULL,
  item_code TEXT NOT NULL,
  drug_id INTEGER,
  change_type TEXT NOT NULL,      -- price / delist / relist
  old_price REAL,
  new_price REAL,
  source_changed_at TEXT,
  detected_at TEXT NOT NULL,
  UNIQUE(platform_code, item_code, source_changed_at, change_type)
);
CREATE TABLE IF NOT EXISTS watermark (
  platform_code TEXT PRIMARY KEY,
  last_source_updated_at TEXT NOT NULL,
  last_success_at TEXT NOT NULL,
  last_job_id INTEGER
);
CREATE TABLE IF NOT EXISTS job_checkpoint (
  job_id INTEGER PRIMARY KEY,
  platform_code TEXT NOT NULL,
  mode TEXT NOT NULL,             -- full / incremental
  status TEXT NOT NULL,           -- running / done / failed / stopped
  list_page INTEGER NOT NULL DEFAULT 0,
  list_done INTEGER NOT NULL DEFAULT 0,
  detail_cursor TEXT,
  detail_total INTEGER,
  detail_done INTEGER NOT NULL DEFAULT 0,
  payload_json TEXT,
  heartbeat_at TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS alert (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform_code TEXT NOT NULL,
  job_id INTEGER,
  kind TEXT NOT NULL,             -- parse / login / platform_down / config
  severity TEXT NOT NULL,        -- warning / critical
  message TEXT NOT NULL,
  raw_excerpt TEXT,
  created_at TEXT NOT NULL,
  resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS run_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER,
  platform_code TEXT NOT NULL,
  level TEXT NOT NULL,
  message TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init(self) -> None:
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        with self._lock:
            conn = self._conn()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # ---------- 平台配置 ----------
    def upsert_platform(self, cfg: dict) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO platform_config(code,name,base_url,mode,config_json,"
                "updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(code) DO UPDATE SET name=excluded.name,"
                "base_url=excluded.base_url,mode=excluded.mode,"
                "config_json=excluded.config_json,updated_at=excluded.updated_at",
                (cfg["code"], cfg["name"], cfg["base_url"], cfg["mode"],
                 json.dumps(cfg, ensure_ascii=False), now_iso()))

    def list_platforms(self, only_enabled: bool = False) -> list[dict]:
        with self.tx() as c:
            sql = "SELECT * FROM platform_config"
            if only_enabled:
                sql += " WHERE enabled=1"
            rows = c.execute(sql + " ORDER BY code").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["config"] = json.loads(d.pop("config_json"))
            out.append(d)
        return out

    def set_platform_enabled(self, code: str, enabled: bool) -> None:
        with self.tx() as c:
            c.execute("UPDATE platform_config SET enabled=?,updated_at=? WHERE code=?",
                      (1 if enabled else 0, now_iso(), code))

    # ---------- 原始数据 upsert，返回是否有变化 ----------
    def upsert_raw(self, item: dict) -> tuple[bool, dict | None]:
        """返回 (是否变化, 旧行或None)。价格/状态/版本变化才算变化。"""
        with self.tx() as c:
            old = c.execute(
                "SELECT * FROM raw_drug WHERE platform_code=? AND item_code=?",
                (item["platform_code"], item["item_code"])).fetchone()
            if old is None:
                c.execute(
                    "INSERT INTO raw_drug(platform_code,item_code,generic_name,"
                    "dosage_form,spec_text,manufacturer,price,status,"
                    "source_updated_at,version,first_seen_at,last_seen_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (item["platform_code"], item["item_code"], item["generic_name"],
                     item["dosage_form"], item["spec_text"], item["manufacturer"],
                     item["price"], item["status"], item["source_updated_at"],
                     item["version"], now_iso(), now_iso()))
                return True, None
            changed = (old["price"] != item["price"] or
                       old["status"] != item["status"] or
                       old["version"] != item["version"])
            c.execute(
                "UPDATE raw_drug SET generic_name=?,dosage_form=?,spec_text=?,"
                "manufacturer=?,price=?,status=?,source_updated_at=?,version=?,"
                "last_seen_at=? WHERE id=?",
                (item["generic_name"], item["dosage_form"], item["spec_text"],
                 item["manufacturer"], item["price"], item["status"],
                 item["source_updated_at"], item["version"], now_iso(), old["id"]))
            return changed, dict(old)

    def get_raw(self, platform: str, item_code: str) -> dict | None:
        with self.tx() as c:
            r = c.execute(
                "SELECT * FROM raw_drug WHERE platform_code=? AND item_code=?",
                (platform, item_code)).fetchone()
        return dict(r) if r else None

    def list_raw(self, platform: str | None = None) -> list[dict]:
        with self.tx() as c:
            if platform:
                rows = c.execute(
                    "SELECT * FROM raw_drug WHERE platform_code=? ORDER BY id",
                    (platform,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM raw_drug ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def record_change(self, platform: str, item_code: str, drug_id,
                      change_type: str, old_price, new_price,
                      source_changed_at: str | None) -> bool:
        """幂等写入变更历史，返回是否真正插入。"""
        with self.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO price_change(platform_code,item_code,drug_id,"
                "change_type,old_price,new_price,source_changed_at,detected_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (platform, item_code, drug_id, change_type, old_price, new_price,
                 source_changed_at, now_iso()))
            return cur.rowcount > 0

    def list_changes(self, platform: str | None = None) -> list[dict]:
        with self.tx() as c:
            sql = ("SELECT pc.*, d.generic_name AS drug_generic FROM price_change pc "
                   "LEFT JOIN drug d ON d.id=pc.drug_id")
            if platform:
                sql += " WHERE pc.platform_code=?"
            sql += " ORDER BY pc.id"
            rows = c.execute(sql, (platform,) if platform else ()).fetchall()
        return [dict(r) for r in rows]

    # ---------- 归一品种与映射 ----------
    def get_or_create_drug(self, norm: dict) -> int:
        with self.tx() as c:
            r = c.execute(
                "SELECT id FROM drug WHERE generic_name=? AND dosage_form=? "
                "AND spec_canonical=? AND manufacturer=?",
                (norm["generic_name"], norm["dosage_form"],
                 norm["spec_canonical"], norm["manufacturer"])).fetchone()
            if r:
                return r["id"]
            cur = c.execute(
                "INSERT INTO drug(generic_name,dosage_form,spec_canonical,manufacturer)"
                " VALUES(?,?,?,?)",
                (norm["generic_name"], norm["dosage_form"],
                 norm["spec_canonical"], norm["manufacturer"]))
            return cur.lastrowid

    def upsert_mapping(self, platform: str, item_code: str, drug_id,
                       status: str, confidence: float, reason: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO drug_mapping(platform_code,item_code,drug_id,status,"
                "confidence,reason,updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(platform_code,item_code) DO UPDATE SET "
                "drug_id=excluded.drug_id,status=excluded.status,"
                "confidence=excluded.confidence,reason=excluded.reason,"
                "updated_at=excluded.updated_at",
                (platform, item_code, drug_id, status, confidence, reason, now_iso()))

    def get_mapping(self, platform: str, item_code: str) -> dict | None:
        with self.tx() as c:
            r = c.execute(
                "SELECT * FROM drug_mapping WHERE platform_code=? AND item_code=?",
                (platform, item_code)).fetchone()
        return dict(r) if r else None

    def list_mappings(self, status: str | None = None) -> list[dict]:
        with self.tx() as c:
            sql = ("SELECT m.*, r.generic_name, r.dosage_form, r.spec_text, "
                   "r.manufacturer FROM drug_mapping m JOIN raw_drug r "
                   "ON r.platform_code=m.platform_code AND r.item_code=m.item_code")
            if status:
                sql += " WHERE m.status=?"
            sql += " ORDER BY m.id"
            rows = c.execute(sql, (status,) if status else ()).fetchall()
        return [dict(r) for r in rows]

    def list_drugs(self) -> list[dict]:
        with self.tx() as c:
            rows = c.execute(
                "SELECT d.*, COUNT(m.id) AS source_count FROM drug d "
                "LEFT JOIN drug_mapping m ON m.drug_id=d.id AND m.status!='rejected' "
                "GROUP BY d.id ORDER BY d.id").fetchall()
        return [dict(r) for r in rows]

    # ---------- 水位 / 断点 / 任务 ----------
    def update_watermark(self, platform: str, ts: str, job_id: int) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO watermark(platform_code,last_source_updated_at,"
                "last_success_at,last_job_id) VALUES(?,?,?,?) "
                "ON CONFLICT(platform_code) DO UPDATE SET "
                "last_source_updated_at=excluded.last_source_updated_at,"
                "last_success_at=excluded.last_success_at,"
                "last_job_id=excluded.last_job_id",
                (platform, ts, now_iso(), job_id))

    def get_watermark(self, platform: str) -> dict | None:
        with self.tx() as c:
            r = c.execute("SELECT * FROM watermark WHERE platform_code=?",
                          (platform,)).fetchone()
        return dict(r) if r else None

    def create_job(self, platform: str, mode: str) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO job_checkpoint(platform_code,mode,status,started_at)"
                " VALUES(?,?, 'running',?)", (platform, mode, now_iso()))
            return cur.lastrowid

    def save_checkpoint(self, job_id: int, **fields) -> None:
        if not fields:
            return
        keys = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [job_id]
        if "payload" in fields:
            idx = list(fields).index("payload")
            vals[idx] = json.dumps(fields["payload"], ensure_ascii=False)
            keys = keys.replace("payload=?", "payload_json=?")
        with self.tx() as c:
            c.execute(f"UPDATE job_checkpoint SET {keys} WHERE job_id=?", vals)

    def get_checkpoint(self, job_id: int) -> dict | None:
        with self.tx() as c:
            r = c.execute("SELECT * FROM job_checkpoint WHERE job_id=?",
                          (job_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["payload"] = json.loads(d.pop("payload_json") or "null")
        return d

    def find_resumable_job(self, platform: str) -> dict | None:
        with self.tx() as c:
            r = c.execute(
                "SELECT * FROM job_checkpoint WHERE platform_code=? AND status='running'"
                " ORDER BY job_id DESC LIMIT 1", (platform,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["payload"] = json.loads(d.pop("payload_json") or "null")
        return d

    def finish_job(self, job_id: int, status: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE job_checkpoint SET status=?, finished_at=? WHERE job_id=?",
                (status, now_iso(), job_id))

    def list_jobs(self) -> list[dict]:
        with self.tx() as c:
            rows = c.execute(
                "SELECT * FROM job_checkpoint ORDER BY job_id DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.pop("payload_json", None)
            out.append(d)
        return out

    def has_running_job(self, platform: str) -> bool:
        with self.tx() as c:
            r = c.execute(
                "SELECT 1 FROM job_checkpoint WHERE platform_code=? AND status='running'"
                " LIMIT 1", (platform,)).fetchone()
        return r is not None

    # ---------- 告警 / 日志 ----------
    def add_alert(self, platform: str, job_id, kind: str, severity: str,
                  message: str, excerpt: str | None = None) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO alert(platform_code,job_id,kind,severity,message,"
                "raw_excerpt,created_at) VALUES(?,?,?,?,?,?,?)",
                (platform, job_id, kind, severity, message, excerpt, now_iso()))
            return cur.lastrowid

    def resolve_alert(self, alert_id: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE alert SET resolved_at=? WHERE id=?",
                      (now_iso(), alert_id))

    def list_alerts(self, include_resolved: bool = False) -> list[dict]:
        with self.tx() as c:
            sql = "SELECT * FROM alert"
            if not include_resolved:
                sql += " WHERE resolved_at IS NULL"
            sql += " ORDER BY id DESC"
            rows = c.execute(sql).fetchall()
        return [dict(r) for r in rows]

    def add_log(self, platform: str, job_id, level: str, message: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO run_log(job_id,platform_code,level,message,created_at)"
                " VALUES(?,?,?,?,?)", (job_id, platform, level, message, now_iso()))

    def list_logs(self, limit: int = 200) -> list[dict]:
        with self.tx() as c:
            rows = c.execute(
                "SELECT * FROM run_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
