#!/usr/bin/env python3
"""Manga Reader 2.0

零第三方依赖的本地漫画阅读器：Python 标准库 + 浏览器原生 HTML/CSS/JS。
图片展示在本地完成；只有用户主动触发时，才把图片发送给兼容 OpenAI 的视觉模型。
"""

from __future__ import annotations

import argparse
import base64
import collections
import copy
import errno
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import ProxyHandler, Request, build_opener
import webbrowser

APP_VERSION = "2.0.0"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
DEFAULT_BASE_URL = "http://127.0.0.1:1234"
DEFAULT_MODEL = "qwen/qwen3.8-27b"
MAX_RETRIES = 2
REQUEST_TIMEOUT = 900      # 单页请求上限；Extra High 思考下单页可达 3 分钟
DEFAULT_MAX_TOKENS = 16384  # 只是上限，模型写完就停，调大不浪费
LOCK_NAME = ".manga_reader.lock"   # 单实例标记：两个进程写同一份 data.json 会互相覆盖
MAX_GLOSSARY_TERMS = 120           # 术语表注入上限：再多就吃掉正文预算，收益也趋零

# 页面与接口统一加的防御性响应头：nosniff 挡住把 JSON 当脚本执行，
# frame-ancestors/X-Frame-Options 挡住被别的站点套进 iframe 里点按钮。
SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
)


SYSTEM_PROMPT = r"""你是专业的日语漫画 OCR 与翻译引擎。请分析这一页漫画，按正确阅读顺序提取文字并翻译成自然的简体中文。

版面硬规则：标准四格像两条竖排清单，先把右列读到底，再读左列；普通漫画按横向分镜带读。顺序由分镜边框决定，不能根据台词连贯性猜测。

规则：
1. 先看分镜边框，再提取文字；不能根据台词连贯性猜顺序。
   标准四格：页面左右各一列，每列自上而下有 4 个同宽、上下对齐的分镜，形成 2×4 网格。顺序固定为：右上 → 右2 → 右3 → 右下 → 左上 → 左2 → 左3 → 左下。禁止右上→左上、右2→左2这样的横排交替。左列的标题、章节号、角色介绍框也必须等右列全部读完后才出现。
   特殊四格：上方有一个横跨整页的单独分镜，下方为左右规则双列（每列通常 2～3 格）。顺序：上方整宽分镜 → 下方右列全部 → 下方左列全部。
   其他版式都按普通漫画处理，包括只有部分高度分列、左右格宽度或高度不一致、上下格不对齐、三列、跨列大图、嵌入小格或散落分镜：以横向分镜带为单位从上到下；同一横带内有多格时先右后左，读完整条横带再进入下一条。不要仅因页面某处出现两列就先读完一整列。单列则自上而下。
   每个分镜内部再按先上后下、先右后左排列。index 必须严格按以上顺序从 1 连续编号，items 数组顺序与 index 一致。
2. 每个独立气泡或独立旁白为一条，合并同一气泡内因排版产生的换行，不要合并不同气泡。
3. 结合整页人物、场景、语气和上下文翻译，不逐句孤立翻译，不添加解释或注音。
4. 拟声词应自然意译；无实际文字可提取时返回空数组。
5. 只翻译画面上真实存在的文字。看不清、被遮挡、被裁切或无法确认的字，保留原样或用 □ 占位，绝不根据剧情推测补全，绝不虚构台词。
6. 原文的语气符号（……、！？、～）和说话被打断的省略，在译文中尽量保留，不要擅自补成完整句子。
7. 同一人物在不同页面的称呼必须一致。若下方提供了「术语表」或「前情」，严格沿用其中的译名，不得另起译法。
8. type 只能是 bubble（对话气泡）、narration（旁白或叙述框）、sfx（拟声词）、sign（画面内的印刷文字：信封、招牌、告示、屏幕、书本、票据等）。
9. 水印、版权信息、页码、扫描来源标记不要作为条目输出。
10. index 从 1 开始连续编号，与 items 数组顺序一致。
11. original 和 translation 中均不得包含换行。
12. 额外输出 entities 数组，列出本页出现的专有名词（人名、地名、组织名、作品名）：
    source 用画面原文里的写法，target 用你在这页译文里实际使用的译名，kind 取 person / place / org / work。
    只列专有名词，普通名词、职务和称谓后缀不算。本页没有专有名词时输出空数组。
13. 输出前必须自检 index 顺序：等宽双列时，所有右列条目必须连续排在左列之前，不能出现右1→左1→右2；普通横带时，同一横带必须是右格→左格，再进入下一横带。发现违反就重排后再输出。
14. 只输出如下结构的 JSON 对象，不要输出 Markdown、代码块或任何额外内容：
   {"items":[{"index":1,"original":"提取的原文","translation":"简体中文译文","type":"bubble"}],"entities":[{"source":"原文写法","target":"中文译名","kind":"person"}]}"""

# 与提示词规则一一对应的结构化输出约束。
# 端点不支持时 response_variants() 会依次降级到 json_object，最后回退到纯提示词。
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "original": {"type": "string"},
                    "translation": {"type": "string"},
                    "type": {"type": "string", "enum": ["bubble", "narration", "sfx", "sign"]},
                },
                "required": ["index", "original", "translation", "type"],
                "additionalProperties": False,
            },
        },
        # 顺带抽取专有名词，交给 DataStore 累积成自动术语表
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "target": {"type": "string"},
                    "kind": {"type": "string", "enum": ["person", "place", "org", "work"]},
                },
                "required": ["source", "target", "kind"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items", "entities"],
    "additionalProperties": False,
}

class OutputTruncated(ValueError):
    """输出长度撞上 max_tokens：思考内容与正文共用同一额度时最容易发生。"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if value.endswith("/chat/completions"):
        value = value[: -len("/chat/completions")].rstrip("/")
    if not value.startswith(("http://", "https://")):
        raise ValueError("模型地址必须以 http:// 或 https:// 开头")
    return value


def api_endpoint(base_url: str, resource: str) -> str:
    """同时接受服务根地址、/v1 地址和完整 chat/completions 地址。"""
    base = clean_base_url(base_url)
    if re.search(r"/v\d+[a-z]*$", base):
        return f"{base}/{resource}"
    return f"{base}/v1/{resource}"


def to_bool(value: Any, fallback: bool) -> bool:
    """只认真正的布尔与常见字符串写法。bool("false") 是 True，
    直接强转会把用户的开关反过来存，属于难查的配置类 bug。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off", ""}:
            return False
    return fallback


def is_local_host(host: str) -> bool:
    host = (host or "").lower()
    if host in ("localhost", "0.0.0.0", "::1") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


_DIRECT_OPENER = build_opener(ProxyHandler({}))
_DEFAULT_OPENER = build_opener()


def open_request(request: Request, timeout: float):
    """局域网模型服务强制直连：系统代理（Clash/Surge 等）会把 192.168.x.x 也拦掉，
    表现就是莫名其妙的 403。公网地址仍走系统代理。"""
    try:
        host = urlparse(request.full_url).hostname or ""
    except ValueError:
        host = ""
    opener = _DIRECT_OPENER if is_local_host(host) else _DEFAULT_OPENER
    return opener.open(request, timeout=timeout)


def default_llm_settings(base_url: str, model: str) -> dict[str, Any]:
    return {
        "base_url": clean_base_url(base_url),
        "model": model,
        "api_key": "",
        "timeout": REQUEST_TIMEOUT,
        "max_tokens": DEFAULT_MAX_TOKENS,
        "temperature": 0.2,
        "structured": True,
        "thinking": True,
        "auto_glossary": True,
    }


def strip_thinking(text: str) -> str:
    text = re.sub(r"<think(?:ing)?>[\s\S]*?</think(?:ing)?>", "", text, flags=re.I)
    text = re.sub(r"<think(?:ing)?>[\s\S]*$", "", text, flags=re.I)
    return text.strip()


def extract_json(text: str) -> dict[str, Any]:
    """接受纯 JSON、Markdown 代码块或夹杂少量说明的模型输出。"""
    text = strip_thinking(text).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        for attempt in (candidate, re.sub(r",\s*([}\]])", r"\1", candidate)):
            try:
                value = json.loads(attempt)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    raise ValueError("模型没有返回可解析的 JSON 对象")


def normalize_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items = payload.get("items", payload.get("llm_results"))
    if not isinstance(raw_items, list):
        raise ValueError("JSON 中缺少 items 数组")
    results = []
    for position, item in enumerate(raw_items, 1):
        if not isinstance(item, dict):
            continue
        original = str(item.get("original", item.get("original_jp_texts", ""))).strip()
        translation = str(item.get("translation", item.get("translated_cn_texts", ""))).strip()
        if not original and not translation:
            continue
        item_type = str(item.get("type", "sign"))
        legacy_types = {
            "sentence inside bubbles": "bubble",
            "sentence outside bubbles": "narration",
            "non-vertical text": "sfx",
            "other vertical text": "other",
            "other": "sign",  # 旧缓存里的“其他”基本都是信封、招牌这类画面文字
        }
        item_type = legacy_types.get(item_type, item_type)
        if item_type not in {"bubble", "narration", "sfx", "sign", "other"}:
            item_type = "sign"
        results.append({
            "index": position,
            "original": " ".join(original.splitlines()),
            "translation": " ".join(translation.splitlines()),
            "type": item_type,
        })
    return results


ENTITY_KINDS = {"person", "place", "org", "work"}


def normalize_entities(payload: dict[str, Any]) -> list[dict[str, str]]:
    """清洗模型给出的专有名词，丢掉空值、超长值和非法 kind。"""
    raw = payload.get("entities")
    if not isinstance(raw, list):
        return []
    entities = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source", "")).strip()
        target = str(item.get("target", "")).strip()
        kind = str(item.get("kind", "")).strip()
        if not source or not target:
            continue
        if len(source) > 40 or len(target) > 40:
            continue
        if source in seen:
            continue
        seen.add(source)
        entities.append({
            "source": source,
            "target": target,
            "kind": kind if kind in ENTITY_KINDS else "person",
        })
    return entities


def parse_glossary_pairs(raw: str) -> list[tuple[str, str]]:
    """把「原文=译文」多行文本解析成键值对。"""
    pairs = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for separator in ("=>", "⇒", "=", "＝"):
            if separator in line:
                key, value = line.split(separator, 1)
                key, value = key.strip(), value.strip()
                if key and value:
                    pairs.append((key, value))
                break
    return pairs


def build_glossary_block_from_pairs(
    manual: list[tuple[str, str]], auto: list[tuple[str, str]], limit: int = MAX_GLOSSARY_TERMS
) -> str:
    """手写条目优先占用额度，再加自动累积的，总数封顶 limit——否则术语越攒越多，
    整块提示词会把正文预算挤掉，翻译反而变差。"""
    manual = manual[:limit]
    auto = auto[: max(0, limit - len(manual))]
    blocks = []
    if manual:
        blocks.append(
            "## 术语表（必须严格使用以下译名，不得意译或改写）\n"
            + "\n".join(f"- {key} ⇒ {value}" for key, value in manual)
        )
    if auto:
        blocks.append(
            "## 已确认译名（此前页面累积，必须沿用，不要另起译法）\n"
            + "\n".join(f"- {key} ⇒ {value}" for key, value in auto)
        )
    return ("\n\n" + "\n\n".join(blocks)) if blocks else ""


