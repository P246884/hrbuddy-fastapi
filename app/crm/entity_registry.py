ENTITY_REGISTRY = {

    "employee": {

        "aliases": [
            "employee",
            "employee details",
            "basic details",
            "employee profile",
            "profile",
            "employee info",
"employee details",
"employee info",
"employee profile",
"user",
"users",
"staff",
"people",
"colleague",
"colleagues",
"info",
"information",
"details",
"detail",
"about"
        ],

        "crm_entity": "bam_employee",

        "is_master_entity": True,

        "primary_field": "bam_employeeid",

        "employee_lookup": None,

        "search_field": "bam_name",

        "fields": {
            "employee_guid": "bam_employeeid",
            "employee_name": "bam_name",
            "employee_code": "bam_employeeno",
            "department": "bam_department",
            "designation": "bam_designation",
            "experience": "bam_totalexperienceinyears",
            "manager": "bam_manager",
            "date_of_birth": "bam_dateofbirth"
        },
        "searchable_fields": {

        "name": "bam_name",

        "designation": "bam_designation",

        "department": "bam_department",

        "employee_code": "bam_employeeno"
    },

        # --- config-driven additions (single source of truth) ---
        "formatter": "employee",
        "prompt_description": "employee info, profile, list (also users/staff/people/colleagues)",
        "routing_signals": [],
        # words that signal a "list of people" query -> target=multiple.
        # Used by fast_intent (registry-driven, no hardcoding there).
        "list_signals": [
            "employees", "users", "staff", "people", "colleagues",
            "everyone", "team members", "directory",
        ],
        "block_signals": []
    },

    "leave": {

        "aliases": [
            "leave balance",
            "leave balances",
            "remaining leave",
            "remaining leaves",
            "leave detail",
            "leave details",
            "leave info",
            "leave"
        ],

        "crm_entity": "bam_employeeleavestructure",

        "is_master_entity": False,

        "primary_field":
            "bam_employeeleavestructureid",

        "employee_lookup":
            "bam_employee",

        "type_filter_field":
            "leave_type",

        "fields": {
            "leave_type":
                "bam_leavetype",

            "balance":
                "bam_remainingleavesdec",

            "accounting_period":
                "bam_accountingperiod",

            "leave_type_guid":
                "bam_leavetype",

            "leave_structure_guid":
                "bam_employeeleavestructureid"
        },
    "allowed_types": [
    "sick",
    "annual",
    "casual",
    "comp off",
    "compoff",
    "carry forward"
                     ],
        # --- config-driven additions ---
        "formatter": "leave",
        "prompt_description": "leave BALANCE / remaining leaves ONLY (e.g. leave balance, kitni leave bachi)",
        # words that mean THIS entity (balance), winning over leave_history
        "routing_signals": ["balance", "remaining", "kitni leave", "kitne leave"],
        "block_signals": []
    },

    "leave_history": {

        "aliases": [
            "approved leaves",
            "rejected leaves",
            "cancelled leaves",
            "requested leaves",
            "leave history",
            "leave requests",
            "past leaves",
            "previous leaves",
            "applied leaves",
            "last leaves",
            "recent leaves",
            "leaves",
            "leave records",
            "leave list",
            "my leaves",
            "last 5 leaves",
            "last 3 leaves",
            "last 10 leaves"
        ],

        "crm_entity": "bam_leave",

        "is_master_entity": False,

        "primary_field":
            "bam_leaveid",

        "employee_lookup":
            "bam_employee",

        "type_filter_field":
            "leave_type",

        "date_field":
            "createdon",

        # Date-RANGE filters (e.g. "leaves of year 2026") target the leave's
        # actual start date, not when the record was created.
        "date_filter_field":
            "bam_startdate",

        "status_field":
            "statuscode",

        "fields": {

            "leave_type":
                "bam_leavetype",

            "from_date":
                "bam_startdate",

            "to_date":
                "bam_enddate",

            "status":
                "statuscode",

            "days":
                "bam_noofdays",

            "manager":
                "bam_manager",

            "leave_guid":
                "bam_leaveid"
        },
    "allowed_types": [
    "sick",
    "annual",
    "casual",
    "comp off",
    "compoff",
    "carry forward"
                     ],
        # --- config-driven additions ---
        "formatter": "leave_history",
        "prompt_description": "any leave RECORDS: applied/approved/rejected/cancelled/requested/pending leaves, leave history, past/previous/recent leaves",
        # status/history words -> this entity (even with singular "leave")
        "routing_signals": [
            "requested", "approved", "rejected", "cancelled", "canceled",
            "applied", "taken", "history", "past leave", "previous leave",
            "recent leave", "last leave",
            # usage-insight queries aggregate over RECORDS, so route here
            "used the most", "most used", "use the most", "used most",
            "average", "avg", "trend", "how often", "which month",
            "most leaves", "most leave", "which leave type",
        ],
        "block_signals": [],
        # per-module status code map (falls back to global STATUS_MAPPING)
        "status_map": {
            1: "Requested",
            100010001: "Approved",
            100010004: "Rejected",
            100010003: "Cancelled",
        }
    },

    # ----------------------------------------------------------------
    # NEW MODULE TEST: attendance — added with REGISTRY ONLY (no edits to
    # fast_intent / response_builder / prompts). Proves the config-driven
    # design works end to end. Field names are placeholders; swap them for
    # your real CRM attendance entity when wiring it for real.
    # ----------------------------------------------------------------
    # ----------------------------------------------------------------
    # attendance — real CRM entity: bam_employeeattendance.
    # NOTE: per employee per date there are TWO rows — "System Hours" and
    # "Office Hours" — distinguished by bam_attendancemarkedby. We surface that
    # field so each row is labelled instead of looking like a duplicate.
    # ----------------------------------------------------------------
    "attendance": {
        "aliases": [
            "attendance", "clock in", "clock out", "punch in", "punch out",
            "in time", "out time", "working hours", "attendance record",
            "system hours", "office hours",
        ],
        "crm_entity": "bam_employeeattendance",
        "is_master_entity": False,
        "primary_field": "bam_employeeattendanceid",   # CONFIRM exact PK name
        "employee_lookup": "bam_employee",              # CONFIRM lookup field
        "date_field": "bam_attendancedate",             # CONFIRM date column
        "status_field": "statuscode",
        "fields": {
            "date": "bam_attendancedate",               # CONFIRM date column
            "marked_by": "bam_attendancemarkedby",       # System Hours / Office Hours
            "in_time": "bam_intimestring",
            "out_time": "bam_outtimestring",
            "attendance_guid": "bam_employeeattendanceid",
        },
        "allowed_types": [],
        "formatter": "generic",
        "prompt_description": "attendance / clock-in / clock-out / working-hours records (system & office hours)",
        "routing_signals": [
            "attendance", "clock in", "clock out", "punch in", "punch out",
            "in time", "out time", "working hours", "system hours", "office hours",
        ],
        "block_signals": [],
        # bam_attendancemarkedby is an OPTIONSET (stores a code, not text).
        # value_maps render the code as a label (display) and convert a label
        # back to its code (filter). VERIFY these two codes in your CRM and swap
        # if reversed — only 810100002 was seen in logs; 810100001 is assumed.
        "value_maps": {
            "marked_by": {
                810100000: "System Hours",
                810100002: "Office Hours",
            }
        },
    },

    # ----------------------------------------------------------------
    # holiday — global (org-wide) list. NOT employee-scoped, so
    # employee_lookup is None and the query builder adds no person filter.
    # Date filters (this month / this year / upcoming) hit bam_startdate via
    # date_filter_field. CONFIRM the PK/field names against your CRM.
    # ----------------------------------------------------------------
    "holiday": {
        "aliases": [
            "holiday", "holidays", "public holiday", "public holidays",
            "gazetted holiday", "holiday list", "chutti list", "chuttiyan",
        ],
        "crm_entity": "bam_holiday",
        "is_master_entity": False,
        "primary_field": "bam_holidayid",     # CONFIRM exact PK name
        "employee_lookup": None,               # global list, no person scope
        "date_field": "bam_startdate",
        "date_filter_field": "bam_startdate",
        "fields": {
            "name": "bam_name",
            "from_date": "bam_startdate",
            "to_date": "bam_enddate",
            "days": "bam_noofdays",
            "business_unit": "bam_businessunit",
            "holiday_guid": "bam_holidayid",
        },
        "allowed_types": [],
        "formatter": "generic",
        "prompt_description": "company holidays / public holidays (with dates)",
        "routing_signals": ["holiday", "holidays", "public holiday", "chutti list"],
        "block_signals": [],
    },

    # CRM Notes / attachments. Fetched by objectid (the regarding record —
    # here a bam_leave). employee_lookup is reused as the objectid filter, so
    # passing filters={"employee_guid": <leave_guid>} filters notes for that
    # leave. documentbody is the base64 file body (used for download).
    "annotation": {
        "aliases": ["attachment", "attachments", "note", "notes", "document"],
        "crm_entity": "annotation",
        "is_master_entity": False,
        "primary_field": "annotationid",
        "employee_lookup": "objectid",         # filter notes by regarding record
        "fields": {
            "annotation_guid": "annotationid",
            "filename": "filename",
            "mimetype": "mimetype",
            "subject": "subject",
            "notetext": "notetext",
            "documentbody": "documentbody",
            "is_document": "isdocument",
        },
        "allowed_types": [],
        "formatter": "generic",
        "prompt_description": "leave attachments (notes)",
        "routing_signals": [],
        "block_signals": [],
    }
}


