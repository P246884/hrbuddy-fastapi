import requests

BASE_URL = "https://test-nest.elite-sis.com/"
# BASE_URL = "https://localhost:58072"


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
                timeout=30
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
                timeout=30
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

        return response.json()

    except Exception as ex:

        print(
            "API ERROR:",
            str(ex)
        )

        return {
            "success": False,
            "message": str(ex)
        }