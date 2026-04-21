# Copy this file to config.py and fill in your credentials.
# config.py is gitignored so your password won't get committed.

# ----- REQUIRED -----
EMP_CODE = "Resident"
EMP_USER = "23023"
EMP_PASSWORD = "Hh123456@"

# ----- POLL RATE (read carefully before changing) -----
# Empirically measured: tmwork.net rate-limits at roughly 1,000 req/hour.
# At 2s polling we hit the wall after 9 minutes (1,800 req/hr exceeds budget).
# At 4s polling we make 900 req/hr - safely under the limit.
#
# Actual observed limits:
#   - 1.5s -> throttled in ~25 seconds
#   - 2.0s -> throttled in ~9 minutes
#   - 4.0s -> should run indefinitely (untested long-term)
#
# DO NOT set below 3.0 without evidence. Going faster means the bot runs for
# 9 minutes then sits locked out for 15+ minutes - net LESS time available
# to claim shifts than running steady at 4s.
POLL_INTERVAL_SECONDS = 4.0
POLL_JITTER_SECONDS = 0.5

# On rate-limit (400 response), wait this long then double each consecutive
# failure up to RATE_LIMIT_BACKOFF_MAX.
RATE_LIMIT_BACKOFF_START = 30.0
RATE_LIMIT_BACKOFF_MAX = 300.0  # 5 min cap

# How far into the future to scan for shifts.
DAYS_AHEAD_TO_CHECK = 30

# ----- FILTERS -----
# Skip shifts that start 12 AM - 8 AM (midnight shifts).
SKIP_MIDNIGHT_SHIFTS = True

# Don't claim shifts starting sooner than this many minutes from now.
# Set to 0 to disable.
MIN_LEAD_TIME_MINUTES = 60

# Sleep-window filter: while you're asleep, don't claim morning shifts.
# If it's currently between SLEEP_WINDOW_START_HOUR and SLEEP_WINDOW_END_HOUR,
# AND the shift starts between MORNING_SHIFT_START_HOUR and MORNING_SHIFT_END_HOUR,
# skip it.
SLEEP_WINDOW_ENABLED = True
SLEEP_WINDOW_START_HOUR = 23   # 11 PM
SLEEP_WINDOW_END_HOUR = 8      # 8 AM
MORNING_SHIFT_START_HOUR = 8   # 8 AM
MORNING_SHIFT_END_HOUR = 12    # 12 PM (noon)

# ----- SAFETY -----
# If True, log what the bot would claim but don't actually claim.
DRY_RUN = False