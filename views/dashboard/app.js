/*
 * 上下文管家（context_launcher）插件可视化视图脚本
 * 约束（按契约第 4 节）：
 *   1. 后端调用一律走 window.AstrBotPluginView bridge，不使用任何浏览器端存储与网络请求 API。
 *   2. 原生 JS，无构建、无 CDN、无第三方依赖，不 import 任何外部文件。
 *   3. 所有可能抛异常的地方都就地 try/except，异常不上抛、页面不白屏。
 */
(function () {
    "use strict";

    /* ===================== 常量与状态 ===================== */

    const REFRESH_MS = 15000;        // 自动刷新间隔（毫秒）
    const TREND_POINTS = 60;         // 走势图最多绘制的请求数
    const RECENT_ROWS = 20;          // 最近请求表最多显示行数
    const BRIDGE_TIMEOUT_MS = 8000;  // 等待 bridge 注入的超时

    const state = {
        bridge: null,      // bridge 对象
        ctx: null,         // bridge.ready() 返回的上下文
        data: null,        // 最近一次 summary 数据
        trend: [],         // 走势图数据点
        timer: null,       // 自动刷新定时器
        resizeTimer: null, // 重绘防抖定时器
        loading: false     // 是否正在请求
    };

    /* ===================== 通用小工具 ===================== */

    // 取元素，异常返回 null
    function byId(id) {
        try {
            return document.getElementById(id);
        } catch (err) {
            return null;
        }
    }

    // 写文本，异常忽略
    function setText(id, text) {
        const el = byId(id);
        if (!el) return;
        try {
            el.textContent = text === null || text === undefined ? "" : String(text);
        } catch (err) {
            /* 忽略 */
        }
    }

    // 设置进度条宽度（0-100 百分数）
    function setWidth(id, pct) {
        const el = byId(id);
        if (!el) return;
        try {
            let v = Number(pct);
            if (!isFinite(v) || v < 0) v = 0;
            if (v > 100) v = 100;
            el.style.width = v.toFixed(1) + "%";
        } catch (err) {
            /* 忽略 */
        }
    }

    // 显示 / 隐藏元素
    function show(el) {
        if (!el) return;
        try {
            el.hidden = false;
        } catch (err) {
            /* 忽略 */
        }
    }

    function hide(el) {
        if (!el) return;
        try {
            el.hidden = true;
        } catch (err) {
            /* 忽略 */
        }
    }

    // 清空子节点
    function clearNode(el) {
        if (!el) return;
        try {
            while (el.firstChild) {
                el.removeChild(el.firstChild);
            }
        } catch (err) {
            /* 忽略 */
        }
    }

    // 创建元素
    function makeEl(tag, className) {
        const el = document.createElement(tag);
        if (className) el.className = className;
        return el;
    }

    // 转数字，非法值返回 0
    function num(v) {
        const n = Number(v);
        return isFinite(n) ? n : 0;
    }

    // 是否是可用的数字
    function hasNum(v) {
        return v !== null && v !== undefined && v !== "" && isFinite(Number(v));
    }

    // 整数千分位
    function fmtInt(v) {
        if (!hasNum(v)) return "—";
        try {
            return String(Math.round(Number(v)).toLocaleString("en-US"));
        } catch (err) {
            return String(Math.round(Number(v)));
        }
    }

    // token / 字符数的紧凑写法
    function fmtTokens(v) {
        if (!hasNum(v)) return "—";
        const n = Number(v);
        const abs = Math.abs(n);
        try {
            if (abs >= 1000000) return (n / 1000000).toFixed(abs >= 10000000 ? 1 : 2) + "M";
            if (abs >= 1000) return (n / 1000).toFixed(abs >= 100000 ? 0 : 1) + "k";
        } catch (err) {
            /* 忽略，退回整数 */
        }
        return String(Math.round(n));
    }

    // 比例（0-1）→ 百分数文本
    function fmtPercent(v, digits) {
        if (!hasNum(v)) return "—";
        const d = typeof digits === "number" ? digits : 1;
        try {
            return (Number(v) * 100).toFixed(d) + "%";
        } catch (err) {
            return "—";
        }
    }

    function pad2(x) {
        return (x < 10 ? "0" : "") + x;
    }

    // 时间戳（秒或毫秒）→ 本地 HH:MM:SS
    function fmtTime(ts) {
        if (!hasNum(ts)) return "—";
        let v = Number(ts);
        if (v <= 0) return "—";
        if (v > 1e12) v = v / 1000; // 毫秒时间戳
        try {
            const date = new Date(v * 1000);
            return pad2(date.getHours()) + ":" + pad2(date.getMinutes()) + ":" + pad2(date.getSeconds());
        } catch (err) {
            return "—";
        }
    }

    // 秒 → 人类可读时长（文案走 i18n）
    function fmtDuration(sec) {
        if (!hasNum(sec) || Number(sec) < 0) return "—";
        let s = Math.floor(Number(sec));
        const d = Math.floor(s / 86400);
        const h = Math.floor((s % 86400) / 3600);
        const m = Math.floor((s % 3600) / 60);
        const rest = s % 60;
        const parts = [];
        if (d > 0) parts.push(t("views.dashboard.uptime.days", "{n} 天", { n: d }));
        if (h > 0) parts.push(t("views.dashboard.uptime.hours", "{n} 小时", { n: h }));
        if (m > 0) parts.push(t("views.dashboard.uptime.minutes", "{n} 分", { n: m }));
        if (!parts.length) parts.push(t("views.dashboard.uptime.seconds", "{n} 秒", { n: rest }));
        return parts.join(" ");
    }

    // 异常 → 简短文本
    function errText(err) {
        let msg = "";
        try {
            if (err && err.message !== undefined && err.message !== null) {
                msg = String(err.message);
            } else if (err !== undefined && err !== null) {
                msg = String(err);
            }
        } catch (e) {
            msg = "";
        }
        if (!msg) msg = t("views.dashboard.error.unknown", "未知错误");
        return msg;
    }

    /* ===================== bridge 与 i18n ===================== */

    function getBridge() {
        try {
            return window.AstrBotPluginView || window.AstrBotPluginPage || null;
        } catch (err) {
            return null;
        }
    }

    // 轮询等待服务端注入 bridge
    function waitForBridge(timeoutMs) {
        return new Promise(function (resolve) {
            let done = false;
            const finish = function (value) {
                if (done) return;
                done = true;
                try {
                    clearInterval(timer);
                } catch (err) {
                    /* 忽略 */
                }
                resolve(value);
            };
            const started = Date.now();
            const timer = setInterval(function () {
                try {
                    const b = getBridge();
                    if (b) {
                        finish(b);
                        return;
                    }
                    if (Date.now() - started >= timeoutMs) {
                        finish(null);
                    }
                } catch (err) {
                    finish(null);
                }
            }, 100);
        });
    }

    // 变量替换：{name}
    function formatVars(text, vars) {
        if (!vars || !text) return text;
        let out = String(text);
        try {
            const names = Object.keys(vars);
            for (let i = 0; i < names.length; i++) {
                const key = "{" + names[i] + "}";
                out = out.split(key).join(String(vars[names[i]]));
            }
        } catch (err) {
            /* 忽略 */
        }
        return out;
    }

    // 取文案；bridge 缺失或异常时退回中文兜底
    function t(key, fallback, vars) {
        let text = "";
        try {
            const b = state.bridge;
            if (b && typeof b.t === "function") {
                const got = b.t(key, fallback);
                if (typeof got === "string" && got) text = got;
            }
        } catch (err) {
            text = "";
        }
        if (!text) text = typeof fallback === "string" ? fallback : String(key);
        return formatVars(text, vars);
    }

    // 把页面上所有 data-i18n 节点按当前语言刷一遍
    function applyI18n(root) {
        try {
            const scope = root || document;
            const nodes = scope.querySelectorAll("[data-i18n]");
            for (let i = 0; i < nodes.length; i++) {
                const el = nodes[i];
                const key = el.getAttribute("data-i18n");
                if (!key) continue;
                const fb = el.getAttribute("data-fallback") || "";
                el.textContent = t(key, fb);
            }
        } catch (err) {
            /* 忽略 */
        }
    }

    /* ===================== 后端请求 ===================== */

    // 走 bridge 的 GET；endpoint 不含插件名前缀
    function apiGet(endpoint, params) {
        return new Promise(function (resolve, reject) {
            let b = null;
            try {
                b = state.bridge;
            } catch (err) {
                b = null;
            }
            if (!b || typeof b.apiGet !== "function") {
                reject(new Error(t("views.dashboard.error.bridge", "插件视图 SDK 未就绪")));
                return;
            }
            let result = null;
            try {
                result = b.apiGet(endpoint, params || {});
            } catch (err) {
                reject(err);
                return;
            }
            if (result && typeof result.then === "function") {
                result.then(function (value) {
                    resolve(value);
                }, function (err) {
                    reject(err);
                });
            } else {
                resolve(result);
            }
        });
    }

    // 拉取 summary；失败只在内部处理，绝不抛出
    function loadData() {
        if (state.loading) return Promise.resolve();
        state.loading = true;
        setBusy(true);
        return apiGet("summary", {}).then(function (data) {
            if (!data || typeof data !== "object") {
                throw new Error(t("views.dashboard.error.badData", "返回数据格式不正确"));
            }
            return data;
        }).then(function (data) {
            // summary 没带 recent 时，补一次 events 接口（失败静默，不打扰用户）
            if (!Array.isArray(data.recent) || data.recent.length === 0) {
                return apiGet("events", { n: 120 }).then(function (res) {
                    let list = [];
                    if (res && Array.isArray(res.events)) list = res.events;
                    else if (Array.isArray(res)) list = res;
                    if (list.length) data.recent = list;
                    return data;
                }, function () {
                    return data;
                });
            }
            return data;
        }).then(function (data) {
            state.data = data;
            clearError();
            renderAll();
        }).catch(function (err) {
            showError(t("views.dashboard.error.load", "加载失败：{msg}", { msg: errText(err) }));
        }).then(function () {
            state.loading = false;
            setBusy(false);
        }, function () {
            state.loading = false;
            setBusy(false);
        });
    }

    /* ===================== 提示条 ===================== */

    function showError(msg) {
        setText("error-text", msg);
        show(byId("error-bar"));
    }

    function clearError() {
        hide(byId("error-bar"));
    }

    function setBusy(busy) {
        const btn = byId("refresh-btn");
        if (!btn) return;
        try {
            btn.disabled = !!busy;
            btn.textContent = busy
                ? t("views.dashboard.action.refreshing", "刷新中…")
                : t("views.dashboard.action.refresh", "刷新");
        } catch (err) {
            /* 忽略 */
        }
    }

    // 归因代码 → 展示文案：优先用后端 causes 里的 label，其次 i18n，最后原样代码
    function causeLabel(code, labelMap) {
        const key = code ? String(code) : "";
        if (!key) return t("views.dashboard.cause.other", "其他");
        if (labelMap && labelMap[key]) return labelMap[key];
        return t("views.dashboard.cause." + key, key);
    }

    function buildCauseMap(d) {
        const map = {};
        try {
            const list = Array.isArray(d && d.causes) ? d.causes : [];
            for (let i = 0; i < list.length; i++) {
                const item = list[i];
                if (item && item.cause && item.label) map[String(item.cause)] = String(item.label);
            }
        } catch (err) {
            /* 忽略 */
        }
        return map;
    }

    /* ===================== 渲染 ===================== */

    // 单个区块失败不影响其它区块
    function guarded(fn) {
        try {
            fn();
        } catch (err) {
            /* 忽略 */
        }
    }

    function renderAll() {
        const d = state.data;
        if (!d) return;
        guarded(function () { renderHeader(d); });
        guarded(function () { renderNotices(d); });
        guarded(function () { renderKpis(d); });
        guarded(function () { renderCompare(d); });
        guarded(function () { renderCauses(d); });
        guarded(function () { renderTrend(d); });
        guarded(function () { renderRecent(d); });
        guarded(function () { renderFooter(d); });
    }

    // 1. 顶部：插件名 + 模式 chip + 运行时长
    function renderHeader(d) {
        let name = "";
        try {
            if (state.ctx) name = String(state.ctx.displayName || state.ctx.pluginName || "");
        } catch (err) {
            name = "";
        }
        if (!name) name = t("views.dashboard.title", "上下文管家");
        setText("plugin-name", name);

        const mode = d.mode ? String(d.mode) : "";
        let modeText = t("views.dashboard.mode.unknown", "未知模式");
        if (mode === "observe") modeText = t("views.dashboard.mode.observe", "只观测（observe）");
        else if (mode === "guard") modeText = t("views.dashboard.mode.guard", "主动压缩（guard）");
        setText("mode-chip", modeText);

        const enabledChip = byId("enabled-chip");
        if (d.enabled === false) {
            setText("enabled-chip", t("views.dashboard.chip.disabled", "已停用"));
            show(enabledChip);
        } else {
            hide(enabledChip);
        }

        const cacheChip = byId("cache-chip");
        if (d.cache_supported === false) {
            setText("cache-chip", t("views.dashboard.chip.noCache", "无缓存数据"));
            show(cacheChip);
        } else {
            hide(cacheChip);
        }

        setText("uptime", hasNum(d.uptime_s) ? fmtDuration(d.uptime_s) : "—");
    }

    // 9. cache_supported=false / 停用 / 预算超限的提示条
    function renderNotices(d) {
        // provider 不返回缓存字段
        const cacheBar = byId("notice-cache");
        if (d.cache_supported === false) {
            setText("notice-cache", t("views.dashboard.notice.cacheUnsupported",
                "还没拿到缓存字段：可能模型不返回，也可能这些请求还没返回用量。"));
            show(cacheBar);
        } else {
            hide(cacheBar);
        }

        // 插件停用
        const offBar = byId("notice-disabled");
        if (d.enabled === false) {
            setText("notice-disabled", t("views.dashboard.notice.disabled",
                "插件当前处于停用状态，以下仅为历史统计。"));
            show(offBar);
        } else {
            hide(offBar);
        }

        // 预算提醒
        const budgetBar = byId("notice-budget");
        if (num(d.budget_warns) > 0) {
            setText("notice-budget", t("views.dashboard.notice.budgetWarns",
                "本次运行有 {n} 次超出尾部预算的提醒。", { n: Math.round(num(d.budget_warns)) }));
            show(budgetBar);
        } else {
            hide(budgetBar);
        }
    }

    // 2. 四个 KPI
    function renderKpis(d) {
        const supported = d.cache_supported !== false;
        const prompt = num(d.prompt_tokens);
        const hit = num(d.hit_tokens);
        const miss = num(d.miss_tokens);

        // 缓存命中率
        let rate = null;
        if (supported) {
            if (hasNum(d.hit_rate)) rate = num(d.hit_rate);
            else if (prompt > 0) rate = hit / prompt;
        }
        const rateEl = byId("kpi-hit-rate");
        if (rateEl) {
            rateEl.textContent = rate === null ? "—" : fmtPercent(rate, 1);
            try {
                rateEl.className = "kpi-value" + (rate === null ? "" : (rate >= 0.8 ? " good" : (rate >= 0.5 ? " warn" : " bad")));
            } catch (err) {
                /* 忽略 */
            }
        }
        setText("kpi-hit-rate-hint", supported
            ? t("views.dashboard.kpi.hitRateHint", "命中 {hit} / 输入 {prompt}", {
                hit: fmtTokens(hit),
                prompt: fmtTokens(prompt)
            })
            : t("views.dashboard.kpi.hitRateEmpty", "无缓存字段可读"));

        // 每轮未命中 token
        const missPer = supported ? num(d.miss_per_turn) : null;
        const missEl = byId("kpi-miss");
        if (missEl) {
            missEl.textContent = missPer === null ? "—" : fmtTokens(missPer);
            try {
                missEl.className = "kpi-value" + (missPer === null ? "" : (missPer <= 2000 ? " good" : (missPer <= 20000 ? " warn" : " bad")));
            } catch (err) {
                /* 忽略 */
            }
        }
        setText("kpi-miss-hint", supported
            ? t("views.dashboard.kpi.missPerTurnHint", "累计未命中 {miss}", { miss: fmtTokens(miss) })
            : t("views.dashboard.kpi.hitRateEmpty", "无缓存字段可读"));

        // 前缀断点次数
        const breaks = num(d.breaks);
        setText("kpi-breaks", fmtInt(breaks));
        setText("kpi-breaks-hint", t("views.dashboard.kpi.breaksHint",
            "尾部变化 {tail} · 干净轮 {clean}", {
            tail: fmtInt(d.tail_changes),
            clean: fmtInt(d.clean)
        }));

        // 压缩节省字符
        const compress = d.compress && typeof d.compress === "object" ? d.compress : {};
        const saved = num(compress.chars_saved);
        setText("kpi-chars", fmtTokens(saved));
        setText("kpi-chars-hint", t("views.dashboard.kpi.charsSavedHint",
            "工具 {tool} 条 · 超长 {over} 条", {
            tool: fmtInt(compress.tool_compressed),
            over: fmtInt(compress.oversize_trimmed)
        }));
    }

    // 3. 断点轮 vs 干净轮
    function renderCompare(d) {
        const bAvg = num(d.break_avg_miss);
        const cAvg = num(d.clean_avg_miss);
        const max = Math.max(bAvg, cAvg, 1);
        setWidth("compare-break", (bAvg / max) * 100);
        setWidth("compare-clean", (cAvg / max) * 100);
        setText("compare-break-value", fmtTokens(bAvg));
        setText("compare-clean-value", fmtTokens(cAvg));
        setText("compare-break-count", t("views.dashboard.compare.count", "{count} 轮", { count: fmtInt(d.break_count) }));
        setText("compare-clean-count", t("views.dashboard.compare.count", "{count} 轮", { count: fmtInt(d.clean_count) }));

        let key = "views.dashboard.compare.conclusionNone";
        let fb = "样本还不够，先观察几轮。";
        let times = 0;
        if (bAvg > 0 && cAvg > 0) {
            times = bAvg / cAvg;
            if (times >= 1.2) {
                key = "views.dashboard.compare.conclusionBad";
                fb = "断点轮的平均未命中是干净轮的 {times} 倍，前缀改写代价明显。";
            } else {
                key = "views.dashboard.compare.conclusionGood";
                fb = "断点轮与干净轮的平均未命中接近（{times} 倍），前缀基本稳定。";
            }
        }
        setText("compare-conclusion", t(key, fb, { times: times.toFixed(1) }));
    }

    // 4. 归因分布
    function renderCauses(d) {
        const host = byId("causes-list");
        if (!host) return;
        const list = Array.isArray(d.causes) ? d.causes.slice() : [];
        clearNode(host);

        if (!list.length) {
            const p = makeEl("p", "empty");
            p.textContent = t("views.dashboard.causes.empty", "暂无归因数据。");
            host.appendChild(p);
            return;
        }

        try {
            list.sort(function (a, b) { return num(b && b.count) - num(a && a.count); });
        } catch (err) {
            /* 忽略 */
        }

        for (let i = 0; i < list.length; i++) {
            const item = list[i] || {};
            const row = makeEl("div", "cause-row");

            const name = makeEl("span", "cause-name");
            name.textContent = item.label ? String(item.label) : causeLabel(item.cause, null);
            try {
                name.title = item.cause ? String(item.cause) : "";
            } catch (err) {
                /* 忽略 */
            }

            const track = makeEl("div", "cause-track");
            const fill = makeEl("div", "cause-fill");
            let pct = num(item.pct);
            if (!hasNum(item.pct) && num(d.req) > 0) pct = num(item.count) / num(d.req);
            if (!isFinite(pct) || pct < 0) pct = 0;
            if (pct > 1) pct = 1;
            try {
                fill.style.width = (pct * 100).toFixed(1) + "%";
            } catch (err) {
                /* 忽略 */
            }
            track.appendChild(fill);

            const meta = makeEl("span", "cause-meta");
            meta.textContent = t("views.dashboard.causes.meta", "{count} 次 · {pct}", {
                count: fmtInt(item.count),
                pct: fmtPercent(pct, 0)
            });

            row.appendChild(name);
            row.appendChild(track);
            row.appendChild(meta);
            host.appendChild(row);
        }
    }

    // 5. 命中率走势：整理数据点后交给 canvas
    function renderTrend(d) {
        const recent = Array.isArray(d.recent) ? d.recent : [];
        const slice = recent.slice(Math.max(0, recent.length - TREND_POINTS));
        const points = [];
        for (let i = 0; i < slice.length; i++) {
            const p = slice[i] || {};
            const prompt = num(p.prompt);
            const hit = num(p.hit);
            const miss = num(p.miss);
            const total = prompt > 0 ? prompt : hit + miss;
            const cause = p.cause ? String(p.cause) : "";
            points.push({
                rate: total > 0 ? (hit / total) : null,
                isBreak: cause !== "" && cause !== "append",
                cause: cause,
                ts: num(p.ts)
            });
        }
        state.trend = points;

        const emptyEl = byId("trend-empty");
        const wrap = byId("trend-canvas");
        if (!points.length) {
            setText("trend-empty", t("views.dashboard.trend.empty", "还没有请求样本。"));
            show(emptyEl);
            if (wrap && wrap.parentNode) hide(wrap.parentNode);
            return;
        }
        hide(emptyEl);
        if (wrap && wrap.parentNode) show(wrap.parentNode);
        drawTrend();
    }

    // 读取 CSS 变量（主题色随 <html data-theme> 变化）
    function cssVar(name, fallback) {
        try {
            const v = getComputedStyle(document.documentElement).getPropertyValue(name);
            if (v && v.trim()) return v.trim();
        } catch (err) {
            /* 忽略 */
        }
        return fallback;
    }

    // 手绘折线图，处理 devicePixelRatio
    function drawTrend() {
        const canvas = byId("trend-canvas");
        if (!canvas) return;
        const points = state.trend || [];

        let dpr = 1;
        try {
            dpr = window.devicePixelRatio || 1;
        } catch (err) {
            dpr = 1;
        }
        if (!isFinite(dpr) || dpr <= 0) dpr = 1;

        let cssW = 0;
        let cssH = 0;
        try {
            cssW = canvas.clientWidth || (canvas.parentNode ? canvas.parentNode.clientWidth : 0) || 600;
            cssH = canvas.clientHeight || (canvas.parentNode ? canvas.parentNode.clientHeight : 0) || 200;
        } catch (err) {
            cssW = 600;
            cssH = 200;
        }
        cssW = Math.max(80, cssW);
        cssH = Math.max(60, cssH);

        try {
            canvas.width = Math.round(cssW * dpr);
            canvas.height = Math.round(cssH * dpr);
        } catch (err) {
            /* 忽略 */
        }

        let ctx = null;
        try {
            ctx = canvas.getContext("2d");
        } catch (err) {
            ctx = null;
        }
        if (!ctx) return;

        try {
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        } catch (err) {
            try {
                ctx.scale(dpr, dpr);
            } catch (e2) {
                /* 忽略 */
            }
        }

        const accent = cssVar("--cl-accent", "#2f6feb");
        const soft = cssVar("--cl-accent-soft", "rgba(47,111,235,0.12)");
        const grid = cssVar("--cl-grid", "#edf1f7");
        const muted = cssVar("--cl-muted", "#667085");
        const bad = cssVar("--cl-bad", "#d92d20");

        try {
            ctx.clearRect(0, 0, cssW, cssH);
        } catch (err) {
            /* 忽略 */
        }

        const padL = 42;
        const padR = 12;
        const padT = 12;
        const padB = 22;
        const plotW = Math.max(1, cssW - padL - padR);
        const plotH = Math.max(1, cssH - padT - padB);

        const xy = function (rate, index, total) {
            const x = total > 1
                ? padL + (index / (total - 1)) * plotW
                : padL + plotW / 2;
            const y = padT + (1 - rate) * plotH;
            return { x: x, y: y };
        };

        // 网格与刻度
        try {
            ctx.lineWidth = 1;
            ctx.font = "11px system-ui, -apple-system, Segoe UI, sans-serif";
            ctx.textBaseline = "middle";
            ctx.textAlign = "right";
            const ticks = [0, 0.25, 0.5, 0.75, 1];
            for (let i = 0; i < ticks.length; i++) {
                const rate = ticks[i];
                const y = padT + (1 - rate) * plotH;
                ctx.strokeStyle = grid;
                ctx.beginPath();
                ctx.moveTo(padL, Math.round(y) + 0.5);
                ctx.lineTo(padL + plotW, Math.round(y) + 0.5);
                ctx.stroke();
                ctx.fillStyle = muted;
                ctx.fillText(Math.round(rate * 100) + "%", padL - 8, y);
            }
        } catch (err) {
            /* 忽略 */
        }

        // 有效点（rate 非 null）
        const valid = [];
        for (let i = 0; i < points.length; i++) {
            if (points[i].rate !== null && points[i].rate !== undefined) {
                valid.push({ index: i, rate: Math.max(0, Math.min(1, points[i].rate)), isBreak: points[i].isBreak });
            }
        }
        if (!valid.length) return;

        // 折线 + 面积
        try {
            ctx.beginPath();
            for (let i = 0; i < valid.length; i++) {
                const pos = xy(valid[i].rate, valid[i].index, points.length);
                if (i === 0) ctx.moveTo(pos.x, pos.y);
                else ctx.lineTo(pos.x, pos.y);
            }
            ctx.strokeStyle = accent;
            ctx.lineWidth = 2;
            ctx.lineJoin = "round";
            ctx.stroke();
        } catch (err) {
            /* 忽略 */
        }

        try {
            ctx.beginPath();
            const first = xy(valid[0].rate, valid[0].index, points.length);
            ctx.moveTo(first.x, padT + plotH);
            for (let i = 0; i < valid.length; i++) {
                const pos = xy(valid[i].rate, valid[i].index, points.length);
                ctx.lineTo(pos.x, pos.y);
            }
            const last = xy(valid[valid.length - 1].rate, valid[valid.length - 1].index, points.length);
            ctx.lineTo(last.x, padT + plotH);
            ctx.closePath();
            ctx.fillStyle = soft;
            ctx.fill();
        } catch (err) {
            /* 忽略 */
        }

        // 断点请求标红点
        try {
            for (let i = 0; i < valid.length; i++) {
                if (!valid[i].isBreak) continue;
                const pos = xy(valid[i].rate, valid[i].index, points.length);
                ctx.beginPath();
                ctx.arc(pos.x, pos.y, 3, 0, Math.PI * 2);
                ctx.fillStyle = bad;
                ctx.fill();
            }
        } catch (err) {
            /* 忽略 */
        }

        // 单点时补一个可见圆点
        if (valid.length === 1) {
            try {
                const pos = xy(valid[0].rate, valid[0].index, points.length);
                ctx.beginPath();
                ctx.arc(pos.x, pos.y, 3.5, 0, Math.PI * 2);
                ctx.fillStyle = accent;
                ctx.fill();
            } catch (err) {
                /* 忽略 */
            }
        }
    }

    // 6. 最近请求表
    function renderRecent(d) {
        const body = byId("recent-body");
        if (!body) return;
        const labelMap = buildCauseMap(d);
        const list = Array.isArray(d.recent) ? d.recent : [];
        const rows = list.slice(Math.max(0, list.length - RECENT_ROWS)).reverse();
        clearNode(body);

        const emptyEl = byId("recent-empty");
        if (!rows.length) {
            setText("recent-empty", t("views.dashboard.recent.empty", "还没有请求记录。"));
            show(emptyEl);
            return;
        }
        hide(emptyEl);

        for (let i = 0; i < rows.length; i++) {
            const row = rows[i] || {};
            const tr = makeEl("tr", "row");
            const cause = row.cause ? String(row.cause) : "";
            if (cause && cause !== "append") tr.className = "row row-break";

            const tdTime = makeEl("td");
            tdTime.textContent = fmtTime(row.ts);

            const tdCause = makeEl("td");
            let causeText = causeLabel(cause, labelMap);
            if (row.split !== null && row.split !== undefined && row.split !== "") {
                causeText += " · " + t("views.dashboard.cause.split", "切点 {n}", { n: String(row.split) });
            }
            tdCause.textContent = causeText;

            const tdN = makeEl("td", "num");
            tdN.textContent = fmtInt(row.n);

            const tdPrompt = makeEl("td", "num");
            tdPrompt.textContent = fmtTokens(row.prompt);

            const tdHit = makeEl("td", "num");
            tdHit.textContent = fmtTokens(row.hit);

            const tdMiss = makeEl("td", "num");
            tdMiss.textContent = fmtTokens(row.miss);
            if (num(row.miss) > 0 && num(row.prompt) > 0 && num(row.miss) / num(row.prompt) >= 0.2) {
                tdMiss.className = "num bad";
            }

            const tdEst = makeEl("td", "num");
            tdEst.textContent = fmtTokens(row.est);

            tr.appendChild(tdTime);
            tr.appendChild(tdCause);
            tr.appendChild(tdN);
            tr.appendChild(tdPrompt);
            tr.appendChild(tdHit);
            tr.appendChild(tdMiss);
            tr.appendChild(tdEst);
            body.appendChild(tr);
        }
    }

    // 7. 底部信息
    function renderFooter(d) {
        setText("footer-version", t("views.dashboard.footer.version", "版本 {v}", {
            v: d.version ? String(d.version) : "—"
        }));
        setText("footer-priority", t("views.dashboard.footer.priority", "优先级 {v}", {
            v: hasNum(d.priority) ? String(Math.round(num(d.priority))) : "—"
        }));
        setText("footer-tokens", t("views.dashboard.footer.tokens",
            "累计 prompt {prompt} / 输出 {out}", {
            prompt: fmtTokens(d.prompt_tokens),
            out: fmtTokens(d.out_tokens)
        }));

        const budget = d.budget && typeof d.budget === "object" ? d.budget : {};
        const unlimited = t("views.dashboard.footer.budgetUnlimited", "不限");
        const fmtBudget = function (v) {
            return num(v) > 0 ? fmtTokens(v) : unlimited;
        };
        setText("footer-budget", t("views.dashboard.footer.budget",
            "预算 尾部 {tail} / 上下文 {ctx} / 单条 {msg}", {
            tail: fmtBudget(budget.max_tail_chars),
            ctx: fmtBudget(budget.max_context_chars),
            msg: fmtBudget(budget.max_msg_chars)
        }));

        const defer = Array.isArray(d.defer_to) ? d.defer_to : [];
        if (defer.length) {
            setText("footer-defer", t("views.dashboard.footer.defer", "让路插件：{list}", {
                list: defer.join("、")
            }));
        } else {
            setText("footer-defer", "");
        }
    }

    /* ===================== 交互与生命周期 ===================== */

    function bindUi() {
        const btn = byId("refresh-btn");
        if (btn) {
            btn.addEventListener("click", function () {
                loadData();
            });
        }
        const retry = byId("error-retry");
        if (retry) {
            retry.addEventListener("click", function () {
                loadData();
            });
        }
        // 窗口尺寸变化：防抖后重绘（重绘保持 devicePixelRatio）
        window.addEventListener("resize", function () {
            try {
                if (state.resizeTimer) clearTimeout(state.resizeTimer);
                state.resizeTimer = setTimeout(function () {
                    try {
                        drawTrend();
                    } catch (err) {
                        /* 忽略 */
                    }
                }, 120);
            } catch (err) {
                /* 忽略 */
            }
        });
        // 页面重新可见时补一次刷新与重绘
        document.addEventListener("visibilitychange", function () {
            try {
                if (document.hidden) return;
                drawTrend();
                loadData();
            } catch (err) {
                /* 忽略 */
            }
        });
    }

    // 15 秒自动刷新；页面隐藏时暂停
    function startAutoRefresh() {
        try {
            if (state.timer) clearInterval(state.timer);
            state.timer = setInterval(function () {
                try {
                    if (document.hidden) return;
                    loadData();
                } catch (err) {
                    /* 忽略 */
                }
            }, REFRESH_MS);
        } catch (err) {
            /* 忽略 */
        }
    }

    // 语言 / 主题变化时重刷文案并重绘图表
    function bindContext(b) {
        try {
            if (!b || typeof b.onContext !== "function") return;
            b.onContext(function (ctx) {
                try {
                    if (ctx && typeof ctx === "object") state.ctx = ctx;
                    applyI18n(document);
                    renderAll();
                } catch (err) {
                    /* 忽略 */
                }
            });
        } catch (err) {
            /* 忽略 */
        }
    }

    // 主题变化（外部切换 data-theme）也要重绘，保证颜色跟随
    function watchTheme() {
        try {
            const target = document.documentElement;
            if (!target || typeof MutationObserver !== "function") return;
            const ob = new MutationObserver(function () {
                try {
                    drawTrend();
                } catch (err) {
                    /* 忽略 */
                }
            });
            ob.observe(target, { attributes: true, attributeFilter: ["data-theme", "class"] });
        } catch (err) {
            /* 忽略 */
        }
    }

    /* ===================== 启动 ===================== */

    function boot() {
        applyI18n(document);
        return waitForBridge(BRIDGE_TIMEOUT_MS).then(function (b) {
            if (!b) {
                showError(t("views.dashboard.error.bridge", "插件视图 SDK 未就绪，无法加载数据。"));
                return null;
            }
            state.bridge = b;
            let ready = null;
            try {
                ready = typeof b.ready === "function" ? b.ready() : null;
            } catch (err) {
                ready = null;
                showError(t("views.dashboard.error.ready", "视图初始化失败：{msg}", { msg: errText(err) }));
            }
            if (!ready || typeof ready.then !== "function") {
                return null;
            }
            return ready.then(function (ctx) {
                if (ctx && typeof ctx === "object") state.ctx = ctx;
                return null;
            }, function (err) {
                showError(t("views.dashboard.error.ready", "视图初始化失败：{msg}", { msg: errText(err) }));
                return null;
            });
        }).then(function () {
            bindContext(state.bridge);
            watchTheme();
            bindUi();
            startAutoRefresh();
            applyI18n(document);
            return loadData();
        }).catch(function (err) {
            showError(t("views.dashboard.error.load", "加载失败：{msg}", { msg: errText(err) }));
        });
    }

    function start() {
        try {
            const p = boot();
            if (p && typeof p.catch === "function") {
                p.catch(function () {
                    /* 兜底，理论上不会走到 */
                });
            }
        } catch (err) {
            showError(t("views.dashboard.error.load", "加载失败：{msg}", { msg: errText(err) }));
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start);
    } else {
        start();
    }
})();