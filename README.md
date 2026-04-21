# Shift Auto-Claim Bot: An Exercise in API Reverse-Engineering and Rate-Limit Analysis

A Python bot that watches a university workforce-management portal for newly-posted work shifts and automatically claims eligible ones, with configurable filters for shift timing and personal availability.

**Status:** Experimental. The project successfully demonstrated end-to-end functionality but was ultimately shelved due to server-side rate-limiting constraints that made sustained 24/7 operation impractical. Full details in the [Findings](#findings) section.

---

## Motivation

The portal in question is a commercial workforce-management platform used by my university's student employment office. Work shifts are occasionally dropped (un-claimed) by other employees and posted to a "swap board" where anyone can claim them. In practice, desirable shifts are claimed within 1–2 seconds of appearing, rewarding whoever has the page open and is refreshing at the right moment.

This project was an attempt to automate the detection-and-claim flow to participate in the same race more reliably, while applying several custom filters (skip overnight shifts, skip shifts I couldn't realistically prepare for, etc.).

---

## What the bot does

1. **Authenticates** against the portal by scraping the anti-forgery token from the sign-in page, POSTing credentials to the login endpoint, and extracting an `APP.Token` value embedded in the post-login landing page.
2. **Polls the swap board's summary endpoint** on a configurable interval. This endpoint returns per-day shift counts across the upcoming 30 days in a single request.
3. **Caches per-day signatures** so that the heavier "details for day X" endpoint is only hit when the count for that day actually changes, minimizing server load and API call volume.
4. **Filters candidate shifts** against user-defined rules:
   - Skip overnight shifts (12 AM – 8 AM starts)
   - Skip shifts already assigned to me
   - Skip shifts starting within a commute-buffer window (default 60 minutes)
   - Skip morning shifts posted during my sleep window (won't wake up in time to prepare)
5. **Claims eligible shifts** via a single `PUT` request to the platform's "quick-claim" endpoint, measured at ~450ms end-to-end latency from detection.
6. **Handles session expiry** automatically by re-authenticating on HTTP 401.
7. **Implements exponential backoff** on rate-limit responses (30s → 60s → 120s → 240s → 300s cap).

---

## Architecture

```
┌─────────────────────┐
│   rso_bot.py        │
│                     │
│  ┌───────────────┐  │
│  │ TeamWorkClient│  │ ← session cookies + x-api-token
│  │   - login()   │  │
│  │   - get_*()   │  │
│  │   - claim()   │  │
│  └───────┬───────┘  │
│          │          │
│  ┌───────▼───────┐  │
│  │   ShiftBot    │  │ ← filter logic, signature cache,
│  │ - poll_once() │  │   deduplication, backoff
│  │ - should_claim│  │
│  └───────────────┘  │
└─────────────────────┘
        │
        ▼
  config.py (gitignored)
  - credentials
  - poll rate
  - filter settings
```

Key design decisions:
- **Two-tier polling** (cheap summary + per-day details) to amortize the cost of freshness checks
- **Dataclass-based** shift representation for clarity
- **Dry-run mode** built in from the start so filter logic could be validated without actually claiming
- **All tunables in config.py** so behavior could be adjusted without code changes

---

## How it was built

### Reverse-engineering the API

The platform is a single-page application backed by a JSON API. The workflow was:

1. **Captured HAR files** of manual usage (login flow, swap board navigation, shift claim).
2. **Grepped the SPA's JavaScript bundles** for function names like `ShowClaim`, `QuickClaim`, and `swapboard` to map UI actions to API endpoints.
3. **Identified the authentication scheme**: session cookies + a per-session `APP.Token` value injected server-side into post-login HTML as a literal string, used as the `x-api-token` header on all subsequent API calls.
4. **Identified the anti-forgery token**: a standard ASP.NET `__RequestVerificationToken` scraped from the login form.
5. **Reconstructed the claim request**: `PUT /api/shift/swap/quick-claim?id=<shift_id>&bid=<location_id>&schid=`, with no body, relying entirely on query string and auth headers.

The most useful single artifact was the SPA's own JavaScript — the `QuickClaim` function literally spells out the request signature:

```javascript
var url = APP.Root + "api/shift/swap/quick-claim?id=" + sftId + "&bid=" + locId + "&schid=";
AppData.SendAjax("PUT", url).then(function (result) { ... });
```

From there, replicating the browser's behavior in Python with `requests` was mechanical.

### Anti-detection measures (that turned out not to matter)

To blend in with normal browser traffic, the bot:

- Uses the exact `User-Agent` observed in real browser traffic
- Sends all `sec-ch-ua-*`, `sec-fetch-*`, `Accept-Language`, `Accept-Encoding`, and `priority` headers the real browser sends
- Adds randomized jitter (±0.5s) to poll intervals
- Maintains persistent session cookies across requests

As [described below](#findings), these measures made no measurable difference. The server's rate limiter appears to count per account / session, not per fingerprint.

---

## Findings

This is the honest part — the part that made the project educational but ultimately caused it to be shelved.

### The rate-limit pattern

Four test runs at different poll rates produced a consistent pattern:

| Poll interval | Requests/hour | Time until HTTP 400 | Total requests before trip |
|---|---|---|---|
| 1.5s | ~2,400 | ~25 seconds | ~33 |
| 2.0s | ~1,800 | ~7 minutes | ~210 |
| 2.0s (with caching) | ~1,800 | ~9 minutes | ~270 |
| 4.0s | ~900 | ~41 minutes | ~610 |

Extrapolating from these four points, the server appears to enforce a sliding-window rate limit of roughly **600–900 requests per window**, with a cooldown of 15–30 minutes. The limit is enforced by returning HTTP 400 on all subsequent API requests (an unusual choice — most services use 429 Too Many Requests for this).

### The rate-limit counts against the legitimate user, not just the bot

The most important finding: **while the bot was rate-limited, the user's normal browser access was also blocked.** The throttle is tied to the account, not the session fingerprint. This meant every test session temporarily locked the real user out of their own dashboard, potentially causing them to miss the very shifts the bot was meant to catch. Net utility: likely negative.

### Browser fingerprinting is irrelevant

The jump from "basic headers" to "byte-for-byte identical to Chrome DevTools output" produced no change in time-to-throttle. The server is counting events, not inspecting them.

### Sub-second polling is infeasible

Polling faster than the server's own UI (1.5s) triggers the rate limit in seconds rather than minutes, strongly implying the server treats its published UI cadence as a soft ceiling. Respecting that ceiling extends uptime but still hits the longer-window cap eventually.

### Business-logic errors exist alongside rate-limit errors

One claim attempt returned `HTTP 417: "Times open in schedule?"` — the server correctly detected a time conflict with an existing shift on the user's schedule and refused the claim. This was handled correctly (failed claims don't get retried) but highlighted that the claim endpoint has multiple failure modes beyond rate limiting.

---

## Why the project was shelved

After the 41-minute test run at 4-second polling concluded with another lockout — this time also affecting browser access — the project reached a clear decision point:

1. Any poll rate fast enough to reliably beat 2-second shift lifetimes eventually triggers a lockout
2. Lockouts affect the legitimate user, not just the bot
3. Slower poll rates (8s+) are safe but offer minimal advantage over manual refreshing
4. Continued iteration against the rate limiter carries nonzero risk of account flagging

The engineering was working correctly. The constraints made the goal unachievable. Shelving was the right call.

If I were to revisit this, the only realistic path forward would be a **notification-only bot**: poll once every 30 seconds (well under the rate limit), send a push notification when a new shift appears, and let the user decide whether to claim it manually. This trades some speed for sustainability, and sidesteps the business-logic pitfalls (time conflicts, ineligibility) by keeping the human in the loop.

---

## Tech stack

- Python 3.11+
- `requests` for HTTP
- Standard library only for everything else (dataclasses, logging, re, datetime)

No async, no scraping frameworks, no browser automation. Deliberately minimal — the whole thing is ~625 lines in a single file.

---

## Repo layout

```
.
├── rso_bot.py            # Main bot: client, filter logic, poll loop
├── config.example.py     # Template for user-specific settings
├── requirements.txt      # Just `requests`
├── .gitignore            # Excludes config.py and logs
└── README.md             # This file
```

`config.py` is gitignored — users copy `config.example.py` and fill in their own credentials.

---

## What I'd do differently

- **Start with a time-budget calculation.** Before writing a line of code, I should have estimated "how often do shifts drop, how long do they live, and what polling rate is needed to catch them" versus "what polling rate is the server actually going to tolerate." If I'd plotted those two on the same axis up front, the unfeasibility would have been visible before implementation.
- **Test rate limits experimentally before committing to an architecture.** The first hour of dev should have been a throwaway script that just hammered the cheapest endpoint at various intervals to map the rate-limit curve. Instead I built the full system first and discovered the constraint later.
- **Build the notification-only version first.** It's a strict subset of the auto-claim version, is safer, and would have been the fallback anyway. "Start with the safer version, add automation only if it's provably needed" is a better default than the reverse.

---

## Disclaimer

This repository is an engineering portfolio piece. It is not intended for use against any specific service, is not deployed, and does not contain credentials or identifying references to any real platform or organization. The code as written will not work without a target that happens to expose a compatible API shape; replicating it against a real service may violate that service's terms of use and/or your organization's acceptable-use policies.

If you're considering building something similar: check your employer's/organization's automation policy first, and seriously consider whether a notification-only tool would meet your real needs.
