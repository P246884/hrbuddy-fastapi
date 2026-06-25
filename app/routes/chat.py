from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from app.models.chat_models import ChatRequest
from app.agents.hr_agent import process_message
import jwt
import json

router = APIRouter()

SECRET_KEY = "HRBUDDY_SUPER_SECRET_KEY_2026_THIS_IS_32PLUS_BYTES_SECRET!!"
ALGORITHM = "HS256"


def get_user_from_token(auth: str):
    if not auth:
        raise HTTPException(status_code=401, detail="Missing token")
    try:
        token = auth.replace("Bearer ", "")
        decoded = jwt.decode(
            token, SECRET_KEY,
            algorithms=[ALGORITHM],
            audience="HRBuddyClient",
            issuer="HRBuddy"
        )
        return decoded
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))


@router.post("/chat")
def chat(
    request: ChatRequest,
    authorization: str = Header(None, convert_underscores=False)
):
    user = get_user_from_token(authorization)

    user_context = {
        "name": user.get("UserName", "User"),
        "is_hr": str(user.get("IsHR", "false")).lower() == "true",
        "is_admin": str(user.get("IsAdmin", "false")).lower() == "true",
        "is_manager": str(user.get("IsManager", "false")).lower() == "true",
        "user_guid": user.get("UserGuid", ""),
        "business_guid": user.get("BusinessGuid", "")
    }

    user_guid = user_context["user_guid"]

    # Continuation context comes ONLY from the client. Picker flows (type /
    # date / reason) send their context explicitly via request.context. A
    # freshly typed message carries no context, so it always starts fresh and
    # can never resurrect a stale or cancelled action. (Pickers block the input
    # box, so the user only types freely when nothing is pending.)
    pending = request.context if request.context else None

    result = process_message(
        message=request.message,
        user=user_context,
        token=authorization,
        pending_context=pending
    )

    if isinstance(result, tuple):
        response, new_context = result
    else:
        response = result
        new_context = None

    print(type(response))

    # -----------------------------------
    # JSON response detect karo
    # (leave actions se aata hai)
    # -----------------------------------
    if isinstance(response, str):
        try:
            parsed = json.loads(response)
            if isinstance(parsed, dict) and "type" in parsed:
                return JSONResponse(content=parsed)
        except Exception:
            pass

        # Normal string — streaming
        return StreamingResponse(iter([response]), media_type="text/plain")

    # Generator — streaming
    return StreamingResponse(response, media_type="text/plain")