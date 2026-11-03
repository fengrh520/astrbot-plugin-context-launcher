"""context_launcher 插件的离线单元测试（v2）。

不依赖 AstrBot、不联网：用桩模块顶掉 `astrbot.*`，再以「包」的形式加载插件，
验证归因、usage 降级、observe/guard 两种模式、压缩幂等与 Web API 形状。

    python -m unittest tests.test_context_launcher -v
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 兼容两种布局：本仓库 plugins/<name>/，或独立插件仓库（插件就在仓库根）
PLUGIN_DIR = os.path.join(ROOT, "plugins", "astrbot_plugin_context_launcher")
if not os.path.isfile(os.path.join(PLUGIN_DIR, "main.py")):
    PLUGIN_DIR = ROOT
DATA_TMP = tempfile.mkdtemp(prefix="ctxl_test_")

LOGS: list[tuple[str, str]] = []


class _Logger:
    def info(self, m):
        LOGS.append(("info", str(m)))

    def warning(self, m):
        LOGS.append(("warn", str(m)))

    def error(self, m):
        LOGS.append(("error", str(m)))

    def debug(self, m):
        return None


def _install_stubs() -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = _Logger()

    event_mod = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:
        pass

    class _Perm:
        ADMIN = "admin"

    class _Filter:
        PermissionType = _Perm

        @staticmethod
        def on_llm_request(**kw):
            def deco(fn):
                fn._hook_priority = kw.get("priority")
                return fn

            return deco

        @staticmethod
        def on_llm_response(**kw):
            def deco(fn):
                return fn

            return deco

        @staticmethod
        def command(name):
            def deco(fn):
                return fn

            return deco

        @staticmethod
        def permission_type(kind):
            def deco(fn):
                return fn

            return deco

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = _Filter

    star_mod = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context):
            self.context = context

    class Context:
        def __init__(self):
            self.registered_web_apis = []
            self.stars = []

        def register_web_api(self, route, handler, methods, desc):
            self.registered_web_apis.append((route, handler, methods, desc))

        def get_all_stars(self):
            return self.stars

    def register(*args, **kwargs):
        def deco(cls):
            return cls

        return deco

    star_mod.Star = Star
    star_mod.Context = Context
    star_mod.register = register

    web_mod = types.ModuleType("astrbot.api.web")

    def json_response(payload=None, **kw):
        return {"__json__": True, "payload": payload}

    class _Req:
        class query:
            @staticmethod
            def get(name, default=None, type=None):
                return default

    web_mod.json_response = json_response
    web_mod.request = _Req

    core_mod = types.ModuleType("astrbot.core")
    utils_mod = types.ModuleType("astrbot.core.utils")
    path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")
    path_mod.get_astrbot_plugin_data_path = lambda: DATA_TMP
    utils_mod.astrbot_path = path_mod
    core_mod.utils = utils_mod

    astrbot.api = api
    astrbot.core = core_mod
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event_mod,
            "astrbot.api.star": star_mod,
            "astrbot.api.web": web_mod,
            "astrbot.core": core_mod,
            "astrbot.core.utils": utils_mod,
            "astrbot.core.utils.astrbot_path": path_mod,
        }
    )


def _load_plugin():
    _install_stubs()
    pkg_name = "ctxl_under_test"
    spec = importlib.util.spec_from_file_location(
        pkg_name, os.path.join(PLUGIN_DIR, "main.py"), submodule_search_locations=[PLUGIN_DIR]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = module
    spec.loader.exec_module(module)
    return module


main = _load_plugin()


class _NS:
    pass


def _msg(role, text):
    return {"role": role, "content": text}


def _req(system_prompt="SYS", msgs=None, tail=None):
    req = _NS()
    req.system_prompt = system_prompt
    req.contexts = list(msgs or [])
    tool_mgr = _NS()
    tool_mgr.tools = []
    req.func_tool = tool_mgr
    req.extra_user_content_parts = [{"text": t} for t in (tail or [])]
    return req


def _event(umo):
    ev = _NS()
    ev.unified_msg_origin = umo
    return ev


class _Usage:
    def __init__(self, data):
        self._data = data

    def model_dump(self):
        return dict(self._data)


def _resp(prompt=20000, hit=18000, out=200, cache=True):
    resp = _NS()
    raw = _NS()
    raw.model = "stub"
    payload = {
        "prompt_tokens": prompt,
        "completion_tokens": out,
        "prompt_tokens_details": {"cached_tokens": hit} if cache else None,
    }
    if cache:
        payload["prompt_cache_hit_tokens"] = hit
        payload["prompt_cache_miss_tokens"] = prompt - hit
    raw.usage = _Usage(payload)
    resp.raw_completion = raw
    resp.completion_text = "ok"
    return resp


class ContextLauncherTest(unittest.TestCase):
    def build(self, **conf):
        base = {"enabled": True, "mode": "observe", "max_tail_chars": 1800}
        base.update(conf)
        return main.ContextLauncher(main.Context(), base)

    def turn(self, plugin, umo, req, resp=None):
        async def _run():
            ev = _event(umo)
            await plugin.on_llm_request(ev, req)
            if resp is not None:
                await plugin.on_llm_response(ev, resp)

        asyncio.run(_run())

    # ---------------------------------------------------------------- 归因
    def test_cause_first_then_append(self):
        p = self.build()
        self.turn(p, "s1", _req(msgs=[_msg("user", "hi")]), _resp())
        self.assertEqual(p._recent[-1]["cause"], "first")
        self.turn(
            p, "s1",
            _req(msgs=[_msg("user", "hi"), _msg("assistant", "yo"), _msg("user", "q")]),
            _resp(),
        )
        self.assertEqual(p._recent[-1]["cause"], "append")

    def test_cause_system_and_tools(self):
        p = self.build()
        self.turn(p, "s2", _req(msgs=[_msg("user", "hi")]), _resp())
        self.turn(p, "s2", _req(system_prompt="SYS2", msgs=[_msg("user", "hi")]), _resp())
        self.assertEqual(p._recent[-1]["cause"], "system")

        p2 = self.build()
        self.turn(p2, "s3", _req(msgs=[_msg("user", "hi")]), _resp())
        req = _req(msgs=[_msg("user", "hi")])
        tool = _NS()
        tool.name = "web_search"
        tool.model_dump = lambda: {"name": "web_search"}
        req.func_tool.tools = [tool]
        self.turn(p2, "s3", req, _resp())
        self.assertEqual(p2._recent[-1]["cause"], "tools")

    def test_cause_history_and_history_tool(self):
        p = self.build()
        self.turn(p, "s4", _req(msgs=[_msg("user", "a"), _msg("assistant", "b"), _msg("user", "c")]), _resp())
        self.turn(p, "s4", _req(msgs=[_msg("user", "a"), _msg("assistant", "B!"), _msg("user", "c")]), _resp())
        self.assertEqual(p._recent[-1]["cause"], "history")
        self.assertEqual(p._recent[-1]["split"], 1)

        p2 = self.build()
        self.turn(p2, "s5", _req(msgs=[_msg("user", "a"), _msg("tool", "T1"), _msg("user", "c")]), _resp())
        self.turn(p2, "s5", _req(msgs=[_msg("user", "a"), _msg("tool", "T1-slim"), _msg("user", "c")]), _resp())
        self.assertEqual(p2._recent[-1]["cause"], "history_tool")

    def test_cause_tail_and_shrink(self):
        p = self.build()
        self.turn(p, "s6", _req(msgs=[_msg("user", "a")], tail=["[今天] 03"]), _resp())
        self.turn(p, "s6", _req(msgs=[_msg("user", "a")], tail=["[今天] 04"]), _resp())
        self.assertEqual(p._recent[-1]["cause"], "tail")

        p2 = self.build()
        self.turn(p2, "s7", _req(msgs=[_msg("user", "1"), _msg("assistant", "1"), _msg("user", "2")]), _resp())
        self.turn(p2, "s7", _req(msgs=[_msg("user", "1")]), _resp())
        self.assertEqual(p2._recent[-1]["cause"], "history_shrink")

    # ---------------------------------------------------------------- usage
    def test_usage_accounting(self):
        p = self.build()
        self.turn(p, "u1", _req(msgs=[_msg("user", "hi")]), _resp(prompt=20000, hit=17000))
        rec = p._recent[-1]
        self.assertEqual((rec["hit"], rec["miss"], rec["out"]), (17000, 3000, 200))
        self.assertTrue(p._summary()["cache_supported"])

    def test_provider_without_cache_fields_degrades(self):
        p = self.build()
        self.turn(p, "u2", _req(msgs=[_msg("user", "hi")]), _resp(prompt=500, hit=0, cache=False))
        self.assertIsNone(p._recent[-1]["hit"])
        self.assertFalse(p._summary()["cache_supported"])

    def test_break_and_clean_average(self):
        p = self.build()
        self.turn(p, "u3", _req(msgs=[_msg("user", "a")]), _resp(prompt=2000, hit=1000))
        self.turn(p, "u3", _req(msgs=[_msg("user", "a"), _msg("assistant", "b")]), _resp(prompt=2000, hit=1000))
        self.turn(p, "u3", _req(system_prompt="SYS2", msgs=[_msg("user", "a")]), _resp(prompt=2000, hit=0))
        summary = asyncio.run(p._api_summary())["payload"]
        self.assertEqual(summary["break_count"], 1)
        self.assertEqual(summary["clean_count"], 2)
        self.assertEqual(summary["break_avg_miss"], 2000)

    # ---------------------------------------------------------------- 压缩
    def test_observe_mode_never_touches_payload(self):
        p = self.build()
        long_tool = "T" * 5000
        req = _req(msgs=[_msg("user", "u"), _msg("tool", long_tool), _msg("assistant", "a"), _msg("user", "z")])
        before = [m["content"] for m in req.contexts]
        self.turn(p, "c1", req, _resp())
        self.assertEqual([m["content"] for m in req.contexts], before)
        self.assertEqual(p.compress_stat["tool_compressed"], 0)

    def test_guard_mode_compresses_old_tool_results(self):
        p = self.build(
            mode="guard",
            compress_tool_results=True,
            compress_tool_keep_recent=1,
            compress_keep_recent_msgs=2,
        )
        req = _req(
            msgs=[
                _msg("user", "u1"),
                _msg("tool", "OLD" * 100),
                _msg("user", "u2"),
                _msg("tool", "NEW" * 100),
                _msg("assistant", "a2"),
                _msg("user", "u3"),
            ]
        )
        self.turn(p, "c2", req, _resp())
        self.assertTrue(req.contexts[1]["content"].startswith("⏺"))
        self.assertEqual(req.contexts[3]["content"], "NEW" * 100)
        self.assertEqual(p.compress_stat["tool_compressed"], 1)
        self.assertEqual(p._recent[-1]["cause"], "compress")

    def test_compression_is_idempotent(self):
        p = self.build(mode="guard", compress_tool_results=True, compress_keep_recent_msgs=1)
        req = _req(msgs=[_msg("user", "u1"), _msg("tool", "X" * 400), _msg("user", "u2")])
        self.turn(p, "c3", req, _resp())
        saved = p.compress_stat["chars_saved"]
        self.turn(p, "c3", req, _resp())
        self.assertEqual(p.compress_stat["chars_saved"], saved)

    # ---------------------------------------------------------------- 接口
    def test_web_api_shape(self):
        p = self.build()
        self.turn(p, "x1", _req(msgs=[_msg("user", "hi")]), _resp())
        body = asyncio.run(p._api_summary())
        self.assertTrue(body["__json__"])
        for key in ("hit_rate", "miss_per_turn", "breaks", "cache_supported", "compress", "budget", "causes", "recent"):
            self.assertIn(key, body["payload"])
        events = asyncio.run(p._api_events())["payload"]["events"]
        self.assertIsInstance(events, list)

    def test_render_summary_does_not_crash(self):
        p = self.build()
        self.turn(p, "x2", _req(msgs=[_msg("user", "hi")]), _resp())
        self.assertIn("上下文管家", p._render_summary())


if __name__ == "__main__":
    unittest.main()
