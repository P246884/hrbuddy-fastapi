from app.tools.leave_tools import get_leave_balances
from app.tools.attendance_tools import get_attendance


def execute_tool(decision, user, token):

    tool = decision.get("tool")
    filters = decision.get("filters", {})

    if tool == "get_leave_balances":
        return get_leave_balances(user, token, filters)

    if tool == "get_attendance":
        return get_attendance(user, token, filters)

    return "I could not process your request."