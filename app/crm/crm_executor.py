from app.api_clients.hrms_api import call_hrbuddy_api


def execute_crm_query(
    crm_query: dict,
    token: str,
    user: dict
):
    return call_hrbuddy_api(
        endpoint="/api/hrbuddy/dynamic-query",
        token=token,
        user=user,
        method="POST",
        body=crm_query
    )