def merge_settings(patch: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """把请求里的字段合并进配置并做完整校验，返回一份全新的配置字典。

    设置面板保存和“测试连接”走同一套校验：否则测试连接会拿着没校验的
    timeout 去掉 int()，报出来的 500 和真正的连接问题混在一起，很难判断。
    缺省与 null 都表示“不修改”，避免 str(None) 把字符串 "None" 存进配置。
    """

    def pick(key: str, fallback: Any) -> Any:
        value = patch.get(key)
        return fallback if value is None else value

    base_url = clean_base_url(str(pick("base_url", current.get("base_url", DEFAULT_BASE_URL))))
    model = str(pick("model", current.get("model", DEFAULT_MODEL))).strip()
    if not model:
        raise ValueError("模型名称不能为空")
    try:
        timeout = int(pick("timeout", current.get("timeout", REQUEST_TIMEOUT)))
        max_tokens = int(pick("max_tokens", current.get("max_tokens", DEFAULT_MAX_TOKENS)))
        temperature = float(pick("temperature", current.get("temperature", 0.2)))
    except (TypeError, ValueError) as exc:
        raise ValueError("超时和 tokens 必须是整数，温度必须是数字") from exc
    if not 10 <= timeout <= 3600:
        raise ValueError("超时时间应在 10～3600 秒之间")
    if not 256 <= max_tokens <= 65536:
        raise ValueError("最大输出 tokens 应在 256～65536 之间")
    if not 0 <= temperature <= 2:
        raise ValueError("温度应在 0～2 之间")
    api_key = str(pick("api_key", current.get("api_key", ""))).strip()
    if len(api_key) > 4096:                      # 别让一段上千字符的串把缓存文件撑爆
        raise ValueError("API Key 过长")
    return {
        "base_url": base_url,
        "model": model,
        "api_key": api_key,
        "timeout": timeout,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "structured": to_bool(pick("structured", None), current.get("structured", True)),
        "thinking": to_bool(pick("thinking", None), current.get("thinking", True)),
        "auto_glossary": to_bool(pick("auto_glossary", None), current.get("auto_glossary", True)),
    }


class DataStore:
    """版本化 JSON 缓存，读取时自动迁移 v1，写入采用原子替换。"""

    def __init__(self, path: Path, base_url: str, model: str):
        self.path = path
        self.lock = threading.RLock()
        self.data = self._load(base_url, model)

    def _load(self, base_url: str, model: str) -> dict[str, Any]:
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    old = json.load(handle)
                if isinstance(old, dict) and old.get("version") == 2:
                    defaults = default_llm_settings(base_url, model)
                    # 手改过的缓存可能把这些字段写成别的类型，先钉回字典，
                    # 否则后面每一处 .get 都会抛 AttributeError，只会在请求里炸出来
                    if not isinstance(old.get("llm"), dict):
                        old["llm"] = {}
                    if not isinstance(old.get("pages"), dict):
                        old["pages"] = {}
                    if not isinstance(old.get("entities"), dict):
                        old["entities"] = {}
                    for key, value in defaults.items():
                        old["llm"].setdefault(key, value)
                    book = old["entities"]
                    for record in book.values():
                        if isinstance(record, dict):
                            record.setdefault("manual", False)
                    legacy = str(old["llm"].get("glossary") or "").strip()
                    if legacy:
                        for key, value in parse_glossary_pairs(legacy):
                            record = book.setdefault(key, {})
                            record.update({
                                "target": value,
                                "kind": str(record.get("kind") or "person"),
                                "manual": True,
                                "count": int(record.get("count", 0)),
                                "updated_at": utc_now(),
                            })
                        old["llm"]["glossary"] = ""
                    return old
                if isinstance(old, dict) and isinstance(old.get("images"), dict):
                    pages = {}
                    for name, entry in old["images"].items():
                        if isinstance(entry, dict) and "llm_results" in entry:
                            pages[name] = {
                                "results": normalize_results({"items": entry["llm_results"]}),
                                "model": "migrated-v1",
                                "updated_at": "",
                            }
                    return {
                        "version": 2,
                        "llm": default_llm_settings(base_url, model),
                        "pages": pages,
                        "entities": {},
                    }
            except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
                print(f"缓存读取失败，将使用空缓存：{exc}")
                # 别直接丢弃：改名留档，用户可能只是手改坏了一个字符
                broken = self.path.with_name(self.path.name + ".broken")
                try:
                    self.path.replace(broken)
                    print(f"  原文件已另存为 {broken}，修好后改回原名即可恢复")
                except OSError:
                    pass
        return {
            "version": 2,
            "llm": default_llm_settings(base_url, model),
            "pages": {},
            "entities": {},
        }

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return copy.deepcopy(self.data)

    def page(self, name: str) -> dict[str, Any]:
        with self.lock:
            entry = self.data["pages"].get(name, {})
            return copy.deepcopy(entry) if isinstance(entry, dict) else {}

    def has_results(self, name: str) -> bool:
        with self.lock:
            return "results" in (self.data["pages"].get(name) or {})

    def page_statuses(self, names: list[str]) -> list[dict[str, Any]]:
        """批量取状态字段，不复制整页译文——轮询接口每秒都在调它，只加一次锁。"""
        with self.lock:
            pages = self.data["pages"]
            statuses = []
            for name in names:
                entry = pages.get(name) or {}
                if not isinstance(entry, dict):
                    entry = {}
                statuses.append({
                    "translated": "results" in entry,
                    "count": len(entry.get("results") or []),
                    "error": str(entry.get("error", "")),
                })
            return statuses

    # ---------- 术语表：手动与自动共用同一张键值表 ----------
    def entity_list(self) -> list[dict[str, Any]]:
        """排序：手动在前 → 出现次数降序 → 同次数内最近更新的在前。"""
        with self.lock:
            rows = []
            for source, record in (self.data.get("entities") or {}).items():
                if not isinstance(record, dict):
                    continue          # 手改坏的行直接忽略，别让整个面板 500
                rows.append({
                    "source": source,
                    "target": str(record.get("target", "")),
                    "kind": str(record.get("kind", "")),
                    "manual": bool(record.get("manual")),
                    "count": int(record.get("count", 0)),
                    "updated_at": str(record.get("updated_at", "")),
                })
            # 稳定排序两趟：先按时间倒序打底，再按主次排序，时间次序在同等档内保留
            rows.sort(key=lambda row: row["updated_at"], reverse=True)
            rows.sort(key=lambda row: (not row["manual"], -row["count"]))
            return rows

    def entity_count(self) -> int:
        with self.lock:
            return len(self.data.get("entities") or {})

    def upsert_entity(self, source: str, target: str, kind: str = "") -> dict[str, Any]:
        """新增或修改一条术语。写成 manual=True，此后自动累积不再覆盖它。"""
        source, target = (source or "").strip(), (target or "").strip()
        if not source or not target:
            raise ValueError("原文和译文都不能为空")
        if len(source) > 40 or len(target) > 40:
            raise ValueError("原文和译文都不能超过 40 个字符")
        with self.lock:
            book = self.data.setdefault("entities", {})
            record = book.get(source) or {}
            record.update({
                "target": target,
                "kind": kind if kind in ENTITY_KINDS else str(record.get("kind") or "person"),
                "manual": True,
                "count": int(record.get("count", 0)),
                "updated_at": utc_now(),
            })
            book[source] = record
            self._save_locked()
            return {"source": source, "target": target, "kind": record["kind"], "manual": True}

    def delete_entity(self, source: str) -> bool:
        with self.lock:
            book = self.data.setdefault("entities", {})
            if source not in book:
                return False
            book.pop(source)
            self._save_locked()
            return True

    def clear_entities(self) -> int:
        """只清自动累积的条目，用户手写的保留。"""
        with self.lock:
            book = self.data.get("entities") or {}
            kept = {
                key: value for key, value in book.items()
                if isinstance(value, dict) and value.get("manual")
            }
            removed = len(book) - len(kept)
            self.data["entities"] = kept
            self._save_locked()
            return removed

    def merge_entities(self, entities: list[dict[str, str]]) -> int:
        """自动累积。同一条目首次写入即锁定译名，只有用户能改，避免来回横跳。"""
        if not entities:
            return 0
        with self.lock:
            if not self.data["llm"].get("auto_glossary", True):
                return 0
            book = self.data.setdefault("entities", {})
            added = 0
            for entry in entities:
                source = entry["source"]
                record = book.get(source)
                if isinstance(record, dict):
                    record["count"] = int(record.get("count", 1)) + 1
                    record["updated_at"] = utc_now()
                    continue
                book[source] = {
                    "target": entry["target"],
                    "kind": entry.get("kind", ""),
                    "manual": False,
                    "count": 1,
                    "updated_at": utc_now(),
                }
                added += 1
            self._save_locked()
            return added

    def glossary_block(self) -> str:
        """手写条目进“必须严格使用”段，自动累积的进“已确认译名”段。
        顺序沿用面板排序（手动优先 → 出现次数多的优先），额度才落在高频词上；
        早前按字典插入顺序截断，术语一多就会出现“新角色进不去”的怪事。"""
        rows = [row for row in self.entity_list() if row["target"].strip()]
        manual = [(row["source"], row["target"]) for row in rows if row["manual"]]
        auto = [(row["source"], row["target"]) for row in rows if not row["manual"]]
        return build_glossary_block_from_pairs(manual, auto)

    def settings(self) -> dict[str, Any]:
        with self.lock:
            return copy.deepcopy(self.data["llm"])

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.data["llm"] = merge_settings(patch, self.data["llm"])
            self._save_locked()
            return copy.deepcopy(self.data["llm"])

    def save_result(self, name: str, results: list[dict[str, Any]], model: str) -> None:
        with self.lock:
            self.data["pages"][name] = {
                "results": results,
                "model": model,
                "updated_at": utc_now(),
            }
            self._save_locked()

    def save_error(self, name: str, message: str) -> None:
        with self.lock:
            entry = self.data["pages"].setdefault(name, {})
            entry["error"] = message[:1000]
            entry["failed_at"] = utc_now()
            self._save_locked()

    def clear_error(self, name: str) -> bool:
        """只清失败标记，保留已有译文——删除"失败记录"不该连带丢掉翻好的结果。"""
        with self.lock:
            entry = self.data["pages"].get(name)
            if not entry or "error" not in entry:
                return False
            entry.pop("error", None)
            entry.pop("failed_at", None)
            if not entry:
                self.data["pages"].pop(name, None)
            self._save_locked()
            return True

    def clear_page(self, name: str) -> None:
        with self.lock:
            self.data["pages"].pop(name, None)
            self._save_locked()

    def clear_errors(self, names: list[str]) -> int:
        """批量清失败标记，只在最后落盘一次。逐条调 clear_error 会触发 N 次 fsync。"""
        with self.lock:
            pages = self.data["pages"]
            cleaned = 0
            for name in names:
                entry = pages.get(name)
                if not entry or "error" not in entry:
                    continue
                entry.pop("error", None)
                entry.pop("failed_at", None)
                if not entry:
                    pages.pop(name, None)
                cleaned += 1
            if cleaned:
                self._save_locked()
            return cleaned

    def _save_locked(self) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(self.data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)


class LLMClient:
    def __init__(self, store: DataStore):
        self.store = store

    @staticmethod
    def headers(api_key: str = "") -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if api_key.strip():
            headers["Authorization"] = "Bearer " + api_key.strip()
        return headers

    @staticmethod
    def response_content(body: dict[str, Any]) -> str:
        if body.get("error"):
            raise ValueError(str(body["error"]))
        message = body["choices"][0]["message"]
        content = message.get("content") or ""
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") for part in content
                if isinstance(part, dict) and part.get("type") in {"text", "output_text"}
            )
        if not content.strip() and message.get("reasoning_content"):
            raise ValueError("模型只返回了思考内容，未返回最终 JSON")
        return content

    def list_models(self, base_url: str | None = None, api_key: str | None = None) -> list[str]:
        settings = self.store.settings()
        base = base_url or settings["base_url"]
        key = settings.get("api_key", "") if api_key is None else api_key
        request = Request(api_endpoint(base, "models"), headers=self.headers(key))
        try:
            with open_request(request, 20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            names = [str(item["id"]) for item in payload.get("data", []) if isinstance(item, dict) and item.get("id")]
            # 嵌入 / 重排模型不能对话看图，列出来只会让人选错
            return [name for name in names if "embed" not in name.lower() and "rerank" not in name.lower()]
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError, AttributeError) as exc:
            raise RuntimeError(f"无法读取模型列表：{exc}") from exc

    def test_connection(self, settings: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "model": settings["model"],
            "messages": [{"role": "user", "content": "只回复 ok，不要输出其他内容。"}],
            "max_tokens": 32,
            "temperature": 0,
            "stream": False,
        }
        request = Request(
            api_endpoint(settings["base_url"], "chat/completions"),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self.headers(str(settings.get("api_key", ""))),
            method="POST",
        )
        started = time.monotonic()
        try:
            with open_request(request, min(int(settings.get("timeout", 60)), 60)) as response:
                body = json.loads(response.read().decode("utf-8"))
            content = self.response_content(body)
            return {"ok": True, "latency": round(time.monotonic() - started, 2), "reply": content[:80]}
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"HTTP {exc.code}：{detail}") from exc
        except (URLError, TimeoutError, OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(str(exc)) from exc

    @staticmethod
    def _image_url(path: Path) -> str:
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    @staticmethod
    def response_variants(settings: dict[str, Any]) -> list[Any]:
        """按能力从强到弱排列的输出格式：json_schema → json_object → 纯提示词。"""
        variants: list[Any] = []
        if settings.get("structured", True):
            variants.append({
                "type": "json_schema",
                "json_schema": {"name": "manga_page", "strict": True, "schema": RESPONSE_SCHEMA},
            })
            variants.append({"type": "json_object"})
        variants.append(None)
        return variants

    def translate(self, path: Path, context: str = "") -> tuple[list[dict[str, Any]], str, list[dict[str, str]]]:
        settings = self.store.settings()
        system_text = SYSTEM_PROMPT + self.store.glossary_block()
        user_text = "请识别并翻译这一页漫画，只输出提示词要求的 JSON 对象。"
        if context.strip():
            user_text += (
                "\n\n## 前情（此前页面已完成的翻译，仅用于理解人物关系、称呼与语气；"
                "不要重复输出这些内容，不要沿用其中的 index）\n" + context.strip()
            )
        base_payload = {
            "model": settings["model"],
            "messages": [
                {"role": "system", "content": system_text},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {"type": "image_url", "image_url": {"url": self._image_url(path)}},
                    ],
                },
            ],
            "temperature": settings.get("temperature", 0.2),
            "max_tokens": settings.get("max_tokens", DEFAULT_MAX_TOKENS),
            "stream": False,
        }
        url = api_endpoint(settings["base_url"], "chat/completions")
        headers = self.headers(str(settings.get("api_key", "")))
        timeout = int(settings.get("timeout", REQUEST_TIMEOUT))
        # 关闭思考：LM Studio / vLLM 认 reasoning_effort，个别端点会报未知字段，
        # 一旦报错就永久丢弃该参数再试，不影响翻译本身。
        quiet = not settings.get("thinking", True)
        allow_quiet_param = quiet
        last_error: Exception | None = None
        fatal = False
        expanded = False

        for attempt in range(MAX_RETRIES + 1):
            for variant in self.response_variants(settings):
                payload = dict(base_payload)
                if variant is not None:
                    payload["response_format"] = variant
                if quiet and allow_quiet_param:
                    payload["reasoning_effort"] = "none"
                request = Request(
                    url,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                try:
                    with open_request(request, timeout) as response:
                        body = json.loads(response.read().decode("utf-8"))
                    choice = (body.get("choices") or [{}])[0]
                    finish = choice.get("finish_reason")
                    try:
                        content = self.response_content(body)
                        parsed = extract_json(content)
                        return (
                            normalize_results(parsed),
                            settings["model"],
                            normalize_entities(parsed),
                        )
                    except ValueError as exc:
                        if finish == "length":
                            raise OutputTruncated(
                                f"输出被截断（finish_reason=length，max_tokens={base_payload.get('max_tokens')}）。"
                                "思考内容与 JSON 共用同一额度：请调大「最大输出 tokens」，或关闭「启用模型思考」"
                            ) from exc
                        raise
                except OutputTruncated as exc:
                    last_error = exc
                    # 思考强度调高时正文常被挤出预算，先自动翻倍重试一次再报错
                    if not expanded and int(base_payload.get("max_tokens", DEFAULT_MAX_TOKENS)) < 32768:
                        expanded = True
                        base_payload["max_tokens"] = min(int(base_payload.get("max_tokens", DEFAULT_MAX_TOKENS)) * 2, 32768)
                        break
                    fatal = True
                    break
                except HTTPError as exc:
                    detail = exc.read().decode("utf-8", errors="replace")[:1000]
                    last_error = ValueError(f"模型服务 HTTP {exc.code}：{detail}")
                    if allow_quiet_param and "reasoning_effort" in detail.lower():
                        allow_quiet_param = False
                        continue
                    # 端点不认这种 response_format，降一级再试
                    if variant is not None and exc.code in {400, 404, 415, 422}:
                        continue
                    break
                except (URLError, TimeoutError, OSError, ValueError, KeyError, IndexError, TypeError) as exc:
                    last_error = exc
                    break
            if fatal:
                break
            if attempt < MAX_RETRIES:
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(str(last_error or "模型调用失败"))


def build_context(store: DataStore, names: list[str], index: int, limit: int = 2) -> str:
    """把前面已翻译页面的原文+译文拼成「前情」，用于保持称呼、指代与语气连贯。
    只取对话与旁白：信封收件人、招牌这类画面文字会被模型误当成角色名，
    反过来污染后续页面的译名（实测「中村実衣子」污染了「みいちゃん」）。"""
    blocks = []
    for prev in range(max(0, index - limit), index):
        entry = store.page(names[prev])
        results = entry.get("results") or []
        lines = [
            f"{str(item.get('original', ''))[:160]} ⇒ {str(item.get('translation', ''))[:160]}"
            for item in results
            if item.get("type") != "sign"
        ]
        if not lines:
            continue
        blocks.append(f"第 {prev + 1} 页：\n" + "\n".join(lines))
    return "\n\n".join(blocks)


class TranslationWorker:
    """串行翻译队列。支持插队（重试优先插到队首）与取消（排队中的直接移除，执行中的丢弃结果）。"""

    def __init__(self, images: list[Path], names: list[str], store: DataStore):
        self.images = images
        self.names = names
        self.store = store
        self.client = LLMClient(store)
        self.lock = threading.RLock()
        self.wakeup = threading.Condition(self.lock)
        self.jobs: collections.deque[int] = collections.deque()
        self.queued: set[int] = set()
        self.current: int | None = None
        self.cancel_current = False
        self.cancelled: set[int] = set()   # 已取消、可在面板里重试的页
        self.cancelled_count = 0
        self.completed = 0
        self.failed = 0
        self.thread = threading.Thread(target=self._run, name="translation-worker", daemon=True)
        self.thread.start()

    def enqueue(self, indexes: list[int], force: bool = False, front: bool = False) -> int:
        """front=True 时插到队首：重试失败页应当优先于后面排着的整批任务。"""
        added = 0
        with self.lock:
            for index in indexes:
                if not 0 <= index < len(self.images) or index == self.current:
                    continue
                if not force and self.store.has_results(self.names[index]):
                    continue
                if index in self.queued:
                    if not front:
                        continue
                    # 已经在队里：插队要把它提到最前面，而不是静默忽略
                    try:
                        self.jobs.remove(index)
                    except ValueError:
                        pass
                    self.jobs.appendleft(index)
                    added += 1
                    continue
                self.cancelled.discard(index)
                try:
                    self.jobs.remove(index)
                except ValueError:
                    pass
                self.queued.add(index)
                if front:
                    self.jobs.appendleft(index)
                else:
                    self.jobs.append(index)
                added += 1
            if added:
                self.wakeup.notify()
        return added

    def cancel(self, index: int) -> str:
        """取消一页。无法中断已发出的 HTTP 请求，所以执行中的只能是"丢弃结果"。"""
        with self.lock:
            if index == self.current:
                self.cancel_current = True
                return "dropping"
            if index in self.queued:
                self.queued.discard(index)
                try:                       # 队列里的陈旧条目也要去掉，否则重试后会重复出现
                    self.jobs.remove(index)
                except ValueError:
                    pass
                self.cancelled.add(index)
                return "canceled"
            if index in self.cancelled:
                return "canceled"
            return "none"

    def cancel_all(self) -> int:
        with self.lock:
            removed = len(self.queued)
            self.cancelled.update(self.queued)
            self.queued.clear()
            self.jobs.clear()
            if self.current is not None and not self.cancel_current:
                self.cancel_current = True
                removed += 1
            return removed

    def dismiss(self, index: int) -> bool:
        """从任务面板移掉一条记录：已取消的出栈、失败的清掉错误标记，都不碰已有译文。"""
        if not 0 <= index < len(self.names) or index == self.current:
            return False
        with self.lock:
            removed = index in self.cancelled
            self.cancelled.discard(index)
        cleared = self.store.clear_error(self.names[index])
        return removed or cleared

    def dismiss_all(self) -> int:
        with self.lock:
            removed = len(self.cancelled)
            self.cancelled.clear()
        return removed + self.store.clear_errors(list(self.names))

    def tasks(self) -> dict[str, Any]:
        with self.lock:
            return {
                "current": None if self.current is None else {
                    "index": self.current,
                    "dropping": self.cancel_current,
                },
                "queued": list(dict.fromkeys(i for i in self.jobs if i in self.queued)),
                "cancelled": sorted(self.cancelled),
                "completed": self.completed,
                "failed": self.failed,
                "cancelled_count": self.cancelled_count,
            }

    def context_for(self, index: int, limit: int = 2) -> str:
        return build_context(self.store, self.names, index, limit)

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "active": self.current is not None,
                "current": self.current,
                "pending": len(self.queued),
                "completed": self.completed,
                "failed": self.failed,
                "cancelled": self.cancelled_count,
            }

    def is_queued(self, index: int) -> bool:
        with self.lock:
            return index in self.queued or index == self.current

    def busy_indexes(self) -> set[int]:
        """一次取全部排队/处理中的页，供状态接口批量查询。"""
        with self.lock:
            busy = set(self.queued)
            if self.current is not None:
                busy.add(self.current)
            return busy

    def _run(self) -> None:
        while True:
            with self.lock:
                while not self.jobs:
                    self.wakeup.wait()
                index = self.jobs.popleft()
                if index not in self.queued:   # 取出前已被取消，跳过
                    continue
                self.queued.discard(index)
                self.current = index
                self.cancel_current = False
            name = self.names[index]
            print(f"[翻译] {index + 1}/{len(self.images)} {name}", flush=True)
            try:
                context = self.context_for(index)
                results, model, entities = self.client.translate(self.images[index], context)
                with self.lock:
                    dropped = self.cancel_current
                    self.cancel_current = False
                    if dropped:
                        self.cancelled_count += 1
                        self.cancelled.add(index)
                if dropped:
                    print(f"[取消] {name}：结果已丢弃", flush=True)
                else:
                    with self.lock:
                        self.completed += 1
                    self.store.save_result(name, results, model)
                    added = self.store.merge_entities(entities)
                    note = f"，新增译名 {added}" if added else ""
                    print(f"[完成] {name}：{len(results)} 条{note}", flush=True)
            except Exception as exc:
                message = str(exc)
                with self.lock:
                    dropped = self.cancel_current
                    self.cancel_current = False
                    if dropped:
                        self.cancelled_count += 1
                        self.cancelled.add(index)
                    else:
                        self.failed += 1
                if dropped:
                    print(f"[取消] {name}：请求返回后已丢弃", flush=True)
                else:
                    self.store.save_error(name, message)
                    print(f"[失败] {name}：{message}", flush=True)
            finally:
                with self.lock:
                    self.current = None


