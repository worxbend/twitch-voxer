"""Private local-container control commands. No model import or host Python needed."""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .config import load_settings
from .token_store import read_tokens


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_control(path: str, *, method: str = "GET") -> dict:
    settings = load_settings()
    record = read_tokens(Path(settings.SETTINGS_FILE).with_name("control.json"))
    request = urllib.request.Request(
        record["origin"] + path,
        headers={"Authorization": "Bearer " + record["secret"]},
        method=method,
    )
    # Never send the private bearer through an ambient HTTP proxy or follow
    # an authentication redirect to another origin.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(request, timeout=5) as response:
        return json.load(response)


def main() -> None:
    command = sys.argv[1] if len(sys.argv) == 2 else ""
    try:
        if command == "status":
            print(json.dumps(request_control("/status")))
        elif command == "stage":
            print(request_control("/status")["stage"])
        elif command == "setup-url":
            print(request_control("/control/open", method="POST")["url"])
        else:
            raise SystemExit("Usage: python -m voxer.control status|stage|setup-url")
    except OSError, ValueError, KeyError, urllib.error.URLError:
        raise SystemExit(
            "Control is not available yet. Check container status and port 4343."
        ) from None


if __name__ == "__main__":
    main()
