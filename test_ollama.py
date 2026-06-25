from app.crm.entity_registry import ACTION_PATTERNS
from app.intent.fast_intent import clean_text

test_messages = [
    "reject harshal's last leave",
    "reject last leave of harshal's",
    "reject last leave",
    "reject leave of harshal",
]

for original in test_messages:
    msg = clean_text(original)
    print(f"\nMSG: '{msg}'")
    matched = False
    for action, patterns in ACTION_PATTERNS.items():
        for p in patterns:
            if p in msg:
                print(f"  MATCH: {action} -> '{p}'")
                matched = True
    if not matched:
        print("  NO MATCH FOUND")