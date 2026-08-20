import requests

#BASE_URL = "https://test-nest.elite-sis.com/"
BASE_URL = "https://localhost:58072"


def call_hrbuddy_api(
    endpoint: str,
    token: str,
    user: dict,
    method="GET",
    body=None
):

    headers = {
        "Authorization": token,
        "UserGuid": user.get(
            "user_guid",
            ""
        ),
        "BusinessGuid": user.get(
            "business_guid",
            ""
        )
    }

    try:

        # -----------------------------------
        # GET
        # -----------------------------------
        if method == "GET":

            response = requests.get(
                f"{BASE_URL}{endpoint}",
                headers=headers,
                verify=False,
                timeout=60
            )

        # -----------------------------------
        # POST
        # -----------------------------------
        else:

            response = requests.post(
                f"{BASE_URL}{endpoint}",
                headers=headers,
                json=body,
                verify=False,
                timeout=60
            )

        print(
            "API STATUS:",
            response.status_code
        )

        print(
            "API RESPONSE:",
            response.text
        )

        if response.status_code != 200:

            return {
                "success": False,
                "message":
                    f"API failed: "
                    f"{response.status_code}"
            }

        data = response.json()
        # The .NET API returns {"rsponse": true, "responseText": "..."} (note
        # the misspelled key). Normalize it to a "success"/"message" shape so
        # all callers can rely on response.get("success").
        if isinstance(data, dict) and "success" not in data:
            _ok = data.get("rsponse")
            if _ok is None:
                _ok = data.get("Rsponse")
            if _ok is None:
                _ok = data.get("response")
            if _ok is not None:
                data["success"] = bool(_ok)
            _txt = data.get("responseText") or data.get("ResponseText")
            if _txt and "message" not in data:
                data["message"] = _txt
        return data

    except Exception as ex:

        print(
            "API ERROR:",
            str(ex)
        )

        return {
            "success": False,
            "message": str(ex)
        }