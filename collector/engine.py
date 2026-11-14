"""采集引擎：单平台任务执行 + 多平台调度。

任务分两阶段，进度全部落 job_checkpoint：
  1) 列表阶段：逐页抓列表（断点 list_page），解析条目摘要；
  2) 详情阶段：全量抓全部详情；增量只抓 updated_at 高于水位或库中不存在的条目。
任何一步失败/被杀，重启后按断点续跑，raw_drug 与 price_change 均幂等，
因此不重复写、不漏抓。解析失败只告警并跳过，绝不写入空数据。
"""
from __future__ import annotations

import threading
import traceback
from pathlib import Path
from urllib.parse import urlencode

from .client import AuthExpired, PlatformClient, PlatformError
from .db import DB
from .normalize import normalize_item
from .parsers import (ParseError, parse_html_detail, parse_html_list,
                      parse_json_detail, parse_json_list)

STOP_FLAG = Path(__file__).resolve().parent.parent / "data" / "STOP_ALL"


class StopFileEvent(threading.Event):
    """同时识别运行时停止文件，便于跨进程/控制台触发停止。"""

    def is_set(self) -> bool:
        return super().is_set() or STOP_FLAG.exists()


class PlatformRunner:
    def __init__(self, db: DB, cfg: dict, stop_event: threading.Event | None = None,
                 log=print):
        self.db = db
        self.cfg = cfg
        self.code = cfg["code"]
        self.stop = stop_event or StopFileEvent()
        self.log = log

    # ---------- 取数适配 ----------
    def _list_url(self, page: int) -> str:
        c = self.cfg
        if c["mode"] == "api_open":
            return f'{c["api_list_path"]}?{urlencode({"page": page})}'
        if c["mode"] == "mixed_login":
            return f'{c["api_list_path"]}?{urlencode({"page": page})}'
        path = c["list_path"]
        sep = "&" if "?" in path else "?"
        return f"{path}{sep}page={page}"

    def _detail_url(self, item_code: str) -> str:
        c = self.cfg
        if c["mode"] == "api_open":
            return f'{c["api_detail_path"]}?{urlencode({"code": item_code})}'
        if c["mode"] == "mixed_login":
            return f'{c["detail_path"]}?{urlencode({"code": item_code})}'
        return f'{c["detail_path"]}?{urlencode({"code": item_code})}'

    def _parse_list(self, text: str):
        if self.cfg["mode"] in ("api_open", "mixed_login"):
            return parse_json_list(text)
        return parse_html_list(text)

    def _parse_detail(self, text: str) -> dict:
        if self.cfg["mode"] == "api_open":
            return parse_json_detail(text)
        return parse_html_detail(text)

    def _fetch_list(self, client: PlatformClient, page: int) -> str:
        url = self._list_url(page)
        if self.cfg["mode"] in ("api_open", "mixed_login"):
            return client.fetch_json(url)
        return client.fetch_html(url)

    def _fetch_detail(self, client: PlatformClient, code: str) -> str:
        url = self._detail_url(code)
        if self.cfg["mode"] == "api_open":
            return client.fetch_json(url)
        return client.fetch_html(url)

    # ---------- 主流程 ----------
    def run(self, mode: str) -> int:
        # 恢复未完成任务；否则新建
        resume = self.db.find_resumable_job(self.code)
        if resume and not self.stop.is_set():
            job_id = resume["job_id"]
            mode = resume["mode"]
            self.log(f"[{self.code}] 恢复未完成任务 job={job_id}({mode})，"
                     f"列表页={resume['list_page']} 详情={resume['detail_done']}")
        else:
            job_id = self.db.create_job(self.code, mode)
            self.log(f"[{self.code}] 新建采集任务 job={job_id}({mode})")
        self.db.add_log(self.code, job_id, "info", f"任务开始({mode})")

        try:
            with PlatformClient(self.cfg, log=self.log) as client:
                if self.cfg.get("auth"):
                    client.login()
                summaries = self._list_phase(client, job_id)
                if self.stop.is_set():
                    self.db.add_log(self.code, job_id, "warning", "收到停止信号，任务挂起")
                    return job_id
                self._detail_phase(client, job_id, mode, summaries)
            if self.stop.is_set():
                self.db.add_log(self.code, job_id, "warning", "收到停止信号，任务挂起")
                return job_id
            self.db.finish_job(job_id, "done")
            self.db.add_log(self.code, job_id, "info", "任务完成")
            return job_id
        except PlatformError as e:
            self.db.finish_job(job_id, "failed")
            self.db.add_alert(self.code, job_id, "platform_down", "critical",
                              f"平台不可用: {e}")
            self.db.add_log(self.code, job_id, "error", f"平台故障: {e}")
            self.log(f"[{self.code}] 平台故障，已隔离: {e}")
            return job_id
        except Exception as e:
            self.db.finish_job(job_id, "failed")
            self.db.add_alert(self.code, job_id, "config", "critical",
                              f"任务异常终止: {e}",
                              traceback.format_exc()[:500])
            self.db.add_log(self.code, job_id, "error", f"异常: {e}")
            raise

    def _heartbeat(self, job_id: int) -> None:
        from .db import now_iso
        self.db.save_checkpoint(job_id, heartbeat_at=now_iso())

    def _list_phase(self, client: PlatformClient, job_id: int) -> list[dict]:
        cp = self.db.get_checkpoint(job_id)
        # list_page 存“已完成的最后一页”，续跑从下一页开始；全部完成则不重抓
        if cp["list_done"]:
            return (cp["payload"] or {}).get("summaries", [])
        start_page = cp["list_page"] + 1
        payload = cp["payload"] or {"summaries": []}
        page = start_page
        while not self.stop.is_set():
            self._heartbeat(job_id)
            text = self._fetch_list(client, page)
            try:
                rows, cur_page, pages = self._parse_list(text)
            except ParseError as e:
                self.db.add_alert(self.code, job_id, "parse", "critical",
                                  f"列表第{page}页解析失败(页面可能改版): {e}",
                                  e.excerpt)
                self.db.add_log(self.code, job_id, "error",
                                f"列表第{page}页解析失败，已告警并中止本页")
                raise PlatformError(f"列表结构无法解析，已告警: {e}")
            payload["summaries"].extend(rows)
            self.db.add_log(self.code, job_id, "info",
                            f"列表第{page}/{pages}页，{len(rows)}条")
            if page >= pages:
                # 先落全部摘要再标记完成，保证断点与数据一致
                self.db.save_checkpoint(job_id, list_page=page, list_done=1,
                                        payload=payload)
                break
            self.db.save_checkpoint(job_id, list_page=page, payload=payload)
            page += 1
        return payload["summaries"]

    def _detail_phase(self, client: PlatformClient, job_id: int, mode: str,
                      summaries: list[dict]) -> None:
        cp = self.db.get_checkpoint(job_id)
        watermark = self.db.get_watermark(self.code)
        wm_ts = watermark["last_source_updated_at"] if watermark else None

        # 增量候选：新条目 或 updated_at 高于水位
        candidates: list[dict] = []
        for s in summaries:
            if mode == "full":
                candidates.append(s)
                continue
            existing = self.db.get_raw(self.code, s["item_code"])
            if existing is None:
                candidates.append(s)
            elif s.get("source_updated_at") and wm_ts and \
                    s["source_updated_at"] > wm_ts:
                candidates.append(s)
        self.db.save_checkpoint(job_id, detail_total=len(candidates),
                                detail_cursor=cp["detail_cursor"])
        self.db.add_log(self.code, job_id, "info",
                        f"详情候选 {len(candidates)} 条（模式={mode}"
                        f"，水位={wm_ts or '无'}）")

        done_codes = set((cp["payload"] or {}).get("detail_done_codes", []))
        max_ts = wm_ts
        for idx, summary in enumerate(candidates):
            code = summary["item_code"]
            if code in done_codes:
                # 重启续跑：该条目在被杀前已处理完，直接跳过（不漏不重）
                continue
            if self.stop.is_set():
                return
            self._heartbeat(job_id)
            try:
                text = self._fetch_detail(client, code)
                item = self._parse_detail(text)
            except AuthExpired as e:
                self.db.add_alert(self.code, job_id, "login", "warning",
                                  f"登录态失效且重登失败: {e}")
                raise PlatformError(str(e))
            except ParseError as e:
                # 单条解析失败：告警+跳过，不写空数据，不影响其他条目
                self.db.add_alert(self.code, job_id, "parse", "critical",
                                  f"详情 {code} 解析失败(页面可能改版): {e}",
                                  e.excerpt)
                self.db.add_log(self.code, job_id, "error",
                                f"详情 {code} 解析失败，已跳过并告警")
                self._advance_detail(job_id, cp, code, idx)
                continue
            item["platform_code"] = self.code
            self._persist_item(job_id, item)
            if item.get("source_updated_at"):
                if max_ts is None or item["source_updated_at"] > max_ts:
                    max_ts = item["source_updated_at"]
            self._advance_detail(job_id, cp, code, idx)

        if not self.stop.is_set() and max_ts:
            self.db.update_watermark(self.code, max_ts, job_id)
            self.db.add_log(self.code, job_id, "info", f"水位推进至 {max_ts}")

    def _advance_detail(self, job_id: int, cp: dict, code: str, idx: int) -> None:
        payload = cp["payload"] or {}
        done = set(payload.get("detail_done_codes", []))
        done.add(code)
        payload["detail_done_codes"] = sorted(done)
        self.db.save_checkpoint(job_id, detail_done=idx + 1,
                                detail_cursor=code, payload=payload)

    def _persist_item(self, job_id: int, item: dict) -> None:
        changed, old = self.db.upsert_raw(item)
        # 归一映射
        norm = normalize_item(item["generic_name"], item["dosage_form"],
                              item["spec_text"], item["manufacturer"])
        existing_map = self.db.get_mapping(self.code, item["item_code"])
        drug_id = None
        if norm["confidence"] >= 0.9:
            drug_id = self.db.get_or_create_drug(norm)
            if not existing_map or existing_map["status"] == "pending":
                self.db.upsert_mapping(
                    self.code, item["item_code"], drug_id, "auto",
                    norm["confidence"], norm["reason"])
        else:
            if not existing_map:
                self.db.upsert_mapping(
                    self.code, item["item_code"], None, "pending",
                    norm["confidence"], norm["reason"])
                self.db.add_alert(
                    self.code, job_id, "parse", "warning",
                    f"药品 {item['item_code']} 规格无法可靠归一，转人工确认: "
                    f"{norm['reason']}")

        # 变更历史（幂等：唯一键防重；撤网单独记 delist）
        if old is None:
            self.db.add_log(self.code, job_id, "info",
                            f"新挂网 {item['item_code']} 价格={item['price']}")
            return
        if old["status"] == "listed" and item["status"] == "delisted":
            inserted = self.db.record_change(
                self.code, item["item_code"], drug_id, "delist",
                old["price"], None, item["source_updated_at"])
            if inserted:
                self.db.add_log(self.code, job_id, "warning",
                                f"撤网 {item['item_code']} 原价={old['price']}")
        elif old["price"] != item["price"] and item["status"] == "listed":
            inserted = self.db.record_change(
                self.code, item["item_code"], drug_id, "price",
                old["price"], item["price"], item["source_updated_at"])
            if inserted:
                delta = round(item["price"] - old["price"], 2)
                self.db.add_log(self.code, job_id, "info",
                                f"调价 {item['item_code']} "
                                f"{old['price']}->{item['price']} ({delta:+})")
        elif old["status"] == "delisted" and item["status"] == "listed":
            self.db.record_change(
                self.code, item["item_code"], drug_id, "relist",
                old["price"], item["price"], item["source_updated_at"])


class Scheduler:
    """多平台并发调度：线程池，每平台独立 Playwright，互不影响。"""

    def __init__(self, db: DB, log=print):
        self.db = db
        self.log = log
        self.stop = StopFileEvent()

    def run_all(self, mode: str, platforms: list[dict] | None = None,
                max_workers: int = 4) -> dict:
        platforms = platforms or self.db.list_platforms(only_enabled=True)
        results: dict[str, str] = {}
        threads: list[threading.Thread] = []

        def worker(cfg: dict):
            try:
                runner = PlatformRunner(self.db, cfg, self.stop, self.log)
                runner.run(mode)
                results[cfg["code"]] = "finished"
            except Exception as e:  # 兜底：单平台异常不拖垮调度
                results[cfg["code"]] = f"error: {e}"
                self.log(f"[{cfg['code']}] 调度异常: {e}")

        for cfg in platforms:
            t = threading.Thread(target=worker, args=(cfg,), daemon=True)
            threads.append(t)
            t.start()
        for t in threads:
            t.join()
        return results

    def request_stop(self) -> None:
        self.stop.set()