STATUS_MAPPING = {
    "approved": 100010001,
    "rejected": 100010004,
    "requested": 1,
    "cancelled": 100010003,
    "canceled": 100010003
}

# -------------------------------------------------------
# ACTION PATTERNS
# Fast intent ke liye — apply/approve/reject/cancel
# Hinglish + English + shortcuts
# -------------------------------------------------------

ACTION_PATTERNS = {

    "apply_leave": [
        # English
        "apply leave",
        "apply for leave",
        "apply sick",
        "apply annual",
        "apply casual",
        "apply comp",
        "apply carry",
        "take leave",
        "request leave",
        "i want leave",
        "i need leave",
        "need a leave",
        "want a leave",
        "submit leave",
        "mark leave",
        "i am sick",
        "i am unwell",
        "feeling sick",
        "not feeling well",
        "leave from",
        "leave on",
        "leave for",
        "leave next",
        "leave tomorrow",
        "leave today",
        # Shortcuts
        "sl apply",
        "cl apply",
        "al apply",
        "apply sl",
        "apply cl",
        "apply al",
        # Hinglish
        "leave chahiye",
        "leave leni hai",
        "leave lena hai",
        "chutti chahiye",
        "chutti leni hai",
        "chutti lena hai",
        "bimar hoon",
        "tabiyat theek nahi",
        "leave do",
        "leave dedo",
        "leave approve karo meri",
        "mujhe leave chahiye",
        "mujhe leave leni",
        "leave apply karo",
        "leave apply karni hai",
        # Verb-conjugation independent (catches krdo / kardo / kar do / karni / karo)
        "leave apply",
        "apply krdo",
        "apply kardo",
        "apply kar do",
        "apply krdo",
        "leave laga",
        "leave lga",
        "chutti laga",
        "ki leave apply",
        "ki chutti",
    ],

    "approve_leave": [
        # English
        "approve leave",
        "approve the leave",
        "accept leave",
        "approve request",
        "grant leave",
        "allow leave",
        "approve last leave",
        "approve latest leave",
        "approve recent leave",
        "approve pending leave",
        "approve leave of",
        "approve leave for",
        # Hinglish
        "leave approve karo",
        "leave approve karden",
        "approve kar do",
        "leave accept karo",
        "ki leave approve",
        "ka leave approve",
    ],

    "reject_leave": [
        # English
        "reject leave",
        "reject the leave",
        "decline leave",
        "deny leave",
        "refuse leave",
        "reject last leave",
        "reject latest leave",
        "reject recent leave",
        "reject pending leave",
        "reject leave of",
        "reject leave for",
        "reject harshal",
        # Generic: "reject <name>'s" or "reject <name> leave"
        # Handled by keyword "reject" alone when "leave" also present
        # Hinglish
        "leave reject karo",
        "leave reject karden",
        "reject kar do",
        "leave decline karo",
        "ki leave reject",
        "ka leave reject",
    ],

    "cancel_leave": [
        # English
        "cancel leave",
        "cancel my leave",
        "cancel my last leave",
        "cancel my latest leave",
        "cancel my recent leave",
        "cancel last leave",
        "cancel latest leave",
        "withdraw leave",
        "cancel the leave",
        "cancel application",
        "cancel leave request",
        "cancel last leave",
        "cancel latest leave",
        "cancel pending leave",
        # Hinglish
        "leave cancel karo",
        "leave cancel karni hai",
        "leave wapas lo",
        "leave cancel kardo",
        "meri leave cancel",
        "ki leave cancel",
        "ka leave cancel",
    ]
}