PAGE_HTML = r"""<!doctype html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>漫画阅读器</title>
<style>
:root{color-scheme:dark;--bg:#101114;--panel:#181a1f;--panel2:#202229;--text:#f2f2f0;--muted:#9599a3;--line:#2d3038;--accent:#e7b85c;--accent-text:#18140b;--danger:#ef7d7d;--ok:#75c79a;--shadow:0 16px 48px #0007}
:root[data-theme="light"]{color-scheme:light;--bg:#eceae5;--panel:#f8f7f3;--panel2:#fff;--text:#24231f;--muted:#716f69;--line:#d9d5cc;--accent:#9a6715;--accent-text:#fff;--danger:#b94040;--ok:#247a4c;--shadow:0 16px 48px #40372022}
*{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;overflow:hidden}body{background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}button,input,select{font:inherit}button,select,input{color:var(--text)}button{cursor:pointer}.icon-btn,.tool-btn{border:1px solid var(--line);background:var(--panel);border-radius:8px;min-height:36px;padding:7px 11px;transition:.16s;display:inline-flex;align-items:center;justify-content:center;line-height:1;text-align:center}.icon-btn{width:38px;min-width:38px;padding:0;font-size:17px}.icon-btn svg{width:17px;height:17px;flex:none;display:block}.icon-btn:hover{color:var(--accent)}#translation-btn{font-size:15px}.icon-btn:hover,.tool-btn:hover{border-color:var(--accent);background:var(--panel2)}.header .tool-btn{flex:none;white-space:nowrap}button:disabled{opacity:.4;cursor:not-allowed}.primary{background:var(--accent);color:var(--accent-text);border-color:var(--accent);font-weight:650}.danger{color:var(--danger)}.hidden{display:none!important}
.header{height:58px;display:flex;align-items:center;gap:10px;padding:9px 14px;background:var(--panel);border-bottom:1px solid var(--line);position:relative;z-index:20}.brand{display:flex;align-items:baseline;gap:8px;margin-right:8px;white-space:nowrap;flex:none}.brand strong{font-size:16px;letter-spacing:.5px}.brand small{font-size:10px;color:var(--muted);letter-spacing:1px}.nav-group{display:flex;align-items:center;gap:6px}.page-jump{width:58px;height:36px;text-align:center;border:1px solid var(--line);border-radius:8px;background:var(--bg);outline:none}.page-jump:focus{border-color:var(--accent)}.total{color:var(--muted);white-space:nowrap}.toolbar-spacer{flex:1}.tool-select{height:36px;border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:0 8px;max-width:118px}.workspace{height:calc(100vh - 94px);height:calc(100dvh - 94px);display:grid;grid-template-columns:210px minmax(0,1fr) 370px;position:relative}.workspace.no-sidebar{grid-template-columns:0 minmax(0,1fr) 370px}.workspace.no-translation{grid-template-columns:210px minmax(0,1fr) 0}.workspace.no-sidebar.no-translation{grid-template-columns:0 minmax(0,1fr) 0}
.sidebar{background:var(--panel);border-right:1px solid var(--line);overflow:hidden;min-width:0}.sidebar-inner{width:210px;height:100%;display:flex;flex-direction:column}.thumbnails{overflow:auto;padding:10px 10px 20px;scrollbar-width:thin}.thumb{width:100%;border:2px solid transparent;background:transparent;padding:6px;border-radius:9px;color:var(--muted);text-align:left;margin-bottom:6px;position:relative}.thumb:hover{background:var(--panel2)}.thumb.active{border-color:var(--accent);color:var(--text);background:var(--panel2)}.thumb img{display:block;width:100%;height:112px;object-fit:contain;background:#0b0b0d;border-radius:4px}.thumb-label{display:flex;align-items:center;gap:6px;padding-top:5px;font-size:11px;overflow:hidden}.thumb-label span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.cache-dot{width:6px;height:6px;flex:none;border-radius:50%;background:var(--line)}.cache-dot.done{background:var(--ok)}.cache-dot.busy{background:var(--accent);animation:pulse 1s infinite alternate}@keyframes pulse{to{opacity:.25}}
.stage{position:relative;height:100%;min-width:0;overflow:hidden;background:var(--bg)}.viewer{width:100%;height:100%;min-width:0;overflow:auto;position:relative;scrollbar-width:thin;outline:none}.pages{min-width:100%;min-height:100%;display:flex;align-items:center;justify-content:center;gap:14px;padding:16px 16px 28px}.pages.zoomed{align-items:center;justify-content:center;width:max-content;cursor:grab}.pages.zoomed.dragging{cursor:grabbing}.pages.continuous{display:block;padding:16px 16px 28px;width:100%;cursor:default}.page-frame{position:relative;flex:0 0 auto;min-width:0;text-align:center}.pages.continuous .page-frame{margin:0 auto 20px;max-width:100%}.page-image{display:block;margin:auto;box-shadow:0 3px 20px #0005;user-select:none;-webkit-user-drag:none;background:#fff}.page-number{position:absolute;bottom:7px;left:50%;transform:translateX(-50%);font-size:10px;color:#fff;background:#111b;border-radius:10px;padding:2px 8px;opacity:0;transition:.15s;pointer-events:none}.page-frame:hover .page-number{opacity:1}.edge-button{position:absolute;top:0;bottom:0;width:70px;border:0;background:transparent;z-index:4;opacity:0;color:var(--text);display:flex;align-items:center;transition:opacity .18s,background .18s}.edge-button:hover{opacity:.85}#prev-edge{left:0;justify-content:flex-start;padding-left:10px}#prev-edge:hover{background:linear-gradient(90deg,var(--bg),transparent)}#next-edge{right:0;justify-content:flex-end;padding-right:10px}#next-edge:hover{background:linear-gradient(270deg,var(--bg),transparent)}.edge-button span{display:inline-flex;align-items:center;justify-content:center;width:36px;height:36px;background:var(--panel);border:1px solid var(--line);border-radius:50%;box-shadow:var(--shadow);font-size:19px;line-height:1;padding:0}.edge-button span svg{width:19px;height:19px;display:block}
.viewer-hud{position:absolute;z-index:6;left:16px;bottom:16px;display:flex;flex-direction:column;align-items:flex-start;gap:7px;max-width:calc(100% - 32px);opacity:0;transform:translateY(10px);pointer-events:none;transition:opacity .2s,transform .2s}.viewer-hud.show{opacity:1;transform:none}
.viewer-tools{display:flex;align-items:center;gap:5px;white-space:nowrap;max-width:100%;overflow-x:auto;padding:6px 8px;border:1px solid var(--line);border-radius:11px;background:color-mix(in srgb,var(--panel) 92%,transparent);box-shadow:var(--shadow);backdrop-filter:blur(10px);pointer-events:auto;scrollbar-width:none}.viewer-tools::-webkit-scrollbar{display:none}.viewer-tools button,.viewer-tools span{flex:none}.viewer-tools button{border:0;background:transparent;min-height:28px;padding:4px 9px;border-radius:6px;color:var(--text);line-height:1;display:inline-flex;align-items:center;justify-content:center}.viewer-tools button:hover,.viewer-tools button.active{background:var(--panel2);color:var(--accent)}.viewer-tools button svg{width:14px;height:14px;flex:none;display:block}.viewer-tools i{height:20px;width:1px;flex:none;background:var(--line);margin:0 3px}
.viewer-hint{font-size:11px;line-height:1.6;color:var(--muted);background:color-mix(in srgb,var(--panel) 88%,transparent);border:1px solid var(--line);border-radius:8px;padding:4px 10px;backdrop-filter:blur(8px);pointer-events:none;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}#zoom-label{min-width:46px;flex:none;text-align:center;color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}
.translation{background:var(--panel);border-left:1px solid var(--line);overflow:hidden;min-width:0}.translation-inner{width:370px;height:100%;display:flex;flex-direction:column}.translation-head{padding:14px 16px 10px;border-bottom:1px solid var(--line)}.translation-title{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}.translation-title h2{font-size:14px;margin:0}.text-button{border:0;background:none;color:var(--muted);padding:3px}.text-button:hover{color:var(--accent)}.segments{display:flex;background:var(--bg);padding:3px;border-radius:8px}.segment,.theme-opt{flex:1;border:0;border-radius:6px;background:transparent;color:var(--muted);padding:5px;font-size:12px}.segment.active,.theme-opt.active{background:var(--panel2);color:var(--text);box-shadow:0 1px 4px #0003}.translation-meta{padding:10px 16px;color:var(--muted);font-size:12px;border-bottom:1px solid var(--line)}.translation-list{padding:14px;overflow:auto;flex:1;scrollbar-width:thin}.empty{margin:20vh auto 0;max-width:250px;text-align:center;color:var(--muted)}.empty strong{display:block;color:var(--text);font-size:15px;margin-bottom:8px}.result-card{padding:13px 12px;border-bottom:1px solid var(--line)}.result-card:first-child{border-radius:8px 8px 0 0}.result-card:last-child{border-bottom:0}
.translation-group{border-bottom:1px solid var(--line);padding-bottom:4px}.translation-group:last-child{border-bottom:0}
.group-head{display:flex;align-items:center;gap:8px;padding:8px 2px 4px;color:var(--muted);font-size:11px}.group-head span{flex:none;letter-spacing:.5px}.group-head em{font-style:normal;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;opacity:.75}
.empty.small{margin:14px auto;padding:0;max-width:none;font-size:12.5px;line-height:1.7}.empty.small strong{font-size:13px;margin-bottom:4px}
.check-field{margin-bottom:16px}.check-line{display:flex;align-items:center;gap:9px}.field .check-line input{width:15px;height:15px;flex:none;margin:0;padding:0;border:none;border-radius:2px;background:none;accent-color:var(--accent);cursor:pointer}.check-line span{font-size:13px;line-height:1.4;color:var(--text);white-space:nowrap}.check-field .field-help{padding-left:24px;margin-top:5px}.link-btn{border:0;background:none;padding:0;color:var(--accent);font:inherit;font-size:11px;cursor:pointer;text-decoration:underline}
.entity-add{display:flex;gap:6px;margin-bottom:8px}
.field .entity-add input{flex:1;min-width:0;width:auto;border:1px solid var(--line);background:var(--bg);border-radius:8px;padding:7px 9px;outline:none;font-size:12.5px}
.field .entity-add input:focus{border-color:var(--accent)}
.entity-add button{flex:none;min-height:34px;padding:0 12px}
.entity-list{max-height:168px;overflow-y:auto;border:1px solid var(--line);border-radius:8px;background:var(--bg);scrollbar-width:thin}
.field #entity-search{width:100%;border:1px solid var(--line);background:var(--bg);border-radius:8px;padding:7px 9px;outline:none;font-size:12.5px;margin-bottom:8px;color:var(--text)}
.field #entity-search:focus{border-color:var(--accent)}
.entity-list:empty{display:none}
.entity-empty{padding:14px;text-align:center;color:var(--muted);font-size:12px}
.task-summary{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:10px;flex-wrap:wrap}
.task-summary>span{font-size:12px;color:var(--muted);line-height:1.5;flex:1;min-width:0}
.task-buttons{display:flex;gap:6px;flex:none}
.task-buttons .tool-btn{min-height:30px;padding:0 10px;font-size:12px;white-space:nowrap}
.task-buttons .tool-btn:disabled{opacity:.4;cursor:default}
.task-list{max-height:min(50vh,380px);overflow-y:auto;border:1px solid var(--line);border-radius:9px;background:var(--bg);scrollbar-width:thin}
.task-list:empty{display:none}
.task-row{display:flex;align-items:center;gap:9px;padding:7px 10px;border-bottom:1px solid var(--line);font-size:12.5px}
.task-row:last-child{border-bottom:0}
.task-badge{flex:none;font-size:10px;line-height:1.7;padding:0 5px;border-radius:4px;border:1px solid var(--line);color:var(--muted);white-space:nowrap}
.task-badge.running{color:var(--accent);border-color:color-mix(in srgb,var(--accent) 55%,transparent)}
.task-badge.dropping,.task-badge.failed{color:var(--danger);border-color:color-mix(in srgb,var(--danger) 55%,transparent)}
.task-name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.task-action{flex:none;border:0;background:none;color:var(--muted);cursor:pointer;font-size:11px;padding:2px 6px;border-radius:5px}
.task-action:hover{background:var(--panel2);color:var(--text)}
.task-action.retry:hover{color:var(--accent)}
.task-action.cancel:hover,.task-action.dismiss:hover{color:var(--danger)}
.task-badge.cancelled{border-style:dashed}
.task-empty{padding:22px;text-align:center;color:var(--muted);font-size:12px}
.theme-choice{max-width:320px}
.entity-row{display:flex;align-items:center;gap:8px;padding:5px 8px;border-bottom:1px solid var(--line)}
.entity-row:last-child{border-bottom:0}
.entity-src{flex:0 0 36%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12.5px;color:var(--text)}
.field .entity-row input{flex:1;min-width:0;width:auto;background:transparent;border:1px solid transparent;border-radius:5px;padding:3px 6px;font-size:12.5px;color:var(--text);outline:none}
.field .entity-row input:hover{border-color:var(--line)}
.field .entity-row input:focus{border-color:var(--accent);background:var(--panel2)}
.entity-tag{flex:none;font-size:10px;color:var(--muted);border:1px solid var(--line);border-radius:4px;padding:0 4px;line-height:1.6}
.entity-tag.manual{color:var(--accent);border-color:color-mix(in srgb,var(--accent) 55%,transparent)}
.entity-del{flex:none;border:0;background:none;color:var(--muted);cursor:pointer;padding:2px 4px;font-size:14px;line-height:1}
.entity-del:hover{color:var(--danger)}.result-top{display:flex;align-items:center;gap:7px;margin-bottom:7px}.result-index{font:10px/1.4 ui-monospace,SFMono-Regular,monospace;color:var(--muted)}.type-tag{font-size:10px;color:var(--muted);border:1px solid var(--line);border-radius:4px;padding:1px 5px}.original{color:var(--muted);font-size:13px;overflow-wrap:anywhere}.translation-text{font-size:16px;line-height:1.75;overflow-wrap:anywhere}.original+.translation-text{margin-top:8px}.error-box{padding:12px;margin:12px;border:1px solid color-mix(in srgb,var(--danger) 45%,transparent);border-radius:8px;color:var(--danger);font-size:12px;overflow-wrap:anywhere}.busy-box{padding:9px 12px;margin:0 0 10px;border:1px solid color-mix(in srgb,var(--accent) 45%,transparent);border-radius:8px;color:var(--accent);font-size:12px;overflow-wrap:anywhere}.mini-action{margin-top:10px;border:1px solid var(--line);background:var(--panel2);border-radius:6px;padding:5px 10px;color:var(--text)}
.footer{height:36px;background:var(--panel);border-top:1px solid var(--line);display:flex;align-items:center;gap:12px;padding:0 14px;color:var(--muted);font-size:11px;position:relative;z-index:20}.status-message{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}.footer-task{flex:none;color:var(--accent);white-space:nowrap}.progress-track{height:2px;background:var(--line);position:absolute;left:0;right:0;top:-1px}.progress-bar{height:100%;background:var(--accent);width:0;transition:width .25s}.footer-spacer{flex:1}
.toast{position:fixed;left:50%;bottom:56px;transform:translate(-50%,15px);background:var(--panel2);color:var(--text);border:1px solid var(--line);box-shadow:var(--shadow);border-radius:9px;padding:10px 16px;z-index:100;opacity:0;pointer-events:none;transition:.2s;max-width:min(90vw,600px)}.toast.show{opacity:1;transform:translate(-50%,0)}
.toast[popover]{inset:auto;left:50%;bottom:56px;margin:0;width:auto;height:auto;overflow:visible;max-width:min(90vw,600px)}
.dialog{width:min(92vw,620px);max-height:88vh;max-height:88dvh;overflow-y:auto;border:1px solid var(--line);border-radius:14px;background:var(--panel);color:var(--text);box-shadow:var(--shadow);padding:0}.dialog::backdrop{background:#000a;backdrop-filter:blur(3px)}.dialog-head{display:flex;align-items:center;justify-content:space-between;padding:18px 20px;border-bottom:1px solid var(--line)}.dialog-head h2{margin:0;font-size:17px}.dialog-body{padding:20px}
#settings-dialog .dialog-body>*{margin-bottom:0}
#settings-dialog .dialog-body>*+*{border-top:1px solid var(--line);margin-top:15px;padding-top:15px}.field{margin-bottom:16px}.field label{display:block;color:var(--muted);font-size:12px;margin-bottom:6px}.field input{width:100%;border:1px solid var(--line);background:var(--bg);border-radius:8px;padding:10px;outline:none}.field input:focus{border-color:var(--accent)}.field textarea{width:100%;border:1px solid var(--line);background:var(--bg);border-radius:8px;padding:10px;outline:none;resize:vertical;min-height:68px;font:inherit;font-size:13px;line-height:1.55;color:var(--text)}.field textarea:focus{border-color:var(--accent)}.mono{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--accent)}.field-help{font-size:11px;color:var(--muted);margin-top:6px}.dialog-actions{display:flex;justify-content:flex-end;gap:8px;padding:14px 20px;border-top:1px solid var(--line)}.field-row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}.settings-actions{display:flex;align-items:center;gap:8px}.settings-actions span{font-size:12px;color:var(--muted);overflow-wrap:anywhere}.help-body{padding:6px 20px 20px}.help-section{padding:14px 0}.help-section+.help-section{border-top:1px solid var(--line)}.help-section h3{margin:0 0 8px;font-size:10.5px;font-weight:600;letter-spacing:1.6px;color:var(--muted)}.help-row{display:flex;align-items:center;gap:14px;min-height:30px;padding:3px 0}.help-row .keys{flex:0 0 170px;display:flex;flex-wrap:wrap;justify-content:flex-end;gap:5px}.help-row .desc{flex:1;min-width:0;font-size:13px;line-height:1.5;color:var(--text)}
kbd{display:inline-flex;align-items:center;justify-content:center;min-width:26px;height:24px;padding:0 7px;border:1px solid var(--line);border-bottom-width:2px;border-radius:5px;background:var(--panel2);color:var(--text);font:12px/1 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:nowrap}
@media(max-width:560px){.help-row{flex-direction:column;align-items:flex-start;gap:5px;padding:6px 0}.help-row .keys{flex:none;justify-content:flex-start}}
@media(max-width:1050px){.workspace,.workspace.no-sidebar{grid-template-columns:minmax(0,1fr) 330px}.workspace.no-translation,.workspace.no-sidebar.no-translation{grid-template-columns:minmax(0,1fr) 0}/* 侧栏改为 absolute 后脱流、不再占格位，剩下的子元素会被自动放置挤进 0 宽的首列，   所以这里显式钉住列位；no-sidebar 也要覆盖，否则译文栏按 370 列宽留出 40px 空隙 */.workspace>.stage{grid-column:1}.workspace>.translation{grid-column:2}.sidebar{position:absolute;z-index:12;left:0;top:0;bottom:0;width:210px;box-shadow:var(--shadow);transform:translateX(-100%);transition:.2s}.workspace.sidebar-open .sidebar{transform:none}.translation-inner{width:330px}}
@media(max-width:900px),(max-height:520px){.header{height:54px;padding:8px;gap:6px}.tool-select{max-width:82px;padding:0 4px}.workspace,.workspace.no-translation{height:calc(100vh - 88px);height:calc(100dvh - 88px);display:block}.sidebar{position:absolute;z-index:12;left:0;top:0;bottom:0;width:210px;box-shadow:var(--shadow);transform:translateX(-100%);transition:.2s}.workspace.sidebar-open .sidebar{transform:none}.translation{position:absolute;right:0;top:0;bottom:0;width:min(90vw,370px);z-index:14;box-shadow:var(--shadow);transform:translateX(100%);transition:.2s}.workspace.translation-open .translation{transform:none}.translation-inner{width:100%}.viewer{height:100%}.footer{height:34px;padding:0 9px}.edge-button{width:46px}.viewer-hud{left:8px;bottom:8px;max-width:calc(100% - 16px)}.viewer-hint{display:none}.pages{padding:10px 10px 20px;gap:8px}}
/* 窄屏按“有用性”逐级让位：翻译本页 > 翻译栏 > 设置 > 批量翻译 > 主题 > 帮助。
   每档都按剩余宽度给足余量，避免文字按钮被挤出屏幕。 */
@media(max-width:760px){.brand{display:none}}
@media(max-width:700px){#help-btn{display:none}}
@media(max-width:580px){#translate-all{display:none}}
@media(max-width:480px){#mode-double,#hud-help{display:none}.page-jump,.total{display:none}.field-row{grid-template-columns:1fr}.footer{display:none}.header{gap:4px}.workspace,.workspace.no-translation{height:calc(100vh - 54px);height:calc(100dvh - 54px)}}
@media(max-width:400px){#tasks-btn{display:none}.task-buttons .tool-btn{padding:0 8px;font-size:11px}}
/* 手机横屏：宽度够（780~880px）但高度只有 360~430px，落在移动档却纵向被两侧栏吃光。
   这里按高度单独处理：压掉底部栏、收窄头部，并让阅读区给抽屉让位，
   否则竖版整页图居中后会被右侧译文栏压掉一截。 */
@media(max-height:520px){
  .header{height:48px;padding:6px 8px}
  .footer{display:none}
  .workspace,.workspace.no-translation{height:calc(100vh - 48px);height:calc(100dvh - 48px)}
  .viewer-hud{left:6px;bottom:6px;max-width:calc(100% - 12px)}
  .viewer-tools{padding:4px 6px;gap:4px}
  .viewer-tools button{min-height:24px;padding:3px 7px}
  .translation{width:min(56vw,340px)}
  /* 用 width + margin 真正缩窄阅读区，而不是 padding：padding 的话滚动条仍贴在视口右缘，
     会被滑入的译文栏盖住，连滚动位置都看不见。 */
  .viewer{transition:width .2s,margin .2s}
  .workspace.translation-open .viewer{width:calc(100% - min(56vw,340px));margin-right:min(56vw,340px)}
  .workspace.sidebar-open .viewer{width:calc(100% - 210px);margin-left:210px}
  .workspace.sidebar-open.translation-open .viewer{width:calc(100% - 210px - min(56vw,340px))}
}
</style>
</head>
<body>
<header class="header">
  <button class="icon-btn" id="sidebar-btn" title="页面列表 (S)" aria-label="页面列表"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7h16M4 12h16M4 17h16"/></svg></button>
  <div class="brand"><strong>漫画阅读器</strong></div>
  <div class="nav-group">
    <button class="icon-btn" id="prev-btn" title="上一页 (← / →)" aria-label="上一页"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.5 5.5 8 12l6.5 6.5"/></svg></button>
    <input class="page-jump" id="page-jump" type="number" min="1" value="1" aria-label="页码">
    <span class="total" id="total-label">/ 0</span>
    <button class="icon-btn" id="next-btn" title="下一页 (← / →)" aria-label="下一页"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M9.5 5.5 16 12l-6.5 6.5"/></svg></button>
  </div>
  <div class="toolbar-spacer"></div>
  <button class="tool-btn primary" id="translate-btn" title="翻译本页 (T)">翻译本页</button>
  <button class="tool-btn optional-tool" id="translate-all" title="翻译所有未处理页面">批量翻译</button>
  <button class="icon-btn optional-tool" id="translation-btn" title="译文面板 (A)" aria-label="译文面板"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="3.2" y="4.5" width="7" height="15" rx="1.6"/><path d="M14 8h6.5M14 12h6.5M14 16h4.5"/></svg></button>
  <button class="icon-btn" id="tasks-btn" title="任务队列" aria-label="任务队列"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="5.2" cy="6.5" r="1.15" fill="currentColor" stroke="none"/><circle cx="5.2" cy="12" r="1.15" fill="currentColor" stroke="none"/><circle cx="5.2" cy="17.5" r="1.15" fill="currentColor" stroke="none"/><path d="M9.4 6.5h10.6M9.4 12h10.6M9.4 17.5h6.8"/></svg></button>
  <button class="icon-btn" id="settings-btn" title="模型设置" aria-label="设置"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg></button>
  <button class="icon-btn optional-tool" id="help-btn" title="快捷键 (?)" aria-label="帮助"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M9.1 9a2.9 2.9 0 1 1 3.9 2.72c-.9.34-1.5 1.1-1.5 2.06v.32"/><circle cx="11.5" cy="17.3" r="1.15" fill="currentColor" stroke="none"/></svg></button>
</header>
<div class="workspace" id="workspace">
  <aside class="sidebar"><div class="sidebar-inner"><div class="thumbnails" id="thumbnails"></div></div></aside>
  <section class="stage" id="stage">
    <main class="viewer" id="viewer" tabindex="0"><div class="pages" id="pages"></div></main>
    <button class="edge-button" id="prev-edge" title="上一页（← / ↑）" aria-label="上一页"><span><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.5 5.5 8 12l6.5 6.5"/></svg></span></button>
    <button class="edge-button" id="next-edge" title="下一页（→ / ↓）" aria-label="下一页"><span><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M9.5 5.5 16 12l-6.5 6.5"/></svg></span></button>
    <div class="viewer-hud" id="viewer-hud">
      <div class="viewer-tools" id="viewer-tools">
        <button id="zoom-out" title="缩小 (-)" aria-label="缩小"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M5 12h14"/></svg></button><span id="zoom-label">100%</span><button id="zoom-in" title="放大 (+)" aria-label="放大"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg></button>
        <button id="zoom-fit" title="适应窗口 (0)">适应</button><i></i>
        <button id="mode-single" data-mode="single">单页</button><button id="mode-double" data-mode="double">双页</button><button id="mode-continuous" data-mode="continuous">连续</button><i></i>
        <button id="fullscreen-btn" title="全屏 (F)" aria-label="全屏"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 4H4v5M15 4h5v5M9 20H4v-5M15 20h5v-5"/></svg></button><button id="hud-help" title="快捷键 (?)" aria-label="快捷键"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M9.1 9a2.9 2.9 0 1 1 3.9 2.72c-.9.34-1.5 1.1-1.5 2.06v.32"/><circle cx="11.5" cy="17.3" r="1.15" fill="currentColor" stroke="none"/></svg></button>
      </div>
      <div class="viewer-hint">← → 翻页 · Space 下一页 · + − 缩放 · 0 适应 · B 双页 · F 全屏 · T 翻译 · 单击页面收起</div>
    </div>
  </section>
  <aside class="translation"><div class="translation-inner">
    <div class="translation-head"><div class="translation-title"><h2>页面译文</h2><button class="text-button" id="clear-cache" title="删除本页翻译缓存">清除缓存</button></div>
      <div class="segments"><button class="segment active" data-view="both">中日对照</button><button class="segment" data-view="cn">仅中文</button><button class="segment" data-view="jp">仅原文</button></div>
    </div>
    <div class="translation-meta" id="translation-meta">尚未翻译</div>
    <div class="translation-list" id="translation-list"></div>
  </div></aside>
</div>
<footer class="footer"><div class="progress-track"><div class="progress-bar" id="progress-bar"></div></div><span class="status-message" id="status">正在载入…</span><div class="footer-spacer"></div><span class="footer-task" id="task-label"></span></footer>
<div class="toast" id="toast" popover="manual"></div>
<dialog class="dialog" id="settings-dialog">
  <div class="dialog-head"><h2>设置</h2><button class="icon-btn" data-close="settings-dialog" aria-label="关闭"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/></svg></button></div>
  <div class="dialog-body">
    <div class="field"><label>界面主题</label>
      <div class="segments theme-choice">
        <button class="theme-opt" data-theme-opt="light">亮色</button>
        <button class="theme-opt" data-theme-opt="dark">暗色</button>
        <button class="theme-opt" data-theme-opt="system">跟随系统</button>
      </div>
      <div class="field-help">只有「跟随系统」会随操作系统的深浅色设置自动切换。</div>
    </div>
    <div class="field"><label for="base-url">OpenAI 兼容服务地址</label><input id="base-url" spellcheck="false" placeholder="http://127.0.0.1:1234 或 https://api.example.com/v1"><div class="field-help">支持服务根地址、/v1 地址或完整 chat/completions 地址。</div></div>
    <div class="field"><label for="model-name">视觉模型名称</label><input id="model-name" list="models" spellcheck="false"><datalist id="models"></datalist><div class="field-help" id="model-help">只需模型支持图片输入。开启结构化输出时优先使用 JSON Schema，端点不支持会自动降级。</div></div>
    <div class="field"><label for="api-key">API Key（本地服务可留空）</label><input id="api-key" type="password" autocomplete="off" placeholder="sk-..."><div class="field-help">密钥只保存在本机 data.json，不会回填到输入框。<button class="link-btn hidden" id="clear-api-key">清除已保存的密钥</button></div></div>
    <div class="field-row">
      <div class="field"><label for="request-timeout">超时（秒）</label><input id="request-timeout" type="number" min="10" max="3600"></div>
      <div class="field"><label for="max-tokens">最大输出 tokens</label><input id="max-tokens" type="number" min="256" max="65536" step="256"></div>
      <div class="field"><label for="temperature">温度</label><input id="temperature" type="number" min="0" max="2" step="0.1"></div>
    </div>
    <div class="field check-field"><div class="check-line"><input id="use-structured" type="checkbox"><span>请求 JSON 结构化输出</span></div><div class="field-help">端点不支持时会自动降级为提示词约束，不会报错。</div></div>
    <div class="field check-field"><div class="check-line"><input id="use-thinking" type="checkbox"><span>启用模型思考</span></div><div class="field-help">关闭可明显提速；思考内容与正文共用「最大输出 tokens」额度。</div></div>
    <div class="settings-actions"><button class="tool-btn" id="fetch-models">读取模型</button><button class="tool-btn" id="test-connection">测试连接</button><span id="connection-result"></span></div>
    <div class="field"><label>术语表 <span class="mono" id="entity-count">0 条</span></label>
      <div class="entity-add"><input id="entity-source" placeholder="原文" spellcheck="false"><input id="entity-target" placeholder="译文" spellcheck="false"><button class="tool-btn" id="entity-add">添加</button></div>
      <input id="entity-search" placeholder="搜索原文或译文" spellcheck="false">
      <div class="entity-list" id="entity-list"></div>
      <div class="field-help">手动添加的和自动识别的都在这一张表里，译文可直接改、条目可删。改过的条目会被锁定，自动累积不再覆盖它。</div>
    </div>
    <div class="field check-field"><div class="check-line"><input id="use-auto-glossary" type="checkbox"><span>自动累积译名</span></div><div class="field-help">翻译时顺带提取人名、地名、机构名并加入上面的清单。<button class="link-btn" id="clear-entities">清空自动条目</button></div></div>
  </div>
  <div class="dialog-actions"><button class="tool-btn" data-close="settings-dialog">取消</button><button class="tool-btn primary" id="save-settings">保存</button></div>
</dialog>
<dialog class="dialog" id="tasks-dialog">
  <div class="dialog-head"><h2>任务队列</h2><button class="icon-btn" data-close="tasks-dialog" aria-label="关闭"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"/></svg></button></div>
  <div class="dialog-body">
    <div class="task-summary"><span id="task-summary">空闲</span><div class="task-buttons"><button class="tool-btn" id="cancel-all-tasks">全部取消</button><button class="tool-btn" id="dismiss-all-tasks">全部删除</button></div></div>
    <div class="task-list" id="task-list"></div>
    <div class="field-help">排队中的可以直接移除；正在执行的那页无法中断请求，只能丢弃它的结果。</div>
  </div>
</dialog>
<dialog class="dialog" id="help-dialog" style="width:min(92vw,560px)">
  <div class="dialog-head"><h2>阅读快捷键</h2><button class="icon-btn" data-close="help-dialog" aria-label="关闭"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/></svg></button></div>
  <div class="dialog-body help-body">
    <section class="help-section">
      <h3>翻页</h3>
      <div class="help-row"><span class="keys"><kbd>←</kbd><kbd>↑</kbd><kbd>PgUp</kbd></span><span class="desc">上一页</span></div>
      <div class="help-row"><span class="keys"><kbd>→</kbd><kbd>↓</kbd><kbd>PgDn</kbd><kbd>Space</kbd></span><span class="desc">下一页</span></div>
      <div class="help-row"><span class="keys"><kbd>Home</kbd><kbd>End</kbd></span><span class="desc">跳到第一页 / 最后一页</span></div>
    </section>
    <section class="help-section">
      <h3>视图</h3>
      <div class="help-row"><span class="keys"><kbd>+</kbd><kbd>−</kbd></span><span class="desc">放大 / 缩小</span></div>
      <div class="help-row"><span class="keys"><kbd>0</kbd></span><span class="desc">适应窗口</span></div>
      <div class="help-row"><span class="keys"><kbd>B</kbd></span><span class="desc">单页 / 双页切换</span></div>
      <div class="help-row"><span class="keys"><kbd>F</kbd></span><span class="desc">进入 / 退出全屏</span></div>
      <div class="help-row"><span class="keys"><kbd>双击</kbd></span><span class="desc">放大到 200% / 还原</span></div>
    </section>
    <section class="help-section">
      <h3>翻译与面板</h3>
      <div class="help-row"><span class="keys"><kbd>T</kbd><kbd>R</kbd></span><span class="desc">翻译 / 重新翻译（双页模式下同时提交两页）</span></div>
      <div class="help-row"><span class="keys"><kbd>S</kbd></span><span class="desc">显示 / 隐藏页面列表</span></div>
      <div class="help-row"><span class="keys"><kbd>A</kbd></span><span class="desc">显示 / 隐藏译文面板</span></div>
      <div class="help-row"><span class="keys"><kbd>单击画面</kbd></span><span class="desc">呼出 / 收起左下角工具条</span></div>
      <div class="help-row"><span class="keys"><kbd>?</kbd></span><span class="desc">打开本帮助</span></div>
    </section>
  </div>
</dialog>
<script>
"use strict";
const $ = (selector) => document.querySelector(selector);
const state = {images:[], current:0, folder:"", settings:{}, worker:{}, prefs:{mode:"single",zoom:1,view:"both",sidebar:true,translation:true,theme:"system"}, pages:new Map(), entityCount:0, entities:[], translationKey:"", toastHideTimer:null, timer:null, toastTimer:null, touchX:null, drag:null, dragMoved:0, hudVisible:false, renderToken:0};
const prefKey = "manga-reader:prefs:" + location.pathname;
/* 隐私模式或禁用站点存储时 localStorage 会直接抛异常：读写都包一层，
   否则一次翻页就在 goTo 里抛出，界面从此不再响应。 */
function storeGet(key){try{return localStorage.getItem(key);}catch(_){return null;}}
function storeSet(key,value){try{localStorage.setItem(key,value);}catch(_){}}
try { Object.assign(state.prefs, JSON.parse(storeGet(prefKey) || "{}")); } catch (_) {}
function savePrefs(){storeSet(prefKey,JSON.stringify(state.prefs));}
function escapeHTML(value){
  /* 同时转义引号：这个函数既用于文本节点，也用于 title=""、data-*="" 这类属性值，
     只处理 &<> 的话，含双引号的内容能直接闭合属性并注入新属性。 */
  return String(value??"").replace(/[&<>"']/g,ch=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
}
async function api(path, options={}){
  const response=await fetch(path,{headers:{"Content-Type":"application/json"},...options});
  const payload=await response.json().catch(()=>({}));
  if(!response.ok) throw new Error(payload.error||`请求失败 (${response.status})`);
  return payload;
}
function toast(message){
  const el=$("#toast");
  el.textContent=message;
  clearTimeout(state.toastTimer);
  clearTimeout(state.toastHideTimer);
  /* dialog 用 showModal() 时占据 top layer，普通 z-index 压不过它；popover 让提示条也进顶层 */
  if(el.showPopover){
    try{
      if(!el.matches(":popover-open"))el.showPopover();
      el.style.display="";
    }catch(_){el.style.display="block";}   /* popover 不可用时的兜底，至少能显示 */
  }
  el.classList.add("show");
  state.toastTimer=setTimeout(()=>{
    el.classList.remove("show");
    state.toastHideTimer=setTimeout(()=>{
      if(el.matches&&el.matches(":popover-open")){try{el.hidePopover();}catch(_){}}
      el.style.display="";
      state.toastTimer=null;
      state.toastHideTimer=null;
    },220);
  },3200);
}
function pageName(index=state.current){return state.images[index]?.name||"";}
function isDouble(){return state.prefs.mode==="double"&&innerWidth>720;}
function visibleIndexes(){
  if(state.prefs.mode==="continuous") return state.images.map((_,i)=>i);
  if(isDouble()){const second=state.current+1;return second<state.images.length?[state.current,second]:[state.current];}
  return [state.current];
}
function syncThemeChoice(){
  document.querySelectorAll("[data-theme-opt]").forEach(el=>el.classList.toggle("active",el.dataset.themeOpt===state.prefs.theme));
}
function resolvedTheme(){
  if(state.prefs.theme!=="system")return state.prefs.theme==="light"?"light":"dark";
  return matchMedia("(prefers-color-scheme: light)").matches?"light":"dark";
}
function applyPrefs(){
  // 偏好存在 localStorage 里，可能被手改或旧版本写坏，先兜一遍
  if(!Number.isFinite(state.prefs.zoom)||state.prefs.zoom<=0)state.prefs.zoom=1;
  if(!["single","double","continuous"].includes(state.prefs.mode))state.prefs.mode="single";
  if(!["both","cn","jp"].includes(state.prefs.view))state.prefs.view="both";
  if(!["dark","light","system"].includes(state.prefs.theme))state.prefs.theme="system";
  document.documentElement.dataset.theme=resolvedTheme();
  $("#workspace").classList.toggle("no-sidebar",!state.prefs.sidebar);$("#workspace").classList.toggle("no-translation",!state.prefs.translation);
  $("#workspace").classList.toggle("sidebar-open",state.prefs.sidebar);$("#workspace").classList.toggle("translation-open",state.prefs.translation);
  document.querySelectorAll("[data-view]").forEach(el=>el.classList.toggle("active",el.dataset.view===state.prefs.view));
  document.querySelectorAll("[data-mode]").forEach(el=>el.classList.toggle("active",el.dataset.mode===state.prefs.mode));
  $("#zoom-label").textContent=Math.round(state.prefs.zoom*100)+"%";
}
function imageElement(index){
  const info=state.images[index];
  if(!info)return document.createComment("missing page");
  const image=document.createElement("img");image.className="page-image";image.src=`/api/image/${index}`;image.alt=`第 ${index+1} 页：${info.name}`;image.draggable=false;
  if(state.prefs.mode==="continuous") image.loading="lazy";
  image.addEventListener("load",()=>{applySizing();if(state.prefs.mode==="continuous"&&index===state.current&&image.dataset.scrolled!=="1"){image.dataset.scrolled="1";scrollCurrent(false);}});
  image.addEventListener("error",()=>{image.alt="图片载入失败";});return image;
}
function renderReader(){
  const pages=$("#pages");pages.replaceChildren();pages.className="pages"+(state.prefs.mode==="continuous"?" continuous":"");
  for(const index of visibleIndexes()){
    const frame=document.createElement("section");frame.className="page-frame";frame.dataset.index=index;frame.append(imageElement(index));
    const number=document.createElement("span");number.className="page-number";number.textContent=`${index+1} / ${state.images.length}`;frame.append(number);pages.append(frame);
  }
  $("#prev-edge").classList.toggle("hidden",state.prefs.mode==="continuous");$("#next-edge").classList.toggle("hidden",state.prefs.mode==="continuous");
  requestAnimationFrame(()=>{applySizing();updateActiveThumbnail();});
}
function viewerBox(){
  /* clientWidth/Height 含 padding，而抽屉让位正是靠 .viewer 的 padding 实现的，
     所以这里必须扣掉，否则连续模式会按整屏宽度铺图，右侧被译文栏压住。 */
  const viewer=$("#viewer"),cs=getComputedStyle(viewer);
  return {
    width:Math.max(160,viewer.clientWidth-parseFloat(cs.paddingLeft||0)-parseFloat(cs.paddingRight||0)),
    height:Math.max(160,viewer.clientHeight-parseFloat(cs.paddingTop||0)-parseFloat(cs.paddingBottom||0))
  };
}
function applySizing(){
  const viewer=$("#viewer"),pages=$("#pages"),zoom=state.prefs.zoom;
  const frames=[...document.querySelectorAll(".page-frame")];
  const images=frames.map(frame=>frame.querySelector("img")).filter(Boolean);
  if(!images.length)return;
  pages.classList.toggle("zoomed",zoom>1.001&&state.prefs.mode!=="continuous");
  $("#zoom-label").textContent=Math.round(zoom*100)+"%";
  if(state.prefs.mode==="continuous"){
    const width=Math.max(160,viewerBox().width-32)*zoom;
    frames.forEach(frame=>{frame.style.width=width+"px";frame.style.maxWidth="none";const image=frame.querySelector("img");image.style.width="100%";image.style.height="auto";});
    return;
  }
  if(images.some(image=>!image.naturalWidth||!image.naturalHeight))return;
  const box=viewerBox();
  const gap=14,availableWidth=Math.max(160,box.width-32-gap*(images.length-1));
  const availableHeight=Math.max(160,box.height-44);
  const totalNaturalWidth=images.reduce((sum,image)=>sum+image.naturalWidth,0);
  const maxNaturalHeight=Math.max(...images.map(image=>image.naturalHeight));
  const fitScale=Math.min(availableWidth/totalNaturalWidth,availableHeight/maxNaturalHeight);
  images.forEach(image=>{
    image.style.width=Math.max(1,Math.round(image.naturalWidth*fitScale*zoom))+"px";
    image.style.height=Math.max(1,Math.round(image.naturalHeight*fitScale*zoom))+"px";
  });
}
function updateHeader(){
  const total=state.images.length;$("#page-jump").value=state.current+1;$("#page-jump").max=total;$("#total-label").textContent=`/ ${total}`;
  $("#prev-btn").disabled=$("#next-btn").disabled=total===0;
  $("#progress-bar").style.width=`${total?(state.current+1)/total*100:0}%`;
  const shown=visibleIndexes();
  const anyTranslated=shown.some(i=>state.images[i]&&state.images[i].translated);  $("#translate-btn").textContent=anyTranslated?"重新翻译":"翻译本页";
  $("#translate-btn").title=shown.length>1?`翻译当前跨页（第 ${shown[0]+1}、${shown[shown.length-1]+1} 页）`:"翻译本页 (T)";
  document.title=`${state.current+1}/${total} · ${pageName()} · 漫画阅读器`;
  renderFooter();
}
function renderThumbnails(){
  const list=$("#thumbnails");list.replaceChildren();state.images.forEach((image,index)=>{
    const button=document.createElement("button");button.className="thumb"+(index===state.current?" active":"");button.dataset.index=index;
    button.innerHTML=`<img loading="lazy" src="/api/image/${index}" alt=""><div class="thumb-label"><i class="cache-dot ${image.translated?"done":image.busy?"busy":""}"></i><span>${index+1}. ${escapeHTML(image.name)}</span></div>`;
    button.addEventListener("click",()=>goTo(index));list.append(button);
  });
}
function refreshThumbnailStates(){state.images.forEach((image,index)=>{const el=$(`.thumb[data-index="${index}"]`);if(!el)return;el.classList.toggle("active",index===state.current);const dot=el.querySelector(".cache-dot");dot.className=`cache-dot ${image.translated?"done":image.busy?"busy":""}`;});}
function updateActiveThumbnail(){refreshThumbnailStates();const active=$(".thumb.active");if(active)active.scrollIntoView({block:"nearest"});}
function goTo(index,scroll=true){
  if(!state.images.length)return;
  let value=Number(index);
  if(!Number.isFinite(value))value=0;
  let next=Math.max(0,Math.min(state.images.length-1,Math.trunc(value)));
  if(isDouble())next=Math.floor(next/2)*2;
  const changed=next!==state.current;state.current=next;
  if(scroll){toggleHud(false);storeSet(prefKey+":page",String(next));if(changed||state.prefs.mode!=="continuous")renderReader();else scrollCurrent();}
  updateHeader();renderTranslation();
  const amount=isDouble()?2:1;[next+amount,next+amount*2].filter(i=>i>=0&&i<state.images.length).forEach(i=>{const image=new Image();image.src=`/api/image/${i}`;});
}
function navigate(step){
  if(!state.images.length)return;
  const amount=isDouble()?2:1;goTo(state.current+step*amount);
}
function scrollCurrent(smooth=true){const frame=$(`.page-frame[data-index="${state.current}"]`);if(frame&&state.prefs.mode==="continuous")frame.scrollIntoView({behavior:smooth?"smooth":"auto",block:"start"});}
async function loadPage(index=state.current,force=false){
  if(!state.images[index])return null;if(!force&&state.pages.has(index))return state.pages.get(index);
  try{const page=await api(`/api/page?index=${index}`);state.pages.set(index,page);return page;}catch(error){toast(error.message);return null;}
}
function translationBody(index,page){
  const info=state.images[index]||{};
  const results=page?.results||[];
  if(info.busy&&!results.length)return '<div class="empty small"><strong>正在处理</strong>可以继续阅读，完成后译文会自动出现。</div>';
  if(results.length){
    const typeNames={bubble:"气泡",narration:"旁白",sfx:"拟声",sign:"画面",other:"其他"};
    const showJP=state.prefs.view!=="cn",showCN=state.prefs.view!=="jp";
    let notice="";
    /* 重新翻译一次可能两三分钟，旧译文先留在原地比整块抽走有用 */
    if(info.busy)notice+='<div class="busy-box">正在重新翻译，完成后会自动替换下面这段。</div>';
    if(info.error)notice+=`<div class="error-box">上次重译失败，下面是更早缓存的结果：<br>${escapeHTML(info.error)}</div>`;
    return notice+results.map(item=>`<article class="result-card"><div class="result-top"><span class="result-index">${String(item.index).padStart(2,"0")}</span><span class="type-tag">${typeNames[item.type]||"其他"}</span></div>${showJP?`<div class="original">${escapeHTML(item.original)}</div>`:""}${showCN?`<div class="translation-text">${escapeHTML(item.translation)}</div>`:""}</article>`).join("");
  }
  if(page&&"results"in page)return '<div class="empty small"><strong>本页没有可显示的文本</strong>模型返回了空结果。</div>';
  let html='<div class="empty small"><strong>按需调用视觉模型</strong>按 T 或点击“翻译本页”，原文与中文译文会显示在这里。</div>';
  if(info.error)html+=`<div class="error-box">${escapeHTML(info.error)}<br><button class="mini-action" data-retry="${index}">重试</button></div>`;
  return html;
}
async function renderTranslation(){
  const indexList=visibleIndexes();
  if(!indexList.length||!state.images[indexList[0]])return;
  const token=state.renderToken=state.renderToken+1;
  const list=$("#translation-list"),meta=$("#translation-meta");
  /* 只有「换页」才把译文栏滚回顶部；同一页内翻译完成刷新时保持用户当前阅读位置 */
  const pageKey=indexList.join(",");
  const pageChanged=state.translationKey!==pageKey;
  state.translationKey=pageKey;
  const entries=await Promise.all(indexList.map(async index=>({index,page:await loadPage(index)})));
  if(token!==state.renderToken)return;
  const multi=entries.length>1;
  const totalItems=entries.reduce((sum,entry)=>sum+((entry.page?.results?.length)||0),0);
  const anyBusy=entries.some(entry=>state.images[entry.index]?.busy);
  if(multi){
    const label=`第 ${entries[0].index+1}–${entries[entries.length-1].index+1} 页`;
    meta.textContent=anyBusy?`${label} · 模型正在识别与翻译…`:(totalItems?`${label} · ${totalItems} 条文本`:`${label} · 尚无译文`);
    list.innerHTML=entries.map(entry=>`<section class="translation-group"><div class="group-head"><span>第 ${entry.index+1} 页</span><em>${escapeHTML(state.images[entry.index]?.name||"")}</em></div>${translationBody(entry.index,entry.page)}</section>`).join("");
    if(pageChanged)list.scrollTop=0;
    return;
  }
  const entry=entries[0],info=state.images[entry.index]||{},page=entry.page,results=page?.results||[];
  if(anyBusy)meta.textContent="模型正在识别与翻译，通常需要一些时间…";
  else if(results.length)meta.textContent=`${results.length} 条文本${page.model?" · "+page.model:""}`;
  else if(page&&"results"in page)meta.textContent="已完成 · 未检测到文字";
  else meta.textContent=info.error?"处理失败":"尚未翻译";
  list.innerHTML=translationBody(entry.index,page);
  if(pageChanged)list.scrollTop=0;
}
async function translateIndex(index,front=false){
  try{const result=await api("/api/translate",{method:"POST",body:JSON.stringify({index,force:true,front})});state.pages.delete(index);return Number(result.added)||0;}catch(error){toast(error.message);return 0;}
}
async function translateCurrent(){
  if(!state.images.length)return;
  const indexList=visibleIndexes();
  let queued=0;
  for(const index of indexList){queued+=await translateIndex(index);}
  if(!queued){toast("这一页正在处理中，等它跑完即可");return;}
  toast(indexList.length>1?`已加入 ${queued} 页翻译队列，可继续阅读`:"已加入翻译队列，可继续阅读");
  await refreshState();
  renderTranslation();
}
async function translateAll(){
  const missing=state.images.filter(x=>!x.translated).length;if(!missing){toast("所有页面都已有翻译缓存");return;}
  if(!confirm(`将依次翻译 ${missing} 个未处理页面，期间会占用模型服务。是否继续？`))return;
  const button=$("#translate-all");
  if(button.disabled)return;
  button.disabled=true;
  try{
    const result=await api("/api/translate",{method:"POST",body:JSON.stringify({all:true})});
    toast(result.added?`已加入 ${result.added} 页`:"这些页面都已在队列里，没有新增任务");
    await refreshState();
  }catch(error){toast(error.message);}
  finally{button.disabled=false;}
}
function renderFooter(){
  const parts=[state.folder,pageName(),state.settings.model||"未配置模型",Math.round(state.prefs.zoom*100)+"%"];
  $("#status").textContent=parts.filter(Boolean).join(" · ");
  const worker=state.worker||{};
  let task="";
  if(worker.active)task=`翻译中 · 第 ${(worker.current??0)+1} 页${worker.pending?" · 队列 "+worker.pending:""}`;
  else if(worker.pending)task=`排队中 · ${worker.pending} 页`;
  $("#task-label").textContent=task;
}
async function refreshState(){
  try{const signature=list=>list.map(x=>`${x.translated}:${x.busy}:${x.error}`).join("|");const before=state.images.slice();const previous=signature(before);const data=await api("/api/state");state.images=data.images;state.folder=data.folder||"";state.settings=data.settings;state.worker=data.worker;state.entityCount=data.entity_count||0;
    if(previous!==signature(state.images)){
      state.images.forEach((image,index)=>{const old=before[index];if(!old||signature([old])!==signature([image]))state.pages.delete(index);});
      renderTranslation();
    }
    renderFooter();updateHeader();refreshThumbnailStates();
    clearTimeout(state.timer);state.timer=setTimeout(refreshState,state.worker.active||state.worker.pending?1500:6000);
  }catch(error){$("#status").textContent="后端连接中断："+error.message;clearTimeout(state.timer);state.timer=setTimeout(refreshState,5000);}
}
function setZoom(value,center=false){
  const viewer=$("#viewer");
  const prevWidth=viewer.scrollWidth,prevHeight=viewer.scrollHeight,prevLeft=viewer.scrollLeft,prevTop=viewer.scrollTop;
  state.prefs.zoom=Math.max(.25,Math.min(5,Math.round(value*100)/100));savePrefs();applySizing();
  if(state.prefs.mode!=="continuous"){
    if(state.prefs.zoom<=1.001){viewer.scrollLeft=0;viewer.scrollTop=0;}
    else if(center){
      /* 以内容区中心为锚点换算滚动位置：同一内容点留在原地，视觉上就是“原地放大”而不是先跳到左上角。
         锚点要算进 padding——抽屉让位时 .viewer 有单侧 padding，直接用 clientWidth/2 会偏到抽屉那边。 */
      const cs=getComputedStyle(viewer);
      const padL=parseFloat(cs.paddingLeft||0),padR=parseFloat(cs.paddingRight||0);
      const padT=parseFloat(cs.paddingTop||0),padB=parseFloat(cs.paddingBottom||0);
      const anchorX=padL+(viewer.clientWidth-padL-padR)/2,anchorY=padT+(viewer.clientHeight-padT-padB)/2;
      const prevW=prevWidth-padL-padR,prevH=prevHeight-padT-padB;
      const nowW=viewer.scrollWidth-padL-padR,nowH=viewer.scrollHeight-padT-padB;
      const ratioX=prevW>0?nowW/prevW:1,ratioY=prevH>0?nowH/prevH:1;
      viewer.scrollLeft=Math.max(0,(prevLeft+anchorX)*ratioX-anchorX);
      viewer.scrollTop=Math.max(0,(prevTop+anchorY)*ratioY-anchorY);
    }
  }
  renderFooter();
}
function zoom(delta){setZoom(state.prefs.zoom+delta,true);}
function toggleHud(force){state.hudVisible=typeof force==="boolean"?force:!state.hudVisible;const hud=$("#viewer-hud");if(hud)hud.classList.toggle("show",state.hudVisible);}
function setMode(mode){
  if(!["single","double","continuous"].includes(mode))return;
  state.prefs.mode=mode;state.prefs.zoom=1;savePrefs();applyPrefs();
  if(mode==="double")state.current=Math.floor(state.current/2)*2;
  renderReader();updateHeader();renderTranslation();if(mode==="continuous")setTimeout(()=>scrollCurrent(false),60);
}
async function toggleFullscreen(){try{if(document.fullscreenElement)await document.exitFullscreen();else await document.documentElement.requestFullscreen();}catch(_){toast("浏览器未允许全屏");}}
let taskPollTimer=null;
function taskRowHTML(index,label,kind,actions){
  const info=state.images[index]||{};
  const name=info.name||`第 ${index+1} 页`;
  const buttons=(actions||[]).map(action=>{
    const text={cancel:"取消",retry:"重试",dismiss:"删除"}[action];
    return `<button class="task-action ${action}" data-task-${action}="${index}">${text}</button>`;
  }).join("");
  return `<div class="task-row"><span class="task-badge ${kind}">${label}</span><span class="task-name" title="${escapeHTML(name)}">${index+1}. ${escapeHTML(name)}</span>${buttons}</div>`;
}
function renderTasks(data){
  const queued=data.queued||[];
  const cancelled=data.cancelled||[];
  const currentIndex=data.current?data.current.index:null;
  const seen=new Set(), rows=[];

  if(data.current){
    seen.add(currentIndex);
    rows.push(taskRowHTML(currentIndex,data.current.dropping?"取消中":"进行中",
      data.current.dropping?"dropping":"running",[]));
  }
  queued.forEach((index,position)=>{
    if(seen.has(index))return;
    seen.add(index);
    rows.push(taskRowHTML(index,position===0?"下一个":"排队中","queued",["cancel"]));
  });
  cancelled.forEach(index=>{
    if(seen.has(index))return;
    seen.add(index);
    rows.push(taskRowHTML(index,"已取消","cancelled",["retry","dismiss"]));
  });
  const failed=[];
  state.images.forEach((image,index)=>{
    if(!image.error||seen.has(index))return;
    seen.add(index);
    failed.push(index);
    rows.push(taskRowHTML(index,"失败","failed",["retry","dismiss"]));
  });

  $("#task-list").innerHTML=rows.length?rows.join(""):'<div class="task-empty">队列为空</div>';

  const parts=[];
  if(data.current)parts.push("进行中 1");
  if(queued.length)parts.push(`排队 ${queued.length}`);
  if(cancelled.length)parts.push(`已取消 ${cancelled.length}`);
  if(failed.length)parts.push(`失败 ${failed.length}`);
  if(data.completed)parts.push(`完成 ${data.completed}`);
  $("#task-summary").textContent=parts.length?parts.join(" · "):"空闲";

  $("#cancel-all-tasks").disabled=!data.current&&!queued.length;
  $("#dismiss-all-tasks").disabled=!cancelled.length&&!failed.length;
}
async function refreshTasks(){
  if(!$("#tasks-dialog").open)return;
  try{renderTasks(await api("/api/tasks"));}
  catch(error){$("#task-summary").textContent=error.message;}
}
function openTasks(){
  $("#tasks-dialog").showModal();
  refreshTasks();
  clearInterval(taskPollTimer);
  taskPollTimer=setInterval(refreshTasks,1200);
}
$("#tasks-dialog").addEventListener("close",()=>{clearInterval(taskPollTimer);taskPollTimer=null;});
$("#cancel-all-tasks").onclick=async()=>{
  try{
    const result=await api("/api/tasks/cancel",{method:"POST",body:JSON.stringify({all:true})});
    toast(result.removed?`已取消 ${result.removed} 项，可在列表里重试`:"没有正在执行或排队的任务");
    await refreshTasks();await refreshState();
  }catch(error){toast(error.message);}
};
$("#dismiss-all-tasks").onclick=async()=>{
  try{
    const result=await api("/api/tasks/dismiss",{method:"POST",body:JSON.stringify({all:true})});
    toast(result.removed?`已移除 ${result.removed} 条记录`:"没有可删除的记录");
    await refreshTasks();await refreshState();
  }catch(error){toast(error.message);}
};
$("#task-list").addEventListener("click",async event=>{
  const cancel=event.target.closest("[data-task-cancel]");
  const retry=event.target.closest("[data-task-retry]");
  const dismiss=event.target.closest("[data-task-dismiss]");
  try{
    if(cancel){
      const result=await api("/api/tasks/cancel",{method:"POST",body:JSON.stringify({index:Number(cancel.dataset.taskCancel)})});
      toast(result.result==="dropping"?"已标记取消，请求返回后丢弃结果":"已取消，可在列表里重试");
      await refreshTasks();await refreshState();
    }else if(dismiss){
      await api("/api/tasks/dismiss",{method:"POST",body:JSON.stringify({index:Number(dismiss.dataset.taskDismiss)})});
      toast("已从列表移除");
      await refreshTasks();await refreshState();
    }else if(retry){
      if(await translateIndex(Number(retry.dataset.taskRetry),true)){
        toast("已插队重试，优先处理");
        await refreshTasks();await refreshState();
      }
    }
  }catch(error){toast(error.message);}
});

function openSettings(){
  $("#base-url").value=state.settings.base_url||"";$("#model-name").value=state.settings.model||"";$("#api-key").value="";$("#api-key").placeholder=state.settings.has_api_key?"已设置，留空则不修改":"sk-…（本地服务可留空）";
  $("#clear-api-key").classList.toggle("hidden",!state.settings.has_api_key);
  $("#request-timeout").value=state.settings.timeout||900;$("#max-tokens").value=state.settings.max_tokens||16384;$("#temperature").value=state.settings.temperature??0.2;
  $("#use-structured").checked=state.settings.structured!==false;
  $("#use-thinking").checked=state.settings.thinking!==false;
  $("#use-auto-glossary").checked=state.settings.auto_glossary!==false;
  $("#entity-search").value="";
  syncThemeChoice();
  loadEntities();
  $("#connection-result").textContent="";$("#settings-dialog").showModal();
}
function settingsPayload(){/* 密钥不再回填，留空即沿用已保存的值 */
  const payload={base_url:$("#base-url").value,model:$("#model-name").value,timeout:Number($("#request-timeout").value),max_tokens:Number($("#max-tokens").value),temperature:Number($("#temperature").value),structured:$("#use-structured").checked,thinking:$("#use-thinking").checked,auto_glossary:$("#use-auto-glossary").checked};
  const key=$("#api-key").value.trim();
  if(key)payload.api_key=key;
  return payload;
}
$("#prev-btn").onclick=$("#prev-edge").onclick=()=>navigate(-1);$("#next-btn").onclick=$("#next-edge").onclick=()=>navigate(1);
$("#stage").addEventListener("click",event=>{
  if(event.detail>1)return;
  if(event.target.closest(".viewer-hud")||event.target.closest(".edge-button"))return;
  if(state.dragMoved>4){state.dragMoved=0;return;}
  toggleHud();
});
$("#hud-help").onclick=()=>$("#help-dialog").showModal();
$("#translation-list").addEventListener("click",async event=>{const retry=event.target.closest("[data-retry]");if(!retry)return;if(await translateIndex(Number(retry.dataset.retry),true)){toast("已插队重试，优先处理");await refreshState();renderTranslation();}});
$("#page-jump").addEventListener("change",event=>{goTo(Number(event.target.value)-1);event.target.blur();});
$("#zoom-in").onclick=()=>zoom(.25);$("#zoom-out").onclick=()=>zoom(-.25);$("#zoom-fit").onclick=()=>setZoom(1);
document.querySelectorAll("[data-mode]").forEach(el=>el.onclick=()=>setMode(el.dataset.mode));
$("#fullscreen-btn").onclick=toggleFullscreen;
$("#tasks-btn").onclick=()=>openTasks();
document.querySelectorAll("[data-theme-opt]").forEach(el=>el.onclick=()=>{state.prefs.theme=el.dataset.themeOpt;savePrefs();applyPrefs();syncThemeChoice();});
if(window.matchMedia){
  const scheme=matchMedia("(prefers-color-scheme: light)");
  const onSchemeChange=()=>{if(state.prefs.theme==="system"){document.documentElement.dataset.theme=resolvedTheme();syncThemeChoice();}};
  if(scheme.addEventListener)scheme.addEventListener("change",onSchemeChange);else if(scheme.addListener)scheme.addListener(onSchemeChange);
}
$("#translate-btn").onclick=translateCurrent;$("#translate-all").onclick=translateAll;
$("#sidebar-btn").onclick=()=>{state.prefs.sidebar=!state.prefs.sidebar;savePrefs();applyPrefs();setTimeout(applySizing,220);};
$("#translation-btn").onclick=()=>{state.prefs.translation=!state.prefs.translation;savePrefs();applyPrefs();setTimeout(applySizing,220);};
$("#help-btn").onclick=()=>$("#help-dialog").showModal();$("#settings-btn").onclick=openSettings;
document.querySelectorAll("[data-close]").forEach(el=>el.onclick=()=>$("#"+el.dataset.close).close());
document.querySelectorAll("dialog.dialog").forEach(dialog=>{
  dialog.addEventListener("click",event=>{
    if(event.target!==dialog)return;
    const box=dialog.getBoundingClientRect();
    if(event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom)dialog.close();
  });
});
document.querySelectorAll("[data-view]").forEach(el=>el.onclick=()=>{state.prefs.view=el.dataset.view;savePrefs();applyPrefs();renderTranslation();});
$("#clear-cache").onclick=async()=>{const targets=visibleIndexes().filter(i=>state.images[i]&&(state.images[i].translated||state.images[i].error));if(!targets.length)return;if(!confirm(targets.length>1?`删除这 ${targets.length} 页的翻译缓存？图片不会受影响。`:"删除本页的翻译缓存？图片不会受影响。"))return;try{for(const index of targets){await api(`/api/page?index=${index}`,{method:"DELETE"});state.pages.delete(index);}await refreshState();renderTranslation();toast("翻译缓存已清除");}catch(error){toast(error.message);}};
function renderEntities(rows){
  state.entities=rows;
  const keyword=($("#entity-search").value||"").trim().toLowerCase();
  const list=$("#entity-list");
  const shown=keyword?rows.filter(row=>`${row.source} ${row.target}`.toLowerCase().includes(keyword)):rows;
  $("#entity-count").textContent=keyword?`${shown.length} / ${rows.length} 条`:`${rows.length} 条`;
  if(!shown.length){list.innerHTML=keyword?'<div class="entity-empty">没有匹配的条目</div>':"";return;}
  list.innerHTML=shown.map(row=>`<div class="entity-row"><span class="entity-src" title="${escapeHTML(row.source)}">${escapeHTML(row.source)}</span><input value="${escapeHTML(row.target)}" data-src="${escapeHTML(row.source)}" spellcheck="false"><span class="entity-tag ${row.manual?"manual":""}">${row.manual?"手动":"自动"}</span><button class="entity-del" data-src="${escapeHTML(row.source)}" title="删除">×</button></div>`).join("");
}
async function loadEntities(){
  try{const data=await api("/api/entities");renderEntities(data.entities||[]);}catch(error){toast(error.message);}
}
async function saveEntity(source,target){
  try{await api("/api/entities",{method:"POST",body:JSON.stringify({source,target})});await loadEntities();toast("已保存");}
  catch(error){toast(error.message);await loadEntities();}
}
function addEntity(){
  const source=$("#entity-source").value.trim(),target=$("#entity-target").value.trim();
  if(!source||!target){toast("原文和译文都要填");return;}
  $("#entity-source").value="";$("#entity-target").value="";$("#entity-search").value="";
  saveEntity(source,target);
}
$("#entity-search").addEventListener("input",()=>renderEntities(state.entities));
$("#entity-add").onclick=addEntity;
$("#entity-source").addEventListener("keydown",event=>{if(event.key==="Enter"){event.preventDefault();$("#entity-target").focus();}});
$("#entity-target").addEventListener("keydown",event=>{if(event.key==="Enter"){event.preventDefault();addEntity();}});
$("#entity-list").addEventListener("change",event=>{
  const input=event.target.closest("input[data-src]");
  if(!input)return;
  const target=input.value.trim();
  if(!target){toast("译文不能为空");loadEntities();return;}
  saveEntity(input.dataset.src,target);
});
$("#entity-list").addEventListener("click",async event=>{
  const del=event.target.closest(".entity-del");
  if(!del)return;
  try{await api(`/api/entities?source=${encodeURIComponent(del.dataset.src)}`,{method:"DELETE"});await loadEntities();toast("已删除");}
  catch(error){toast(error.message);}
});
$("#clear-entities").onclick=async()=>{if(!confirm("清空自动累积的条目？手动添加的会保留。"))return;try{await api("/api/entities",{method:"DELETE"});await loadEntities();toast("已清空自动条目");}catch(error){toast(error.message);}};
$("#save-settings").onclick=async()=>{try{await api("/api/settings",{method:"POST",body:JSON.stringify(settingsPayload())});$("#settings-dialog").close();await refreshState();toast("模型设置已保存");}catch(error){toast(error.message);}};
/* 密钥不回填到输入框，所以“留空=不改”意味着界面上永远清不掉；单给一个清除入口。 */
$("#clear-api-key").onclick=async()=>{
  if(!confirm("清除已保存的 API Key？需要鉴权的服务在重新填写前将无法使用。"))return;
  try{
    await api("/api/settings",{method:"POST",body:JSON.stringify({api_key:""})});
    await refreshState();
    $("#clear-api-key").classList.add("hidden");
    $("#api-key").placeholder="sk-…（本地服务可留空）";
    toast("已清除 API Key");
  }catch(error){toast(error.message);}
};
$("#fetch-models").onclick=async()=>{const button=$("#fetch-models");button.disabled=true;button.textContent="读取中…";try{const data=await api("/api/models",{method:"POST",body:JSON.stringify(settingsPayload())});$("#models").innerHTML=data.models.map(x=>`<option value="${escapeHTML(x)}">`).join("");$("#model-help").textContent=`发现 ${data.models.length} 个可对话模型，可从输入框下拉列表选择（嵌入类模型已过滤）。`;if(data.models.length&&!data.models.includes($("#model-name").value))$("#model-name").value=data.models[0];}catch(error){toast(error.message);}finally{button.disabled=false;button.textContent="读取模型";}};
$("#test-connection").onclick=async()=>{const button=$("#test-connection"),result=$("#connection-result");button.disabled=true;result.textContent="测试中…";try{const data=await api("/api/test",{method:"POST",body:JSON.stringify(settingsPayload())});result.textContent=`连接正常 · ${data.latency}s · ${data.reply}`;}catch(error){result.textContent="失败："+error.message;}finally{button.disabled=false;}};
document.addEventListener("keydown",event=>{
  if(document.querySelector("dialog[open]"))return;
  const target=event.target,tag=target.tagName;
  if(tag==="TEXTAREA"||(tag==="INPUT"&&target.id!=="page-jump"))return;
  const key=event.key,navigationKeys=["ArrowLeft","ArrowRight","ArrowUp","ArrowDown","PageUp","PageDown","Home","End"," "];
  if(navigationKeys.includes(key)){event.preventDefault();if(["INPUT","SELECT","BUTTON"].includes(tag))target.blur();}
  if(key==="ArrowLeft"||key==="ArrowUp"||key==="PageUp")navigate(-1);
  else if(key==="ArrowRight"||key==="ArrowDown"||key==="PageDown"||key===" ")navigate(1);
  else if(key==="Home")goTo(0);else if(key==="End")goTo(state.images.length-1);
  else if(key==="+"||key==="=")zoom(.25);else if(key==="-"||key==="_")zoom(-.25);else if(key==="0")setZoom(1);
  else if(key.toLowerCase()==="t"||key.toLowerCase()==="r")translateCurrent();
  else if(key.toLowerCase()==="b")setMode(state.prefs.mode==="double"?"single":"double");
  else if(key.toLowerCase()==="f")toggleFullscreen();else if(key.toLowerCase()==="s")$("#sidebar-btn").click();else if(key.toLowerCase()==="a")$("#translation-btn").click();else if(key==="?")$("#help-dialog").showModal();
});
let resizeTimer;addEventListener("resize",()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{renderReader();updateHeader();renderTranslation();},120);});
$("#viewer").addEventListener("wheel",event=>{
  if(event.ctrlKey||event.metaKey){event.preventDefault();zoom(event.deltaY<0?.1:-.1);return;}
  if(state.prefs.mode!=="continuous"&&state.prefs.zoom<=1.001){event.preventDefault();const now=Date.now();if(Math.abs(event.deltaY)>18&&now-(state.wheelAt||0)>260){state.wheelAt=now;navigate(event.deltaY>0?1:-1);}}
},{passive:false});
$("#viewer").addEventListener("dblclick",event=>{if(state.prefs.mode!=="continuous"){event.preventDefault();setZoom(state.prefs.zoom>1.001?1:2,true);}});
$("#viewer").addEventListener("mousedown",event=>{state.dragMoved=0;if(state.prefs.zoom<=1.001||state.prefs.mode==="continuous"||event.button!==0)return;const viewer=$("#viewer");state.drag={x:event.clientX,y:event.clientY,left:viewer.scrollLeft,top:viewer.scrollTop};$("#pages").classList.add("dragging");event.preventDefault();});
addEventListener("mousemove",event=>{if(!state.drag)return;const viewer=$("#viewer");const dx=event.clientX-state.drag.x,dy=event.clientY-state.drag.y;state.dragMoved=Math.max(state.dragMoved,Math.abs(dx)+Math.abs(dy));viewer.scrollLeft=state.drag.left-dx;viewer.scrollTop=state.drag.top-dy;});
addEventListener("mouseup",()=>{state.drag=null;$("#pages").classList.remove("dragging");});
$("#viewer").addEventListener("touchstart",event=>{state.touchX=event.changedTouches[0].clientX;},{passive:true});
$("#viewer").addEventListener("touchend",event=>{if(state.touchX===null)return;const delta=event.changedTouches[0].clientX-state.touchX;state.touchX=null;if(Math.abs(delta)>60)navigate(delta>0?-1:1);},{passive:true});
let scrollTimer;$("#viewer").addEventListener("scroll",()=>{if(state.prefs.mode!=="continuous")return;clearTimeout(scrollTimer);scrollTimer=setTimeout(()=>{const top=$("#viewer").getBoundingClientRect().top;let nearest=state.current,distance=Infinity;document.querySelectorAll(".page-frame").forEach(frame=>{const d=Math.abs(frame.getBoundingClientRect().top-top);if(d<distance){distance=d;nearest=Number(frame.dataset.index);}});if(nearest!==state.current)goTo(nearest,false);},100);},{passive:true});
async function init(){
  applyPrefs();try{const data=await api("/api/state");state.images=data.images;state.folder=data.folder||"";state.settings=data.settings;state.worker=data.worker;if(!state.images.length){$("#status").textContent="当前目录没有支持的图片";return;}
    const stored=Number(storeGet(prefKey+":page")||0);
    const remembered=Number.isFinite(stored)?stored:0;
    state.current=Math.max(0,Math.min(state.images.length-1,remembered));renderThumbnails();renderReader();updateHeader();await renderTranslation();await refreshState();
    if(!storeGet(prefKey+":hud-tip")){storeSet(prefKey+":hud-tip","1");setTimeout(()=>toast("单击页面可显示工具条与快捷键说明"),1000);}
  }catch(error){$("#status").textContent=error.message;toast(error.message);}
}
init();
</script>
</body>
</html>
"""


