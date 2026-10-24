"""Build the Teams app package (manifest + icons) to upload to Teams.

    uv run python deploy/agent-gateway/teams/package.py \
        --app-id <teamsAppId> --bot-host <ca-...-teams.<region>.azurecontainerapps.io>

Writes deploy/agent-gateway/build/agent-gateway-teams.zip. Upload it in Teams
(Apps > Manage your apps > Upload an app) or publish it to your org in the Teams admin
center. ``webApplicationInfo`` is what turns on SSO: Teams gets a token for
``api://botid-<app-id>`` silently instead of showing a sign-in card.
"""

from __future__ import annotations

import argparse
import json
import struct
import zipfile
import zlib
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parents[1] / "build" / "agent-gateway-teams.zip"
ACCENT = (0x2F, 0x5B, 0xD3)


def manifest(app_id: str, bot_host: str, version: str = "1.0.0") -> dict[str, Any]:
    commands = [
        ("help", "What the bot can do"),
        ("agents", "Agents you can use"),
        ("me", "Your tier, team, cost center and token limits"),
        ("new", "Start a new conversation"),
    ]
    return {
        "$schema": "https://developer.microsoft.com/en-us/json-schemas/teams/v1.17/MicrosoftTeams.schema.json",
        "manifestVersion": "1.17",
        "version": version,
        "id": app_id,
        "developer": {
            "name": "Agent gateway",
            "websiteUrl": "https://github.com/Lariat-Labs/security-tools-sdk",
            "privacyUrl": "https://github.com/Lariat-Labs/security-tools-sdk/blob/main/docs/responsible-use.md",
            "termsOfUseUrl": "https://github.com/Lariat-Labs/security-tools-sdk/blob/main/LICENSE",
        },
        "name": {"short": "Agent Gateway", "full": "Agent Gateway security agents"},
        "description": {
            "short": "Run security agents (CVE triage, threat briefs) as yourself.",
            "full": "Ask security questions and the bot runs an agent through the agent gateway on "
            "your behalf. Your agents and token limits come from your Entra groups.",
        },
        "icons": {"color": "color.png", "outline": "outline.png"},
        "accentColor": "#{:02X}{:02X}{:02X}".format(*ACCENT),
        "bots": [
            {
                "botId": app_id,
                "scopes": ["personal"],
                "supportsFiles": False,
                "isNotificationOnly": False,
                "commandLists": [
                    {
                        "scopes": ["personal"],
                        "commands": [{"title": t, "description": d} for t, d in commands],
                    }
                ],
            }
        ],
        "permissions": ["identity", "messageTeamMembers"],
        "validDomains": ["token.botframework.com", bot_host],
        "webApplicationInfo": {"id": app_id, "resource": f"api://botid-{app_id}"},
    }


def _png(size: int, pixel: Any) -> bytes:
    """A minimal RGBA PNG; pixel(x, y) -> (r, g, b, a)."""
    raw = b"".join(b"\x00" + b"".join(bytes(pixel(x, y)) for x in range(size)) for y in range(size))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _shield(x: int, y: int, size: int) -> bool:
    """A simple shield shape, as a fraction of the icon size."""
    u, v = x / size, y / size
    if not 0.22 <= u <= 0.78 or not 0.18 <= v <= 0.84:
        return False
    if v < 0.6:
        return True
    return abs(u - 0.5) <= 0.28 * (0.84 - v) / 0.24


def icons() -> tuple[bytes, bytes]:
    color = _png(192, lambda x, y: (255, 255, 255, 255) if _shield(x, y, 192) else (*ACCENT, 255))
    outline = _png(32, lambda x, y: (255, 255, 255, 255) if _shield(x, y, 32) else (0, 0, 0, 0))
    return color, outline


def build(app_id: str, bot_host: str, out: Path = OUT, version: str = "1.0.0") -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    color, outline = icons()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest(app_id, bot_host, version), indent=2))
        z.writestr("color.png", color)
        z.writestr("outline.png", outline)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--app-id", required=True, help="teamsAppId (the bot app registration)")
    ap.add_argument("--bot-host", required=True, help="host name of the bot's container app")
    ap.add_argument("--version", default="1.0.0", help="bump to update an uploaded app")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    print(
        f"wrote {build(args.app_id, args.bot_host.removeprefix('https://').split('/')[0], args.out, args.version)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
