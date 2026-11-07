# Context Launcher

> Per-request attribution for prefix-cache misses, reconciled against the provider's own usage figures. Observation is read-only by default; compression is off by default.

AstrBot 4.16+ · no third-party dependencies · MIT · v2.0.1 · [中文](README.md)

## Overview

Every turn, AstrBot assembles the system prompt, tool definitions, conversation history and tail injections into one request. A provider's prefix cache only hits when this request shares a byte-exact prefix with the previous one: the cached part is billed at a discount, the rest at full price. When the hit rate drops, the bill alone cannot tell you which component changed the prefix.

The plugin addresses this in three parts:

- **Attribution**: each request fingerprints the system prompt, tool definitions, every history message and the tail injection, then diffs against the previous turn in the same session. The first divergence is classified into one of eight causes (append, tail change, history rewrite, tool result rewrite, system prompt change, history shrink, session start, own compression).
- **Reconciliation**: hit and miss tokens come straight from the provider's `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`. Nothing is estimated. Break turns and clean turns are averaged separately, so you can tell whether rewriting history actually pays off.
- **Compression** (optional, off by default): tool-result trimming and oversized-message trimming. Both are deterministic — the same message yields the same result on every turn, so the plugin never causes a break on its own.

Deliberately out of scope:

- No LLM tools are registered. Tool definitions sit in the prompt every turn and are a permanent cost; an observability plugin should not add to it.
- No LLM summarization. AstrBot's core overflow strategy already covers that, and summaries differ every turn — writing them back into history means rewriting the prefix continuously, the exact cost this plugin is built to remove.
- No access to other plugins' data. When a competing plugin is present, the plugin defers per `defer_to`, observes only, and says so on the panel.

## Effect

![Dashboard](docs/images/dashboard-zh.png)

> Panel illustration: figures come from real request records of 2026-11-07; UI language follows the WebUI setting.

### Measured data

Environment: Tencent Cloud 4C4G, Ubuntu 24.04, AstrBot 4.29.3 + OneBot v11, model `deepseek-flash`, `mode=observe`, compression off.

Plugin side (54 requests, 2026-11-04 02:13 through 11-07 19:08):

| Metric | Value |
|---|---|
| Requests | 54 |
| Hit rate | 90.2% (token-weighted) |
| Median per-turn hit rate | 94.1% |
| Average miss | 3,871 tokens/request (median 2,352, worst 22,293) |
| Prefix breaks | 2 (an old message edited in place) |
| System prompt | 12,840 chars (13,645 after a summary is injected) |
| Tool definitions | 45 |

Provider side (`deepseek-flash`, same machine):

| Range | Requests | Hit rate | Miss per request |
|---|--:|--:|--:|
| 10-27 ~ 11-02 | 561 | 66.0% | 8,453 |
| 11-03 ~ 11-07 | 87 | 90.2% | 3,912 |
| Last 7 days | 177 | 81.9% | 5,702 |
| All (from 10-23) | 1,332 | 78.9% | 6,481 |

The last two rows exist for reconciliation. The official console counts the whole account, including days before the optimization, so it reads 79–82%. This panel only counts requests after the plugin was installed, hence the low 90s. Both are correct; the difference is the window. Compare like for like.

One caveat: before installing the plugin I had already cut tool definitions (about 100 down to 45) and shrank the history (1.06M characters to 216k). The figures above cannot be attributed to any single change.

## Features

| Feature | Description | Default |
|---|---|:--:|
| Request attribution | Locate the first divergence per turn and classify its cause | on |
| Usage reconciliation | Hit rate from provider usage, break turns vs clean turns | on |
| Dashboard | WebUI plugin page showing cause distribution, recent requests, budget warnings | on |
| Commands | `/上下文` family, for when the dashboard is unavailable | on |
| Tool-result trimming | Collapse older tool results in history to one line | off |
| Oversized-message trimming | Keep head and tail, drop the middle, archive the original | off |
| Break warnings | Log when a break or budget overrun is detected | on |

## Installation

**Marketplace**: search for "上下文管家" on the AstrBot WebUI plugin page, install, restart.

**Manual**: place the repository under `data/plugins/astrbot_plugin_context_launcher/` and restart AstrBot. Only bundled AstrBot components are used.

**First run**: keep `mode=observe`, chat for a few turns, then open the dashboard. Read the cause distribution before enabling compression; compression only runs in `guard` mode.

## Configuration

Mirrors `_conf_schema.json`. In most deployments only the first two entries matter.

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Master switch; the hook returns immediately when off |
| `mode` | string | `observe` | `observe` observes only; `guard` permits compression below |
| `max_tail_chars` | int | `3000` | Warn when the tail injection exceeds this many characters; `0` disables |
| `max_context_chars` | int | `0` | Warn when total history characters exceed this; `0` disables |
| `compress_tool_results` | bool | `false` | Tool-result trimming |
| `compress_tool_keep_recent` | int | `2` | Keep this many most recent tool results verbatim |
| `compress_tool_preview_chars` | int | `120` | Characters kept from the first line after trimming |
| `compress_oversize` | bool | `false` | Oversized-message trimming |
| `compress_max_msg_chars` | int | `12000` | Only messages longer than this are processed |
| `compress_keep_head_chars` | int | `4000` | Characters kept from the head |
| `compress_keep_tail_chars` | int | `1500` | Characters kept from the tail |
| `compress_keep_recent_msgs` | int | `2` | Most recent messages are never trimmed |
| `compress_archive` | bool | `true` | Archive trimmed originals into the plugin data directory |
| `log_events` | bool | `true` | Write one JSONL line per request |
| `warn_on_break` | bool | `true` | Log on breaks or budget overruns |
| `defer_to` | list | `["astrbot_plugin_context_slim"]` | Defer list; observation only when these are installed |