def render_page() -> tuple[bytes, str]:
    """每次访问生成一次性 nonce 并写进内联 <script>。

    配合 CSP，即便将来某处漏了 escapeHTML，注入的 <script> 或 onerror= 也执行不了；
    style 仍放行 inline，因为布局依赖行内 style 属性与 element.style 赋值。
    """
    nonce = secrets.token_urlsafe(16)
    html = PAGE_HTML.replace("<style>", f'<style nonce="{nonce}">', 1)
    html = html.replace("<script>", f'<script nonce="{nonce}">', 1)
    return html.encode("utf-8"), nonce


def csp_header(nonce: str) -> str:
    return (
        "default-src 'none'; "
        f"script-src 'nonce-{nonce}'; "
        "style-src 'unsafe-inline'; "
        "img-src 'self'; connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )


class MangaServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:
        """外部设备关网页/切网络时会抛连接重置，属正常断连，不该刷屏。"""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError,
                            ConnectionAbortedError, TimeoutError)):
            return
        super().handle_error(request, client_address)

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], root: Path, store: DataStore):
        self.root = root
        self.store = store
        self.images = sorted(
            (path for path in root.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS),
            key=lambda path: natural_key(path.name),
        )
        self.names = [path.name for path in self.images]
        # 先完成 bind + listen：端口被占用时直接抛错，不会留下已经启动的翻译线程
        super().__init__(address, handler)
        self.worker = TranslationWorker(self.images, self.names, store)


