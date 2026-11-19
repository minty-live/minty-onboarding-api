"""A company's accounts and contacts in the hub's Settings (owner, 2026-11-12): the internal door
accounts-api calls, and ``onboarding/services/chart.py`` behind it - add, edit, delete, with or
without Xero; only what the company made; nothing changes when Xero says no."""

import json
import uuid

import pytest
from minty_db.models import (
    AccountInfo,
    Bill,
    ReportExpense,
    UserEntity,
    XeroContact,
    XeroContactSync,
)
from minty_shared import tokens

from core import xero_client
from onboarding.services import chart

BASE = "/api/internal/chart"
ORG = "11111111-2222-3333-4444-555555555555"


def assertion(user, entity, scope="company-chart"):
    token = tokens.service_assertion("test-secret-for-the-chart-endpoints", scope, entity.id, user_id=str(user.id))
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _secret(settings):
    settings.SECRET_KEY = "test-secret-for-the-chart-endpoints"


def call(client, user, entity, method, path, body=None, scope="company-chart"):
    kwargs = {"content_type": "application/json", "data": json.dumps(body)} if body is not None else {}
    return getattr(client, method)(BASE + path, **kwargs, **assertion(user, entity, scope))


class FakeXero:
    """Records each write; answers with made ids, or refuses with Xero's message."""

    def __init__(self):
        self.sent, self.refuse = [], None
        self.found: list[dict] = []  # Xero's answer to a contact name search
        self.searched: list[dict] = []

    def get(self, token, org, path, params):
        assert path == "Contacts", path
        self.searched.append(params)
        return {"Contacts": self.found}

    def write(self, token, org, method, path, body):
        self.sent.append((method, path, body))
        if self.refuse:
            raise xero_client.XeroRefused(self.refuse)
        if path == "Accounts" and method == "PUT":
            return {"Accounts": [{"AccountID": "x-acc-1", **body}]}
        if path == "Contacts" and not body["Contacts"][0].get("ContactID"):
            return {"Contacts": [{"ContactID": "x-con-1", "Name": body["Contacts"][0]["Name"]}]}
        return {}


@pytest.fixture
def xero(monkeypatch):
    fake = FakeXero()
    monkeypatch.setattr(xero_client, "write", fake.write)
    monkeypatch.setattr(xero_client, "get", fake.get)
    monkeypatch.setattr(chart, "access_token_for", lambda entity_id: "token")
    return fake


@pytest.fixture
def live(entity):
    entity.xero_org_id, entity.status = ORG, "connected"
    entity.save(update_fields=["xero_org_id", "status"])
    return entity


def account(entity, name="Rent", code="469", source="user", xero_id=None, **extra):
    return AccountInfo.objects.create(entity_id=entity.id, name=name, code=code, xero_code=code,
                                      account_type="expense", type="EXPENSE", status="ACTIVE", source=source,
                                      xero_account_id=xero_id, **extra)


def contact(entity, name="Kee Wah", source="user", xero_id=None):
    return XeroContactSync.objects.create(entity_id=entity.id, name=name, source=source, xero_contact_id=xero_id,
                                          xero_org_id=ORG if xero_id else None)


# ---- the door -------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_only_a_minty_backends_assertion_for_this_scope_opens_it(client, user, entity):
    assert client.get(BASE + "/accounts").status_code == 401
    assert call(client, user, entity, "get", "/accounts", scope="xero-sync").status_code == 401
    assert call(client, user, entity, "get", "/accounts").status_code == 200


@pytest.mark.django_db
def test_the_persons_rights_are_checked_again(client, user, entity):
    UserEntity.objects.filter(user_id=user.id, entity_id=entity.id).update(role="cashier")
    viewed = call(client, user, entity, "get", "/accounts")  # a cashier views, and is told what not to do
    assert viewed.status_code == 200
    assert viewed.json()["permissions"] == {"add": False, "edit": False, "delete": False}
    resp = call(client, user, entity, "post", "/accounts", {"code": "470", "name": "Fuel", "type": "expense"})
    assert resp.status_code == 403


