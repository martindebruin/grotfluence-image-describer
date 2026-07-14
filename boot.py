#!/usr/bin/env python3
"""Runtime secret injection for the auto-oat image-describer.

Authenticates to Infisical with a scoped machine identity, fetches the app's
secret values, injects them into the environment, drops the identity creds, and
execs uvicorn. Non-secret config (URLs, model names, etc.) comes from the
container environment (docker-compose). Real secrets live only in the process
env — never on disk or in `docker inspect`.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

DOMAIN = os.environ.get("INFISICAL_DOMAIN", "https://app.infisical.com")
PROJECT = os.environ["INFISICAL_PROJECT_ID"]
ENV = os.environ.get("INFISICAL_ENV", "dev")
CID = os.environ["INFISICAL_CLIENT_ID"]
CS = os.environ["INFISICAL_CLIENT_SECRET"]

# app env var -> Infisical secret key (sites project)
MAP = {
    "WEB_PASSWORD": "autooat_web_password",
    "TELEGRAM_BOT_TOKEN": "telegram_autoOat",
    "TELEGRAM_WEBHOOK_SECRET": "autooat_tg_webhook_secret",
    "DIRECTUS_TOKEN": "grot_directus_token",
}


def _post(url, payload, headers):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=30))


def _get(url, headers):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30))


def main():
    token = _post(f"{DOMAIN}/api/v1/auth/universal-auth/login",
                  {"clientId": CID, "clientSecret": CS}, {})["accessToken"]
    q = urllib.parse.urlencode({"workspaceId": PROJECT, "environment": ENV, "secretPath": "/"})
    raw = _get(f"{DOMAIN}/api/v3/secrets/raw?{q}", {"Authorization": "Bearer " + token})
    secrets = {s["secretKey"]: s["secretValue"] for s in raw["secrets"]}

    env = dict(os.environ)
    missing = []
    for app_name, inf_name in MAP.items():
        if inf_name in secrets:
            env[app_name] = secrets[inf_name]
        else:
            missing.append(inf_name)
    if missing:
        print(f"FATAL: missing Infisical secrets in {PROJECT}/{ENV}: {missing}", file=sys.stderr)
        sys.exit(1)

    for k in ("INFISICAL_CLIENT_ID", "INFISICAL_CLIENT_SECRET"):
        env.pop(k, None)

    os.execvpe("uvicorn",
               ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "4545"],
               env)


if __name__ == "__main__":
    main()
