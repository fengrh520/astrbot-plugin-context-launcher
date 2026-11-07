# Context Launcher

An AstrBot plugin that shows whether each LLM request actually hit the prefix cache — and when it didn't, what changed the prefix.

It reads the assembled request and does not touch it. Default mode is `observe`: not a single byte is modified. No LLM tools registered, no network calls, one JSONL file in its own data directory.

AstrBot 4.16+ · no third-party dependencies · MIT · v2.0.1 · [中文](README.md)

Prefix caching is a discount from the provider: if the start of this request is byte-identical to the last one, that part is billed at the cache rate, usually about a tenth of the normal price. So the expensive thing isn't a long context — it's a context whose beginning keeps changing, and everything after the change point gets recomputed.

![Dashboard](docs/images/dashboard-zh.png)

> The screenshot uses sample data; the dashboard follows the WebUI language.

## Install

```bash
cp -r astrbot_plugin_context_launcher /path/to/astrbot/data/plugins/
docker restart astrbot
```

After the restart the WebUI plugin page has a "Context Launcher → Dashboard" entry: hit rate, average miss tokens per request, break count, characters saved by compression. It refreshes itself every 15 seconds.

If you'd rather not open a page, send `/上下文`.

## What it actually looks at

Plenty of things in AstrBot mutate a request: persona, tools, memory plugins, context compression. They all hook `on_llm_request` at priorities between 0 and 100. This plugin uses `-20`.

Negative priorities run last, so it sees the payload after everyone has injected — the one that is about to be sent.

It fingerprints three things separately: the system prompt, the tool definitions, and every history message. Then it compares them against the previous turn of the same session:

| Cause | Meaning |
|---|---|
| `append` | Prefix is byte-identical, messages were only appended. The good case |
| `tail` | Only the injected tail changed |
| `system` | System prompt changed, everything is recomputed |
| `tools` | Tool definitions changed, everything is recomputed |
| `history` | A message in the middle of the history was rewritten |
| `history_tool` | Same, and the rewritten message is a tool result |
| `history_shrink` | History got shorter — the window slid back |

Hit rate is not estimated. It comes from the provider's `prompt_cache_hit_tokens` and `prompt_cache_miss_tokens`. When a provider doesn't return those fields the dashboard says so plainly instead of showing a fake 0%.

## Four days on my own box

A 4C4G Tencent Cloud instance running OneBot and DeepSeek, observe mode, all compression off.

54 requests the plugin recorded (11-04 02:13 through 11-07 19:08):

```
48 turns had an unchanged prefix, 4 were session starts, 2 had an old message edited in place
On the unchanged ones: median hit rate 94.1%, median miss 2,352 tokens (worst 22,293, best 232)
System prompt 12,840 chars (13,645 after a summary is injected), 45 tool definitions — in the prompt every turn
History stayed between 102 and 200 messages; it was trimmed once
```

The provider's own numbers from the same machine (`deepseek-flash`):

```
10-27 ~ 11-02    561 requests   hit 66.0%   8,453 miss tokens per request
11-03 ~ 11-07     87 requests   hit 90.2%   3,912 miss tokens per request
last 7 days      177 requests   hit 81.9%   5,702 miss tokens per request
all (from 10-23) 1,332 requests hit 78.9%   6,481 miss tokens per request
```

The last two rows exist for reconciliation. The official console counts everything on the account, so it lands in the 79–82% range; this dashboard only counts what happened after the plugin was installed, i.e. the post-optimization days, so it reads low-90s. Both are right — they just cover different windows. Compare like for like.

Early in November I cut tool definitions from ~100 to 45 and shrank the history from 1.06M characters to 216k. The plugin was installed after that, so what it recorded is the post-change state. More than one thing changed in between, so these numbers are not the credit of any single edit.

The interesting row is this one:

```
11-05 10:14   append   hit 9,728   miss 22,293   took 123 seconds
```

The prefix did not change at all — attribution is `append` — and the hit rate was still 30%. Eight hours had passed since the previous turn, so the provider's cache had most likely expired.

Which is the point: attribution and billing are two different axes. A payload nobody touched is not necessarily a cheap payload. Looking at only one of them gets you the wrong conclusion, and that is why the dashboard keeps "break turns" and "clean turns" apart.

## Two compressions, both off by default

The cost first. A compression has to rewrite one message in the history, and a cache hit requires a byte-identical prefix, so everything after that message is billed again. What you save is a few tokens per turn from then on; what you pay is one recomputation from the break point. The "break turns vs clean turns" comparison exists to put a number on that trade.

Both compressions are deterministic: the same history message produces the same result every turn, so the prefix isn't broken again on the next request.

| Compression | Config | When it pays off | Cost |
|---|---|---|---|
| Tool-result trimming | `compress_tool_results` | Some plugin dumps whole web pages, files or logs into history and nothing references them any more | Rewrites mid-history; raise `compress_tool_keep_recent` alongside it |
| Oversize message trimming | `compress_oversize` | Single messages of tens of thousands of characters, already out of the recent turns | Head and tail kept, middle elided, original archived |