# ---- accounts ------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_chart_reads_what_may_be_done_and_the_next_codes(client, user, entity):
    account(entity, "Rent", "469")
    account(entity, "Wages", "477", source="template")
    account(entity, "Hidden", "090", is_system=True)
    body = call(client, user, entity, "get", "/accounts").json()
    rows = {a["name"]: a for a in body["accounts"]}
    assert set(rows) == {"Rent", "Wages"}  # the system row stays hidden
    # a copy of Minty's is the company's too (owner, 2026-11-12); the source still says where it came from
    assert rows["Rent"]["own"] is True and rows["Wages"]["own"] is True
    assert rows["Wages"]["source"] == "template"
    assert body["next_codes"]["expense"] == "478" and body["xero_live"] is False
    assert {"value": "expense", "label": "Expense"} in body["types"]


@pytest.mark.django_db
def test_without_xero_an_account_is_the_companys_own(client, user, entity):
    resp = call(client, user, entity, "post", "/accounts", {"code": "470", "name": "Fuel", "type": "expense"})
    assert resp.status_code == 201, resp.json()
    made = AccountInfo.objects.get(entity_id=entity.id, name="Fuel")
    assert (made.source, made.xero_account_id, made.code) == ("user", None, "470")


@pytest.mark.django_db
def test_a_live_companys_new_account_stays_in_minty(client, user, live, xero):
    """Minty never creates an account in Xero (owner's decisions doc A1, 2026-11-19): it is mapped on
    the matching screen."""
    resp = call(client, user, live, "post", "/accounts",
                {"code": "470", "name": "Fuel", "type": "expense", "description": "Vans"})
    assert resp.status_code == 201
    assert xero.sent == []
    assert AccountInfo.objects.get(entity_id=live.id, name="Fuel").xero_account_id is None


@pytest.mark.django_db
def test_a_taken_code_or_a_bad_field_is_refused(client, user, entity):
    account(entity, "Rent", "469")
    taken = call(client, user, entity, "post", "/accounts", {"code": "469", "name": "Fuel", "type": "expense"})
    assert taken.status_code == 409 and taken.json()["fields"]["code"].startswith("The code 469 is already used")
    bad = call(client, user, entity, "post", "/accounts", {"code": "4 7", "name": "Fuel", "type": "expense"})
    assert bad.status_code == 422 and "letters, numbers" in bad.json()["fields"]["code"]


@pytest.mark.django_db
def test_a_copy_of_mintys_account_is_the_companys_to_edit_and_delete(client, user, entity):
    """Owner, 2026-11-12: the company's copies of Minty's accounts are its own; only the hidden
    system rows stay locked."""
    wages = account(entity, "Wages", "477", source="template")
    edit = call(client, user, entity, "patch", f"/accounts/{wages.id}",
                {"code": "477", "name": "Pay", "type": "expense"})
    assert edit.status_code == 200 and AccountInfo.objects.get(id=wages.id).name == "Pay"
    assert call(client, user, entity, "delete", f"/accounts/{wages.id}").status_code == 200
    hidden = account(entity, "Petty Cash", "090", source="template", is_system=True)
    assert call(client, user, entity, "delete", f"/accounts/{hidden.id}").status_code == 404


@pytest.mark.django_db
def test_an_account_linked_to_xero_is_changed_in_xero_only(client, user, live, xero):
    """The user's rule (2026-11-19, with doc A1): accounts flow from Xero to Minty - one linked to Xero
    can't be edited or deleted in Minty; only unlinked ones can."""
    rent = account(live, "Rent", "469", source="xero", xero_id="x-rent")
    said = "This account comes from Xero. Change it in Xero, and Minty follows."
    edit = call(client, user, live, "patch", f"/accounts/{rent.id}", {"code": "469", "name": "Rent HK", "type": "expense"})
    assert (edit.status_code, edit.json()["error"]) == (409, said)
    gone = call(client, user, live, "delete", f"/accounts/{rent.id}")
    assert (gone.status_code, gone.json()["error"]) == (409, said)
    assert xero.sent == []
    rent.refresh_from_db()
    assert (rent.name, rent.record_status) == ("Rent", "active")