def natural_key(text: str) -> list[Any]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", text)]


class RequestHandler(BaseHTTPRequestHandler):
    server: MangaServer
    protocol_version = "HTTP/1.1"
    timeout = 30   # 单连接读写上限：keep-alive 下长时间无数据就断开，避免半开连接堆积线程
    # 默认的 Server 头会把 Python 版本报出去，等于免费给扫描器做指纹识别
    server_version = "MangaReader/" + APP_VERSION
    sys_version = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        """保持终端安静，仅保留启动信息和翻译任务结果。"""
        return

    def version_string(self) -> str:
        return self.server_version     # 默认实现会拼上 sys_version，留下一个多余空格

    def secure_headers(self) -> None:
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)

    def send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.secure_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message: str, status: int = 400) -> None:
        self.send_json({"error": message}, status)

    @staticmethod
    def bare_host(value: str) -> str:
        """取裸主机名：去掉端口和 IPv6 方括号。裸 IPv6 本身含冒号，不能按冒号切。"""
        value = (value or "").strip().lower()
        if value.startswith("["):
            end = value.find("]")
            return value[1:end] if end > 0 else value
        if value.count(":") == 1:
            return value.rsplit(":", 1)[0]
        return value

    def host_allowed(self) -> bool:
        """只接受 IP 字面量与本机名，挡住 DNS rebinding（攻击者域名解析到 127.0.0.1）。"""
        host = self.bare_host(self.headers.get("Host") or "")
        if not host:
            return False
        if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
            return True
        if host.endswith(".local"):                    # mDNS 名，局域网访问常用
            return True
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return False

    def guard(self) -> bool:
        """跨站写入拦截：校验 Host 与 Origin。返回 False 表示已回 403。"""
        if not self.host_allowed():
            self.send_error_json("主机名不被允许", 403)
            return False
        origin = self.headers.get("Origin")
        if origin:
            if origin.strip().lower() == "null":      # sandbox iframe / 本地文件常发这个
                self.send_error_json("已拒绝跨站请求", 403)
                return False
            request_host = self.bare_host(self.headers.get("Host") or "")
            try:
                origin_host = self.bare_host(urlparse(origin).netloc)
            except ValueError:
                origin_host = ""
            if origin_host and origin_host != request_host:
                self.send_error_json("已拒绝跨站请求", 403)
                return False
        return True

    def read_json(self) -> dict[str, Any]:
        # 只认 application/json：浏览器跨站发起的简单请求设不了这个头，
        # 因此这一步同时挡住了 CSRF（否则一段 text/plain 的 POST 就能改你的模型配置）。
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("请求必须使用 Content-Type: application/json")
        try:
            length = max(0, min(int(self.headers.get("Content-Length", "0")), 1_000_000))
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求正文必须是有效的 JSON 对象") from exc

    def do_GET(self) -> None:
        try:
            if not self.guard():
                return
            parsed = urlparse(self.path)
            if parsed.path == "/":
                body, nonce = render_page()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Security-Policy", csp_header(nonce))
                self.secure_headers()
                self.end_headers()
                self.wfile.write(body)
            elif parsed.path == "/api/state":
                self.send_json(self.state_payload())
            elif parsed.path == "/api/page":
                index = self.query_index(parsed)
                entry = self.server.store.page(self.server.names[index])
                entry.update({"index": index, "name": self.server.names[index]})
                self.send_json(entry)
            elif parsed.path.startswith("/api/image/"):
                self.serve_image(int(parsed.path.rsplit("/", 1)[1]))
            elif parsed.path == "/api/tasks":
                self.send_json(self.server.worker.tasks())
            elif parsed.path == "/api/entities":
                self.send_json({"entities": self.server.store.entity_list()})
            elif parsed.path == "/api/export":
                snapshot = self.server.store.snapshot()
                if isinstance(snapshot.get("llm"), dict):
                    snapshot["llm"]["api_key"] = ""   # 备份不含密钥，恢复后请重新填写
                body = json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="manga-data-v2.json"')
                self.send_header("Content-Length", str(len(body)))
                self.secure_headers()
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error_json("接口不存在", 404)
        except (ValueError, IndexError) as exc:
            self.send_error_json(str(exc) or "无效页面", 400)
        except Exception as exc:
            # 把内部异常原样回给浏览器等于白送一份堆栈信息；细节留在终端
            print(f"[错误] {self.path}：{exc}", flush=True)
            self.send_error_json("服务器内部错误：" + str(exc)[:300], 500)

    def do_POST(self) -> None:
        try:
            if not self.guard():
                return
            payload = self.read_json()
            path = urlparse(self.path).path
            if path == "/api/translate":
                if payload.get("all"):
                    indexes = list(range(len(self.server.images)))
                else:
                    raw_index = payload.get("index")
                    if raw_index is None or isinstance(raw_index, bool):
                        raise ValueError("缺少页码")
                    indexes = [int(raw_index)]
                    if not 0 <= indexes[0] < len(self.server.images):
                        raise ValueError("页码超出范围")
                added = self.server.worker.enqueue(
                    indexes, bool(payload.get("force")), bool(payload.get("front"))
                )
                self.send_json({"added": added}, 202)
            elif path == "/api/settings":
                settings = self.server.store.update_settings(payload)
                print(
                    f"[设置] 已保存 · {settings['base_url']} · {settings['model']}"
                    f" · 输出上限 {settings['max_tokens']} · 超时 {settings['timeout']}s"
                    f" · 思考{'开' if settings.get('thinking') else '关'}"
                    f" · 结构化{'开' if settings.get('structured') else '关'}"
                    f" · 自动术语{'开' if settings.get('auto_glossary') else '关'}",
                    flush=True,
                )
                self.send_json({"ok": True, "settings": self.public_settings()})
            elif path == "/api/models":
                current = self.server.store.settings()
                base_url = payload.get("base_url")
                api_key = payload.get("api_key")
                models = self.server.worker.client.list_models(
                    str(base_url) if base_url else current["base_url"],
                    str(api_key) if api_key is not None else current.get("api_key", ""),
                )
                self.send_json({"models": models})
            elif path in {"/api/tasks/cancel", "/api/tasks/dismiss"}:
                worker = self.server.worker
                canceling = path.endswith("cancel")
                if payload.get("all"):
                    removed = worker.cancel_all() if canceling else worker.dismiss_all()
                    self.send_json({"ok": True, "removed": removed})
                else:
                    raw_index = payload.get("index")
                    if raw_index is None or isinstance(raw_index, bool):
                        raise ValueError("缺少页码")
                    index = int(raw_index)
                    if canceling:
                        result = worker.cancel(index)
                        if result == "none":
                            raise ValueError("该页不在任务队列中")
                        self.send_json({"ok": True, "result": result})
                    else:
                        if not worker.dismiss(index):
                            raise ValueError("该页没有可删除的任务记录")
                        self.send_json({"ok": True})
            elif path == "/api/entities":
                entry = self.server.store.upsert_entity(
                    str(payload.get("source") or ""),
                    str(payload.get("target") or ""),
                    str(payload.get("kind") or ""),
                )
                self.send_json({"ok": True, "entity": entry, "count": self.server.store.entity_count()})
            elif path == "/api/test":
                # 走与保存相同的校验：脏值（列表、字符串数字）在这里就被挡住，
                # 不会悄悄进到 URL 里变成一句看不懂的 500
                probe = merge_settings(payload, self.server.store.settings())
                probe["timeout"] = max(5, min(int(probe["timeout"]), 60))
                self.send_json(self.server.worker.client.test_connection(probe))
            else:
                self.send_error_json("接口不存在", 404)
        except (ValueError, TypeError) as exc:
            self.send_error_json(str(exc), 400)
        except Exception as exc:
            print(f"[错误] {self.path}：{exc}", flush=True)
            self.send_error_json("服务器内部错误：" + str(exc)[:300], 500)

    def do_DELETE(self) -> None:
        try:
            if not self.guard():
                return
            parsed = urlparse(self.path)
            if parsed.path == "/api/entities":
                # 带 source 是删单条；完全不带 source 才是“清空自动条目”。
                # 用 get("source") or "" 判断，`?source=` 这种手抖会变成一次全清。
                params = parse_qs(parsed.query, keep_blank_values=True)
                if "source" in params:
                    source = params["source"][0]
                    if not source.strip():
                        raise ValueError("source 不能为空")
                    removed = int(self.server.store.delete_entity(source))
                else:
                    removed = self.server.store.clear_entities()
                self.send_json({"ok": True, "removed": removed, "count": self.server.store.entity_count()})
                return
            if parsed.path != "/api/page":
                self.send_error_json("接口不存在", 404)
                return
            index = self.query_index(parsed)
            if self.server.worker.is_queued(index):
                raise ValueError("本页正在处理或排队，暂时不能清除缓存")
            self.server.store.clear_page(self.server.names[index])
            self.send_json({"ok": True})
        except (ValueError, IndexError) as exc:
            self.send_error_json(str(exc), 400)
        except Exception as exc:
            print(f"[错误] {self.path}：{exc}", flush=True)
            self.send_error_json("服务器内部错误：" + str(exc)[:300], 500)

    def query_index(self, parsed: Any) -> int:
        values = parse_qs(parsed.query).get("index", [])
        if not values:
            raise ValueError("缺少页面索引")
        index = int(values[0])
        if not 0 <= index < len(self.server.images):
            raise ValueError("页码超出范围")
        return index

    def public_settings(self) -> dict[str, Any]:
        """api_key 不下发给浏览器：只告知“是否已设置”。"""
        settings = self.server.store.settings()
        settings["has_api_key"] = bool(str(settings.get("api_key") or "").strip())
        settings["api_key"] = ""
        return settings

    def state_payload(self) -> dict[str, Any]:
        names = self.server.names
        statuses = self.server.store.page_statuses(names)
        busy = self.server.worker.busy_indexes()
        images = [
            {
                "name": name,
                "translated": status["translated"],
                "count": status["count"],
                "busy": index in busy,
                "error": status["error"],
            }
            for index, (name, status) in enumerate(zip(names, statuses))
        ]
        return {
            "version": APP_VERSION,
            "folder": self.server.root.name or str(self.server.root),
            "images": images,
            "settings": self.public_settings(),
            "worker": self.server.worker.status(),
            "entity_count": self.server.store.entity_count(),
        }

    def serve_image(self, index: int) -> None:
        if not 0 <= index < len(self.server.images):
            self.send_error_json("图片不存在", 404)
            return
        path = self.server.images[index]
        try:
            stat = path.stat()
        except OSError:                      # 服务运行期间文件被删/改名，别当成 500
            self.send_error_json("图片已被移除", 404)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(stat.st_size))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.secure_headers()
        self.end_headers()
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(256 * 1024):
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            # 快速翻页或关标签会掐断连接。响应头已经发出去了，这时再写一份错误 JSON
            # 只会污染这条连接，直接标记关闭即可。
            self.close_connection = True


