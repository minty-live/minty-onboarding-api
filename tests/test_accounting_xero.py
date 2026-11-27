"""Accounting Setup's Xero path - X2, X3 (Figma 348:1874, 348:1949): which of the Xero chart's
sales and expense accounts Minty uses. The ticks are ``account_module`` rows (Petty Cash's), and an
expense tick is also ``entity_account_xero.is_active``, which the apps still read. X4 (the five
daily-closing accounts) is 4.5's ``/accounting/slots``, whose options already include Xero's rows.
"""

import uuid

import pytest
from minty_db.models import AccountInfo, AccountModule, Entity, EntityAccountXero

from core import xero_client

BASE = "/api/onboarding/accounting"


def post(client, auth, path, payload):
    return client.post(BASE + path, payload, content_type="application/json", **auth)


def _xero(entity, code, name, account_type, xero_type):
    return AccountInfo.objects.create(
        entity_id=entity.id,
        name=name,
        code=code,
        xero_code=code,
        account_type=account_type,
        type=xero_type,
        status="ACTIVE",
        source="xero",
        xero_account_id=f"x-{code}",
    )


@pytest.fixture
def xero_company(entity):
    """Connected to Xero, its chart synced: two sales, two expense accounts and a bank."""
    Entity.objects.filter(id=entity.id).update(setup_mode="xero", xero_org_id="org-1")
    entity.refresh_from_db()
    rows = {
        "sales": _xero(entity, "200", "Sales", "revenue", "REVENUE"),
        "service": _xero(entity, "210", "Service Income", "revenue", "REVENUE"),
        "purchases": _xero(entity, "300", "Purchases", "direct_costs", "DIRECTCOSTS"),
        "rent": _xero(entity, "469", "Rent", "expense", "EXPENSE"),
        "bank": _xero(entity, "090", "Petty Cash", "bank", "BANK"),
    }
    return entity, rows


def _setup(client, auth, entity):
    resp = client.get(BASE, {"entity_id": str(entity.id)}, **auth)
    assert resp.status_code == 200, resp.content
    return resp.json()


def _ticks(client, auth, entity, kind, ids):
    return post(
        client,
        auth,
        "/xero-accounts",
        {"entity_id": str(entity.id), "kind": kind, "account_ids": [str(i) for i in ids]},
    )


def _used(body, kind):
    return {r["code"]: r["used"] for r in body["accounts"][kind]}


@pytest.mark.django_db
def test_the_xero_chart_is_listed_with_nothing_used_yet(client, auth, xero_company):
    entity, _rows = xero_company
    body = _setup(client, auth, entity)
    assert body["connected"] is True
    assert _used(body, "sales") == {"200": False, "210": False}
    assert _used(body, "expense") == {"300": False, "469": False}
    assert all(r["from_xero"] for r in body["accounts"]["sales"])


@pytest.mark.django_db
def test_a_disconnected_company_keeping_its_organisation_is_not_connected(
    client, auth, xero_company
):
    entity, _rows = xero_company
    Entity.objects.filter(id=entity.id).update(status="disconnected")
    assert _setup(client, auth, entity)["connected"] is False


@pytest.mark.django_db
def test_x2_ticks_are_petty_cash_module_rows(client, auth, xero_company):
    entity, rows = xero_company
    assert _ticks(client, auth, entity, "sales", [rows["sales"].id]).status_code == 200
    assert _used(_setup(client, auth, entity), "sales") == {"200": True, "210": False}
    assert AccountModule.objects.get(account_id=rows["sales"].id, module_code="PETTY_CASH").is_active

    # Exactly the ticked ones: a second save moves the tick.
    assert _ticks(client, auth, entity, "sales", [rows["service"].id]).status_code == 200
    assert _used(_setup(client, auth, entity), "sales") == {"200": False, "210": True}
    assert not AccountModule.objects.get(
        account_id=rows["sales"].id, module_code="PETTY_CASH"
    ).is_active


@pytest.mark.django_db
def test_x3_ticks_also_reach_the_old_petty_cash_rows(client, auth, xero_company):
    entity, rows = xero_company
    resp = _ticks(client, auth, entity, "expense", [rows["rent"].id])
    assert resp.status_code == 200, resp.content
    assert _used(_setup(client, auth, entity), "expense") == {"300": False, "469": True}
    old = {e.account_id: e.is_active for e in EntityAccountXero.objects.filter(account__entity_id=entity.id)}
    assert old[rows["rent"].id] is True and old[rows["purchases"].id] is False


@pytest.mark.django_db
def test_an_expense_tick_from_the_old_wizard_still_counts_as_used(client, auth, xero_company):
    entity, rows = xero_company
    EntityAccountXero.objects.create(
        account_id=rows["purchases"].id, name="Purchases", type="DIRECTCOSTS", is_active=True
    )
    assert _used(_setup(client, auth, entity), "expense") == {"300": True, "469": False}


@pytest.mark.django_db
@pytest.mark.parametrize(
    "kind, pick, status, sentence",
    [
        ("sales", [], 400, "Tick at least one sales account to continue."),
        ("expense", [], 400, "Tick at least one expense account to continue."),
        ("sales", ["rent"], 400, "Pick only sales accounts here. Reload the list and try again."),
        ("bank", ["bank"], 400, "Choose which accounts to tick: sales or expense."),
    ],
)
def test_bad_ticks_are_refused_with_a_sentence(
    client, auth, xero_company, kind, pick, status, sentence
):
    entity, rows = xero_company
    resp = _ticks(client, auth, entity, kind, [rows[p].id for p in pick])
    assert (resp.status_code, resp.json()["error"]) == (status, sentence)


