"""A service's ``(body, status)`` as a route's answer, and a refusal that says why in the log.

Production, 2026-11-26: three 400s in a row on ``POST /accounting/xero-accounts`` and the log held
only "POST ... 400", so nobody could tell which of its refusals it was. Every refused answer now
logs its sentence once: 4xx at INFO ("refused"), 5xx at WARNING ("failed"). A per-field refusal
names its fields, never their values.
"""

import logging

from django.http import JsonResponse

logger = logging.getLogger("minty-onboarding")


def answered(body: dict, status: int) -> JsonResponse:
    if status >= 400:
        fields = sorted(body.get("fields") or {}) if isinstance(body.get("fields"), dict) else []
        log, word = (logger.warning, "failed") if status >= 500 else (logger.info, "refused")
        log("%s %s: %s%s", word, status, body.get("error"), f" fields={fields}" if fields else "")
    return JsonResponse(body, status=status)
