"""Sign in to Kiro with a browser (device code) and store the login for the Incident to PR demo (kiroAuthMode=web).

    python scripts/kiro_web_login.py --start-url https://my-org.awsapps.com/start --idc-region us-east-1
    python scripts/kiro_web_login.py --builder-id

Open the printed URL, approve the request, and the script stores:
  incident-to-pr/kiro-web-login  refresh token and OIDC client registration (read only by the refresher Lambda)
  incident-to-pr/kiro-web-token  short-lived access token (read by the AgentCore Runtime)
Tokens are never printed. Run it again when the IAM Identity Center session ends.
"""
import argparse
import json
import time
from datetime import datetime, timedelta, timezone

import boto3

SCOPES = ["codewhisperer:completions", "codewhisperer:analysis", "codewhisperer:conversations"]
BUILDER_ID_URL = "https://view.awsapps.com/start"


def _put(client, name, value):
    try:
        client.put_secret_value(SecretId=name, SecretString=json.dumps(value))
    except client.exceptions.ResourceNotFoundException:
        client.create_secret(Name=name, SecretString=json.dumps(value))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start-url", help="IAM Identity Center start URL")
    parser.add_argument("--idc-region", default="us-east-1", help="IAM Identity Center region")
    parser.add_argument("--builder-id", action="store_true", help="sign in with AWS Builder ID")
    parser.add_argument("--profile-arn", help="Kiro (CodeWhisperer) profile ARN for IAM Identity Center accounts")
    parser.add_argument("--region", default="ap-northeast-2", help="region of the demo stack secrets")
    args = parser.parse_args()
    if bool(args.start_url) == args.builder_id:
        parser.error("use either --start-url or --builder-id")
    start_url = BUILDER_ID_URL if args.builder_id else args.start_url
    oidc = boto3.client("sso-oidc", region_name=args.idc_region)
    client = oidc.register_client(clientName="incident-to-pr-kiro", clientType="public", scopes=SCOPES)
    device = oidc.start_device_authorization(clientId=client["clientId"], clientSecret=client["clientSecret"],
                                             startUrl=start_url)
    print("Open this URL in a browser and approve the request:")
    print(" ", device["verificationUriComplete"])
    print("Code:", device["userCode"], flush=True)
    deadline, interval = time.time() + device["expiresIn"], device.get("interval", 5)
    while True:
        time.sleep(interval)
        try:
            token = oidc.create_token(clientId=client["clientId"], clientSecret=client["clientSecret"],
                                      grantType="urn:ietf:params:oauth:grant-type:device_code",
                                      deviceCode=device["deviceCode"])
            break
        except oidc.exceptions.AuthorizationPendingException:
            pass
        except oidc.exceptions.SlowDownException:
            interval += 5
        except (oidc.exceptions.ExpiredTokenException, oidc.exceptions.InvalidGrantException):
            raise SystemExit("The device code expired before approval. Run the script again.") from None
        if time.time() > deadline:
            raise SystemExit("The device code expired before approval")
    secrets = boto3.client("secretsmanager", region_name=args.region)
    login = {"clientId": client["clientId"], "clientSecret": client["clientSecret"],
             "clientSecretExpiresAt": client["clientSecretExpiresAt"], "refreshToken": token["refreshToken"],
             "region": args.idc_region, "startUrl": start_url}
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=token["expiresIn"])).isoformat().replace("+00:00", "Z")
    access = {"accessToken": token["accessToken"], "expiresAt": expires_at}
    if args.profile_arn:
        login["profileArn"] = access["profileArn"] = args.profile_arn
    _put(secrets, "incident-to-pr/kiro-web-login", login)
    _put(secrets, "incident-to-pr/kiro-web-token", access)
    print(f"Stored the Kiro web login in Secrets Manager ({args.region}); access token expires at {expires_at}.")


if __name__ == "__main__":
    main()