@pytest.mark.django_db
def test_an_account_minty_made_or_another_companys_is_refused(client, auth, xero_company):
    entity, _rows = xero_company
    own = AccountInfo.objects.create(
        entity_id=entity.id, name="Tips", code="250", xero_code="250", account_type="revenue",
        type="REVENUE", status="ACTIVE", source="user",
    )
    resp = _ticks(client, auth, entity, "sales", [own.id])
    assert resp.status_code == 400
    resp = _ticks(client, auth, entity, "sales", [uuid.uuid4()])
    assert resp.status_code == 400


@pytest.mark.django_db
def test_a_company_not_connected_cannot_tick_xero_accounts(client, auth, xero_company):
    entity, rows = xero_company
    Entity.objects.filter(id=entity.id).update(status="disconnected")
    resp = _ticks(client, auth, entity, "sales", [rows["sales"].id])
    assert resp.status_code == 409
    assert resp.json()["error"] == "Connect Xero first, then choose the accounts Minty uses."


@pytest.mark.django_db
def test_refresh_asks_accounts_api_for_a_sync(client, auth, xero_company, monkeypatch):
    entity, _rows = xero_company
    asked = []
    monkeypatch.setattr(xero_client, "sync", lambda entity_id: asked.append(str(entity_id)) or True)
    resp = post(client, auth, "/xero-refresh", {"entity_id": str(entity.id)})
    assert resp.status_code == 200, resp.content
    assert asked == [str(entity.id)] and "accounts" in resp.json()

    monkeypatch.setattr(xero_client, "sync", lambda entity_id: False)
    resp = post(client, auth, "/xero-refresh", {"entity_id": str(entity.id)})
    assert resp.status_code == 502
    assert resp.json()["error"] == "Xero didn't send your accounts just now. Try Refresh again in a moment."


@pytest.mark.django_db
def test_refresh_needs_a_connection(client, auth, entity):
    resp = post(client, auth, "/xero-refresh", {"entity_id": str(entity.id)})
    assert resp.status_code == 409


# ---- a refusal says why in the log (production, 2026-11-26: three 400s, no telling which) -----------


def _refusals(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("refused ")]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "kind, pick, sentence",
    [
        ("expense", [], "Tick at least one expense account to continue."),
        ("sales", ["rent"], "Pick only sales accounts here. Reload the list and try again."),
        ("bank", ["bank"], "Choose which accounts to tick: sales or expense."),
    ],
)
def test_each_refused_tick_logs_its_sentence_once(client, auth, xero_company, caplog, kind, pick, sentence):
    entity, rows = xero_company
    with caplog.at_level("INFO", logger="minty-onboarding"):
        resp = _ticks(client, auth, entity, kind, [rows[p].id for p in pick])
    assert resp.status_code == 400
    assert _refusals(caplog) == [f"refused 400: {sentence}"]


@pytest.mark.django_db
def test_a_saved_tick_logs_no_refusal(client, auth, xero_company, caplog):
    entity, rows = xero_company
    with caplog.at_level("INFO", logger="minty-onboarding"):
        assert _ticks(client, auth, entity, "sales", [rows["sales"].id]).status_code == 200
    assert _refusals(caplog) == []


@pytest.mark.django_db
def test_ticks_not_offered_log_why_each_one_was_refused(client, auth, xero_company, user, countries, caplog):
    """The "Reload the list" refusal names its cause: ids a sync removed or another company's
    (not_found), a system account, an archived one, one of the other kind, one with no Xero id."""
    entity, rows = xero_company
    system = _xero(entity, "260", "Cash Sales", "revenue", "REVENUE")
    AccountInfo.objects.filter(id=system.id).update(is_system=True)
    archived = _xero(entity, "270", "Old Sales", "revenue", "REVENUE")
    AccountInfo.objects.filter(id=archived.id).update(record_status="archived")
    mine = AccountInfo.objects.create(
        entity_id=entity.id, name="Tips", code="250", xero_code="250", account_type="revenue",
        type="REVENUE", status="ACTIVE", source="user",
    )
    other = Entity.objects.create(id=str(uuid.uuid4()), name="Other Co", country_code="HK", status="onboarding")
    theirs = _xero(other, "280", "Their Sales", "revenue", "REVENUE")
    sent = [rows["sales"].id, system.id, archived.id, mine.id, theirs.id, rows["rent"].id, uuid.uuid4()]
    with caplog.at_level("INFO", logger="minty-onboarding"):
        resp = _ticks(client, auth, entity, "sales", sent)
    assert resp.status_code == 400
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("xero ticks refused")]
    refused = sorted(str(i) for i in sent[1:])
    assert lines == [
        f"xero ticks refused: entity={entity.id} kind=sales sent=7 offered=2 "
        "reasons={'archived': 1, 'no_xero_id': 1, 'not_found': 2, 'other_kind': 1, 'system': 1} "
        f"ids={refused[:5]}"
    ], lines


@pytest.mark.django_db
def test_a_step_five_refusal_logs_its_sentence_too(client, auth, entity, caplog):
    with caplog.at_level("INFO", logger="minty-onboarding"):
        resp = client.get("/api/onboarding/account-codes", {"entity_id": str(entity.id)}, **auth)
    assert resp.status_code == 409
    assert _refusals(caplog) == ["refused 409: Connect to Xero first to load account codes."]
