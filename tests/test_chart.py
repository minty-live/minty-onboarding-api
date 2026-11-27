"""A company's accounts and contacts in the hub's Settings (owner, 2026-11-12): the internal door
accounts-api calls, and ``onboarding/services/chart.py`` behind it - add, edit, delete, with or
without Xero; only what the company made; nothing changes when Xero says no."""

import json
import uuid

import pytest
from minty_db.models import (
    AccountInfo,
    AccountModule,
    Bill,
    BillLine,
    Entity,
    EntityAccountXero,
    EntityBillAccountXero,
    EntityPettycashSettings,
    Report,
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


@pytest.fixture
def disconnected(entity):
    """Holding its organisation, Xero off (a disconnect keeps it; matching runs disconnected too)."""
    entity.xero_org_id, entity.status = ORG, "disconnected"
    entity.save(update_fields=["xero_org_id", "status"])
    return entity


#: The user's rule (2026-11-23): while live on Xero, no account is added, changed or deleted in Minty.
ACCOUNTS_IN_XERO = ("This company is connected to Xero, so accounts are added and changed in Xero. "
                    "Make the change there, and Minty follows.")


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
def test_a_live_company_adds_no_account_in_minty(client, user, live, xero):
    """The user's rule (2026-11-23): while live on Xero an account is added in Xero, never in Minty -
    its own or one of Minty's. The read still says what the person's role may do."""
    fuel = template("471", "Diesel")
    for body in ({"code": "470", "name": "Fuel", "type": "expense", "description": "Vans"},
                 {"template_id": str(fuel.id)}):
        resp = call(client, user, live, "post", "/accounts", body)
        assert (resp.status_code, resp.json()) == (409, {"error": ACCOUNTS_IN_XERO})
    assert xero.sent == []
    assert not AccountInfo.objects.filter(entity_id=live.id).exists()
    read = call(client, user, live, "get", "/accounts").json()
    assert read["xero_live"] is True and read["permissions"]["add"] is True


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
def test_a_live_company_changes_and_deletes_no_account_in_minty(client, user, live, xero):
    """The user's rule (2026-11-23): while live on Xero, no account is edited or deleted in Minty -
    linked to Xero or not."""
    rent = account(live, "Rent", "469", source="xero", xero_id="x-rent")
    own = account(live, "Spare", "471")
    for row in (rent, own):
        edit = call(client, user, live, "patch", f"/accounts/{row.id}",
                    {"code": row.code, "name": "Renamed", "type": "expense"})
        assert (edit.status_code, edit.json()) == (409, {"error": ACCOUNTS_IN_XERO})
        gone = call(client, user, live, "delete", f"/accounts/{row.id}")
        assert (gone.status_code, gone.json()) == (409, {"error": ACCOUNTS_IN_XERO})
    assert xero.sent == []
    assert sorted(AccountInfo.objects.filter(entity_id=live.id, record_status="active")
                  .values_list("name", flat=True)) == ["Rent", "Spare"]


#: A matching is choosing the links: a row linked to Xero waits for it (the user, 2026-11-27).
MATCHING_OPEN = "The Xero matching is still open. Finish or close it in Settings, under Integration, then try again."


@pytest.mark.django_db
def test_xero_off_an_account_linked_to_xero_is_minty_s_and_keeps_its_link(client, user, disconnected):
    """Disconnected = a Minty-only company (the user, 2026-11-27; was "changed in Xero only" since
    2026-11-19): a linked account is edited and deleted here, its stored link kept for reconnecting."""
    from minty_db.models import EntityBillAccountXero

    rent = account(disconnected, "Rent", "469", source="xero", xero_id="x-rent")
    rent.xero_code = "X469"
    rent.save(update_fields=["xero_code"])
    bill_code = EntityBillAccountXero.objects.create(entity_id=disconnected.id, account_code="X469",
                                                     account_name="Rent", account_type="EXPENSE",
                                                     xero_account_id="x-rent")
    edit = call(client, user, disconnected, "patch", f"/accounts/{rent.id}",
                {"code": "470", "name": "Rent HK", "type": "expense"})
    assert edit.status_code == 200, edit.json()
    rent.refresh_from_db()
    # Minty's code and name change; the link and Xero's code stay for the matching at reconnect
    assert (rent.code, rent.name, rent.xero_account_id, rent.xero_code) == ("470", "Rent HK", "x-rent", "X469")
    assert rent.minty_changed_at is not None, "marked: the reconnect opens the matching for it"
    bills = EntityBillAccountXero.objects.filter(entity_id=disconnected.id)
    assert [(b.account_code, b.account_name, b.xero_account_id) for b in bills] == [("X469", "Rent HK", "x-rent")]

    gone = call(client, user, disconnected, "delete", f"/accounts/{rent.id}")
    assert gone.status_code == 200, gone.json()
    assert not AccountInfo.objects.filter(id=rent.id).exists()
    assert not EntityBillAccountXero.objects.filter(id=bill_code.id).exists(), "out of the bill picker too"

    own = account(disconnected, "Spare", "471")
    edit = call(client, user, disconnected, "patch", f"/accounts/{own.id}",
                {"code": "471", "name": "Spare parts", "type": "expense"})
    assert edit.status_code == 200
    assert call(client, user, disconnected, "post", "/accounts",
                {"code": "472", "name": "Fuel", "type": "expense"}).status_code == 201


@pytest.mark.django_db
def test_while_a_matching_is_open_a_linked_account_or_contact_waits(client, user, disconnected):
    """The matching is choosing each account's Xero account (the user, 2026-11-27): a linked row
    waits until it is finished or closed; Minty's own still change."""
    from minty_db.models import Entity

    Entity.objects.filter(id=disconnected.id).update(xero_matching="new_accounts")
    rent = account(disconnected, "Rent", "469", source="xero", xero_id="x-rent")
    kee = contact(disconnected, "Kee Wah", source="xero", xero_id="x-kee")
    for path in ("/accounts", "/contacts"):  # the page hides Edit and Delete on linked rows
        assert call(client, user, disconnected, "get", path).json()["matching_open"] is True
    answers = [
        call(client, user, disconnected, "patch", f"/accounts/{rent.id}",
             {"code": "469", "name": "Rent HK", "type": "expense"}),
        call(client, user, disconnected, "delete", f"/accounts/{rent.id}"),
        call(client, user, disconnected, "patch", f"/contacts/{kee.id}", {"name": "Kee Wah Bakery"}),
        call(client, user, disconnected, "delete", f"/contacts/{kee.id}"),
    ]
    assert [(a.status_code, a.json()["error"]) for a in answers] == [(409, MATCHING_OPEN)] * 4
    assert AccountInfo.objects.get(id=rent.id).name == "Rent"
    assert XeroContactSync.objects.get(id=kee.id).name == "Kee Wah"
    own = account(disconnected, "Spare", "471")
    assert call(client, user, disconnected, "patch", f"/accounts/{own.id}",
                {"code": "471", "name": "Spare parts", "type": "expense"}).status_code == 200


@pytest.mark.django_db
def test_xero_off_a_contact_linked_to_xero_is_renamed_and_deleted_here(client, user, disconnected):
    """Disconnected = Minty-only for contacts too (the user, 2026-11-27): the link is kept."""
    kee = contact(disconnected, "Kee Wah", source="xero", xero_id="x-kee")
    renamed = call(client, user, disconnected, "patch", f"/contacts/{kee.id}", {"name": "Kee Wah Bakery"})
    assert renamed.status_code == 200, renamed.json()
    kee.refresh_from_db()
    assert (kee.name, kee.xero_contact_id) == ("Kee Wah Bakery", "x-kee")
    assert kee.minty_changed_at is not None
    assert call(client, user, disconnected, "delete", f"/contacts/{kee.id}").json() == {
        "message": "Kee Wah Bakery deleted."}
    assert not XeroContactSync.objects.filter(id=kee.id).exists()


@pytest.mark.django_db
def test_only_a_change_with_xero_off_is_marked_for_the_reconnect(client, user, entity, disconnected):
    """``minty_changed_at`` (the user, 2026-11-27): set when a company holding an organisation changes a
    row with Xero off - added or edited - so the reconnect's matching asks about it."""
    added = call(client, user, disconnected, "post", "/accounts", {"code": "470", "name": "Fuel", "type": "expense"})
    assert AccountInfo.objects.get(id=added.json()["account"]["id"]).minty_changed_at is not None
    sam = call(client, user, disconnected, "post", "/contacts", {"name": "Sam's Fish"})
    assert XeroContactSync.objects.get(id=sam.json()["contact"]["id"]).minty_changed_at is not None


@pytest.mark.django_db
def test_a_company_that_never_had_xero_marks_nothing(client, user, entity):
    rent = account(entity, "Rent", "469")
    assert call(client, user, entity, "patch", f"/accounts/{rent.id}",
                {"code": "469", "name": "Rent HK", "type": "expense"}).status_code == 200
    kee = contact(entity, "Kee Wah")
    assert call(client, user, entity, "patch", f"/contacts/{kee.id}", {"name": "Kee Wah Bakery"}).status_code == 200
    assert AccountInfo.objects.get(id=rent.id).minty_changed_at is None
    assert XeroContactSync.objects.get(id=kee.id).minty_changed_at is None


@pytest.mark.django_db
def test_delete_removes_an_unused_account_and_refuses_a_used_one(client, user, entity):
    spare = account(entity, "Spare", "471")
    gone = call(client, user, entity, "delete", f"/accounts/{spare.id}")
    assert gone.json() == {"message": "Spare deleted."}
    assert not AccountInfo.objects.filter(id=spare.id).exists()

    rent = account(entity, "Rent", "469")
    Bill.objects.create(entity_id=entity.id, reference="B-1")
    from minty_db.models import BillLine

    BillLine.objects.create(bill=Bill.objects.get(reference="B-1"), account_id=rent.id, account_code="469")
    kept = call(client, user, entity, "delete", f"/accounts/{rent.id}")
    assert kept.status_code == 409
    # it says what uses it and offers Archive (the user, 2026-11-27)
    assert kept.json() == {"error": "Rent is used by 1 bill. Archive it instead.", "can_archive": True}
    assert AccountInfo.objects.get(id=rent.id).record_status == "active"
    names = [a["name"] for a in call(client, user, entity, "get", "/accounts").json()["accounts"]]
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
    assert refused.json()["error"] == "Kee Wah is used by 1 expense. Archive it instead."
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
def test_an_archived_used_account_added_again_is_the_same_row_back(client, user, entity):
    rent = account(entity, "Rent", "469")
    Bill.objects.create(entity_id=entity.id, reference="B-1")
    from minty_db.models import AccountModule, BillLine

    BillLine.objects.create(bill=Bill.objects.get(reference="B-1"), account_id=rent.id, account_code="469")
    # archived by the old rule (before 2026-11-18 a used account's delete archived it)
    AccountModule.objects.create(account_id=rent.id, entity_id=entity.id, module_code="PETTY_CASH", is_active=False)
    AccountInfo.objects.filter(id=rent.id).update(record_status="archived", status="ARCHIVED")

    again = call(client, user, entity, "post", "/accounts", {"code": "469", "name": "rent", "type": "expense"})
    assert again.status_code == 201 and again.json()["account"]["id"] == str(rent.id)
    assert again.json()["message"] == "Rent is back."
    rent.refresh_from_db()
    assert (rent.record_status, rent.status) == ("active", "ACTIVE")
    assert AccountModule.objects.get(account_id=rent.id, module_code="PETTY_CASH").is_active is True


@pytest.mark.django_db
def test_xeros_archived_account_of_that_code_is_never_restored_a_new_minty_one_is_made(client, user, disconnected):
    from minty_db.models import XeroAccount

    XeroAccount.objects.create(entity_id=disconnected.id, xero_org_id=ORG, xero_guid="x-spare", code="471",
                               name="Spare", xero_type="EXPENSE", status="ARCHIVED")
    again = call(client, user, disconnected, "post", "/accounts", {"code": "471", "name": "Spare", "type": "expense"})
    assert again.status_code == 201, again.json()
    assert AccountInfo.objects.get(entity_id=disconnected.id, name="Spare").xero_account_id is None
    assert XeroAccount.objects.get(xero_guid="x-spare").status == "ARCHIVED"


@pytest.mark.django_db
def test_each_account_carries_its_minty_code_and_its_xero_code(client, user, live):
    """The user, 2026-11-24 (Minty Code + Xero Code): Settings lists both. An account not linked to
    Xero has no Xero code, whatever its old copy column says."""
    AccountInfo.objects.create(entity_id=live.id, name="Rent", code="6100", xero_code="469", account_type="expense",
                               type="EXPENSE", status="ACTIVE", source="user", xero_account_id="x-rent")
    account(live, "Own", "6200")
    rows = {a["name"]: (a["code"], a["xero_code"])
            for a in call(client, user, live, "get", "/accounts").json()["accounts"]}
    assert rows == {"Rent": ("6100", "469"), "Own": ("6200", "")}

# ---- archive and restore (the user, 2026-11-27): an account or contact in use can't be deleted - the
# refusal says what uses it and offers Archive. Archived: out of every picker, still shown on old
# records, restorable. Only while not live on Xero; a linked row waits while a matching is open.


def _added(client, user, entity, name="Rent", code="469"):
    """An expense account added in Settings, with its Petty Cash tick, bill code and module rows."""
    resp = call(client, user, entity, "post", "/accounts", {"code": code, "name": name, "type": "expense"})
    assert resp.status_code == 201, resp.json()
    return AccountInfo.objects.get(id=resp.json()["account"]["id"])


def _expenses(entity, n, **on):
    report, _ = Report.objects.get_or_create(entity_id=entity.id, transaction_date="2026-11-01")
    for _ in range(n):
        ReportExpense.objects.create(report_id=report.id, amount=1, **on)


def _bills(entity, n, account=None, **on):
    for i in range(n):
        bill = Bill.objects.create(entity_id=entity.id, reference=f"B-{i}-{account.id if account else ''}", **on)
        if account is not None:
            BillLine.objects.create(bill=bill, account_id=account.id, account_code=account.code)
            BillLine.objects.create(bill=bill, account_id=account.id, account_code=account.code)


# ---- accounts -----------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_used_accounts_delete_says_what_uses_it_and_offers_archive(client, user, entity):
    rent = _added(client, user, entity)
    _expenses(entity, 2, account_id=rent.id)
    _bills(entity, 1, account=rent)  # one bill, two lines on it: one bill
    refused = call(client, user, entity, "delete", f"/accounts/{rent.id}")
    assert (refused.status_code, refused.json()) == (
        409, {"error": "Rent is used by 2 expenses and 1 bill. Archive it instead.", "can_archive": True})
    # the list says it too, so the page asks Archive instead before any delete is sent
    row = next(a for a in call(client, user, entity, "get", "/accounts").json()["accounts"] if a["name"] == "Rent")
    assert (row["used_note"], row["can_archive"]) == ("Rent is used by 2 expenses and 1 bill. Archive it instead.", True)

    fuel = _added(client, user, entity, "Fuel", "470")
    _expenses(entity, 1, account_id=fuel.id)
    assert call(client, user, entity, "delete", f"/accounts/{fuel.id}").json()["error"] == (
        "Fuel is used by 1 expense. Archive it instead.")


@pytest.mark.django_db
def test_an_account_daily_closing_uses_is_neither_deleted_nor_archived(client, user, entity):
    rent = _added(client, user, entity)
    EntityPettycashSettings.objects.create(entity_id=entity.id, director_account_id=rent.id)
    said = "Rent is used in Petty Cash Settings. Choose another account there first."
    row = next(a for a in call(client, user, entity, "get", "/accounts").json()["accounts"] if a["name"] == "Rent")
    assert (row["used_note"], row["can_archive"]) == (said, False)
    refused = call(client, user, entity, "delete", f"/accounts/{rent.id}")
    assert (refused.status_code, refused.json()) == (409, {"error": said})
    archived = call(client, user, entity, "post", f"/accounts/{rent.id}/archive", {})
    assert (archived.status_code, archived.json()) == (409, {"error": said})


@pytest.mark.django_db
def test_archive_takes_an_account_out_of_every_picker_and_restore_brings_it_back(client, user, entity):
    rent = _added(client, user, entity)
    _expenses(entity, 1, account_id=rent.id)
    done = call(client, user, entity, "post", f"/accounts/{rent.id}/archive", {})
    assert (done.status_code, done.json()) == (200, {"message": "Rent archived."})
    rent.refresh_from_db()
    # archived by Minty: Xero's status is untouched, so it is never taken for one Xero archived (A3)
    assert (rent.record_status, rent.status) == ("archived", "ACTIVE")
    assert not EntityAccountXero.objects.get(account_id=rent.id).is_active, "Petty Cash's picker"
    assert not EntityBillAccountXero.objects.get(entity_id=entity.id, account_code="469").is_active, "the bills'"
    assert not AccountModule.objects.filter(account_id=rent.id, is_active=True).exists()

    read = call(client, user, entity, "get", "/accounts").json()
    assert "Rent" not in [a["name"] for a in read["accounts"]]
    assert [(a["name"], a["used"]) for a in read["archived_accounts"]] == [("Rent", True)]

    back = call(client, user, entity, "post", f"/accounts/{rent.id}/restore", {})
    assert (back.status_code, back.json()["message"]) == (200, "Rent is back.")
    rent.refresh_from_db()
    assert rent.record_status == "active"
    assert EntityAccountXero.objects.get(account_id=rent.id).is_active
    assert EntityBillAccountXero.objects.get(entity_id=entity.id, account_code="469").is_active
    assert call(client, user, entity, "get", "/accounts").json()["archived_accounts"] == []


@pytest.mark.django_db
def test_restore_is_refused_when_another_account_took_its_code(client, user, entity):
    rent = _added(client, user, entity)
    _expenses(entity, 1, account_id=rent.id)
    assert call(client, user, entity, "post", f"/accounts/{rent.id}/archive", {}).status_code == 200
    _added(client, user, entity, "Office Rent", "469")
    refused = call(client, user, entity, "post", f"/accounts/{rent.id}/restore", {})
    assert refused.status_code == 409 and "469" in refused.json()["error"]
    assert AccountInfo.objects.get(id=rent.id).record_status == "archived"


@pytest.mark.django_db
def test_no_archive_while_live_on_xero_and_a_linked_one_waits_for_the_matching(client, user, disconnected):
    rent = account(disconnected, "Rent", "469", source="xero", xero_id="x-rent")
    Entity.objects.filter(id=disconnected.id).update(xero_matching="new_accounts")
    resp = call(client, user, disconnected, "post", f"/accounts/{rent.id}/archive", {})
    assert (resp.status_code, resp.json()["error"]) == (409, MATCHING_OPEN)
    Entity.objects.filter(id=disconnected.id).update(xero_matching=None, status="connected")
    resp = call(client, user, disconnected, "post", f"/accounts/{rent.id}/archive", {})
    assert (resp.status_code, resp.json()["error"]) == (409, ACCOUNTS_IN_XERO)
    assert AccountInfo.objects.get(id=rent.id).record_status == "active"


@pytest.mark.django_db
def test_xero_off_a_linked_account_archives_and_its_xero_bill_code_follows(client, user, disconnected):
    rent = account(disconnected, "Rent", "469", source="xero", xero_id="x-rent")
    EntityBillAccountXero.objects.create(entity_id=disconnected.id, account_code="469", account_name="Rent",
                                         account_type="EXPENSE", xero_account_id="x-rent")
    assert call(client, user, disconnected, "post", f"/accounts/{rent.id}/archive", {}).status_code == 200
    assert not EntityBillAccountXero.objects.get(entity_id=disconnected.id, xero_account_id="x-rent").is_active
    assert AccountInfo.objects.get(id=rent.id).xero_account_id == "x-rent", "the link kept"


@pytest.mark.django_db
def test_a_cashier_neither_archives_nor_restores(client, user, entity):
    from minty_db.models import UserEntity

    rent = _added(client, user, entity)
    UserEntity.objects.filter(user_id=user.id, entity_id=entity.id).update(role="cashier")
    assert call(client, user, entity, "post", f"/accounts/{rent.id}/archive", {}).status_code == 403
    assert call(client, user, entity, "post", f"/accounts/{rent.id}/restore", {}).status_code == 403


# ---- contacts -----------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_used_contacts_delete_says_what_uses_it_and_offers_archive(client, user, entity):
    kee = contact(entity, "Kee Wah")
    _expenses(entity, 3, contact_id=kee.id)
    _bills(entity, 2, contact_id=kee.id)
    refused = call(client, user, entity, "delete", f"/contacts/{kee.id}")
    assert (refused.status_code, refused.json()) == (
        409, {"error": "Kee Wah is used by 3 expenses and 2 bills. Archive it instead.", "can_archive": True})
    row = call(client, user, entity, "get", "/contacts").json()["contacts"][0]
    assert (row["used_note"], row["can_archive"]) == ("Kee Wah is used by 3 expenses and 2 bills. Archive it instead.",
                                                      True)


@pytest.mark.django_db
def test_a_contact_daily_closing_uses_is_neither_deleted_nor_archived(client, user, entity):
    walk_in = contact(entity, "Walk-in Customer")
    EntityPettycashSettings.objects.create(entity_id=entity.id, cash_sale_contact_id=walk_in.id)
    said = "Walk-in Customer is used in Petty Cash Settings. Choose another contact there first."
    assert call(client, user, entity, "delete", f"/contacts/{walk_in.id}").json() == {"error": said}
    assert call(client, user, entity, "post", f"/contacts/{walk_in.id}/archive", {}).json() == {"error": said}


@pytest.mark.django_db
def test_archive_takes_a_contact_off_the_lists_and_restore_brings_it_back(client, user, entity):
    kee = contact(entity, "Kee Wah")
    _expenses(entity, 1, contact_id=kee.id)
    done = call(client, user, entity, "post", f"/contacts/{kee.id}/archive", {})
    assert (done.status_code, done.json()) == (200, {"message": "Kee Wah archived."})
    assert XeroContactSync.objects.get(id=kee.id).record_status == "archived"
    read = call(client, user, entity, "get", "/contacts").json()
    assert read["contacts"] == [] and [c["name"] for c in read["archived_contacts"]] == ["Kee Wah"]

    back = call(client, user, entity, "post", f"/contacts/{kee.id}/restore", {})
    assert (back.status_code, back.json()["message"]) == (200, "Kee Wah is back.")
    assert XeroContactSync.objects.get(id=kee.id).record_status == "active"


@pytest.mark.django_db
def test_a_contact_is_not_restored_over_one_of_the_same_name(client, user, entity):
    kee = contact(entity, "Kee Wah")
    _expenses(entity, 1, contact_id=kee.id)
    assert call(client, user, entity, "post", f"/contacts/{kee.id}/archive", {}).status_code == 200
    contact(entity, "kee  wah")
    refused = call(client, user, entity, "post", f"/contacts/{kee.id}/restore", {})
    assert refused.status_code == 409 and "already in your contacts" in refused.json()["error"]


@pytest.mark.django_db
def test_a_contact_linked_to_xero_is_archived_in_xero_while_live(client, user, live):
    kee = contact(live, "Kee Wah", source="xero", xero_id="x-kee")
    resp = call(client, user, live, "post", f"/contacts/{kee.id}/archive", {})
    assert (resp.status_code, resp.json()["error"]) == (
        409, "This contact comes from Xero. Change it in Xero, and Minty follows.")
