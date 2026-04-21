#!/usr/bin/env python3
"""
RSO Shift Auto-Claim Bot for TeamWork by ScheduleSource (tmwork.net)

Watches the SwapBoard for available shifts and claims them automatically,
skipping midnight shifts (12 AM - 8 AM).

Usage:
    1. Copy config.example.py to config.py and fill in your credentials
    2. python3 rso_bot.py
"""

import json
import logging
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import requests

try:
    import config
except ImportError:
    print("ERROR: config.py not found. Copy config.example.py to config.py and fill in your credentials.")
    sys.exit(1)


# ============================================================================
# CONFIGURATION (pulled from config.py)
# ============================================================================
BASE_URL = "https://tmwork.net"
EMP_CODE = config.EMP_CODE
EMP_USER = config.EMP_USER
EMP_PASSWORD = config.EMP_PASSWORD

# Polling behavior
# Based on empirical testing, tmwork.net has a sliding-window rate limit of
# roughly 1,000 requests/hour. We default to 4.0s (900 req/hr) which stays
# safely under that threshold. DO NOT go below 3s without clear evidence the
# limit has been raised - at 2s we hit 400s after ~9 minutes every time.
POLL_INTERVAL_SECONDS = getattr(config, "POLL_INTERVAL_SECONDS", 4.0)
POLL_JITTER_SECONDS = getattr(config, "POLL_JITTER_SECONDS", 0.5)
DAYS_AHEAD_TO_CHECK = getattr(config, "DAYS_AHEAD_TO_CHECK", 30)

# Rate-limit back-off: starts at this, doubles on each consecutive failure up to max
RATE_LIMIT_BACKOFF_START = getattr(config, "RATE_LIMIT_BACKOFF_START", 30.0)
RATE_LIMIT_BACKOFF_MAX = getattr(config, "RATE_LIMIT_BACKOFF_MAX", 300.0)

# Filter rules
SKIP_MIDNIGHT_SHIFTS = getattr(config, "SKIP_MIDNIGHT_SHIFTS", True)
# Start hours in [MIDNIGHT_START_HOUR, MIDNIGHT_END_HOUR) are considered midnight
MIDNIGHT_START_HOUR = 0  # 12 AM
MIDNIGHT_END_HOUR = 8    # 8 AM

# Minimum lead time before shift start. Shifts starting sooner than this from
# "right now" will be skipped, so you have time to actually commute there.
# Default: 60 minutes. Set to 0 to disable this filter.
MIN_LEAD_TIME_MINUTES = getattr(config, "MIN_LEAD_TIME_MINUTES", 60)

# Sleep-window filter: don't claim morning shifts (8 AM - 12 PM) while I'm asleep.
# If the bot is running during my sleep window (e.g. 11 PM - 8 AM) and sees a
# shift that starts in the morning (8 AM - 12 PM), skip it - I won't know about
# it in time to prepare. Non-morning shifts and shifts during waking hours are
# unaffected.
SLEEP_WINDOW_ENABLED = getattr(config, "SLEEP_WINDOW_ENABLED", True)
SLEEP_WINDOW_START_HOUR = getattr(config, "SLEEP_WINDOW_START_HOUR", 23)  # 11 PM
SLEEP_WINDOW_END_HOUR = getattr(config, "SLEEP_WINDOW_END_HOUR", 8)       # 8 AM
# The shift-start hour range to block when within the sleep window
MORNING_SHIFT_START_HOUR = getattr(config, "MORNING_SHIFT_START_HOUR", 8)   # 8 AM
MORNING_SHIFT_END_HOUR = getattr(config, "MORNING_SHIFT_END_HOUR", 12)      # 12 PM (noon)

# Safety: dry-run mode won't actually claim shifts, just logs
DRY_RUN = getattr(config, "DRY_RUN", False)

# User-agent and headers to match a real Chrome browser as closely as possible.
# Pulled directly from a real HAR capture of the tmwork.net web UI.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.0.0 Safari/537.36"
)

# Headers the real browser sends with every API request. These make our requests
# indistinguishable from a real browser session.
BROWSER_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "priority": "u=1, i",
}


# ============================================================================
# LOGGING SETUP
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("rso_bot.log"),
    ],
)
log = logging.getLogger("rso_bot")


# ============================================================================
# DATA TYPES
# ============================================================================
class RateLimited(Exception):
    """Raised when the server returns a rate-limit response (400/429 etc.)."""
    def __init__(self, retry_after: float = RATE_LIMIT_BACKOFF_START):
        self.retry_after = retry_after
        super().__init__(f"Rate limited, retry after {retry_after}s")


