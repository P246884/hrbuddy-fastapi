from app.crm.entity_registry import (
    ENTITY_REGISTRY,
    STATUS_MAPPING
)


def _to_number(value):
    """Coerce a filter value to int (preferred) or float for numeric
    comparisons. The backend's NormalizeJsonValue handles JSON integers
    cleanly, so we send whole numbers as int."""
    try:
        f = float(value)
        return int(f) if f == int(f) else f
    except (TypeError, ValueError):
        return value


def build_dynamic_query(
    entity_name: str,
    filters: dict,
    current_user: dict
):

    entity = ENTITY_REGISTRY.get(
        entity_name
    )

    if not entity:

        raise Exception(
            f"Unknown entity: {entity_name}"
        )

    crm_filters = {}

    is_master_entity = entity.get(
        "is_master_entity",
        False
    )

    print("ENTITY:", entity_name)
    print("IS MASTER:", is_master_entity)
    print("FILTERS RECEIVED:", filters)

    if is_master_entity:

        if filters.get("employee_code"):
            # Exact identifier — most reliable for disambiguation.
            crm_filters["bam_employeeno"] = filters.get("employee_code")

        elif filters.get("employee_guids"):

            crm_filters[
                entity["primary_field"] + "_in"
            ] = filters.get("employee_guids")

        elif filters.get("employee_guid"):

            crm_filters[
                entity["primary_field"]
            ] = filters.get("employee_guid")

        elif filters.get("employee_name_contains"):
            # "employees whose name is related to / like / contains X"
            crm_filters["bam_name_contains"] = filters.get("employee_name_contains")

        elif filters.get("employee_name"):

            crm_filters[
                entity["search_field"]
            ] = filters.get("employee_name")

        elif filters.get("starts_with"):
            # "employees whose name starts with A"
            crm_filters["bam_name"] = filters.get("starts_with")

        elif filters.get("target") == "self":

            crm_filters[
                entity["primary_field"]
            ] = current_user.get("user_guid")

    else:

        employee_lookup = entity.get(
            "employee_lookup"
        )

        if filters.get("employee_guids"):

            crm_filters[
                employee_lookup + "_in"
            ] = filters.get(
                "employee_guids"
            )

        elif (
            filters.get("employee_guid")
            and employee_lookup
        ):

            crm_filters[
                employee_lookup
            ] = filters.get(
                "employee_guid"
            )

        elif (
            filters.get("target") == "self"
            and employee_lookup
        ):

            crm_filters[
                employee_lookup
            ] = current_user.get(
                "user_guid"
            )

        status = filters.get("status")

        if status:

            status_value = STATUS_MAPPING.get(
                str(status).lower()
            )

            if status_value is not None:

                status_field = entity.get(
                    "status_field",
                    "statuscode"
                )

                crm_filters[
                    status_field
                ] = status_value

        if filters.get("days"):

            crm_filters[
                "last_x_days"
            ] = filters.get("days")

        if filters.get("months"):

            crm_filters[
                "last_x_months"
            ] = filters.get("months")

        if filters.get("from_date"):

            crm_filters[
                "from_date"
            ] = filters.get("from_date")

        if filters.get("to_date"):

            crm_filters[
                "to_date"
            ] = filters.get("to_date")

        if filters.get("top"):

            crm_filters[
                "top"
            ] = filters.get("top")

            crm_filters[
                "order_by"
            ] = entity.get(
                "date_field",
                "createdon"
            )

            crm_filters[
                "order_direction"
            ] = "desc"

        # Field-specific date window. Use date_filter_field when set (e.g.
        # leaves filter on bam_startdate, the actual leave date — not createdon),
        # else fall back to date_field. Backend supports _gte / _lte on a field.
        _date_field = entity.get("date_filter_field") or entity.get("date_field")
        if _date_field:
            if filters.get("date_from"):
                crm_filters[_date_field + "_gte"] = filters.get("date_from")
            if filters.get("date_to"):
                # Cover the WHOLE day: a bare "YYYY-MM-DD" as _lte means midnight,
                # which would exclude same-day records that carry a time
                # component. Extend to end-of-day so datetime fields match.
                _dt = str(filters.get("date_to"))
                if len(_dt) == 10:  # bare date, no time
                    _dt = _dt + " 23:59:59"
                crm_filters[_date_field + "_lte"] = _dt

        # marked_by (attendance: "System Hours" / "Office Hours"). The CRM
        # field is an optionset, so filter by CODE. Map the label -> code via
        # the module's value_maps; if no map, send the value as-is.
        if filters.get("marked_by"):
            _mb_field = entity.get("fields", {}).get("marked_by")
            if _mb_field:
                _mb_val = filters.get("marked_by")
                _vmap = (entity.get("value_maps") or {}).get("marked_by") or {}
                _label_to_code = {str(v).lower(): k for k, v in _vmap.items()}
                crm_filters[_mb_field] = _label_to_code.get(
                    str(_mb_val).lower(), _mb_val
                )

    # ------------------------------------------------------------------
    # Designation filter
    # bam_designation is a LookupType (stores a GUID). We can ONLY filter it
    # by GUID. Sending text (e.g. bam_designation_contains='intern') makes CRM
    # try to parse 'intern' as a System.Guid and throws a FormatException.
    # So: filter by GUID only. The name->GUID resolution happens upstream
    # (dynamic_executor). If no GUID could be resolved, we DO NOT add a text
    # filter here — the executor returns a friendly "not found" message instead.
    # ------------------------------------------------------------------
    if is_master_entity:
        if filters.get("designation_guids"):
            crm_filters["bam_designation_in"] = filters.get("designation_guids")
        elif filters.get("designation_guid"):
            crm_filters["bam_designation"] = filters.get("designation_guid")

        # Department filter (same lookup-by-GUID rule)
        if filters.get("department_guids"):
            crm_filters["bam_department_in"] = filters.get("department_guids")
        elif filters.get("department_guid"):
            crm_filters["bam_department"] = filters.get("department_guid")

        # Manager filter (bam_manager is a lookup to bam_employee, so a
        # resolved employee GUID works here). Supports one or many managers.
        if filters.get("manager_guids"):
            crm_filters["bam_manager_in"] = filters.get("manager_guids")
        elif filters.get("manager_guid"):
            crm_filters["bam_manager"] = filters.get("manager_guid")

        # Experience filters (bam_totalexperienceinyears is numeric).
        # Backend supports _gt / _gte / _lt / _lte operators directly.
        if filters.get("experience_gt") not in (None, "", []):
            crm_filters["bam_totalexperienceinyears_gt"] = _to_number(
                filters.get("experience_gt"))
        if filters.get("experience_gte") not in (None, "", []):
            crm_filters["bam_totalexperienceinyears_gte"] = _to_number(
                filters.get("experience_gte"))
        if filters.get("experience_lt") not in (None, "", []):
            crm_filters["bam_totalexperienceinyears_lt"] = _to_number(
                filters.get("experience_lt"))
        if filters.get("experience_lte") not in (None, "", []):
            crm_filters["bam_totalexperienceinyears_lte"] = _to_number(
                filters.get("experience_lte"))

        # Top filter for master entity
        if filters.get("top"):
            crm_filters["top"] = filters.get("top")

    print("CRM FILTERS:", crm_filters)

    query_payload = {

        "crm_entity":
            entity["crm_entity"],

        "crm_filters":
            crm_filters,

        "fields":
            entity["fields"]
    }

    print(
        "CRM QUERY PAYLOAD:",
        query_payload
    )

    return query_payload