"""Petty Cash's account codes in onboarding (owner, 2026-11-18): a company keeping its books in Minty
picks which of step 4's expense accounts appear when someone adds a petty cash expense - Petty Cash
Settings' own Account Code step, as Payment Request Settings picks its codes. The ticks are Petty
Cash's ``account_module`` rows and the old ``entity_account_xero.is_active`` minty-petty-cash-api
reads. A Xero company chose them in X3 (its ticks are Petty Cash's list there), so it has none here.
"""

import pytest
from minty_db.models import AccountModule, Entity, EntityAccountXero

from tests.test_steps_follow_accounting import BASE, _own, post

CODES = "/api/onboarding/petty-cash-codes"


def _read(client, auth, entity):
    return client.get(CODES, {"entity_id": str(entity.id)}, **auth)


def _save(client, auth, entity, codes):
    return post(client, auth, CODES, {"entity_id": str(entity.id), "selected_codes": codes})


@pytest.fixture
def minty(client, auth, entity):
    assert post(client, auth, BASE + "/mode", {"entity_id": str(entity.id), "mode": "minty"}).status_code == 200
    Entity.objects.filter(id=entity.id).update(business_type="retail", uses_sales_cash=True)
    entity.refresh_from_db()
    return entity


def _petty(account):
    module = AccountModule.objects.filter(account_id=account.id, module_code="PETTY_CASH").first()
    old = EntityAccountXero.objects.filter(account_id=account.id).first()
    return (module.is_active if module else None, old.is_active if old else None)


@pytest.mark.django_db
def test_the_list_is_step_4s_expense_accounts_ticked_as_the_sheet_says(client, auth, minty):
    _own(minty, "5000", "Inventory Purchases", "direct_costs", modules=(True, True))
    _own(minty, "6003", "Rent", modules=(False, True))
    _own(minty, "4000", "Cash Sales", "sales")  # a sales account is no expense code

    resp = _read(client, auth, minty)
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert [(c["code"], c["name"]) for c in body["codes"]] == [("5000", "Inventory Purchases"), ("6003", "Rent")]
    assert body["selected_codes"] == ["5000"]


@pytest.mark.django_db
def test_a_save_ticks_exactly_those_in_both_tables(client, auth, minty):
    stock = _own(minty, "5000", "Inventory Purchases", "direct_costs")
    rent = _own(minty, "6003", "Rent", modules=(False, True))

    resp = _save(client, auth, minty, ["6003"])
    assert resp.status_code == 200, resp.content
    assert resp.json()["selected_codes"] == ["6003"]
    assert _petty(rent) == (True, True)
    assert _petty(stock) == (False, False)
    assert _read(client, auth, minty).json()["selected_codes"] == ["6003"]


@pytest.mark.django_db
def test_none_ticked_or_a_code_not_listed_is_refused(client, auth, minty):
    _own(minty, "5000", "Inventory Purchases", "direct_costs")
    resp = _save(client, auth, minty, [])
    assert (resp.status_code, resp.json()["error"]) == (400, "Pick at least one account code.")
    assert _save(client, auth, minty, ["9999"]).status_code == 400
    assert post(client, auth, CODES, {"entity_id": str(minty.id), "selected_codes": "5000"}).status_code == 400


@pytest.mark.django_db
def test_a_xero_company_chose_them_in_x3(client, auth, entity):
    Entity.objects.filter(id=entity.id).update(setup_mode="xero", xero_org_id="org-1")
    resp = _read(client, auth, entity)
    assert resp.status_code == 409
    assert "Xero" in resp.json()["error"]
