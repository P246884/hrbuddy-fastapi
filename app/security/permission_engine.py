def _to_bool(value):
    return str(value).lower() == "true"


def is_manager_of_employee(current_user_guid: str, target_employee_guid: str, token: str, user: dict):
    """
    Central manager check — target employee ke bam_manager mein
    current_user_guid hai ya nahi
    Ye function everywhere use hoga
    """
    if not target_employee_guid or not current_user_guid:
        return False
    try:
        from app.api_clients.hrms_api import call_hrbuddy_api
        result = call_hrbuddy_api(
            endpoint="/api/hrbuddy/dynamic-query",
            token=token, user=user, method="POST",
            body={
                "crm_entity": "bam_employee",
                "crm_filters": {"bam_employeeid": target_employee_guid},
                "fields": {
                    "employee_guid": "bam_employeeid",
                    "manager_guid": "bam_manager"
                }
            }
        )
        if result.get("success") and result.get("data"):
            mgr_guid = str(result["data"][0].get("manager_guid", ""))
            print(f"MANAGER CHECK - target: {target_employee_guid}, mgr: {mgr_guid}, user: {current_user_guid}")
            return mgr_guid == current_user_guid
    except Exception as e:
        print("Manager check error:", e)
    return False


def can_read_entity(
    entity: str,
    current_user: dict,
    target_employee=None,
    token: str = None
):
    is_hr = _to_bool(current_user.get("is_hr"))
    is_admin = _to_bool(current_user.get("is_admin"))
    current_user_guid = current_user.get("user_guid", "")

    # No target — self query
    if not target_employee:
        return True

    # Own data
    if target_employee == current_user_guid:
        return True

    # HR / Admin — full access
    if is_hr or is_admin:
        return True

    # Manager check — bam_manager field se verify
    if token and target_employee:
        return is_manager_of_employee(current_user_guid, target_employee, token, current_user)

    return False