@dataclass
class Shift:
    """A single shift from the SwapBoard."""
    id: int
    loc_id: int
    checksum: str
    start: datetime
    end: datetime
    shift_group: str  # e.g. "Midnight", "Morning"
    station: str
    location: str
    directed_to_me: bool
    is_mine: bool       # True if this is my own shift (shouldn't try to claim)
    can_swap: bool      # True if claim action is actually available
    title: str

    @classmethod
    def from_api(cls, data: dict) -> "Shift":
        return cls(
            id=data["Id"],
            loc_id=data.get("LocId") or 0,
            checksum=str(data.get("CheckSum") or ""),
            start=datetime.fromisoformat(data["Start"]),
            end=datetime.fromisoformat(data["End"]),
            shift_group=data.get("ShiftGroup") or "",
            station=data.get("StnName") or "",
            location=data.get("LocName") or "",
            directed_to_me=bool(data.get("ToMe")),
            is_mine=bool(data.get("IsMe")),
            can_swap=bool(data.get("CanSwap", True)),
            title=data.get("Title") or "",
        )

    def is_midnight(self) -> bool:
        """True if this is a midnight shift (start hour 12 AM - 7:59 AM)."""
        if self.shift_group.strip().lower() == "midnight":
            return True
        return MIDNIGHT_START_HOUR <= self.start.hour < MIDNIGHT_END_HOUR

    def pretty(self) -> str:
        return (
            f"[id={self.id}] {self.start.strftime('%a %m/%d %I:%M%p')}-"
            f"{self.end.strftime('%I:%M%p')} "
            f"@ {self.station} ({self.location}) "
            f"group={self.shift_group!r} toMe={self.directed_to_me}"
        )


