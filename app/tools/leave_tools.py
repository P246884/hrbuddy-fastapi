from app.api_clients.hrms_api import call_hrbuddy_api
from app.services.response_formatter import format_leave_response


def get_leave_balances(user, token, filters):

    data = call_hrbuddy_api(
        endpoint="/api/hrbuddy/all-leave-balances",
        token=token,
        user=user
    )

    balances = data.get("data", [])

    exclude = filters.get("exclude", [])

    if exclude:
        balances = [
            x for x in balances
            if x["leaveType"].lower() not in [
                e.lower() for e in exclude
            ]
        ]

    return format_leave_response(balances)