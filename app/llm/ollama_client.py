import json
import time
import ollama
import re
from app.llm.intent_alias_registry import resolve_entity_from_query
from app.llm.prompts import SYSTEM_PROMPT
from app.crm.entity_registry import ENTITY_REGISTRY
from ollama import Client
OLLAMA_HOST = "http://192.168.3.11:11434"
client = Client(host=OLLAMA_HOST)
INTENT_MODEL = "qwen2.5:1.5b"
CHAT_MODEL = "qwen2.5:1.5b"
# INTENT_MODEL = "qwen2.5:3b"
# CHAT_MODEL = "qwen2.5:3b"




VALID_ENTITIES = {
    "employee",
    "leave",
    "leave_history",
    "attendance",
    "payroll",
    "holiday",
    "analytics"
}
ENTITY_ALIASES = {
    "leave balance": "leave",
    "leave balances": "leave",
    "leaves": "leave",
    "leave": "leave",
    "leave history": "leave_history",
    "profile": "employee",
    "employee profile": "employee"
}
def _empty_filters():
    return {
        "employee_name": "",
        "employee_names": [],
        "status": "",
        "type": "",
        "types": [],
        "days": "",
        "months": "",
        "top": "",
        "from_date": "",
        "to_date": "",
        "starts_with": "",
        "designation": "",
        "designations": [],
        "department": "",
        "departments": [],
        "manager": "",
        "managers": [],
        "experience_gt": "",
        "experience_gte": "",
        "experience_lt": "",
        "experience_lte": "",
        "dynamic_filters": []
    }


def _normalize_decision(
    parsed: dict,
    message: str
) -> dict:

    if not isinstance(parsed, dict):
        parsed = {}

    parsed.setdefault("entity", "")
    parsed.setdefault("operation", "read")
    parsed.setdefault("target", "self")
    parsed.setdefault("answer", "")

    filters = parsed.get("filters") or {}

    if not isinstance(filters, dict):
        filters = {}

    normalized_filters = _empty_filters()
    normalized_filters.update(filters)
    if filters.get("exclude_types"):
     normalized_filters["type"] = "exclude"
     normalized_filters["types"] = filters.get("exclude_types")

    if filters.get("include_types"):
     normalized_filters["type"] = "include"
     normalized_filters["types"] = filters.get("include_types")
    # ----------------------------------
    # Generic Exclude Detection
    # ----------------------------------

    for key, value in normalized_filters.items():

        if not isinstance(value, list):
            continue

        excludes = []

        for item in value:

            item = str(item).lower()

            if item.startswith("exclude_"):

                excludes.append(
                    item.replace(
                        "exclude_",
                        ""
                    )
                )

            elif item.startswith("!"):

                excludes.append(
                    item.replace(
                        "!",
                        ""
                    )
                )

        if excludes:

            normalized_filters["type"] = "exclude"

            normalized_filters["types"] = excludes

            break

    # ----------------------------------
    # Employee Alias Mapping
    # ----------------------------------

    for alias_key in [
        "name",
        "employee",
        "employeeName",
        "emp_name",
        "user_name"
    ]:

        if (
            alias_key in filters
            and not normalized_filters.get(
                "employee_name"
            )
        ):

            normalized_filters[
                "employee_name"
            ] = filters.get(alias_key)

    # ----------------------------------
    # Handle type = !something
    # ----------------------------------

    filter_type = normalized_filters.get(
        "type"
    )

    if (
        isinstance(filter_type, str)
        and "!" in filter_type
    ):

        normalized_filters["type"] = "exclude"

        normalized_filters["types"] = [

            value.replace(
                "!",
                ""
            ).replace(
                "_",
                " "
            ).strip()

            for value in filter_type.split(",")

            if value.strip()
        ]

    # ----------------------------------
    # Safety
    # ----------------------------------

    if not isinstance(
        normalized_filters.get("types"),
        list
    ):
        normalized_filters["types"] = []

    if not isinstance(
        normalized_filters.get(
            "employee_names"
        ),
        list
    ):
        normalized_filters[
            "employee_names"
        ] = []

    # ----------------------------------
    # Employee Name Detection
    # ----------------------------------

    if not normalized_filters.get(
        "employee_name"
    ):

        possessive_match = re.search(
            r"\b([a-zA-Z]+)'s\b",
            message,
            re.IGNORECASE
        )

        if possessive_match:

            normalized_filters[
                "employee_name"
            ] = (
                possessive_match.group(1)
            )

    if not normalized_filters.get(
        "employee_name"
    ):

        match = re.search(
            r"(?:of|for)\s+([a-zA-Z]+)",
            message,
            re.IGNORECASE
        )

        if match:

            normalized_filters[
                "employee_name"
            ] = (
                match.group(1)
            )

    parsed["filters"] = normalized_filters

    # ----------------------------------
    # Entity Validation
    # ----------------------------------

    if (
        not parsed.get("entity")
        or parsed.get("entity")
        not in ENTITY_REGISTRY
    ):

        resolved_entity = (
            resolve_entity_from_query(
                message
            )
        )

        if resolved_entity:

            parsed["entity"] = (
                resolved_entity
            )

    # ----------------------------------
    # Target Resolution
    # ----------------------------------

    if normalized_filters.get(
        "employee_name"
    ):

        parsed["target"] = "employee"

    if normalized_filters.get(
        "employee_names"
    ):

        parsed["target"] = "multiple"

    print(
        "NORMALIZED FILTERS:",
        normalized_filters
    )

    return parsed

def extract_decision(message: str):
    print("CALLING OLLAMA...")
    start = time.time()

    try:
        print("PROMPT LENGTH:", len(SYSTEM_PROMPT))
        response = ollama.chat(
            model=INTENT_MODEL,
            format="json",
            keep_alive="30m",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": message}
            ],
            options={
                "temperature": 0,
                "num_predict": 80
            }
        )

        print("OLLAMA TIME:", time.time() - start)

        content = response["message"]["content"]
        print("OLLAMA RESPONSE:", content)

        parsed = json.loads(content)
        return _normalize_decision(parsed, message)

    except Exception as ex:
        print("OLLAMA ERROR:", str(ex))
        return None


def chat_response(message: str):
    # True streaming: yield tokens as Ollama generates them, so the reply
    # flows out live instead of arriving in one shot after full generation.
    def _gen():
        # Sentinel marker so the frontend knows this is a genuine live token
        # stream (and must NOT re-type it with the cosmetic typewriter).
        yield "\x1fLIVE\x1f"
        try:
            stream = ollama.chat(
                model=CHAT_MODEL,
                stream=True,
                keep_alive="30m",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are HRBuddy, an HRMS assistant. You ONLY help "
                            "with HR topics: employee details, leave balance, "
                            "leave history, and applying/approving/rejecting/"
                            "cancelling leaves. "
                            "If the user asks ANYTHING outside HR — jokes, "
                            "general chit-chat, coding, trivia, opinions, world "
                            "facts, etc. — do NOT answer it. Politely reply that "
                            "you can only assist with HR-related queries and give "
                            "one or two example HR questions they can ask. "
                            "Never tell jokes or discuss non-HR topics. "
                            "Keep replies short and professional."
                        )
                    },
                    {"role": "user", "content": message}
                ],
                options={"temperature": 0.4, "num_predict": 400}
            )
            for part in stream:
                piece = part.get("message", {}).get("content", "")
                if piece:
                    yield piece
        except Exception as ex:
            print("CHAT STREAM ERROR:", str(ex))
            yield "Sorry, I couldn't generate a response right now."

    return _gen()