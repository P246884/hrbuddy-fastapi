from decimal import Decimal


def _display(value):
    if value is None:
        return "-"

    if isinstance(value, Decimal):
        value = float(value)

    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return str(round(value, 2))

    return str(value)


def _label(key: str):
    return key.replace("_", " ").title()


def _employee_title(filters: dict, fallback: str = ""):
    name = filters.get("resolved_employee_name") or filters.get("employee_name") or fallback
    code = filters.get("resolved_employee_code")

    if name and code:
        return f"{name} ({code})"

    return name or "the employee"


def build_no_data_response(entity: str, filters: dict):
    employee = _employee_title(filters)

    if entity == "employee":
        return (
            f"I couldn't find any employee record for {employee}.\n\n"
            "Please try with the full employee name or employee code."
        )

    if entity == "leave":
        return (
            f"I couldn't find any leave balance records for {employee}.\n\n"
            "This may happen if the leave structure is not assigned yet, "
            "or if the employee name/code does not match the HRMS records."
        )

    if entity == "leave_history":
        return (
            f"I couldn't find any leave history records for {employee}.\n\n"
            "Try changing the date range, status, or employee name."
        )

    return (
        "I couldn't find matching HRMS records for this request.\n\n"
        "Please try with more specific details."
    )


def build_error_response(entity: str, message: str = ""):
    if "not authorized" in (message or "").lower():
        return "You are not authorized to view this employee's information."

    if "not configured" in (message or "").lower():
        return (
            "This HRMS module is not available in HRBuddy yet. "
            "It may be added in a future phase."
        )

    if message:
        return message

    return "Sorry, I was unable to fetch the requested HRMS data right now."


def build_employee_response(records: list, filters: dict):
    if not records:
        return build_no_data_response("employee", filters)

    lines = []

    if len(records) == 1:
        item = records[0]
        name = item.get("employee_name") or filters.get("resolved_employee_name") or "Employee"
        lines.append(f"Here are {name}'s profile details:")
        lines.append("")

        for key, value in item.items():
            if key == "employee_guid":
                continue
            lines.append(f"• {_label(key)}: {_display(value)}")

        return "\n".join(lines)

    lines.append("I found multiple employee records:")
    lines.append("")

    for index, item in enumerate(records, start=1):
        name = item.get("employee_name", "Employee")
        code = item.get("employee_code", "-")
        dept = item.get("department", "-")
        designation = item.get("designation", "-")
        lines.append(f"{index}. {name} | {code} | {dept} | {designation}")

    lines.append("")
    lines.append("Please specify the employee name or employee code for the exact record.")

    return "\n".join(lines)


def build_leave_response(records: list, filters: dict):
    if not records:
        return build_no_data_response("leave", filters)

    employee = _employee_title(filters)
    filter_type = filters.get("type")
    filter_values = filters.get("types", []) or []

    lines = []

    if employee and employee != "the employee":
        lines.append(f"Here is the leave balance for {employee}:")
    else:
        lines.append("Here is your leave balance:")

    lines.append("")

    for item in records:
        leave_type = item.get("leave_type") or item.get("Leave Type") or "Leave"
        balance = item.get("balance") or item.get("Balance") or 0
        accounting_period = item.get("accounting_period")

        if accounting_period:
            lines.append(f"• {leave_type}: {_display(balance)} ({accounting_period})")
        else:
            lines.append(f"• {leave_type}: {_display(balance)}")

    if filter_type == "exclude" and filter_values:
        lines.append("")
        lines.append(f"Excluded: {', '.join(filter_values)}")

    if filter_type == "include" and filter_values:
        lines.append("")
        lines.append(f"Showing only: {', '.join(filter_values)}")

    return "\n".join(lines)


def build_leave_history_response(records: list, filters: dict):
    if not records:
        return build_no_data_response("leave_history", filters)

    employee = _employee_title(filters)
    lines = []

    if employee and employee != "the employee":
        lines.append(f"Here is the leave history for {employee}:")
    else:
        lines.append("Here is your leave history:")

    lines.append("")

    for index, item in enumerate(records, start=1):
        leave_type = item.get("leave_type") or item.get("type") or item.get("Leave Type") or "Leave"
        from_date = item.get("from_date") or item.get("start_date") or item.get("From Date") or "-"
        to_date = item.get("to_date") or item.get("end_date") or item.get("To Date") or "-"
        status = item.get("status") or item.get("Status") or "-"
        days = item.get("days") or item.get("Days") or "-"

        lines.append(f"{index}. {leave_type}")
        lines.append(f"   • From: {_display(from_date)}")
        lines.append(f"   • To: {_display(to_date)}")
        lines.append(f"   • Status: {_display(status)}")
        lines.append(f"   • Days: {_display(days)}")
        lines.append("")

    return "\n".join(lines).strip()


def build_generic_response(entity: str, records: list, filters: dict):
    if not records:
        return build_no_data_response(entity, filters)

    lines = ["I found the following details:", ""]

    for index, item in enumerate(records, start=1):
        lines.append(f"Record {index}")

        for key, value in item.items():
            if key.endswith("_guid") or key.endswith("_id"):
                continue
            lines.append(f"• {_label(key)}: {_display(value)}")

        lines.append("")

    return "\n".join(lines).strip()


def build_response(entity: str, records: list, filters: dict):
    if entity == "employee":
        return build_employee_response(records, filters)

    if entity == "leave":
        return build_leave_response(records, filters)

    if entity == "leave_history":
        return build_leave_history_response(records, filters)

    return build_generic_response(entity, records, filters)
