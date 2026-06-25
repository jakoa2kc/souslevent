"""Météo-France API helpers — AROME (1.3 km) fine-forecast access.

For now this only **validates the AROME API key** so the IHM can warn (popup) when the key
expired or was revoked, and point to the renewal procedure. Validation is **offline**: the
key is a JWT whose payload carries the expiry (``exp``) and the subscribed APIs — no network
needed. The GRIB data path (cfgrib) is a later step (roadmap M4); the key + its check land
first. See docs/support/meteofrance_arome.md.

Wind direction / units conventions are handled where the data is *consumed*, not here.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone

# The AROME API context as it appears in the key's ``subscribedAPIs`` (portail-api.meteofrance.fr).
AROME_API_CONTEXT = "/public/arome/1.0"
PORTAL_URL = "https://portail-api.meteofrance.fr"
# Optional local-only account hints for the renewal popup. Keep personal account data out of Git;
# store these in .env when useful. The API key itself is read by config.load_config().
ACCOUNT_LOGIN_ENV = "METEOFRANCE_ACCOUNT_LOGIN"
ACCOUNT_EMAIL_ENV = "METEOFRANCE_ACCOUNT_EMAIL"


@dataclass(frozen=True)
class KeyStatus:
    """Result of validating the AROME API key.

    ``ok`` is True when the key is usable (note ``expiring_soon`` is ok=True but worth a
    warning). ``reason`` is a stable tag: ok / missing / malformed / expired /
    not_subscribed / expiring_soon. ``message`` is a ready-to-show French sentence.
    """

    ok: bool
    reason: str
    message: str
    owner: str | None = None
    expires: datetime | None = None
    days_left: int | None = None


def _decode_payload(token: str) -> dict:
    """Decode the JWT payload (middle segment) without verifying the signature."""
    parts = token.strip().split(".")
    if len(parts) < 2 or not parts[1]:
        raise ValueError("format JWT invalide")
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)  # restore base64 padding
    return json.loads(base64.urlsafe_b64decode(payload))


def check_arome_key(
    token: str | None, *, warn_days: int = 30, now: datetime | None = None
) -> KeyStatus:
    """Validate the Météo-France AROME API key offline (JWT decode).

    Returns a :class:`KeyStatus`. Detects: no key, unreadable key, AROME not subscribed,
    expired, and "expires within ``warn_days``" (still usable). ``now`` is injectable for
    tests.
    """
    now = now or datetime.now(timezone.utc)
    if not token or not token.strip():
        return KeyStatus(False, "missing", "Aucune clé API Météo-France configurée.")
    try:
        d = _decode_payload(token)
    except Exception:
        return KeyStatus(False, "malformed", "Clé API Météo-France illisible (format inattendu).")

    owner = (d.get("application") or {}).get("owner")
    contexts = [a.get("context") for a in d.get("subscribedAPIs", []) or []]
    exp_ts = d.get("exp")
    expires = datetime.fromtimestamp(exp_ts, timezone.utc) if exp_ts else None
    days_left = int((expires - now).total_seconds() // 86400) if expires else None

    if AROME_API_CONTEXT not in contexts:
        return KeyStatus(False, "not_subscribed",
                         "La clé n'est pas abonnée à l'API AROME (/public/arome/1.0).",
                         owner, expires, days_left)
    if expires is not None and expires <= now:
        return KeyStatus(False, "expired",
                         f"Clé API AROME expirée le {expires:%d/%m/%Y}.",
                         owner, expires, days_left)
    if days_left is not None and days_left <= warn_days:
        return KeyStatus(True, "expiring_soon",
                         f"Clé API AROME valide, mais expire dans {days_left} j "
                         f"(le {expires:%d/%m/%Y}).", owner, expires, days_left)
    msg = (f"Clé API AROME valide jusqu'au {expires:%d/%m/%Y}." if expires
           else "Clé API AROME valide.")
    return KeyStatus(True, "ok", msg, owner, expires, days_left)


def renewal_text() -> str:
    """Renewal procedure for the IHM popup (mirrors docs/support/meteofrance_arome.md)."""
    account_login = os.environ.get(ACCOUNT_LOGIN_ENV, f"<{ACCOUNT_LOGIN_ENV}>")
    account_email = os.environ.get(ACCOUNT_EMAIL_ENV, f"<{ACCOUNT_EMAIL_ENV}>")
    return (
        f"Compte : {account_login}\n"
        f"E-mail : {account_email}\n"
        f"Portail : {PORTAL_URL}\n\n"
        "Renouvellement de la clé :\n"
        "1. Se connecter au portail avec le compte ci-dessus.\n"
        "2. Vérifier l'abonnement à l'API « AROME » (/public/arome/1.0).\n"
        "3. Ouvrir l'application de clés API configurée sur le portail (PRODUCTION).\n"
        "4. Générer une nouvelle clé (validité jusqu'à 3 ans).\n"
        "5. Coller la clé dans le fichier .env :  METEOFRANCE_API_KEY=...\n"
        "6. Redémarrer Sillage.\n\n"
        "Détails : docs/support/meteofrance_arome.md"
    )
