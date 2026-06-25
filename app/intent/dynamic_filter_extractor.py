import re

def extract_dynamic_filters(
    message: str,
    entity: str
):

    filters = []

    msg = message.lower()

    # designation
    designation_match = re.search(
        r"designation\s+(?:is|=)\s+(.+)",
        msg
    )

    if designation_match:

        filters.append({

            "field": "designation",

            "operator": "eq",

            "value": designation_match.group(1).strip()
        })

    # department

    department_match = re.search(
        r"department\s+(?:is|=)\s+(.+)",
        msg
    )

    if department_match:

        filters.append({

            "field": "department",

            "operator": "eq",

            "value": department_match.group(1).strip()
        })

    # starts with

    starts_match = re.search(
        r"name starts with\s+([a-z])",
        msg
    )

    if starts_match:

        filters.append({

            "field": "name",

            "operator": "startswith",

            "value": starts_match.group(1).upper()
        })

    return filters