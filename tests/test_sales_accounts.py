"""Accounting Setup's sales accounts screen (owner, 2026-11-17; Figma 464:73 row 1): three ticks -
Sales - Cash, Sales - Electronic Payment, Sales - Delivery - that decide which sales methods Petty
Cash Settings asks for. Screen only: no account is made or removed (the business type's sales
accounts still come from the set, 4.2). Cash is 4.2's cash answer (``uses_sales_cash``); Electronic
and Delivery are ``entities.sells_electronic`` / ``sells_delivery`` (minty-db v0.11), NULL until
chosen - the business type picks them then: Restaurant & Food (and no type) both, a retail shop, a
service or a general business Electronic only. Unticking a list takes its methods out.
"""

import uuid

import pytest
from minty_db.models import Entity, EntitySaleSetting, SaleInfo, User, UserEntity

from tests.conftest import make_token

BASE = "/api/onboarding/accounting"
SALES = "/api/onboarding/sales-methods"


def post(client, auth, path, payload):
    return client.post(BASE + path, payload, content_type="application/json", **auth)


def _ticks(client, auth, entity, cash, electronic, delivery):
    return post(client, auth, "/sales-accounts", {"entity_id": str(entity.id), "cash": cash,
                                                   "electronic": electronic, "delivery": delivery})


def _answers(client, auth, entity):
    resp = client.get(BASE, {"entity_id": str(entity.id)}, **auth)
    assert resp.status_code == 200, resp.content
    return resp.json()


@pytest.fixture
def minty(client, auth, entity):
    assert post(client, auth, "/mode", {"entity_id": str(entity.id), "mode": "minty"}).status_code == 200
    cash = SaleInfo.objects.filter(value_name="cash_sales").first() or SaleInfo.objects.create(
        sale_name="Cash", value_name="cash_sales", type="other", display_order=0
    )
    EntitySaleSetting.objects.create(entity_id=entity.id, sale=cash, is_active=True, display_order=0)
    return entity


@pytest.mark.django_db
@pytest.mark.parametrize("business, cash, expected", [
    ("fnb", True, (True, True, True)),
    (None, True, (True, True, True)),
    ("retail", True, (True, True, False)),
    ("service", False, (False, True, False)),
    ("general", True, (True, True, False)),
])
def test_unchosen_ticks_follow_the_business_type_and_the_cash_answer(client, auth, minty, business, cash, expected):
    Entity.objects.filter(id=minty.id).update(business_type=business, uses_sales_cash=cash)
    got = _answers(client, auth, minty)
    assert (got["sells_cash"], got["sells_electronic"], got["sells_delivery"]) == expected
    assert got["sales_chosen"] is False


@pytest.mark.django_db
def test_saving_the_ticks_keeps_them_and_cash_is_the_cash_answer(client, auth, minty):
    Entity.objects.filter(id=minty.id).update(business_type="fnb", uses_sales_cash=True)
    resp = _ticks(client, auth, minty, False, True, False)
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert (body["sells_cash"], body["sells_electronic"], body["sells_delivery"]) == (False, True, False)
    minty.refresh_from_db()
    assert (minty.uses_sales_cash, minty.sells_electronic, minty.sells_delivery) == (False, True, False)
    assert _answers(client, auth, minty)["sales_chosen"] is True
    cash = EntitySaleSetting.objects.get(entity_id=minty.id, sale__value_name="cash_sales")
    assert cash.is_active is False, "unticking Sales - Cash is answering 'We bank all sales'"


@pytest.mark.django_db
def test_none_ticked_is_refused_with_the_screens_sentence(client, auth, minty):
    resp = _ticks(client, auth, minty, False, False, False)
    assert resp.status_code == 400
    assert resp.json()["error"] == "No sales account is ticked yet. Tick at least one to continue."
    minty.refresh_from_db()
    assert minty.sells_electronic is None


@pytest.mark.django_db
@pytest.mark.parametrize("payload", [
    {"cash": True, "electronic": "yes", "delivery": False},
    {"cash": None, "electronic": True, "delivery": False},
    {"cash": True, "electronic": True},
])
def test_each_tick_must_be_true_or_false(client, auth, minty, payload):
    resp = post(client, auth, "/sales-accounts", {"entity_id": str(minty.id), **payload})
    assert resp.status_code == 400


@pytest.mark.django_db
def test_unticking_a_list_takes_its_methods_out_and_step_5_hides_it(client, auth, minty):
    Entity.objects.filter(id=minty.id).update(business_type="fnb", uses_sales_cash=True)
    resp = client.post(SALES, {"entity_id": str(minty.id), "electronic": ["Visa"], "delivery": ["Keeta"]},
                       content_type="application/json", **auth)
    assert resp.status_code == 200, resp.content

    assert _ticks(client, auth, minty, True, True, False).status_code == 200
    methods = client.get(SALES, {"entity_id": str(minty.id)}, **auth).json()
    assert methods["electronic"] == ["Visa"]
    assert methods["delivery"] == []
    assert (methods["sells_electronic"], methods["sells_delivery"]) == (True, False)


@pytest.mark.django_db
def test_a_cashier_cannot_change_the_ticks(client, minty):
    cashier = User.objects.create(id=str(uuid.uuid4()), email="c@example.com", username="c@example.com",
                                  password="x", system_role="normal", approved=True)
    UserEntity.objects.create(user_id=cashier.id, entity_id=minty.id, role="cashier", approved=True)
    headers = {"HTTP_AUTHORIZATION": f"Bearer {make_token(cashier.id)}"}
    resp = _ticks(client, headers, minty, True, True, True)
    assert resp.status_code == 403
    minty.refresh_from_db()
    assert minty.sells_electronic is None
