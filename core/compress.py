"""上下文压缩核心（契约：_ctx_launcher/_v2_spec.md 第 3 节）。

提供三种保守压缩中的前两种：工具结果裁剪、超长单条消息裁剪。
只依赖标准库；任何异常都吞掉并只写 logger.debug，绝不影响聊天主流程。
"""

import hashlib
import logging
import os
from dataclasses import dataclass

# 默认日志器名称，与插件名保持一致
LOGGER_NAME = "astrbot_plugin_context_launcher"

# 幂等标记：命中任意一个就认为该消息已经压缩过，不再重复处理
MARK_ARCHIVE = "全文已归档"
MARK_OMIT = "［上下文管家］"
TOOL_PREFIX = "⏺ "


def _debug(logger, msg, *args):
    """安全地写 debug 日志，日志本身出错也不能往外冒异常。"""
    try:
        if logger is not None:
            logger.debug(msg, *args)
    except Exception:
        pass


def content_text(content) -> str:
    """从 content 抽取纯文本。

    支持 str / bytes / part 列表（part.text、part.content 或 {"text": ...}）/ 带 text 属性的对象。
    无法抽取时返回空串。
    """
    try:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, (bytes, bytearray)):
            return bytes(content).decode("utf-8", "replace")
        if isinstance(content, dict):
            return content_text(content.get("text"))
        if isinstance(content, (list, tuple)):
            parts = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                    continue
                if isinstance(part, dict):
                    text = part.get("text")
                    if text is None:
                        text = part.get("content")
                    if isinstance(text, str):
                        parts.append(text)
                    continue
                text = getattr(part, "text", None)
                if not isinstance(text, str):
                    # 兼容只提供 content 属性的 part 对象
                    text = getattr(part, "content", None)
                if isinstance(text, str):
                    parts.append(text)
            return "".join(parts)
        text = getattr(content, "text", None)
        if isinstance(text, str):
            return text
        return str(content)
    except Exception as exc:
        _debug(logging.getLogger(LOGGER_NAME), "抽取消息文本失败：%s", exc)
        return ""


# 契约里提到的内部名称，保持别名以便调用
_content_text = content_text


def first_line(text: str, limit: int) -> str:
    """取文本首个非空行，并截断到 limit 个字符（确定性，不加省略号）。"""
    try:
        if not isinstance(text, str):
            text = content_text(text)
        normalized = text.strip()
        if not normalized:
            return ""
        line = normalized.splitlines()[0].strip()
        if limit is None:
            return line
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            return line
        if limit <= 0:
            return ""
        return line[:limit]
    except Exception as exc:
        _debug(logging.getLogger(LOGGER_NAME), "提取首行失败：%s", exc)
        return ""


