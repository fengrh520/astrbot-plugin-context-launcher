"""上下文管家（Context Launcher）v2.0.0

把「上下文为什么贵」变成可读的归因，并在你明确允许时做**确定性**压缩。

三件事：
1. 看：每次请求的 system_prompt / 工具定义 / 每条历史 / 尾部注入都算指纹，和同会话
   上一轮比对，第一个差异点归因到 9 类里的某一类；
2. 算：用 provider 返回的真实 usage（prompt_cache_hit_tokens / miss_tokens）算命中率，
   并把「断点轮」和「干净轮」的未命中分开对账——这是判断"改写历史值不值"的唯一硬指标；
3. 压（可选，默认关）：工具结果裁成一行、超长消息保留首尾，都是确定性变换，
   同一条消息每轮得到同一结果，不会自己制造断点。

刻意不做的事：
- 不注册任何 LLM 工具（工具定义每轮进 prompt，是常驻成本）；
- 不自己做 LLM 摘要压缩（AstrBot 核心的 overflow 压缩已经做了，而且做摘要很容易
  每轮生成不同文本，反而把缓存全打断）；
- 不碰别的插件的数据，检测到同类插件（context_slim）自动让路。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import Counter, OrderedDict, deque

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:  # 官方推荐写法；旧版本降级为返回 dict
    from astrbot.api.web import json_response as _json_response
except Exception:  # noqa: BLE001
    _json_response = None

try:  # 官方插件数据目录
    from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path as _get_data_root
except Exception:  # noqa: BLE001
    _get_data_root = None

try:
    from .core.compress import CompressConfig, ContextCompressor, content_text
except Exception:  # noqa: BLE001
    from core.compress import CompressConfig, ContextCompressor, content_text  # type: ignore

PLUGIN_NAME = "astrbot_plugin_context_launcher"
PLUGIN_VERSION = "2.0.0"

# 负数优先级 = 最后执行，看到所有插件注入完的最终 payload，不和别人抢顺序
HOOK_PRIORITY = -20

MAX_LOG_BYTES = 8 * 1024 * 1024
MAX_SESSIONS = 200
RECENT_MAX = 400
PENDING_MAX = 8

BREAK_CAUSES = ("system", "tools", "history", "history_tool", "history_shrink")

CAUSE_LABEL = {
    "first": "首轮（无前序）",
    "append": "只追加（理想）",
    "tail": "仅尾部变化（正常）",
    "system": "系统提示变化",
    "tools": "工具定义变化",
    "history": "历史被改写",
    "history_tool": "工具结果被改写",
    "history_shrink": "历史变短（窗口回退）",
    "compress": "本插件压缩（预期内）",
}


def _data_dir() -> str:
    """插件自己的数据目录，尽量用官方 API，失败则回退。"""
    try:
        if _get_data_root is not None:
            root = _get_data_root()
            if root:
                path = os.path.join(str(root), PLUGIN_NAME)
                os.makedirs(path, exist_ok=True)
                return path
    except Exception:  # noqa: BLE001
        pass
    for candidate in ("/AstrBot/data/plugin_data", os.path.join(os.getcwd(), "data", "plugin_data")):
        try:
            path = os.path.join(candidate, PLUGIN_NAME)
            os.makedirs(path, exist_ok=True)
            return path
        except Exception:  # noqa: BLE001
            continue
    return os.getcwd()


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:12]


def _msg_blob(msg) -> str:
    """整条消息的稳定序列化（含 tool_calls），用于指纹。"""
    if isinstance(msg, dict):
        data = msg
    else:
        data = None
        if hasattr(msg, "model_dump"):
            try:
                data = msg.model_dump()
            except Exception:  # noqa: BLE001
                data = None
        if data is None:
            data = {
                "role": getattr(msg, "role", None),
                "content": getattr(msg, "content", None),
                "tool_calls": getattr(msg, "tool_calls", None),
                "tool_call_id": getattr(msg, "tool_call_id", None),
                "name": getattr(msg, "name", None),
            }
    return json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)


def _role_of(msg) -> str:
    if isinstance(msg, dict):
        return str(msg.get("role") or "")
    return str(getattr(msg, "role", "") or "")


def _raw_content(msg):
    if isinstance(msg, dict):
        return msg.get("content")
    return getattr(msg, "content", None)


def _count_cjk(text: str) -> tuple[int, int]:
    cjk = 0
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            cjk += 1
    return cjk, len(text) - cjk


def _tokens_from(cjk: int, other: int) -> int:
    return int(cjk * 0.6 + other * 0.3)


def estimate_tokens(text: str) -> int:
    cjk, other = _count_cjk(text)
    return _tokens_from(cjk, other)


def _part_text(part) -> str:
    text = None
    if isinstance(part, dict):
        text = part.get("text")
    else:
        text = getattr(part, "text", None)
    if text is None:
        text = part if isinstance(part, str) else str(part)
    return text if isinstance(text, str) else str(text)


def _fmt_int(value) -> str:
    try:
        return f"{int(value):,}"
    except Exception:  # noqa: BLE001
        return str(value)


@register(
    PLUGIN_NAME,
    "local",
    "上下文管家：前缀缓存归因 + 可选确定性压缩 + 内置视图（不注册工具、默认只读）",
    PLUGIN_VERSION,
)
class ContextLauncher(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        cfg = config or {}

        self.enabled = bool(cfg.get("enabled", True))
        self.mode = str(cfg.get("mode", "observe") or "observe").strip().lower()
        if self.mode not in ("observe", "guard"):
            self.mode = "observe"

        self.max_tail_chars = max(0, int(cfg.get("max_tail_chars", 1800) or 0))
        self.max_context_chars = max(0, int(cfg.get("max_context_chars", 0) or 0))
        self.log_events = bool(cfg.get("log_events", True))
        self.warn_on_break = bool(cfg.get("warn_on_break", True))
        self.defer_to = [
            str(x).strip() for x in (cfg.get("defer_to") or ["astrbot_plugin_context_slim"]) if str(x).strip()
        ]

        data_dir = _data_dir()
        self.data_dir = data_dir
        self.archive_dir = os.path.join(data_dir, "archive")
        self.events_path = os.path.join(data_dir, "events.jsonl")

        compress_cfg = CompressConfig(
            tool_trim=bool(cfg.get("compress_tool_results", False)),
            tool_keep_recent=max(0, int(cfg.get("compress_tool_keep_recent", 2) or 0)),
            tool_preview_chars=max(20, int(cfg.get("compress_tool_preview_chars", 120) or 120)),
            oversize_trim=bool(cfg.get("compress_oversize", False)),
            max_msg_chars=max(0, int(cfg.get("compress_max_msg_chars", 12000) or 0)),
            keep_head_chars=max(200, int(cfg.get("compress_keep_head_chars", 4000) or 4000)),
            keep_tail_chars=max(0, int(cfg.get("compress_keep_tail_chars", 1500) or 0)),
            keep_recent_msgs=max(0, int(cfg.get("compress_keep_recent_msgs", 2) or 0)),
            archive=bool(cfg.get("compress_archive", True)),
            archive_dir=self.archive_dir,
        )
        # observe 模式下强制关掉压缩，保证"默认一个字节都不改"
        if self.mode == "observe":
            compress_cfg.tool_trim = False
            compress_cfg.oversize_trim = False
        self.compressor = ContextCompressor(compress_cfg, logger=logger)

        self._prev: OrderedDict[str, dict] = OrderedDict()
        self._pending: dict[str, deque] = {}
        self._recent: deque = deque(maxlen=RECENT_MAX)
        self._last_budget_warn = 0.0
        self._last_break_warn = 0.0
        self._conflict_ts = 0.0
        self._conflicts = self._detect_conflicts()

        self.stat = {
            "since": time.time(),
            "req": 0,
            "prompt": 0,
            "hit": 0,
            "miss": 0,
            "out": 0,
            "causes": Counter(),
            "breaks": 0,
            "tail_changes": 0,
            "clean": 0,
            "budget_warns": 0,
            "miss_break": 0,
            "n_break": 0,
            "miss_clean": 0,
            "n_clean": 0,
            "cache_reported": 0,
        }
        self.compress_stat = {
            "tool_compressed": 0,
            "oversize_trimmed": 0,
            "chars_saved": 0,
        }

        logger.info(
            f"[context_launcher] v{PLUGIN_VERSION} 已加载：mode={self.mode}, priority={HOOK_PRIORITY}, "
            f"尾部预算={self.max_tail_chars or '关'}, 压缩={self._compress_label()}, "
            f"数据目录={data_dir}"
        )

    # ------------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        try:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/summary", self._api_summary, ["GET"], "上下文管家：汇总"
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/events", self._api_events, ["GET"], "上下文管家：最近请求"
            )
            logger.info(f"[context_launcher] Web API 就绪：summary / events")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[context_launcher] Web API 注册跳过: {exc}")

    async def terminate(self) -> None:
        logger.info(f"[context_launcher] 已卸载，本次记录 {self.stat['req']} 次请求")

    # ------------------------------------------------------------------ 小工具

    def _compress_label(self) -> str:
        cfg = self.compressor.config
        parts = []
        if cfg.tool_trim:
            parts.append(f"工具结果(留{cfg.tool_keep_recent})")
        if cfg.oversize_trim:
            parts.append(f"超长消息(>{cfg.max_msg_chars}字)")
        return "、".join(parts) if parts else "关"

    def _detect_conflicts(self) -> list[str]:
        found: list[str] = []
        try:
            for star in self.context.get_all_stars() or []:
                name = getattr(star, "name", "") or ""
                if name and name in self.defer_to:
                    found.append(name)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[context_launcher] 冲突检测失败: {exc}")
        return found

    def _defer_active(self) -> list[str]:
        now = time.time()
        if now - self._conflict_ts < 120:
            return self._conflicts
        self._conflict_ts = now
        self._conflicts = self._detect_conflicts()
        return self._conflicts

    def _ok(self, payload):
        if _json_response is not None:
            try:
                return _json_response(payload)
            except Exception:  # noqa: BLE001
                pass
        return payload

    # ------------------------------------------------------------------ 指纹与归因

    def _fingerprint(self, req) -> dict:
        sys_prompt = getattr(req, "system_prompt", "") or ""
        if not isinstance(sys_prompt, str):
            sys_prompt = str(sys_prompt)

        tool_mgr = getattr(req, "func_tool", None)
        tools = getattr(tool_mgr, "tools", None) if tool_mgr is not None else None
        tools_list = list(tools or [])
        names = []
        for tool in tools_list:
            try:
                names.append(str(getattr(tool, "name", "") or ""))
            except Exception:  # noqa: BLE001
                names.append("?")
        tools_blob = json.dumps(
            [t.model_dump() if hasattr(t, "model_dump") else str(t) for t in tools_list],
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

        msgs = list(getattr(req, "contexts", None) or [])
        frames = []
        heads = []
        ctx_chars = 0
        ctx_cjk = 0
        ctx_other = 0
        for msg in msgs:
            text = content_text(_raw_content(msg))
            cjk, other = _count_cjk(text)
            ctx_cjk += cjk
            ctx_other += other
            ctx_chars += len(text)
            frames.append([_role_of(msg), _sha(_msg_blob(msg)), len(text)])
            heads.append(text[:120])

        parts = list(getattr(req, "extra_user_content_parts", None) or [])
        tail_texts = [_part_text(p) for p in parts]
        tail_joined = "\u0000".join(tail_texts)

        return {
            "sys_h": _sha(sys_prompt),
            "sys_len": len(sys_prompt),
            "tools_h": _sha(tools_blob),
            "tools_len": len(tools_blob),
            "tools_n": len(names),
            "n": len(frames),
            "fps": frames,
            "ctx_chars": ctx_chars,
            "tail_h": _sha(tail_joined),
            "tail_n": len(tail_texts),
            "tail_chars": sum(len(t) for t in tail_texts),
            "est_tokens": estimate_tokens(sys_prompt)
            + estimate_tokens(tools_blob)
            + estimate_tokens(tail_joined)
            + _tokens_from(ctx_cjk, ctx_other),
            "heads": heads,
        }

    @staticmethod
    def _compare(prev: dict, cur: dict) -> dict:
        if not prev:
            return {"cause": "first", "split": None, "detail": ""}

        if cur["sys_h"] != prev["sys_h"]:
            return {
                "cause": "system",
                "split": None,
                "detail": f"system {prev['sys_len']}→{cur['sys_len']} 字",
            }
        if cur["tools_h"] != prev["tools_h"]:
            return {
                "cause": "tools",
                "split": None,
                "detail": f"tools {prev['tools_n']}→{cur['tools_n']} 个",
            }

        pf, cf = prev["fps"], cur["fps"]
        split = None
        for idx in range(min(len(pf), len(cf))):
            if pf[idx][:2] != cf[idx][:2]:
                split = idx
                break

        if split is not None:
            role = (cf[split][0] if split < len(cf) else pf[split][0]) or "?"
            cause = "history_tool" if role == "tool" else "history"
            return {"cause": cause, "split": split, "detail": f"第 {split} 条（role={role}）不同"}

        if len(cf) < len(pf):
            return {
                "cause": "history_shrink",
                "split": len(cf),
                "detail": f"历史 {len(pf)}→{len(cf)} 条",
            }
        if len(cf) > len(pf):
            return {"cause": "append", "split": None, "detail": f"+{len(cf) - len(pf)} 条"}
        if cur["tail_h"] != prev["tail_h"]:
            return {"cause": "tail", "split": None, "detail": "尾部注入变化"}
        return {"cause": "append", "split": None, "detail": "无变化"}

    # ------------------------------------------------------------------ 请求钩子

    @filter.on_llm_request(priority=HOOK_PRIORITY)
    async def on_llm_request(self, event: AstrMessageEvent, req) -> None:
        if not self.enabled:
            return
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "") or "_unknown"
            fp = self._fingerprint(req)
            prev = self._prev.get(umo)
            attr = self._compare(prev, fp)

            # 压缩发生在归因之后：先记"用户/其他插件造成的断点"，再记自己裁了多少
            press = self.compressor.apply(req)
            if press.get("tool_compressed") or press.get("oversize_trimmed"):
                for key in self.compress_stat:
                    self.compress_stat[key] += int(press.get(key, 0) or 0)
                if attr["cause"] in ("append", "tail", "first"):
                    attr = {
                        "cause": "compress",
                        "split": attr.get("split"),
                        "detail": f"裁剪 {press.get('tool_compressed', 0)} 条工具结果 / "
                        f"{press.get('oversize_trimmed', 0)} 条超长消息，省 {press.get('chars_saved', 0)} 字",
                    }
                fp = self._fingerprint(req)

            budget_over = False
            if self.max_tail_chars and fp["tail_chars"] > self.max_tail_chars:
                budget_over = True
                self.stat["budget_warns"] += 1
                now = time.time()
                if self.warn_on_break and now - self._last_budget_warn > 300:
                    self._last_budget_warn = now
                    logger.warning(
                        f"[context_launcher] 尾部注入 {fp['tail_chars']} 字，超预算 {self.max_tail_chars} 字"
                    )
            if self.max_context_chars and fp["ctx_chars"] > self.max_context_chars:
                budget_over = True
                self.stat["budget_warns"] += 1

            cause = attr["cause"]
            self._defer_active()
            self.stat["req"] += 1
            self.stat["causes"][cause] += 1
            if cause in BREAK_CAUSES:
                self.stat["breaks"] += 1
                now = time.time()
                if (
                    self.warn_on_break
                    and cause != "history_shrink"
                    and now - self._last_break_warn > 120
                ):
                    self._last_break_warn = now
                    logger.info(
                        f"[context_launcher] 前缀断点：{CAUSE_LABEL.get(cause, cause)} · {attr.get('detail', '')}"
                    )
            elif cause == "tail":
                self.stat["tail_changes"] += 1
            else:
                self.stat["clean"] += 1

            record = {
                "ts": time.time(),
                "umo": umo,
                "cause": cause,
                "split": attr.get("split"),
                "detail": attr.get("detail", ""),
                "n": fp["n"],
                "sys_len": fp["sys_len"],
                "tools_n": fp["tools_n"],
                "ctx_chars": fp["ctx_chars"],
                "tail_chars": fp["tail_chars"],
                "est": fp["est_tokens"],
                "budget_over": budget_over,
                "trimmed": int(press.get("tool_compressed", 0) or 0)
                + int(press.get("oversize_trimmed", 0) or 0),
                "hit": None,
                "miss": None,
                "prompt": None,
                "out": None,
                "latency_ms": None,
            }

            self._prev[umo] = {
                "sys_h": fp["sys_h"],
                "sys_len": fp["sys_len"],
                "tools_h": fp["tools_h"],
                "tools_len": fp["tools_len"],
                "tools_n": fp["tools_n"],
                "fps": fp["fps"],
                "tail_h": fp["tail_h"],
                "heads": fp["heads"],
            }
            self._prev.move_to_end(umo)
            while len(self._prev) > MAX_SESSIONS:
                self._prev.popitem(last=False)

            self._pending.setdefault(umo, deque(maxlen=PENDING_MAX)).append(record)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[context_launcher] on_llm_request 失败: {exc}")

    # ------------------------------------------------------------------ 响应钩子

    @staticmethod
    def _read_usage(resp) -> dict:
        raw = getattr(resp, "raw_completion", None)
        usage = getattr(raw, "usage", None) if raw is not None else None
        if usage is None:
            usage = getattr(resp, "usage", None)
        if usage is None:
            return {}
        if hasattr(usage, "model_dump"):
            try:
                return dict(usage.model_dump() or {})
            except Exception:  # noqa: BLE001
                return {}
        if isinstance(usage, dict):
            return usage
        try:
            return dict(usage)
        except Exception:  # noqa: BLE001
            return {}

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp) -> None:
        if not self.enabled:
            return
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "") or "_unknown"
            queue = self._pending.get(umo)
            if not queue:
                return
            record = queue.popleft()
            usage = self._read_usage(resp)

            prompt = usage.get("prompt_tokens")
            hit = usage.get("prompt_cache_hit_tokens")
            miss = usage.get("prompt_cache_miss_tokens")
            details = usage.get("prompt_tokens_details") or {}
            if hit is None and isinstance(details, dict):
                hit = details.get("cached_tokens")
            if prompt is not None and hit is not None and miss is None:
                miss = max(0, int(prompt) - int(hit))

            record["prompt"] = int(prompt) if prompt is not None else None
            record["hit"] = int(hit) if hit is not None else None
            record["miss"] = int(miss) if miss is not None else None
            record["out"] = int(usage["completion_tokens"]) if usage.get("completion_tokens") is not None else None
            record["latency_ms"] = int((time.time() - record["ts"]) * 1000)

            if record["prompt"] is not None:
                self.stat["prompt"] += record["prompt"]
            if record["hit"] is not None:
                self.stat["hit"] += record["hit"]
                self.stat["cache_reported"] += 1
            if record["miss"] is not None:
                self.stat["miss"] += record["miss"]
                if record["cause"] in BREAK_CAUSES:
                    self.stat["miss_break"] += record["miss"]
                    self.stat["n_break"] += 1
                else:
                    self.stat["miss_clean"] += record["miss"]
                    self.stat["n_clean"] += 1
            if record["out"] is not None:
                self.stat["out"] += record["out"]

            self._recent.append(record)
            if self.log_events:
                self._append_event(record)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[context_launcher] on_llm_response 失败: {exc}")

    # ------------------------------------------------------------------ 落盘

    def _append_event(self, record: dict) -> None:
        try:
            if os.path.exists(self.events_path) and os.path.getsize(self.events_path) > MAX_LOG_BYTES:
                os.replace(self.events_path, self.events_path + ".1")
            with open(self.events_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[context_launcher] 写日志失败: {exc}")

    # ------------------------------------------------------------------ 指标

    def _summary(self) -> dict:
        total = self.stat["hit"] + self.stat["miss"]
        return {
            "version": PLUGIN_VERSION,
            "mode": self.mode,
            "enabled": self.enabled,
            "priority": HOOK_PRIORITY,
            "uptime_s": int(time.time() - self.stat["since"]),
            "cache_supported": self.stat["cache_reported"] > 0,
            "req": self.stat["req"],
            "prompt_tokens": self.stat["prompt"],
            "hit_tokens": self.stat["hit"],
            "miss_tokens": self.stat["miss"],
            "out_tokens": self.stat["out"],
            "hit_rate": round(self.stat["hit"] / total, 4) if total else 0.0,
            "miss_per_turn": round(self.stat["miss"] / self.stat["req"], 1) if self.stat["req"] else 0,
            "breaks": self.stat["breaks"],
            "tail_changes": self.stat["tail_changes"],
            "clean": self.stat["clean"],
            "break_avg_miss": round(self.stat["miss_break"] / self.stat["n_break"], 1)
            if self.stat["n_break"]
            else 0,
            "clean_avg_miss": round(self.stat["miss_clean"] / self.stat["n_clean"], 1)
            if self.stat["n_clean"]
            else 0,
            "break_count": self.stat["n_break"],
            "clean_count": self.stat["n_clean"],
            "budget_warns": self.stat["budget_warns"],
            "compress": dict(self.compress_stat),
            "budget": {
                "max_tail_chars": self.max_tail_chars,
                "max_context_chars": self.max_context_chars,
                "max_msg_chars": self.compressor.config.max_msg_chars,
            },
            "defer_to": self._defer_active(),
            "causes": [
                {"cause": k, "label": CAUSE_LABEL.get(k, k), "count": v,
                 "pct": round(v / self.stat["req"], 4) if self.stat["req"] else 0}
                for k, v in self.stat["causes"].most_common()
            ],
            "recent": list(self._recent)[-120:],
        }

    async def _api_summary(self):
        return self._ok(self._summary())

    async def _api_events(self):
        limit = 120
        try:
            from astrbot.api.web import request as _request

            limit = _request.query.get("n", 120, type=int)
        except Exception:  # noqa: BLE001
            pass
        limit = max(1, min(400, int(limit or 120)))
        return self._ok({"events": list(self._recent)[-limit:]})

    # ------------------------------------------------------------------ 命令

    def _render_summary(self) -> str:
        data = self._summary()
        if data["req"] == 0:
            return "上下文管家：还没记录到请求，聊一句再看。"
        lines = [
            "🐟 上下文管家 · 本次运行",
            f"请求 {_fmt_int(data['req'])} 次 · 命中率 {data['hit_rate'] * 100:.1f}%"
            if data["cache_supported"]
            else f"请求 {_fmt_int(data['req'])} 次 · 该模型不返回缓存命中数据",
            f"命中 {_fmt_int(data['hit_tokens'])} / 未命中 {_fmt_int(data['miss_tokens'])} tok",
            f"平均每轮未命中 {_fmt_int(round(data['miss_per_turn']))} tok",
            f"前缀断点 {data['breaks']} 次 · 仅尾部变化 {data['tail_changes']} 次",
        ]
        if data["break_count"] and data["clean_count"]:
            lines.append(
                f"断点轮未命中均值 {_fmt_int(round(data['break_avg_miss']))}（{data['break_count']} 次） · "
                f"干净轮 {_fmt_int(round(data['clean_avg_miss']))}（{data['clean_count']} 次）"
            )
        if data["causes"]:
            top = "、".join(f"{c['label']}×{c['count']}" for c in data["causes"][:4])
            lines.append(f"归因：{top}")
        if data["compress"]["chars_saved"]:
            lines.append(
                f"压缩：工具结果 {data['compress']['tool_compressed']} 条 · "
                f"超长消息 {data['compress']['oversize_trimmed']} 条 · "
                f"省 {_fmt_int(data['compress']['chars_saved'])} 字"
            )
        lines.append(f"模式 {data['mode']} · 压缩 {self._compress_label()}")
        if data["defer_to"]:
            lines.append(f"同类插件已装（{', '.join(data['defer_to'])}），只观测不重复动手")
        return "\n".join(lines)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("上下文")
    async def cmd_context(self, event: AstrMessageEvent, sub: str = ""):
        """上下文管家：/上下文 [诊断|归因|重置|压缩 on|off]"""
        sub = (sub or "").strip()
        try:
            if sub in ("诊断", "diagnose"):
                if not self._recent:
                    yield event.plain_result("还没有可诊断的请求。")
                    return
                rec = self._recent[-1]
                lines = [
                    "🔍 最近一次请求",
                    f"归因：{CAUSE_LABEL.get(rec['cause'], rec['cause'])} · {rec.get('detail', '')}",
                    f"历史 {rec['n']} 条 / {_fmt_int(rec['ctx_chars'])} 字 · 工具 {rec['tools_n']} 个 · "
                    f"尾部 {_fmt_int(rec['tail_chars'])} 字",
                ]
                if rec.get("prompt") is not None:
                    lines.append(
                        f"prompt {_fmt_int(rec['prompt'])} tok · 命中 {_fmt_int(rec['hit'])} · "
                        f"未命中 {_fmt_int(rec['miss'])} · 输出 {_fmt_int(rec['out'])}"
                    )
                if rec.get("split") is not None:
                    lines.append(f"断点位置：第 {rec['split']} 条")
                yield event.plain_result("\n".join(lines))
                return

            if sub in ("归因", "causes"):
                counts = Counter(r["cause"] for r in self._recent)
                if not counts:
                    yield event.plain_result("还没有数据。")
                    return
                rows = [f"{CAUSE_LABEL.get(k, k)} × {v}" for k, v in counts.most_common()]
                yield event.plain_result(f"最近 {len(self._recent)} 次请求归因：\n" + "\n".join(rows))
                return

            if sub in ("重置", "reset"):
                for key in list(self.stat.keys()):
                    if key in ("since", "causes"):
                        continue
                    self.stat[key] = 0
                self.stat["causes"] = Counter()
                self.stat["since"] = time.time()
                for key in self.compress_stat:
                    self.compress_stat[key] = 0
                self._recent.clear()
                yield event.plain_result("已重置本次运行的统计。")
                return

            if sub.startswith("压缩"):
                arg = sub[2:].strip().lower()
                cfg = self.compressor.config
                if arg in ("on", "开", "1"):
                    self.mode = "guard"
                    cfg.tool_trim = True
                    cfg.oversize_trim = True
                    yield event.plain_result(
                        f"已开启压缩（运行时生效，不写盘）：{self._compress_label()}\n"
                        "注意：会改写历史里的工具结果与超长消息，可能让当轮缓存失效一次。"
                    )
                    return
                if arg in ("off", "关", "0"):
                    cfg.tool_trim = False
                    cfg.oversize_trim = False
                    yield event.plain_result("已关闭压缩，只观测。")
                    return
                yield event.plain_result("用法：/上下文 压缩 on|off")
                return

            yield event.plain_result(self._render_summary())
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[context_launcher] cmd 失败: {exc}")
            yield event.plain_result("出错了，看日志。")
