from app.crm.crm_query_builder import build_dynamic_query
from app.crm.crm_executor import execute_crm_query


def resolve_employee(
    employee_name,
    token: str,
    user: dict,
    employee_code: str = ""
):
    # If an employee code is given, it's the most reliable identifier — use it.
    if employee_code and str(employee_code).strip():
        query = build_dynamic_query(
            entity_name="employee",
            filters={"employee_code": str(employee_code).strip()},
            current_user=user
        )
        return execute_crm_query(crm_query=query, token=token, user=user)

    # Employee name empty hone pe saari employees fetch nahi karni
    if not employee_name or str(employee_name).strip() == "":
        return {"success": False, "data": [], "message": "Employee name is required."}

    if isinstance(employee_name, list):
        employee_name = ",".join(employee_name)

    query = build_dynamic_query(
        entity_name="employee",
        filters={
            "employee_name": employee_name
        },
        current_user=user
    )

    return execute_crm_query(
        crm_query=query,
        token=token,
        user=user
    )