@pytest.mark.django_db
def test_delete_removes_an_unused_account_and_refuses_a_used_one(client, user, live, xero):
    spare = account(live, "Spare", "471")
    gone = call(client, user, live, "delete", f"/accounts/{spare.id}")
    assert gone.json() == {"message": "Spare deleted."}
    assert not AccountInfo.objects.filter(id=spare.id).exists()

    rent = account(live, "Rent", "469")
    Bill.objects.create(entity_id=live.id, reference="B-1")
    from minty_db.models import BillLine

    BillLine.objects.create(bill=Bill.objects.get(reference="B-1"), account_id=rent.id, account_code="469")
    kept = call(client, user, live, "delete", f"/accounts/{rent.id}")
    assert kept.status_code == 409
    assert kept.json()["error"] == "Rent is already used in your records or Petty Cash Settings, so it can't be deleted."
    assert xero.sent == []
    assert AccountInfo.objects.get(id=rent.id).record_status == "active"
    names = [a["name"] for a in call(client, user, live, "get", "/accounts").json()["accounts"]]
    assert "Rent" in names


@pytest.mark.django_db
def test_an_account_gone_from_xero_says_so_in_the_list(client, user, live):
    """Owner's decisions doc A3: a used account whose Xero account was archived or removed (the sync
    leaves it ARCHIVED and active in Minty) is marked, for Settings to ask the person to map it."""
    from minty_db.models import Report

    rent = account(live, "Rent", "469", source="xero", xero_id="x-rent")
    report = Report.objects.create(entity_id=live.id, transaction_date="2026-11-01")
    ReportExpense.objects.create(report_id=report.id, account_id=rent.id, amount=1)
    unused = account(live, "Old", "468", source="xero", xero_id="x-old")
    # as the sync and the trigger leave them (Postgres archives the record with the status)
    AccountInfo.objects.filter(id__in=[rent.id, unused.id]).update(status="ARCHIVED", record_status="archived")
    account(live, "Fuel", "470", source="xero", xero_id="x-fuel")
    account(live, "Own", "471")
    rows = {a["name"]: a["gone_from_xero"] for a in call(client, user, live, "get", "/accounts").json()["accounts"]}
    assert rows == {"Rent": True, "Fuel": False, "Own": False}, "an unused one isn't asked about"


@pytest.mark.django_db
def test_used_means_a_line_by_code_a_daily_closing_slot_or_an_account_combined_into_it(client, user, entity):
    from minty_db.models import BillLine, EntityPettycashSettings

    by_code = account(entity, "Rent", "469")
    bill = Bill.objects.create(entity_id=entity.id, reference="B-1")
    BillLine.objects.create(bill=bill, account_code="469")  # saved before bill_line.account_id
    slot = account(entity, "Over and short", "480")
    EntityPettycashSettings.objects.create(entity_id=entity.id, discrepancy_account_id=slot.id)
    survivor = account(entity, "Fuel", "470")
    diesel = account(entity, "Diesel", "472")
    AccountInfo.objects.filter(id=diesel.id).update(record_status="archived", status="ARCHIVED",
                                                    merged_into_id=survivor.id)

    read = {a["name"]: a["used"] for a in call(client, user, entity, "get", "/accounts").json()["accounts"]}
    assert read == {"Rent": True, "Over and short": True, "Fuel": True}
    for row in (by_code, slot, survivor):
        assert call(client, user, entity, "delete", f"/accounts/{row.id}").status_code == 409
        assert AccountInfo.objects.filter(id=row.id, record_status="active").exists()


# ---- contacts ------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_without_xero_a_contact_is_the_companys_own_and_a_taken_name_is_refused(client, user, entity):
    resp = call(client, user, entity, "post", "/contacts", {"name": "  Kee   Wah "})
    assert resp.status_code == 201 and resp.json()["contact"]["name"] == "Kee Wah"
    made = XeroContactSync.objects.get(entity_id=entity.id, name="Kee Wah")
    assert (made.source, made.xero_contact_id) == ("user", None)
    again = call(client, user, entity, "post", "/contacts", {"name": "kee wah"})
    assert again.status_code == 409
    assert again.json()["error"] == "Kee Wah is already in your contacts. Use that one, or choose another name."