Both require `mode: guard`. Nothing is rewritten in `observe`. If you don't want to edit config, `/上下文 压缩 on` and `/上下文 压缩 off` toggle them at runtime; nothing is written to disk.

## Why there is no "summarise the old conversation with an LLM"

Because AstrBot core already does it. When the context really overflows, core's overflow strategy fires and shortens the old conversation. Implementing it again would be duplicated work that fights the core's own rewrite.

More importantly: an LLM summary reads differently every turn. Writing it back into history means rewriting the prefix on every request — exactly the cost this plugin exists to remove. Trimming can be made deterministic. Summaries cannot.

If you do want summaries, use core's overflow strategy. It follows the hard context-window limit, which is what it is for.

## Configuration

Mirrors `_conf_schema.json`. Most people only need the first two rows.

| Key | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `true` | Master switch; the hooks return immediately when off |
| `mode` | string | `observe` | `observe` only measures; `guard` allows compression |
| `max_tail_chars` | int | `3000` | Warn when the injected tail exceeds this; `0` disables |
| `max_context_chars` | int | `0` | Warn when total history chars exceed this; `0` disables |
| `compress_tool_results` | bool | `false` | Trim tool results |
| `compress_tool_keep_recent` | int | `2` | Keep the last N tool results verbatim |
| `compress_tool_preview_chars` | int | `120` | Characters kept from the first line |
| `compress_oversize` | bool | `false` | Trim oversized messages |
| `compress_max_msg_chars` | int | `12000` | Threshold before trimming |
| `compress_keep_head_chars` | int | `4000` | Head kept when trimming |
| `compress_keep_tail_chars` | int | `1500` | Tail kept when trimming |
| `compress_keep_recent_msgs` | int | `2` | Most recent messages never trimmed |
| `compress_archive` | bool | `true` | Archive the removed text under the plugin data dir |
| `log_events` | bool | `true` | One JSONL line per request |
| `warn_on_break` | bool | `true` | Log on breaks and over-budget payloads |
| `defer_to` | list | `["astrbot_plugin_context_slim"]` | If these are installed, observe only |

## Commands

| Command | Effect |
|---|---|
| `/上下文` | Summary: mode, hit rate, miss per request, break count, compression savings |
| `/上下文 诊断` | Detail of the latest request |
| `/上下文 归因` | Count and share per cause |
| `/上下文 重置` | Clear the current run's stats |
| `/上下文 压缩 on\|off` | Toggle all compression |

## Things you should know

- On my box the injected tail runs 2,000–3,600 characters. The default 3,000 already filters out most normal turns; if you run more plugins and your tail is fatter, raise it, or set `max_tail_chars` to 0.
- Token counts are estimated from characters, and they read low. One measured case: estimated 22,755 against an actual 32,021 — 30% off. Use them for magnitude, not for accounting.
- Stats start when the plugin starts and reset on restart. No history is backfilled. If you want a record, keep `events.jsonl`.
- Hit rate depends entirely on whether your provider reports cache usage. Models that don't report it give you totals only.
- "The prefix didn't change" and "the request was cheap" are different statements; provider cache expiry, eviction and routing sit in between.
- Only plain-text `content` is handled. Multimodal part lists are skipped rather than rewritten on a guess.
- A trimmed message is visibly trimmed to the model. That is intentional; the elision is marked.
- The dashboard needs AstrBot 4.16+. Older versions still have the commands.

## Running next to other context plugins

`defer_to` ships with `astrbot_plugin_context_slim`. When it's installed this plugin observes only, and says so on the dashboard, so two plugins don't rewrite each other's history.

Point `defer_to` at something else, or empty it to defer to nobody.

Core's built-in overflow compression overlaps in one place: both rewrite history. This plugin's position is to look first — use the dashboard to confirm whether the low hit rate is really caused by long history, and whether core's compression is already running. Then enable `guard` and trim only the part you own, booked separately as `trim`.

## Where data goes

Only the plugin's own data directory.

- `events.jsonl` — one line per request, written when `log_events=true`, rotated when it grows too large.
- `archive/` — the text removed by trimming, named by content hash, written once per distinct content; past the size cap it is counted but not written.

Deleting either is harmless. Turning the switches off stops them being produced.

## Privacy

The plugin makes no outbound requests. Observations and archived text stay local; nothing is reported or telemetered.

`observe` modifies nothing in `req`. `guard` modifies text of history messages only. Any failure logs at debug level and returns zero stats; it never takes the chat pipeline down.

It also registers no LLM tools. Tool definitions ride in the prompt every turn, which is a permanent cost, and an observability plugin shouldn't make itself expensive.

## License

[MIT](./LICENSE) · Copyright (c) 2026 context_launcher contributors
