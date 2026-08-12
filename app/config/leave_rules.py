"""
Leave application business rules — EDIT THESE VALUES to change behaviour.
All apply-leave date/type restrictions read from here, so you can tune the
policy in one place without touching the flow logic.
"""

# ---------------------------------------------------------------------------
# BACKDATED LEAVE WINDOW
# How far into the PAST an employee may apply for leave.
#   MAX_PAST_MONTHS = 2  ->  today 10 Aug  =>  anything before 10 June is blocked
# Set MAX_PAST_MONTHS = None to disable the limit entirely.
# ---------------------------------------------------------------------------
MAX_PAST_MONTHS = 2

# Message shown when a leave start date is older than the window above.
# {months} is replaced with MAX_PAST_MONTHS, {date} with the cutoff date.
OLD_DATE_MESSAGE = (
    "You can't apply for leave on dates older than {months} month(s) "
    "(anything before {date}). Please contact HR for backdated leave requests."
)

# ---------------------------------------------------------------------------
# PAST-DATE LEAVE TYPE RESTRICTION
# For any leave whose start date is in the PAST (yesterday or earlier — today
# is allowed), restrict which leave types can be applied.
#   PAST_ONLY_ALLOWED_TYPES = ["Sick Leave"]  ->  past dates: only Sick Leave
# Matching is case-insensitive and substring-based ("sick" matches "Sick Leave").
# Set PAST_ONLY_ALLOWED_TYPES = [] (empty) to allow all types on past dates.
# ---------------------------------------------------------------------------
PAST_ONLY_ALLOWED_TYPES = ["Sick Leave"]

# Message shown when a non-allowed type is chosen for a past date.
PAST_TYPE_MESSAGE = (
    "For past dates you can only apply {allowed}. "
    "Other leave types must be applied for today or a future date."
)