def batch_translate(store: DataStore, root: Path, force: bool = False) -> tuple[int, int]:
    """命令行静默批量翻译：按自然顺序逐页处理，结果与网页端共用同一份缓存。"""
    images = sorted(
        (path for path in root.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS),
        key=lambda path: natural_key(path.name),
    )
    if not images:
        print(f"目录中没有图片（支持 {', '.join(sorted(IMAGE_EXTENSIONS))}）：{root}")
        return 0, 0
    names = [path.name for path in images]
    todo = [index for index, name in enumerate(names) if force or not store.has_results(name)]
    if not todo:
        print("所有页面都已有译文。加 --force 可以全部重译。")
        return 0, 0

    settings = store.settings()
    print(f"模型 {settings['model']} @ {settings['base_url']}")
    print(f"待处理 {len(todo)}/{len(images)} 页，Ctrl+C 可中断（已完成的会写入缓存）。", flush=True)

    client = LLMClient(store)
    done = failed = 0
    for sequence, index in enumerate(todo, 1):
        name = names[index]
        print(f"[{sequence}/{len(todo)}] {name}", flush=True)
        started = time.monotonic()
        try:
            context = build_context(store, names, index)
            results, model, entities = client.translate(images[index], context)
        except KeyboardInterrupt:
            print("\n已中断。")
            break
        except Exception as exc:
            failed += 1
            store.save_error(name, str(exc))
            print(f"    失败：{exc}", flush=True)
            continue
        store.save_result(name, results, model)
        added = store.merge_entities(entities)
        done += 1
        note = f"，新增译名 {added}" if added else ""
        print(f"    {len(results)} 条{note}，{time.monotonic() - started:.1f}s", flush=True)

    print(f"完成：成功 {done} 页，失败 {failed} 页。")
    return done, failed