## Commands

| Command | Effect |
|---|---|
| `/上下文` | Summary: mode, hit rate, miss per request, breaks, compression savings |
| `/上下文 诊断` | Details of the most recent request |
| `/上下文 归因` | Counts and share per cause |
| `/上下文 重置` | Clear statistics for the current run |
| `/上下文 压缩 on\|off` | Toggle all compression at runtime, nothing written to disk |

## How it works

1. Hooks `on_llm_request` at priority `-20`, i.e. last, so it sees the final payload assembled by every other plugin.
2. Fingerprints the system prompt, tool definitions, each history message and the tail injection, then diffs against the previous turn. Order: session start → system prompt → tool definitions → first differing history message → length change → tail change.
3. Attribution runs before compression, so breaks caused by other plugins and trims caused by this plugin are recorded separately.
4. After the response arrives, usage is read and hit/miss tokens are attributed to that request. When the provider does not report cache fields, only totals are recorded — never a fabricated 0%.

## Compression (off by default)

| Mode | Config | When it helps | Cost |
|---|---|---|---|
| Tool-result trimming | `compress_tool_results` | Other plugins dump whole pages, files or long logs into history and later turns no longer reference them | Edits land mid-history; raise `compress_tool_keep_recent` alongside |
| Oversized-message trimming | `compress_oversize` | A single message is tens of thousands of characters (documents, pasted logs, OCR output) and is no longer in the recent window | Keeps head and tail, drops the middle, archives the original |

Both require `mode=guard`; nothing is rewritten under `observe`. `/上下文 压缩 on|off` toggles them at runtime without touching the config file.

Compression edits messages in history. Since cache hits require a byte-identical prefix, everything after the edited message is re-billed. The "break turns vs clean turns" split on the panel exists to account for that trade-off.

## Why there is no LLM summarization

AstrBot's core overflow strategy already handles it: when the context exceeds the model window, history is truncated or rewritten. Reimplementing it would be duplicate work and would fight the core's rewriting.

The more important reason is determinism. An LLM summary differs every turn; writing it back into history means rewriting the prefix on every turn — precisely the cost this plugin exists to remove. Trimming can be deterministic; summarization cannot.

## Coexistence with other context plugins

`defer_to` includes `astrbot_plugin_context_slim` by default. When that plugin is installed, this one observes only and states that it is deferring, so two plugins never rewrite history at once.

Add other plugin names to defer to them, or clear the list to defer to nobody.

With the core overflow compression, the overlap is "both rewrite history". Use the panel first to confirm whether the low hit rate is caused by an oversized history or by the core already rewriting, then enable `guard` and trim only what this plugin should trim.

## Limitations

- Only requests passing through this plugin's hook are counted; calls made with the same API key by other programs are excluded.
- The official console counts the whole account and is not directly comparable to the panel (see measured data above).
- When a provider omits cache fields, only totals are available and the hit rate is reported as unavailable.
- Statistics start at plugin load, reset on restart, and are not backfilled. `events.jsonl` can be retained externally.
- Token estimates on the panel are character-based and run roughly 30% low in practice. Use them for magnitude only, never for billing reconciliation.
- "The prefix changed" and "the bill hit the cache" are different questions; provider-side cache expiry, eviction and routing sit in between.
- Only plain-text content is processed. Multimodal part lists are skipped and never rewritten.
- Trimmed messages keep an omission marker that the model can see. This is intentional.
- The dashboard requires AstrBot 4.16 or later; older versions use commands only.
- The tail budget defaults to 3,000 characters. Measured injections run 2,000–3,600; heavier plugin stacks can exceed that, so raise it or set `0`.

## Data and privacy

The plugin writes only to its own data directory:

- `events.jsonl`: one line per request when `log_events=true`; rotated when oversized.
- `archive/`: trimmed originals, named by content hash, written once per unique content, counted but not written after the size cap.

Removing these files does not affect operation; turning the switches off stops them being produced.

The plugin makes no external requests, has no telemetry and reports nothing. `observe` mode leaves `req` untouched; `guard` touches history text only. Any exception is logged and returns zero statistics — the chat pipeline is never interrupted.

Note: `events.jsonl` contains `umo`, which embeds platform account identifiers. Redact before sharing publicly.

## Changelog

**2.0.1**

- Default `max_tail_chars` raised from 1800 to 3000: measured tail injections regularly exceed the old default, so the warning fired on almost every request.
- Cache-field notice now distinguishes "model does not report it" from "no successful request yet".

**2.0.0**

- First release: request attribution, usage reconciliation, dashboard, optional deterministic compression.

## License

[MIT](./LICENSE) · Copyright (c) 2026 context_launcher contributors