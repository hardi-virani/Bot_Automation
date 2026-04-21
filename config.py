# Copy this file to config.py and fill in your credentials.
# config.py is gitignored so your password won't get committed.

# ----- REQUIRED -----
# Copy this file to config.py and fill in your credentials.
# config.py is gitignored so your password won't get committed.
EMP_CODE = "##"
EMP_USER = "###"
EMP_PASSWORD = "##**"

# ----- OPTIONAL TUNING -----
# How often to check the swap board. Lower = faster claim, higher = more polite to server.
# The site's own UI polls every 1.5s. Going below 1.0s increases detectability risk.
POLL_INTERVAL_SECONDS = 4

# Randomness added to each poll interval, to look less robotic. Real interval will be
# POLL_INTERVAL_SECONDS +/- this value.
POLL_JITTER_SECONDS = 0.5

# How far into the future to scan for shifts (days). 30 is plenty for RSO.
DAYS_AHEAD_TO_CHECK = 30

# If True, skip shifts that start between 12 AM and 8 AM (midnight shifts).
SKIP_MIDNIGHT_SHIFTS = True

# SAFETY: If True, log what the bot would claim but don't actually claim anything.
# Use this for your first run to verify behavior! Then set back to False.
DRY_RUN = False