@pytest.mark.django_db
def test_a_live_companys_contact_links_to_xeros_of_that_name_else_is_made_there(client, user, live, xero):
    XeroContact.objects.create(entity_id=live.id, xero_org_id=ORG, xero_guid="x-sam", name="Sam's Fish",
                               status="ACTIVE")
    linked = call(client, user, live, "post", "/contacts", {"name": "SAM'S FISH"})
    assert linked.status_code == 201 and xero.sent == []  # no duplicate in Xero
    assert XeroContactSync.objects.get(entity_id=live.id, name="SAM'S FISH").xero_contact_id == "x-sam"
    assert linked.json()["contact"]["xero_id"] == "x-sam"

    call(client, user, live, "post", "/contacts", {"name": "Kee Wah"})
    assert [p["searchTerm"] for p in xero.searched] == ["Kee Wah"]  # Xero searched first (C5)
    assert xero.sent == [("POST", "Contacts", {"Contacts": [{"Name": "Kee Wah", "IsCustomer": False,
                                                             "IsSupplier": True}]})]
    assert XeroContactSync.objects.get(entity_id=live.id, name="Kee Wah").xero_contact_id == "x-con-1"


@pytest.mark.django_db
def test_a_live_company_links_the_contact_xero_has_of_that_name_never_a_twin(client, user, live, xero):
    """C5 (owner's decisions doc, 2026-11-19): a contact Minty hasn't stored yet is found by a live
    search; only the exact name (case and extra spaces ignored) counts."""
    xero.found = [{"ContactID": "x-kee-hk", "Name": "Kee Wah Hong Kong", "ContactStatus": "ACTIVE"},
                  {"ContactID": "x-kee", "Name": "KEE  WAH", "ContactStatus": "ACTIVE"}]
    resp = call(client, user, live, "post", "/contacts", {"name": "Kee Wah"})
    assert resp.status_code == 201 and resp.json()["contact"]["xero_id"] == "x-kee"
    assert xero.sent == []


@pytest.mark.django_db
def test_xero_not_answering_the_search_adds_nothing(client, user, live, xero, monkeypatch):
    def down(*_a):
        raise xero_client.XeroUnanswered

    monkeypatch.setattr(xero_client, "get", down)
    resp = call(client, user, live, "post", "/contacts", {"name": "Kee Wah"})
    assert resp.status_code == 502 and xero.sent == []
    assert not XeroContactSync.objects.filter(entity_id=live.id, name="Kee Wah").exists()


@pytest.mark.django_db
def test_a_contact_linked_to_xero_is_changed_in_xero_only_and_minty_ones_here(client, user, live, xero):
    """The user's rule (2026-11-19, as for accounts): a contact linked to Xero can't be renamed or
    deleted in Minty; one made in Minty, not in Xero yet, can - with no Xero call."""
    kee = contact(live, "Kee Wah", source="xero", xero_id="x-kee")
    said = "This contact comes from Xero. Change it in Xero, and Minty follows."
    renamed = call(client, user, live, "patch", f"/contacts/{kee.id}", {"name": "Kee Wah Bakery"})
    assert (renamed.status_code, renamed.json()["error"]) == (409, said)
    gone = call(client, user, live, "delete", f"/contacts/{kee.id}")
    assert (gone.status_code, gone.json()["error"]) == (409, said)
    assert XeroContactSync.objects.get(id=kee.id).name == "Kee Wah"

    own = contact(live, "Sam's Fish")
    assert call(client, user, live, "patch", f"/contacts/{own.id}", {"name": "Sam's Seafood"}).status_code == 200
    assert call(client, user, live, "delete", f"/contacts/{own.id}").json() == {"message": "Sam's Seafood deleted."}
    template = contact(live, "Walk-in Customer", source="template")
    assert call(client, user, live, "delete", f"/contacts/{template.id}").status_code == 200  # the company's copy
    assert xero.sent == []


@pytest.mark.django_db
def test_a_used_contact_is_refused_and_stays(client, user, live, xero):
    from minty_db.models import EntityPettycashSettings, Report

    kee = contact(live, "Kee Wah")  # made in Minty: linked ones aren't deleted here at all
    report = Report.objects.create(entity_id=live.id, transaction_date="2026-11-01")
    ReportExpense.objects.create(report_id=report.id, contact_id=kee.id, amount=1)
    walk_in = contact(live, "Walk-in Customer")
    EntityPettycashSettings.objects.create(entity_id=live.id, cash_sale_contact_id=walk_in.id)
    survivor = contact(live, "Sam's Fish")
    old = contact(live, "Sams Fish")
    XeroContactSync.objects.filter(id=old.id).update(record_status="archived", merged_into_id=survivor.id)

    read = {c["name"]: c["used"] for c in call(client, user, live, "get", "/contacts").json()["contacts"]}
    assert read == {"Kee Wah": True, "Walk-in Customer": True, "Sam's Fish": True}
    refused = call(client, user, live, "delete", f"/contacts/{kee.id}")
    assert refused.status_code == 409
    assert refused.json()["error"] == (
        "Kee Wah is already used in your records or Petty Cash Settings, so it can't be deleted.")
    for row in (walk_in, survivor):
        assert call(client, user, live, "delete", f"/contacts/{row.id}").status_code == 409
    assert xero.sent == []  # Xero is never told
    assert XeroContactSync.objects.filter(entity_id=live.id, record_status="active").count() == 3


