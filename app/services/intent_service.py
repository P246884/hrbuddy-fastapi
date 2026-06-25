from app.api_clients.hrms_api import call_hrbuddy_api

def process_message(message: str, user: dict, token: str):

    msg = message.lower()

    # ---------------------------------
    # FETCH ALL LEAVES ONCE
    # ---------------------------------
    if "leave" in msg:

        data = call_hrbuddy_api(
            endpoint="/api/hrbuddy/all-leave-balances",
            token=token,
            user=user
        )

        if not data.get("success"):
            return "Unable to fetch leave balances."

        balances = data.get("data", [])

        # ---------------------------------
        # SICK LEAVE
        # ---------------------------------
        if "sick" in msg:

            for item in balances:

                leave_type = item.get("leaveType", "").lower()

                if "sick" in leave_type:

                    return (
                        f"Your Sick Leave balance is "
                        f"{item.get('balance', '0')}"
                    )

            return "No Sick Leave record found."

        # ---------------------------------
        # ANNUAL LEAVE
        # ---------------------------------
        elif "annual" in msg:

            for item in balances:

                leave_type = item.get("leaveType", "").lower()

                if "annual" in leave_type:

                    return (
                        f"Your Annual Leave balance is "
                        f"{item.get('balance', '0')}"
                    )

            return "No Annual Leave record found."

        # ---------------------------------
        # SHOW ALL LEAVES
        # ---------------------------------
        else:

            response = "Your leave balances are:\n\n"

            for item in balances:

                response += (
                    f"{item.get('leaveType')} : "
                    f"{item.get('balance')}\n"
                )

            return response

    return "Sorry, I could not understand your request."