def lock_path(root: Path) -> Path:
    return root / LOCK_NAME


def running_instance(root: Path) -> dict[str, Any] | None:
    """探测同一目录是否已有实例在运行。

    两个进程各持一份内存缓存写同一个 data.json，后写的会把先写的成果整个覆盖掉；
    而端口被占时程序会自动顺延端口，很容易在无意间开出第二个实例。
    这里先读标记文件，再用 HTTP 确认对方真的活着（避免进程被强杀后留下的死标记把启动卡住）。
    """
    try:
        info = json.loads(lock_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    url = str(info.get("url") or "")
    parsed = urlparse(url)
    if parsed.scheme != "http" or not parsed.netloc:
        return None
    request = Request(url.rstrip("/") + "/api/state", headers={"Host": parsed.netloc})
    try:
        with open_request(request, 1.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict) or "version" not in payload:
        return None
    # 目录名对得上才算，别的程序恰好也监听这个端口时不要误判
    if str(payload.get("folder", "")) != (root.name or str(root)):
        return None
    return info


def write_lock(root: Path, url: str) -> None:
    try:
        lock_path(root).write_text(
            json.dumps({"pid": os.getpid(), "url": url, "started": utc_now()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass          # 目录只读也不能因此起不来，标记只是尽力而为


def remove_lock(root: Path) -> None:
    try:
        lock_path(root).unlink()
    except OSError:
        pass


def create_server(host: str, preferred: int, root: Path, store: DataStore, attempts: int = 20) -> MangaServer:
    """从 preferred 开始顺延，直到真正 bind + listen 成功。

    不能"先探测端口、关掉、再重新绑"：套接字一旦 bind 失败，macOS 会把它停在半绑定状态，
    同一个 socket 再 bind(0) 依旧报 EADDRINUSE，最终表现为端口被占用时程序直接退出。
    """
    last_error: OSError | None = None
    for offset in range(attempts):
        try:
            return MangaServer((host, preferred + offset), RequestHandler, root, store)
        except OSError as exc:
            if exc.errno not in (errno.EADDRINUSE, errno.EACCES):
                raise
            last_error = exc
    raise last_error or OSError(f"{preferred}~{preferred + attempts - 1} 范围内没有可用端口")


def main() -> None:
    parser = argparse.ArgumentParser(description="零依赖本地漫画阅读器（VLM OCR + 中文翻译）")
    parser.add_argument("--dir", type=Path, default=Path(__file__).resolve().parent, help="漫画图片目录")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机可访问")
    parser.add_argument("--port", type=int, default=8765, help="首选端口，占用时自动更换")
    parser.add_argument("--url", default=DEFAULT_BASE_URL, help="OpenAI 兼容模型服务根地址")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="视觉模型名称")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--translate-all", action="store_true", help="不启动服务，把目录内未翻译的页面全部翻译后退出")
    parser.add_argument("--force", action="store_true", help="配合 --translate-all，忽略已有译文全部重译")
    parser.add_argument("--allow-multi", action="store_true", help="允许同一目录并行运行多个实例（会互相覆盖 data.json）")
    args = parser.parse_args()

    root = args.dir.expanduser().resolve()
    if not root.is_dir():
        parser.error(f"目录不存在：{root}")
    if not 1 <= args.port <= 65535:
        parser.error("端口应在 1～65535 之间")
    if not args.allow_multi:
        other = running_instance(root)
        if other:
            parser.error(
                f"该目录已有实例在运行：{other.get('url')}（pid {other.get('pid')}）\n"
                "两个进程会互相覆盖 data.json，本次启动已取消。\n"
                "请先关闭旧实例；确实要并行运行请加 --allow-multi。"
            )
    try:
        store = DataStore(root / "data.json", clean_base_url(args.url), args.model)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
        return

    if args.translate_all:
        done, failed = batch_translate(store, root, args.force)
        sys.exit(0 if failed == 0 else 1)

    try:
        server = create_server(args.host, args.port, root, store)
    except OSError as exc:
        parser.error(str(exc))
        return
    port = server.server_address[1]
    if not server.images:
        parser.error(f"目录中没有图片（支持 {', '.join(sorted(IMAGE_EXTENSIONS))}）：{root}")

    # 监听 0.0.0.0 时把 127.0.0.1 打印出来：那才是本机能直接打开的地址
    display_host = "127.0.0.1" if args.host in {"0.0.0.0", "::", ""} else args.host
    if ":" in display_host and not display_host.startswith("["):
        display_host = f"[{display_host}]"
    url = f"http://{display_host}:{port}"
    write_lock(root, url)
    print("=" * 58)
    print(f"Manga Reader {APP_VERSION} · {len(server.images)} 页")
    print(f"阅读地址：{url}")
    print(f"图片目录：{root}")
    print(f"模型服务：{store.settings()['base_url']} · {store.settings()['model']}")
    print("翻译任务在网页里添加（翻译本页 / 批量翻译）；")
    print("也可以用 --translate-all 静默翻译整个目录。按 Ctrl+C 停止服务。")
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print(f"注意：已监听 {args.host}，同一网络下的任何设备都能读取译文、修改设置、")
        print("      并消耗你的模型算力。仅在可信网络中这样使用。")
    print("=" * 58, flush=True)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        server.server_close()
        remove_lock(root)


if __name__ == "__main__":
    main()
