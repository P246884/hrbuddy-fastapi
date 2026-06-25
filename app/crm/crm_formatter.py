HIDDEN_FIELDS = {
    "employee_guid", "leave_type_guid",
    "leave_structure_guid", "leave_guid",
    "accounting_period", "manager_guid"
}

FIELD_CONFIG = {
    "employee_name": ("👤", "Name"),
    "employee_code": ("🆔", "Code"),
    "department":    ("🏢", "Department"),
    "designation":   ("💼", "Designation"),
    "experience":    ("📈", "Experience (yrs)"),
    "leave_type":    ("📋", "Leave Type"),
    "balance":       ("📊", "Balance"),
    "from_date":     ("📅", "From"),
    "to_date":       ("📅", "To"),
    "days":          ("⏱️", "Days"),
    "status":        ("🔔", "Status"),
    "manager":       ("👥", "Manager"),
    "reason":        ("📝", "Reason"),
}

STATUS_LABELS = {
    1:         "🟡 Requested",
    100010001: "✅ Approved",
    100010004: "❌ Rejected",
    100010003: "🚫 Cancelled",
}


def _format_value(key, value):
    if key == "status":
        return STATUS_LABELS.get(value, str(value))
    if key in ("from_date", "to_date") and value:
        return str(value)[:10]
    if key == "balance":
        v = float(value) if value else 0
        return f"{v:.1f} days" if v != int(v) else f"{int(v)} days"
    if key == "days":
        v = float(value) if value else 0
        return f"{v:.1f} day(s)" if v != int(v) else f"{int(v)} day(s)"
    return str(value) if value is not None else "—"


def format_records(records, entity_type=None):
    if not records:
        return "No records found."

    lines = []
    total = len(records)

    for index, item in enumerate(records, start=1):
        # Header line
        if total > 1:
            lines.append(f"**{index} of {total}**")
        
        for key, value in item.items():
            if key in HIDDEN_FIELDS:
                continue
            config = FIELD_CONFIG.get(key)
            if config:
                emoji, label = config
            else:
                emoji = "•"
                label = key.replace("_", " ").title()
            formatted_val = _format_value(key, value)
            lines.append(f"{emoji} **{label}:** {formatted_val}")

        if index < total:
            lines.append("---")

    return "\n".join(lines).strip()