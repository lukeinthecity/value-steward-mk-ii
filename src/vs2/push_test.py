"""Send one test push: `python -m vs2.push_test`.

A port of value-steward's `npm run push:test`, and the way to confirm the ntfy
wiring on the real machine before arming. Reads the same `.env`, sends one
notification, and says which of the three outcomes happened -- sent, skipped
for want of a topic, or failed -- because "nothing appeared on my phone" has
those three very different causes and guessing between them wastes an evening.

Deliberately never prints the topic, only whether one is configured. The topic
is a secret, and a terminal is a place things get pasted from.
"""

from __future__ import annotations

import logging
import os
import sys

from vs2.data.push import load_push_config, send_push

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from dotenv import load_dotenv

    load_dotenv()

    config = load_push_config(os.environ)
    if config is None:
        print(
            "[push] Skipped -- no topic configured.\n"
            "       Set VS_NTFY_TOPIC in .env (see .env.example), subscribe to "
            "that topic in the ntfy app, and run this again.\n"
            "       If VS_PUSH_ENABLED is false, that would also land here."
        )
        return

    # The server is not a secret; the topic is. Printing the server confirms a
    # self-hosted override took effect without revealing the feed.
    print(f"[push] Sending one test notification via {config.server} ...")

    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    result = send_push(
        config,
        label="test",
        title="Value Steward mk II · push test",
        message=f"Test push at {stamp} — if you see this, ntfy is wired up.",
        tags=("white_check_mark",),
    )

    if result.ok:
        print("[push] Sent. Check your phone.")
        return

    print(f"[push] FAILED: {result.error}")
    sys.exit(1)


if __name__ == "__main__":
    main()
