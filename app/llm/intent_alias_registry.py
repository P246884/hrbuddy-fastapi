from app.crm.entity_registry import ENTITY_REGISTRY
from app.intent.fast_intent import clean_text


def resolve_entity_from_query(query: str):
    query = clean_text(query)

    for entity_name, config in ENTITY_REGISTRY.items():
        for alias in config.get("aliases", []):
            if clean_text(alias) in query:
                return entity_name

    return None
