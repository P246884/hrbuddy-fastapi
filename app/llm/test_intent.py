from app.llm.ollama_client import extract_decision

query = "share harshal's leave balance but dont show sick"

for i in range(5):
    print(extract_decision(query))