@pytest.mark.django_db
def test_an_unknown_id_is_not_found(client, user, entity):
    for path in (f"/accounts/{uuid.uuid4()}", "/accounts/nope", f"/contacts/{uuid.uuid4()}"):
        assert call(client, user, entity, "delete", path).status_code == 404


# ---- Minty's accounts (owner, 2026-11-12: "I should be able to add existing ones from Minty") ----


def template(code, name, kind="expense", **extra):
    from minty_db.models import AccountTemplate

    return AccountTemplate.objects.create(code=code, name=name, type=kind, **extra)


@pytest.mark.django_db
def test_the_read_offers_mintys_accounts_the_company_has_not_got(client, user, entity):
    account(entity, "Rent", "469", source="template")
    template("469", "Rent")
    fuel = template("470", "Fuel", description="Vans and cars")
    template("090", "Petty Cash", kind="bank", pettycash_role="petty_cash")  # a hidden system row

    offered = call(client, user, entity, "get", "/accounts").json()["minty_accounts"]
    assert offered == [{"id": str(fuel.id), "code": "470", "name": "Fuel", "type": "expense",
                        "type_label": "Expense", "description": "Vans and cars"}]


@pytest.mark.django_db
def test_a_minty_account_is_added_as_mintys_copy(client, user, entity):
    fuel = template("470", "Fuel")
    resp = call(client, user, entity, "post", "/accounts", {"template_id": str(fuel.id)})
    assert resp.status_code == 201, resp.json()
    made = AccountInfo.objects.get(entity_id=entity.id, name="Fuel")
    assert (made.code, made.source, made.account_type) == ("470", "template", "expense")
    assert resp.json()["account"]["own"] is True  # the company's copy, its own to change (owner, 2026-11-12)
    again = call(client, user, entity, "post", "/accounts", {"template_id": str(fuel.id)})
    assert again.status_code == 404  # no longer offered


@pytest.mark.django_db
def test_a_minty_account_whose_code_is_taken_is_refused_saying_so(client, user, entity):
    account(entity, "Diesel", "470")
    fuel = template("470", "Fuel")
    resp = call(client, user, entity, "post", "/accounts", {"template_id": str(fuel.id)})
    assert resp.status_code == 409
    assert resp.json()["error"] == "The code 470 is already used by Diesel. Choose another code."


@pytest.mark.django_db
def test_a_live_company_gets_mintys_account_in_minty_only(client, user, live, xero):
    fuel = template("470", "Fuel")
    assert call(client, user, live, "post", "/accounts", {"template_id": str(fuel.id)}).status_code == 201
    assert xero.sent == []  # doc A1: never created in Xero
    assert AccountInfo.objects.get(entity_id=live.id, name="Fuel").xero_account_id is None


# ---- deleted, then added again: brought back, never a twin (owner, 2026-11-12) -------------------


def xero_contact_copy(entity, guid, name, status="ACTIVE"):
    return XeroContact.objects.create(entity_id=entity.id, xero_org_id=ORG, xero_guid=guid, name=name,
                                      status=status)


@pytest.mark.django_db
def test_a_deleted_unused_contact_added_again_is_linked_as_it_stands_never_restored(client, user, live, xero):
    """Minty never restores anything in the customer's Xero (doc C4): Xero's archived contact of the
    name is linked again as it is; publish asks to restore it there or map it."""
    kee = contact(live, "Kee Wah", source="xero", xero_id="x-kee")
    xero_contact_copy(live, "x-kee", "Kee Wah", status="ARCHIVED")
    XeroContactSync.objects.filter(id=kee.id).delete()  # deleted before 2026-11-19, archived in Xero then
    sent = len(xero.sent)

    again = call(client, user, live, "post", "/contacts", {"name": "kee wah"})
    assert again.status_code == 201 and again.json()["message"] == "Kee Wah is back."
    assert len(xero.sent) == sent  # nothing restored, nothing made
    assert XeroContactSync.objects.get(entity_id=live.id, record_status="active").xero_contact_id == "x-kee"
    assert XeroContact.objects.get(entity_id=live.id, xero_guid="x-kee").status == "ARCHIVED"


