def format_leave_response(balances):

    if not balances:
        return "No leave balances found."

    response = "📊 Your Leave Balances:\n\n"

    for item in balances:

        response += (
            f"{item['leaveType']} : "
            f"{item['balance']}\n"
        )

    return response