def _to_int(value, default: int = 0) -> int:
    """把配置里的值安全地转成 int。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
    except Exception:
        return default


def _field(msg, key, default=None):
    """同时支持 dict 与对象两种消息形态地读取字段。"""
    try:
        if isinstance(msg, dict):
            value = msg.get(key, default)
        else:
            value = getattr(msg, key, default)
        return default if value is None else value
    except Exception:
        return default


def _is_tool_message(msg) -> bool:
    """判断是否为工具结果消息。"""
    try:
        role = _field(msg, "role")
        return isinstance(role, str) and role.strip().lower() == "tool"
    except Exception:
        return False


def _str_content(msg):
    """若消息 content 是 str 则返回它，否则返回 None（复杂结构一律跳过）。"""
    try:
        if isinstance(msg, dict):
            content = msg.get("content")
        else:
            content = getattr(msg, "content", None)
        return content if isinstance(content, str) else None
    except Exception:
        return None


def _write_content(msg, text: str) -> bool:
    """只在 content 原本就是 str 时写回。"""
    try:
        if _str_content(msg) is None:
            return False
        if isinstance(msg, dict):
            msg["content"] = text
        else:
            setattr(msg, "content", text)
        return True
    except Exception as exc:
        _debug(logging.getLogger(LOGGER_NAME), "写回消息内容失败：%s", exc)
        return False


def _tool_name(msg) -> str:
    """取工具名，取不到时按契约用 tool。"""
    try:
        for key in ("name", "tool_name", "tool_call_name", "function_name"):
            value = _field(msg, key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    except Exception:
        pass
    return "tool"


def _already_compressed(text: str) -> bool:
    """靠已有标记判断是否压缩过（幂等）。"""
    try:
        if not isinstance(text, str):
            return False
        if MARK_ARCHIVE in text or MARK_OMIT in text:
            return True
        return text.startswith(TOOL_PREFIX)
    except Exception:
        return False


@dataclass
class CompressConfig:
    """压缩配置，字段与契约第 3 节一致。"""

    tool_trim: bool = True            # 是否裁工具结果
    tool_keep_recent: int = 2         # 最近几条 tool 结果保留原文
    tool_preview_chars: int = 120     # 压缩后保留首行多少字
    oversize_trim: bool = True        # 是否裁超长单条消息
    max_msg_chars: int = 12000        # 超过多少字才裁
    keep_head_chars: int = 4000
    keep_tail_chars: int = 1500
    keep_recent_msgs: int = 2         # 最近几条消息永不裁
    archive: bool = True              # 原文是否落盘
    archive_dir: str = ""             # 由 main.py 注入绝对路径
    max_archive_bytes: int = 200 * 1024 * 1024   # 归档目录上限


class ContextCompressor:
    """就地压缩 req.contexts，返回统计信息。"""

    def __init__(self, config: CompressConfig, logger=None) -> None:
        try:
            self.config = config if isinstance(config, CompressConfig) else CompressConfig()
        except Exception:
            self.config = CompressConfig()
        self.logger = logger if logger is not None else logging.getLogger(LOGGER_NAME)
        # 本次运行已归档过的 sha1 前缀，避免同内容重复写盘
        self._archived_hashes = set()
        # 归档目录字节数缓存，apply() 开始时置空重新统计
        self._archive_bytes_cache = None

    # ---------------------------------------------------------------- 公开接口
    def apply(self, req) -> dict:
        """就地压缩 req.contexts，返回：

        {"tool_compressed": int, "oversize_trimmed": int, "chars_saved": int}

        任何异常都吞掉并返回 0 统计。
        """
        stats = {"tool_compressed": 0, "oversize_trimmed": 0, "chars_saved": 0}
        try:
            contexts = self._get_contexts(req)
            if not contexts:
                return stats
            self._archive_bytes_cache = None
            cfg = self.config
            total = len(contexts)
            recent_msgs = max(0, _to_int(cfg.keep_recent_msgs, 0))
            recent_start = max(0, total - recent_msgs)
            if getattr(cfg, "tool_trim", False):
                self._trim_tool_results(contexts, recent_start, stats)
            if getattr(cfg, "oversize_trim", False):
                self._trim_oversize(contexts, recent_start, stats)
        except Exception as exc:
            _debug(self.logger, "上下文压缩失败：%s", exc)
        return stats

    # ---------------------------------------------------------------- 内部实现
    def _get_contexts(self, req):
        """同时支持 req.contexts 与 dict 形态的 req。"""
        try:
            if isinstance(req, dict):
                contexts = req.get("contexts")
            else:
                contexts = getattr(req, "contexts", None)
            return contexts if isinstance(contexts, list) else []
        except Exception as exc:
            _debug(self.logger, "读取 req.contexts 失败：%s", exc)
            return []

    def _trim_tool_results(self, contexts, recent_start, stats) -> None:
        """工具结果裁剪：跳过最近 keep_recent_msgs 条消息与最近 tool_keep_recent 条 tool。"""
        try:
            cfg = self.config
            tool_indexes = [i for i, msg in enumerate(contexts) if _is_tool_message(msg)]
            keep_recent_tools = max(0, _to_int(cfg.tool_keep_recent, 0))
            protected = set(tool_indexes[len(tool_indexes) - keep_recent_tools:]) \
                if keep_recent_tools > 0 else set()
            preview_chars = max(0, _to_int(cfg.tool_preview_chars, 0))
            for index in tool_indexes:
                if index >= recent_start or index in protected:
                    continue
                try:
                    msg = contexts[index]
                    text = _str_content(msg)
                    if text is None or not text:
                        continue
                    if _already_compressed(text):
                        continue
                    new_text = self._build_tool_text(msg, text, preview_chars)
                    # 压不动（反而更长）就跳过，既省字符也保证幂等
                    if new_text == text or len(new_text) >= len(text):
                        continue
                    if _write_content(msg, new_text):
                        stats["tool_compressed"] += 1
                        stats["chars_saved"] += len(text) - len(new_text)
                except Exception as exc:
                    _debug(self.logger, "裁剪工具结果失败：%s", exc)
        except Exception as exc:
            _debug(self.logger, "工具结果裁剪失败：%s", exc)

    def _build_tool_text(self, msg, text: str, preview_chars: int) -> str:
        """组装工具结果替换文本。"""
        try:
            name = _tool_name(msg) or "tool"
            new_text = TOOL_PREFIX + name + " · " + first_line(text, preview_chars)
            if getattr(self.config, "archive", False):
                path = self._archive(text)
                if path:
                    new_text += " · " + MARK_ARCHIVE + " " + path
            return new_text
        except Exception as exc:
            _debug(self.logger, "组装工具结果文本失败：%s", exc)
            return text

    def _trim_oversize(self, contexts, recent_start, stats) -> None:
        """超长单条消息裁剪：头 + 省略标记 + 尾。"""
        try:
            cfg = self.config
            max_chars = max(0, _to_int(cfg.max_msg_chars, 0))
            head_chars = max(0, _to_int(cfg.keep_head_chars, 0))
            tail_chars = max(0, _to_int(cfg.keep_tail_chars, 0))
            if max_chars <= 0:
                return
            for index in range(recent_start):
                try:
                    msg = contexts[index]
                    text = _str_content(msg)
                    if text is None or len(text) <= max_chars:
                        continue
                    if MARK_OMIT in text:
                        continue
                    cut = len(text) - head_chars - tail_chars
                    if cut <= 0:
                        continue
                    marker = "\n…" + MARK_OMIT + "已省略 " + str(cut) + " 字"
                    if getattr(cfg, "archive", False):
                        path = self._archive(text)
                        if path:
                            marker += "，" + MARK_ARCHIVE + " " + path
                    marker += "…\n"
                    new_text = text[:head_chars] + marker
                    if tail_chars > 0:
                        new_text += text[len(text) - tail_chars:]
                    if len(new_text) >= len(text):
                        continue
                    if _write_content(msg, new_text):
                        stats["oversize_trimmed"] += 1
                        stats["chars_saved"] += len(text) - len(new_text)
                except Exception as exc:
                    _debug(self.logger, "裁剪超长消息失败：%s", exc)
        except Exception as exc:
            _debug(self.logger, "超长消息裁剪失败：%s", exc)

    def _archive(self, text: str) -> str:
        """把原文归档到 archive_dir/<sha1前16位>.txt，同内容只写一次。

        归档关闭、目录为空、超限或失败时返回空串（此时调用方省略归档说明）。
        """
        try:
            cfg = self.config
            if not getattr(cfg, "archive", False):
                return ""
            archive_dir = getattr(cfg, "archive_dir", "")
            if not isinstance(archive_dir, str) or not archive_dir.strip():
                return ""
            digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]
            path = os.path.join(archive_dir, digest + ".txt")
            if digest in self._archived_hashes or os.path.exists(path):
                self._archived_hashes.add(digest)
                return path
            data = text.encode("utf-8", "replace")
            limit = max(0, _to_int(getattr(cfg, "max_archive_bytes", 0), 0))
            used = self._archive_dir_bytes()
            if limit > 0 and used + len(data) > limit:
                _debug(self.logger, "归档目录已达上限，跳过归档：%s", archive_dir)
                return ""
            os.makedirs(archive_dir, exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(data)
            self._archived_hashes.add(digest)
            self._archive_bytes_cache = used + len(data)
            return path
        except Exception as exc:
            _debug(self.logger, "归档原文失败：%s", exc)
            return ""

    def _archive_dir_bytes(self) -> int:
        """统计归档目录当前字节数（同一次 apply 内只遍历一次）。"""
        try:
            if self._archive_bytes_cache is not None:
                return self._archive_bytes_cache
            total = 0
            archive_dir = getattr(self.config, "archive_dir", "")
            if isinstance(archive_dir, str) and archive_dir.strip() and os.path.isdir(archive_dir):
                for dirpath, _dirnames, filenames in os.walk(archive_dir):
                    for name in filenames:
                        try:
                            total += os.path.getsize(os.path.join(dirpath, name))
                        except OSError:
                            continue
            self._archive_bytes_cache = total
            return total
        except Exception as exc:
            _debug(self.logger, "统计归档目录大小失败：%s", exc)
            return 0