@pytest.mark.django_db
def test_an_archived_used_contact_added_again_is_the_same_row_back(client, user, live, xero):
    from minty_db.models import Report

    kee = contact(live, "Kee Wah", source="xero", xero_id="x-kee")
    report = Report.objects.create(entity_id=live.id, transaction_date="2026-11-01")
    ReportExpense.objects.create(report_id=report.id, contact_id=kee.id, amount=1)
    # archived by the old rule (before 2026-11-18 a used contact's delete archived it)
    XeroContactSync.objects.filter(id=kee.id).update(record_status="archived")

    again = call(client, user, live, "post", "/contacts", {"name": "Kee Wah"})
    assert again.status_code == 201 and again.json()["contact"]["id"] == str(kee.id)
    kee.refresh_from_db()
    assert kee.record_status == "active"
    assert xero.sent == []  # never restored in Xero (doc C4)


@pytest.mark.django_db
def test_offline_both_kinds_of_contact_come_back(client, user, entity):
    from minty_db.models import Report

    report = Report.objects.create(entity_id=entity.id, transaction_date="2026-11-01")
    own = contact(entity, "Kee Wah")
    linked = contact(entity, "Sam's Fish", source="xero", xero_id="x-sam")
    for row in (own, linked):
        ReportExpense.objects.create(report_id=report.id, contact_id=row.id, amount=1)
        XeroContactSync.objects.filter(id=row.id).update(record_status="archived")

    back = call(client, user, entity, "post", "/contacts", {"name": "Kee Wah"})
    assert back.status_code == 201 and back.json()["contact"]["id"] == str(own.id)
    again = call(client, user, entity, "post", "/contacts", {"name": "Sam's Fish"})
    assert again.status_code == 201 and again.json()["contact"]["id"] == str(linked.id)


@pytest.mark.django_db
def test_an_archived_used_account_added_again_is_the_same_row_back(client, user, live, xero):
    rent = account(live, "Rent", "469")
    Bill.objects.create(entity_id=live.id, reference="B-1")
    from minty_db.models import AccountModule, BillLine

    BillLine.objects.create(bill=Bill.objects.get(reference="B-1"), account_id=rent.id, account_code="469")
    # archived by the old rule (before 2026-11-18 a used account's delete archived it)
    AccountModule.objects.create(account_id=rent.id, entity_id=live.id, module_code="PETTY_CASH", is_active=False)
    AccountInfo.objects.filter(id=rent.id).update(record_status="archived", status="ARCHIVED")

    again = call(client, user, live, "post", "/accounts", {"code": "469", "name": "rent", "type": "expense"})
    assert again.status_code == 201 and again.json()["account"]["id"] == str(rent.id)
    assert again.json()["message"] == "Rent is back."
    assert xero.sent == []  # never restored in Xero (doc C4)
    rent.refresh_from_db()
    assert (rent.record_status, rent.status) == ("active", "ACTIVE")
    assert AccountModule.objects.get(account_id=rent.id, module_code="PETTY_CASH").is_active is True


@pytest.mark.django_db
def test_xeros_archived_account_of_that_code_is_never_restored_a_new_minty_one_is_made(client, user, live, xero):
    from minty_db.models import XeroAccount

    XeroAccount.objects.create(entity_id=live.id, xero_org_id=ORG, xero_guid="x-spare", code="471", name="Spare",
                               xero_type="EXPENSE", status="ARCHIVED")
    again = call(client, user, live, "post", "/accounts", {"code": "471", "name": "Spare", "type": "expense"})
    assert again.status_code == 201, again.json()
    assert xero.sent == []
    assert AccountInfo.objects.get(entity_id=live.id, name="Spare").xero_account_id is None
    assert XeroAccount.objects.get(xero_guid="x-spare").status == "ARCHIVED"