# ============================================================================
# CLIENT (handles auth + API calls)
# ============================================================================
class TeamWorkClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.session.headers.update(BROWSER_HEADERS)
        self.api_token: Optional[str] = None
        self.authed_at: Optional[datetime] = None

    # ---- Auth ----
    def login(self) -> None:
        """Perform full login flow and extract APP.Token."""
        log.info("Logging in as %s / %s ...", EMP_CODE, EMP_USER)

        # Step 1: GET /signin to grab the __RequestVerificationToken
        r = self.session.get(f"{BASE_URL}/signin", timeout=10)
        r.raise_for_status()

        rv_token = self._extract_verification_token(r.text)
        if not rv_token:
            raise RuntimeError("Could not find __RequestVerificationToken on signin page")

        # Step 2: POST credentials
        r = self.session.post(
            f"{BASE_URL}/SignIn?handler=EmpLogin",
            data={
                "portal": "emp",
                "EmpCode": EMP_CODE,
                "EmpUser": EMP_USER,
                "EmpPassword": EMP_PASSWORD,
                "__RequestVerificationToken": rv_token,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": f"{BASE_URL}/signin",
                "Origin": BASE_URL,
            },
            allow_redirects=True,
            timeout=10,
        )

        # After login we should be at /emp/
        if "/emp" not in r.url:
            raise RuntimeError(
                f"Login appears to have failed. Landed at: {r.url}. "
                "Check credentials in config.py."
            )

        # Step 3: extract APP.Token from the /emp/ page
        # If redirect already landed us there, r.text has it. Otherwise fetch.
        page_html = r.text
        if "APP.Token" not in page_html:
            r = self.session.get(f"{BASE_URL}/emp/", timeout=10)
            r.raise_for_status()
            page_html = r.text

        token = self._extract_api_token(page_html)
        if not token:
            raise RuntimeError("Could not find APP.Token in /emp/ page. Login may have silently failed.")

        self.api_token = token
        self.authed_at = datetime.now()
        self.session.headers.update({"x-api-token": token})
        log.info("Login OK. APP.Token=%s...", token[:8])

    @staticmethod
    def _extract_verification_token(html: str) -> Optional[str]:
        # <input name="__RequestVerificationToken" type="hidden" value="..." />
        m = re.search(
            r'name="__RequestVerificationToken"[^>]*value="([^"]+)"',
            html,
        )
        if m:
            return m.group(1)
        # alt order of attributes
        m = re.search(
            r'value="([^"]+)"[^>]*name="__RequestVerificationToken"',
            html,
        )
        return m.group(1) if m else None

    @staticmethod
    def _extract_api_token(html: str) -> Optional[str]:
        # APP.Token = '43730b5a-6995-455a-b401-ca58a9603dbe';
        m = re.search(r"APP\.Token\s*=\s*['\"]([^'\"]+)['\"]", html)
        return m.group(1) if m else None

    def ensure_authed(self) -> None:
        if self.api_token is None:
            self.login()

    # ---- Data ----
    def get_swapboard_counts(self, from_date: datetime) -> list[dict]:
        """Gets per-day swap counts - tells us which days have shifts without fetching each."""
        self.ensure_authed()
        url = f"{BASE_URL}/api/shift/swapboardCounts"
        params = {
            "date": from_date.strftime("%Y-%m-%d"),
            "fillgaps": "true",
            "_": int(time.time() * 1000),
        }
        r = self.session.get(
            url,
            params=params,
            headers={
                "Referer": f"{BASE_URL}/emp/",
                "x-requested-with": "XMLHttpRequest",
                "Accept": "application/json",
            },
            timeout=10,
        )
        if r.status_code == 401:
            log.warning("Session expired (swapboardCounts 401). Re-login.")
            self.login()
            return self.get_swapboard_counts(from_date)
        if r.status_code == 429:
            retry = float(r.headers.get("Retry-After", RATE_LIMIT_BACKOFF_START))
            raise RateLimited(retry)
        if r.status_code == 400:
            # tmwork.net returns 400 when rate-limiting (not the standard 429).
            # Treat it the same way.
            raise RateLimited(RATE_LIMIT_BACKOFF_START)
        r.raise_for_status()
        return r.json()

    def get_swapboard_for_day(self, day: datetime) -> list[Shift]:
        """Get shifts available on the swap board for a given day."""
        self.ensure_authed()
        url = f"{BASE_URL}/api/shift/swapboard"
        params = {
            "date": day.strftime("%Y-%m-%d"),
            "range": "day",
            "_": int(time.time() * 1000),
        }
        r = self.session.get(
            url,
            params=params,
            headers={
                "Referer": f"{BASE_URL}/emp/",
                "x-requested-with": "XMLHttpRequest",
                "Accept": "application/json",
            },
            timeout=10,
        )
        if r.status_code == 401:
            log.warning("Session expired (swapboard 401). Re-login.")
            self.login()
            return self.get_swapboard_for_day(day)
        if r.status_code == 429:
            retry = float(r.headers.get("Retry-After", RATE_LIMIT_BACKOFF_START))
            raise RateLimited(retry)
        if r.status_code == 400:
            raise RateLimited(RATE_LIMIT_BACKOFF_START)
        r.raise_for_status()

        raw = r.json()
        if not isinstance(raw, list):
            return []

        shifts = []
        for item in raw:
            try:
                shifts.append(Shift.from_api(item))
            except Exception as e:
                log.warning("Could not parse shift: %s | data=%s", e, item)
        return shifts

    # ---- Claim ----
    def claim_shift(self, shift: Shift) -> tuple[bool, str]:
        """Issues the quick-claim PUT. Returns (success, message)."""
        self.ensure_authed()
        url = f"{BASE_URL}/api/shift/swap/quick-claim"
        params = {"id": shift.id, "bid": shift.loc_id, "schid": ""}

        if DRY_RUN:
            log.info("[DRY-RUN] Would PUT %s params=%s", url, params)
            return True, "dry-run"

        try:
            r = self.session.put(
                url,
                params=params,
                headers={
                    "Referer": f"{BASE_URL}/emp/",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/plain, */*",
                    "Origin": BASE_URL,
                },
                timeout=8,
            )
        except requests.RequestException as e:
            return False, f"network-error: {e}"

        if r.status_code == 401:
            log.warning("Got 401 on claim. Re-logging in and will retry on next poll.")
            self.api_token = None
            return False, "auth-expired"

        # From the client code: null/empty response = success, non-empty = error message
        body = r.text or ""
        if r.ok and (not body or "Claimed" in body):
            return True, "claimed"
        return False, f"http={r.status_code} body={body[:200]!r}"


# ============================================================================
# BOT LOGIC
# ============================================================================
class ShiftBot:
    def __init__(self, client: TeamWorkClient):
        self.client = client
        # Remember shifts we've already attempted so we don't hammer the same one
        self.attempted_ids: set[int] = set()
        self.claimed_ids: set[int] = set()
        # Track shifts we've already logged as skipped (so we don't spam the log)
        self.skipped_logged: set[int] = set()
        # Last-known count per day - used to avoid re-fetching when nothing changed.
        # Signature = (SwapCount, SwapToYou) tuple
        self._day_signatures: dict = {}
        # Days where we got a 400 error - briefly back off from those
        self._day_cooldowns: dict = {}  # date -> datetime until we retry
        self._last_indicated = None

    @staticmethod
    def _is_in_sleep_window(now_hour: int) -> bool:
        """True if the current hour is within my sleep window.
        Sleep window wraps midnight (e.g. 23 -> 8 means 11 PM through 7:59 AM)."""
        start = SLEEP_WINDOW_START_HOUR
        end = SLEEP_WINDOW_END_HOUR
        if start < end:
            # Non-wrapping window, e.g. 1 AM - 7 AM
            return start <= now_hour < end
        else:
            # Wrapping window, e.g. 23 - 8 means 23, 0, 1, 2, 3, 4, 5, 6, 7
            return now_hour >= start or now_hour < end

    def should_claim(self, shift: Shift) -> tuple[bool, str]:
        """Decide whether to claim a shift. Returns (decision, reason)."""
        if shift.id in self.claimed_ids:
            return False, "already-claimed"
        if shift.id in self.attempted_ids:
            return False, "already-attempted"
        if shift.is_mine:
            return False, "already-my-shift"
        if not shift.can_swap:
            return False, "not-claimable"
        if SKIP_MIDNIGHT_SHIFTS and shift.is_midnight():
            return False, "midnight-shift-skipped"
        # Sanity: don't claim shifts in the past
        if shift.end < datetime.now():
            return False, "shift-in-past"
        # Commute buffer: don't claim shifts starting sooner than MIN_LEAD_TIME_MINUTES
        if MIN_LEAD_TIME_MINUTES > 0:
            now = datetime.now()
            minutes_until_start = (shift.start - now).total_seconds() / 60
            if minutes_until_start < MIN_LEAD_TIME_MINUTES:
                return False, f"starts-in-{int(minutes_until_start)}min-need-{MIN_LEAD_TIME_MINUTES}"
        # Sleep-window filter: if it's currently my sleep hours AND this is a
        # morning shift (8 AM - 12 PM), skip it. I'd be asleep when it drops
        # and might not see it before it starts.
        if SLEEP_WINDOW_ENABLED:
            now = datetime.now()
            if self._is_in_sleep_window(now.hour):
                is_morning_shift = (
                    MORNING_SHIFT_START_HOUR <= shift.start.hour < MORNING_SHIFT_END_HOUR
                )
                if is_morning_shift:
                    return False, (
                        f"sleep-window-skip (now={now.strftime('%H:%M')}, "
                        f"shift-starts={shift.start.strftime('%H:%M')})"
                    )
        return True, "ok"

    def poll_once(self) -> None:
        """One poll cycle: fetch counts cheaply, only fetch day details when counts change."""
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        now = datetime.now()

        # Step 1: get counts - quick check of which days have any shifts
        try:
            counts = self.client.get_swapboard_counts(today)
        except RateLimited:
            # MUST re-raise so run_forever's backoff handler can sleep properly.
            # Do NOT fall through to the generic Exception handler below.
            raise
        except Exception as e:
            log.error("Failed to get swap counts: %s", e)
            return

        # Build per-day signature map: date -> (SwapCount, SwapToYou)
        current_signatures: dict = {}
        for c in counts:
            try:
                d = datetime.fromisoformat(c["Date"])
            except Exception:
                continue
            if d < today or d > today + timedelta(days=DAYS_AHEAD_TO_CHECK):
                continue
            sig = (c.get("SwapCount", 0), bool(c.get("SwapToYou")))
            if sig != (0, False):  # only track days with something
                current_signatures[d] = sig

        # Determine which days we actually need to fetch this cycle
        days_to_fetch: list[datetime] = []
        for day, sig in current_signatures.items():
            # Skip if we're in cooldown for this day
            cooldown_until = self._day_cooldowns.get(day)
            if cooldown_until and now < cooldown_until:
                continue
            # Skip if signature hasn't changed since last successful fetch
            if self._day_signatures.get(day) == sig:
                continue
            days_to_fetch.append(day)

        # Clear signatures for days that no longer have shifts (so if they come back we refetch)
        for day in list(self._day_signatures.keys()):
            if day not in current_signatures:
                del self._day_signatures[day]

        if not current_signatures:
            self._last_indicated = None
            return

        # Only log when the set of indicated days changes
        indicated_key = tuple(sorted(d.date() for d in current_signatures))
        if self._last_indicated != indicated_key:
            log.info(
                "Shifts indicated on %d day(s): %s",
                len(current_signatures),
                ", ".join(d.strftime("%m/%d") for d in sorted(current_signatures)),
            )
            self._last_indicated = indicated_key

        if not days_to_fetch:
            return  # all days cached - no work to do

        # Step 2: for each day with a CHANGED signature, fetch details and try to claim
        for day in days_to_fetch:
            try:
                shifts = self.client.get_swapboard_for_day(day)
                # Success - remember this signature so we don't re-fetch needlessly
                self._day_signatures[day] = current_signatures[day]
            except RateLimited:
                # Re-raise so run_forever handles the backoff globally
                raise
            except Exception as e:
                log.error("Failed to get shifts for %s: %s", day.date(), e)
                continue

            for shift in shifts:
                decision, reason = self.should_claim(shift)
                if not decision:
                    # Only log the skip once per shift - avoid spamming the log
                    if (reason not in ("already-attempted", "already-claimed")
                            and shift.id not in self.skipped_logged):
                        log.info("SKIP %s (%s)", shift.pretty(), reason)
                        self.skipped_logged.add(shift.id)
                    continue

                # CLAIM IT - fire immediately
                log.info("CLAIMING %s ...", shift.pretty())
                self.attempted_ids.add(shift.id)
                t0 = time.time()
                ok, msg = self.client.claim_shift(shift)
                elapsed_ms = int((time.time() - t0) * 1000)

                if ok:
                    self.claimed_ids.add(shift.id)
                    log.info(
                        "✅ CLAIMED %s in %dms (%s)",
                        shift.pretty(), elapsed_ms, msg,
                    )
                else:
                    log.warning(
                        "❌ FAILED %s in %dms (%s)",
                        shift.pretty(), elapsed_ms, msg,
                    )

    def run_forever(self) -> None:
        sleep_window_str = (
            f"{SLEEP_WINDOW_START_HOUR:02d}:00-{SLEEP_WINDOW_END_HOUR:02d}:00 "
            f"blocks {MORNING_SHIFT_START_HOUR:02d}:00-{MORNING_SHIFT_END_HOUR:02d}:00 shifts"
            if SLEEP_WINDOW_ENABLED else "off"
        )
        log.info(
            "Bot starting. Poll every ~%.1fs (+/- %.1fs). "
            "Skip-midnight=%s, Min-lead-time=%dmin, Sleep-window=%s, Dry-run=%s, Days-ahead=%d",
            POLL_INTERVAL_SECONDS, POLL_JITTER_SECONDS,
            SKIP_MIDNIGHT_SHIFTS, MIN_LEAD_TIME_MINUTES, sleep_window_str,
            DRY_RUN, DAYS_AHEAD_TO_CHECK,
        )
        self.client.login()
        cycle = 0
        consecutive_rate_limits = 0
        while True:
            cycle += 1
            try:
                self.poll_once()
                # Success - reset the backoff counter
                consecutive_rate_limits = 0
            except KeyboardInterrupt:
                raise
            except RateLimited as e:
                consecutive_rate_limits += 1
                # Exponential backoff: 30s -> 60s -> 120s -> 240s -> 300s (cap)
                backoff = min(
                    e.retry_after * (2 ** (consecutive_rate_limits - 1)),
                    RATE_LIMIT_BACKOFF_MAX,
                )
                log.warning(
                    "⚠️  Server rate-limited (consecutive=%d). Backing off for %.0fs. "
                    "If this keeps happening, increase POLL_INTERVAL_SECONDS in config.py.",
                    consecutive_rate_limits, backoff,
                )
                time.sleep(backoff)
                continue  # skip the normal sleep below
            except Exception as e:
                log.exception("Unexpected error in poll cycle %d: %s", cycle, e)

            # Heartbeat every ~60 cycles so we know it's alive
            if cycle % 60 == 0:
                log.info(
                    "Heartbeat: cycle=%d, attempted=%d, claimed=%d",
                    cycle, len(self.attempted_ids), len(self.claimed_ids),
                )

            sleep_for = POLL_INTERVAL_SECONDS + random.uniform(
                -POLL_JITTER_SECONDS, POLL_JITTER_SECONDS
            )
            time.sleep(max(0.2, sleep_for))


# ============================================================================
# ENTRY POINT
# ============================================================================
def main() -> int:
    client = TeamWorkClient()
    bot = ShiftBot(client)
    try:
        bot.run_forever()
    except KeyboardInterrupt:
        log.info("Stopped by user (Ctrl-C). Claimed total: %d", len(bot.claimed_ids))
        return 0
    except Exception:
        log.exception("Fatal error")
        return 1


if __name__ == "__main__":
    sys.exit(main())