"""A company's chart of accounts and contacts in Settings: add, edit, delete (owner, 2026-11-12).

The one implementation (owner's choice, 2026-11-12): onboarding's own account rules and side
effects (``accounting``: the checks, the old readers' rows, the module rows, the daily-closing
slots), reached from the hub through accounts-api (``onboarding/internal.py``).

WHO OWNS WHAT. Every row here is the company's own copy, so the company edits and deletes any of
them (owner, 2026-11-12): the ones it made (``source`` ``user``), those from its Xero (``xero``),
and its copies of Minty's starter chart and template contacts (``template`` - Minty's own list,
``account_template``, is never touched). Only the hidden system rows daily closing needs stay out.

WITH OR WITHOUT XERO (the owner's decisions doc, 2026-11-19). Minty never creates an account in
Xero (A1): an account added here is Minty's until the matching links it. While the company is live
on Xero no account is added, changed or deleted here at all - the chart is Xero's, and the sync
brings it (409 ``ACCOUNTS_IN_XERO``; the user, 2026-11-23); contacts are unaffected. A contact added while live
is linked to Xero's contact of its name, else made there as a supplier (C5); offline it is Minty's
own row and reaches Xero at publish (C1). A contact linked to Xero is changed in Xero only while
live (409; the user, 2026-11-19) - Merge stays. With Xero off the company is Minty-only (the user,
2026-11-27): its linked accounts and contacts are edited and deleted here like Minty's own, the stored
link kept for the matching at reconnect; while a matching is open a linked row waits for it (409
``MATCHING_OPEN``). Delete is complete when nothing uses it; a used account or contact can't be
deleted (owner, 2026-11-18: refused, 409) - the refusal says what uses it and offers Archive (the user,
2026-11-27): archived, it is out of every picker and still shown on old records, until it is
restored. One daily closing uses is neither deleted nor archived: Petty Cash Settings first.

DELETED, THEN ADDED AGAIN (owner, 2026-11-12): brought back, never a twin. An account added with
the code, name and type of one the company deleted - or a contact with the name of one - is that
one: Minty's archived row restored (its module and old-reader rows on again; archived by the old
rule, before 2026-11-18, or by a merge), or, when the row went
(nothing used it), Xero's archived contact of the name linked again as it stands. Minty never
restores anything in the customer's Xero (C4): one Xero holds archived is asked about at publish
(contacts) or in Settings (accounts, A3).

NO DUPLICATE CONTACTS. A contact's name is unique in its company among active ones, case and
inner spaces ignored. A live company's new contact links to the Xero contact of that name when one
is stored (``xero_contact``) before anything is created.
"""

from __future__ import annotations

import logging
import re
import uuid

from django.db import transaction
from django.db.models import F, Q
from minty_db.models import (
    AccountInfo,
    AccountModule,
    AccountTemplate,
    Entity,
    EntityAccountXero,
    EntityBillAccountXero,
    XeroContact,
    XeroContactSync,
)
from minty_shared import accounts as rules

from core import xero_client
from core.policy import Permission, has_permission_by_user_id
from core.xero_tokens import access_token_for
from onboarding.services import accounting
from onboarding.services.xero_lists import xero_connected

logger = logging.getLogger("minty-onboarding")

DENIED = {"error": "You don't have permission to change this company's accounts and contacts."}
NOT_FOUND = {"error": "That company isn't here any more. Reload the page and try again."}
ACCOUNT_GONE = {"error": "That account isn't in this company. Reload the list and try again."}
CONTACT_GONE = {"error": "That contact isn't in this company. Reload the list and try again."}
RECONNECT = {
    "error": "Xero needs to be connected again. Reconnect it in Settings, under Integration, then try again."
}
#: A contact linked to Xero, while live (the user, 2026-11-19; accounts while live: ACCOUNTS_IN_XERO).
CONTACT_FROM_XERO = "This contact comes from Xero. Change it in Xero, and Minty follows."
UNANSWERED = {"error": "Xero didn't answer just now. Mind trying again in a moment?"}
#: The user's rule (2026-11-23): while live on Xero, the chart is Xero's to change.
ACCOUNTS_IN_XERO = {"error": accounting.ACCOUNTS_IN_XERO}
CONTACT_MAX = 150

#: The company's to edit and delete: every source - its copies of Minty's too (owner, 2026-11-12).
OWNED = ("user", "xero", "template")


class _Stop(Exception):
    """A refusal on the way: the answer and its status."""

    def __init__(self, answer: dict, status: int):
        super().__init__(answer.get("error"))
        self.answer, self.status = answer, status


def _gate(user_id, entity_id, permission) -> Entity:
    if not has_permission_by_user_id(user_id, permission, entity_id):
        raise _Stop(DENIED, 403)
    entity = Entity.objects.filter(id=entity_id).first()
    if entity is None:
        raise _Stop(NOT_FOUND, 404)
    return entity


def _xero(entity: Entity) -> str | None:
    """A live company's Xero token; None for a company not live on Xero. A live company whose
    token can't be had is refused (the change would miss Xero)."""
    if not xero_connected(entity):
        return None
    token = access_token_for(str(entity.id))
    if token is None:
        raise _Stop(RECONNECT, 409)
    return token


def _not_live(entity: Entity, answer: dict) -> None:
    """Refused while the company is live on Xero (the user, 2026-11-23): Xero's to change."""
    if xero_connected(entity):
        raise _Stop(answer, 409)


def _linked_waits(entity: Entity, linked) -> None:
    """A row linked to Xero waits while a matching is open (``accounting.linked_row_waits``); with
    Xero off it is the company's own to change (the user, 2026-11-27)."""
    if linked and accounting.linked_row_waits(entity):
        raise _Stop({"error": accounting.MATCHING_OPEN}, 409)


def _linked_contact_waits(entity: Entity, contact: XeroContactSync) -> None:
    """A contact linked to Xero is changed in Xero while live (the user, 2026-11-19) and waits while a
    matching is open; with Xero off it is Minty's (the user, 2026-11-27)."""
    if contact.xero_contact_id and xero_connected(entity):
        raise _Stop({"error": CONTACT_FROM_XERO}, 409)
    _linked_waits(entity, contact.xero_contact_id)


def _send(token, entity: Entity, method: str, path: str, body: dict) -> dict:
    try:
        return xero_client.write(token, entity.xero_org_id, method, path, body)
    except xero_client.XeroRefused as refused:
        raise _Stop({"error": refused.sentence}, 422) from None
    except xero_client.XeroUnanswered:
        raise _Stop(UNANSWERED, 502) from None


def _answer(fn):
    """``(answer, status)`` from a function that may stop on the way."""

    def run(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _Stop as stop:
            return stop.answer, stop.status

    run.__doc__ = fn.__doc__
    run.__name__ = fn.__name__
    return run


# ---- accounts --------------------------------------------------------------------------------------


def _rights(user_id, entity_id) -> dict:
    """What the person may do here - the page draws its buttons from these, never from a role."""
    def may(permission):
        return has_permission_by_user_id(user_id, permission, entity_id)

    return {"add": may(Permission.COA_CREATE), "edit": may(Permission.COA_UPDATE),
            "delete": may(Permission.COA_DELETE)}


def _account_row(account: AccountInfo, used: bool, uses=None) -> dict:
    row = accounting._row(account, used)
    # what uses it, the delete's own sentence (the user, 2026-11-27): the page asks Archive instead
    row["used_note"], row["can_archive"] = _used_sentence(account.name, "account", *uses) if uses else (None, False)
    row["own"] = account.source in OWNED and not account.is_system
    row["in_xero"] = bool(account.xero_account_id)
    # Minty Code + Xero Code (the user, 2026-11-24): ``code`` is Minty's; Xero's only once linked
    row["xero_code"] = (account.xero_code or "").strip() if account.xero_account_id else ""
    # the owner's decisions doc A3 (2026-11-19): a linked account Xero archived or removed - the sync
    # leaves a used one ARCHIVED here - is asked about in Settings: map it to an account Xero has
    row["gone_from_xero"] = bool(account.xero_account_id) and (account.status or "").upper() != "ACTIVE"
    return row


def _own_account(entity_id, account_id) -> AccountInfo:
    try:
        account = accounting._active(entity_id).filter(id=uuid.UUID(str(account_id))).first()
    except ValueError:
        account = None
    if account is None or account.is_system:
        raise _Stop(ACCOUNT_GONE, 404)
    return account


def _minty_offers(entity: Entity) -> list[AccountTemplate]:
    """Minty's accounts the company can still add (owner, 2026-11-12): the starter chart for its
    country - every business type's, one per name - less the hidden system rows and any the company
    already has (by name, or the same code and name)."""
    taken = {_key(n) for n in accounting._active(entity.id).values_list("name", flat=True)}
    offers, seen = [], set()
    for row in (AccountTemplate.objects.filter(Q(country_code__isnull=True) | Q(country_code=entity.country_code))
                .filter(is_system=False).order_by("sort_order", "code", "name")):
        name = _key(row.name)
        if name in seen or name in taken or accounting._present(entity.id, row):
            continue
        seen.add(name)
        offers.append(row)
    return offers


def _offer_row(row: AccountTemplate) -> dict:
    return {"id": str(row.id), "code": row.code or "", "name": row.name, "type": row.type,
            "type_label": rules.TYPE_WORDS.get(row.type, ""), "description": row.description or ""}


def _bring_account_back(user_id, entity: Entity, clean: dict) -> AccountInfo | None:
    """The account the company deleted with this code, name and type, brought back; None when there
    is none (the add goes on as a new one). In Minty only: Minty never restores anything in the
    customer's Xero, nor links an account Xero archived (owner's decisions doc A1/C4, 2026-11-19)."""
    same = {"code": clean["code"], "name__iexact": clean["name"], "account_type": clean["type"]}
    row = (AccountInfo.objects.filter(entity_id=entity.id, record_status="archived", merged_into__isnull=True,
                                      is_system=False, **same)
           .order_by(F("xero_account_id").desc(nulls_last=True)).first())
    if row is None:
        return None
    with transaction.atomic():
        _reactivate(entity, row)
        # its own name kept (matched ignoring case); a description typed now is taken
        accounting._apply_update(entity, row, {**clean, "name": row.name})
    logger.info("chart: entity %s brought account %s back", entity.id, row.id)
    return row


def _reactivate(entity: Entity, row: AccountInfo) -> None:
    """An archived account active again, with its module, Petty Cash and bill-code rows on."""
    row.record_status = "active"
    if not row.xero_account_id:
        row.status = "ACTIVE"  # a linked one keeps Xero's status: archived there, Settings asks (A3)
    row.save(update_fields=["record_status", "status"])
    AccountModule.objects.filter(account_id=row.id).update(is_active=True)
    EntityAccountXero.objects.filter(account_id=row.id).update(is_active=True)
    _bill_codes(entity, row).update(is_deleted=False, is_active=True)


def _bill_codes(entity: Entity, account: AccountInfo):
    """Payment Request's bill code of an account: Xero's row for a linked one, else its hand-made row."""
    if account.xero_account_id:
        return EntityBillAccountXero.objects.filter(entity_id=entity.id, xero_account_id=account.xero_account_id)
    return EntityBillAccountXero.objects.filter(entity_id=entity.id, account_code=account.code, xero_account_id="")


def _archived_accounts(entity_id):
    """The accounts the company archived (the user, 2026-11-27) - not one Xero archived (its status
    says so, A3), merged away, or a hidden system row."""
    return AccountInfo.objects.filter(entity_id=entity_id, record_status="archived", status="ACTIVE",
                                      merged_into__isnull=True, is_system=False)


def _counted(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _used_sentence(name: str, what: str, expenses: int, bills: int, in_settings: bool) -> tuple[str, bool]:
    """What uses an account or contact, and whether it may be archived instead (the user,
    2026-11-27): one daily closing uses waits for Petty Cash Settings to choose another."""
    if in_settings:
        return f"{name} is used in Petty Cash Settings. Choose another {what} there first.", False
    parts = [_counted(n, word) for n, word in ((expenses, "expense"), (bills, "bill")) if n]
    return f"{name} is used by {' and '.join(parts) if parts else 'your records'}. Archive it instead.", True


def _refuse_used(name: str, what: str, expenses: int, bills: int, in_settings: bool) -> None:
    """A used account or contact isn't deleted (owner, 2026-11-18): the refusal says what uses it and
    offers Archive (``can_archive``)."""
    sentence, can_archive = _used_sentence(name, what, expenses, bills, in_settings)
    raise _Stop({"error": sentence, "can_archive": True} if can_archive else {"error": sentence}, 409)


@_answer
def archive_account(user_id, entity_id, account_id) -> tuple[dict, int]:
    """Archived (the user, 2026-11-27): out of every picker - Petty Cash's tick, the bill codes, the
    module rows - and still shown on old records (they read it by id). Minty's own mark: Xero's status
    is untouched, so it is never taken for an account Xero archived (A3). Not while live on Xero, nor
    one daily closing uses."""
    entity = _gate(user_id, entity_id, Permission.COA_DELETE)
    _not_live(entity, ACCOUNTS_IN_XERO)
    account = _own_account(entity_id, account_id)
    _linked_waits(entity, account.xero_account_id)
    if accounting.in_settings(account):
        _refuse_used(account.name, "account", 0, 0, True)
    with transaction.atomic():
        AccountInfo.objects.filter(id=account.id).update(record_status="archived")
        EntityAccountXero.objects.filter(account_id=account.id).update(is_active=False)
        AccountModule.objects.filter(account_id=account.id).update(is_active=False)
        _bill_codes(entity, account).update(is_active=False)
    logger.info("chart: entity %s archived account %s", entity_id, account.id)
    return {"message": f"{account.name} archived."}, 200


@_answer
def restore_account(user_id, entity_id, account_id) -> tuple[dict, int]:
    """An account the company archived, back in every picker - unless another account took its code
    or name meanwhile."""
    entity = _gate(user_id, entity_id, Permission.COA_CREATE)
    _not_live(entity, ACCOUNTS_IN_XERO)
    try:
        row = _archived_accounts(entity_id).filter(id=uuid.UUID(str(account_id))).first()
    except ValueError:
        row = None
    if row is None:
        raise _Stop(ACCOUNT_GONE, 404)
    _linked_waits(entity, row.xero_account_id)
    _checked(entity_id, {"code": row.code, "name": row.name, "type": row.account_type,
                         "description": row.description}, exclude=row.id)
    with transaction.atomic():
        _reactivate(entity, row)
    logger.info("chart: entity %s restored account %s", entity_id, row.id)
    return {"account": _account_row(row, row.id in accounting._held(entity_id, [row.id])),
            "message": f"{row.name} is back."}, 200


def _checked(entity_id, payload: dict, exclude=None) -> dict:
    clean, fields = accounting._check(entity_id, payload, exclude)
    if fields:
        raise _Stop({"error": next(iter(fields.values())), "fields": fields},
                    409 if accounting._is_clash(fields) else 422)
    return clean


@_answer
def accounts(user_id, entity_id) -> tuple[dict, int]:
    """The active chart (the hidden system rows left out), each with what may be done to it, the
    types, and each type's next free code."""
    entity = _gate(user_id, entity_id, Permission.COA_VIEW)
    rows = [a for a in accounting._active(entity_id).order_by("code", "name") if not a.is_system]
    # the owner's decisions doc A3 (2026-11-19): a linked account Xero archived or removed is archived
    # here too (the sync sets its status, a trigger its record status) - while records use it, it
    # stays in the list, marked, for the person to map it to an account Xero has
    gone = list(AccountInfo.objects.filter(entity_id=entity_id, record_status="archived", is_system=False,
                                           merged_into__isnull=True, xero_account_id__isnull=False)
                .exclude(status="ACTIVE").order_by("code", "name"))
    used_gone = accounting._held(entity_id, [a.id for a in gone])
    rows += [a for a in gone if a.id in used_gone]
    held = accounting._held(entity_id, [a.id for a in rows])
    taken = list(accounting._active(entity_id).values_list("code", "account_type"))
    archived = list(_archived_accounts(entity_id).order_by("code", "name"))
    used_archived = accounting._held(entity_id, [a.id for a in archived])
    uses = accounting.account_uses(entity_id, [a for a in rows if a.id in held])
    return {
        "accounts": [_account_row(a, a.id in held, uses.get(a.id)) for a in rows],
        "archived_accounts": [_account_row(a, a.id in used_archived) for a in archived],
        "types": [{"value": t, "label": rules.TYPE_WORDS[t]} for t in rules.XERO_TYPE],
        "next_codes": {t: rules.next_code(taken, t) for t in rules.XERO_TYPE},
        "xero_live": xero_connected(entity),
        "matching_open": accounting.linked_row_waits(entity),
        "permissions": _rights(user_id, entity_id),
        "minty_accounts": [_offer_row(row) for row in _minty_offers(entity)],
    }, 200


@_answer
def add_account(user_id, entity_id, payload: dict) -> tuple[dict, int]:
    """A new account: the company's own, or - with ``template_id`` - one of Minty's, added as
    Minty's copy (its module and daily-closing defaults, as onboarding's Auto Fill). Never created in
    Xero (owner's decisions doc A1, 2026-11-19): it is mapped to a Xero account on the matching
    screen."""
    entity = _gate(user_id, entity_id, Permission.COA_CREATE)
    _not_live(entity, ACCOUNTS_IN_XERO)  # live on Xero: added in Xero (the user, 2026-11-23)
    template = None
    if payload.get("template_id"):
        template = next((row for row in _minty_offers(entity) if str(row.id) == str(payload["template_id"])), None)
        if template is None:
            raise _Stop({"error": "That account isn't one of Minty's to add any more. Reload the list and try again."},
                        404)
        payload = {"code": template.code, "name": template.name, "type": template.type,
                   "description": template.description}
    clean = _checked(entity_id, payload)
    if template is None:
        back = _bring_account_back(user_id, entity, clean)
        if back is not None:
            return {"account": _account_row(back, back.id in accounting._held(entity_id, [back.id])),
                    "message": f"{back.name} is back."}, 201
    with transaction.atomic():
        if template is not None:
            account = accounting._from_template(user_id, entity, template)
        else:
            account = accounting._create(user_id, entity, clean["code"], clean["name"], clean["type"],
                                         clean["description"], source="user")
    logger.info("chart: entity %s added account %s", entity_id, account.id)
    return {"account": _account_row(account, False), "message": f"{account.name} added."}, 201


@_answer
def edit_account(user_id, entity_id, account_id, payload: dict) -> tuple[dict, int]:
    """The company's own account changed. One linked to Xero is changed in Xero only (the user,
    2026-11-19, with the owner's decisions doc A1: accounts flow from Xero to Minty)."""
    entity = _gate(user_id, entity_id, Permission.COA_UPDATE)
    _not_live(entity, ACCOUNTS_IN_XERO)  # live on Xero: changed in Xero (the user, 2026-11-23)
    account = _own_account(entity_id, account_id)
    _linked_waits(entity, account.xero_account_id)
    clean = _checked(entity_id, payload, exclude=account.id)
    with transaction.atomic():
        accounting._apply_update(entity, account, clean)
    used = account.id in accounting._held(entity_id, [account.id])
    return {"account": _account_row(account, used), "message": f"{account.name} saved."}, 200


@_answer
def delete_account(user_id, entity_id, account_id) -> tuple[dict, int]:
    """The company's own account taken away: deleted. A used one is refused (owner, 2026-11-18); one
    linked to Xero is refused too - it is changed in Xero only (the user, 2026-11-19)."""
    entity = _gate(user_id, entity_id, Permission.COA_DELETE)
    _not_live(entity, ACCOUNTS_IN_XERO)  # live on Xero: changed in Xero (the user, 2026-11-23)
    account = _own_account(entity_id, account_id)
    _linked_waits(entity, account.xero_account_id)
    if accounting._used(account):
        _refuse_used(account.name, "account", *accounting.account_uses(entity_id, [account])[account.id])
    with transaction.atomic():
        accounting._remove(account)
    logger.info("chart: entity %s deleted account %s", entity_id, account_id)
    return {"message": f"{account.name} deleted."}, 200


# ---- contacts --------------------------------------------------------------------------------------


def _key(name: str) -> str:
    return re.sub(r"\s+", " ", name or "").strip().lower()


def _active_contacts(entity_id):
    return XeroContactSync.objects.filter(entity_id=entity_id, record_status="active", merged_into__isnull=True)


def _holder(entity_id, name: str, exclude=None) -> XeroContactSync | None:
    key = _key(name)
    return next((c for c in _active_contacts(entity_id).exclude(id=exclude) if _key(c.name) == key), None)


def _contact_name(payload: dict) -> str:
    name = re.sub(r"\s+", " ", str(payload.get("name") or "")).strip()
    if not name:
        raise _Stop({"error": "The contact's name is still empty. Add a name to continue.",
                     "fields": {"name": "The contact's name is still empty. Add a name to continue."}}, 422)
    if len(name) > CONTACT_MAX:
        sentence = f"The name can be up to {CONTACT_MAX} characters. Shorten it to continue."
        raise _Stop({"error": sentence, "fields": {"name": sentence}}, 422)
    return name


def _taken(entity_id, name: str, exclude=None) -> None:
    holder = _holder(entity_id, name, exclude)
    if holder is not None:
        sentence = f"{holder.name} is already in your contacts. Use that one, or choose another name."
        raise _Stop({"error": sentence, "fields": {"name": sentence}}, 409)


#: A used contact is never deleted (owner, 2026-11-18): it stays, and the delete is refused.
USED_CONTACT = "{name} is already used in your records or Petty Cash Settings, so it can't be deleted."


def _contact_in_settings(entity_id, contact_id) -> bool:
    from minty_db.models import EntityPettycashSettings

    return EntityPettycashSettings.objects.filter(
        Q(director_contact_id=contact_id) | Q(cash_sale_contact_id=contact_id) | Q(discrepancy_contact_id=contact_id),
        entity_id=entity_id).exists()


def _contact_uses(entity_id, ids) -> dict:
    """``{contact id: (expense lines, bills, in Petty Cash Settings)}`` (the refusal's sentence)."""
    from django.db.models import Count
    from minty_db.models import Bill, EntityPettycashSettings, ReportExpense

    ids = list(ids)
    if not ids:
        return {}
    expenses = dict(ReportExpense.objects.filter(report__entity_id=entity_id, contact_id__in=ids)
                    .values("contact_id").annotate(n=Count("id")).values_list("contact_id", "n"))
    bills = dict(Bill.objects.filter(entity_id=entity_id, contact_id__in=ids).values("contact_id")
                 .annotate(n=Count("id")).values_list("contact_id", "n"))
    slots = ("director_contact_id", "cash_sale_contact_id", "discrepancy_contact_id")
    settings = EntityPettycashSettings.objects.filter(entity_id=entity_id).values(*slots).first() or {}
    slotted = {v for v in settings.values() if v}
    return {i: (expenses.get(i, 0), bills.get(i, 0), i in slotted) for i in ids}


def _archived_contacts(entity_id) -> list[XeroContactSync]:
    """The contacts the company archived (the user, 2026-11-27): not merged away, and not one Xero
    archived (its stored copy says so)."""
    rows = XeroContactSync.objects.filter(entity_id=entity_id, record_status="archived", merged_into__isnull=True)
    gone = set(XeroContact.objects.filter(entity_id=entity_id).exclude(status="ACTIVE").values_list("id", flat=True))
    return sorted((c for c in rows if c.xero_contact_link_id not in gone), key=lambda c: _key(c.name))


def _contacts_used(entity_id) -> set:
    """The contacts something uses: an expense line, a bill, a contact combined into it, or one of
    daily closing's contacts."""
    from minty_db.models import Bill, EntityPettycashSettings, ReportExpense

    used = set(ReportExpense.objects.filter(report__entity_id=entity_id, contact_id__isnull=False)
               .values_list("contact_id", flat=True))
    used |= set(Bill.objects.filter(entity_id=entity_id, contact_id__isnull=False).values_list("contact_id", flat=True))
    used |= set(XeroContactSync.objects.filter(entity_id=entity_id, merged_into__isnull=False)
                .values_list("merged_into_id", flat=True))
    slots = ("director_contact_id", "cash_sale_contact_id", "discrepancy_contact_id")
    settings = EntityPettycashSettings.objects.filter(entity_id=entity_id).values(*slots).first()
    return used | {v for v in (settings or {}).values() if v}


def _contact_row(contact: XeroContactSync, used: bool, uses=None) -> dict:
    # xero_id: how the apps' pickers name a contact (its Xero id, else its own id)
    note, can_archive = _used_sentence(contact.name, "contact", *uses) if uses else (None, False)
    return {"id": str(contact.id), "name": contact.name, "own": contact.source in OWNED, "source": contact.source,
            "in_xero": bool(contact.xero_contact_id), "xero_id": contact.xero_contact_id or None, "used": used,
            "used_note": note, "can_archive": can_archive}


def _own_contact(entity_id, contact_id) -> XeroContactSync:
    try:
        contact = _active_contacts(entity_id).filter(id=uuid.UUID(str(contact_id))).first()
    except ValueError:
        contact = None
    if contact is None:
        raise _Stop(CONTACT_GONE, 404)
    return contact


@_answer
def contacts(user_id, entity_id) -> tuple[dict, int]:
    entity = _gate(user_id, entity_id, Permission.COA_VIEW)
    used = _contacts_used(entity_id)
    rows = sorted(_active_contacts(entity_id), key=lambda c: _key(c.name))
    uses = _contact_uses(entity_id, [c.id for c in rows if c.id in used])
    return {"contacts": [_contact_row(c, c.id in used, uses.get(c.id)) for c in rows],
            "archived_contacts": [_contact_row(c, c.id in used) for c in _archived_contacts(entity_id)],
            "xero_live": xero_connected(entity),
            "matching_open": accounting.linked_row_waits(entity), "permissions": _rights(user_id, entity_id)}, 200


def _xero_contact_for(token, entity: Entity, name: str) -> tuple[str, str]:
    """The Xero contact of that name the company doesn't hold yet - from the stored copies, else
    searched live (``GET /Contacts?searchTerm=``, Xero's loose case-insensitive search, so only the
    exact name counts) - else a new one made in Xero as a supplier. ``(ContactID, Xero's name)``; never
    a twin in Xero (owner's decisions doc C5, 2026-11-19)."""
    held = set(XeroContactSync.objects.filter(entity_id=entity.id, xero_contact_id__isnull=False)
               .values_list("xero_contact_id", flat=True))
    copy = next((c for c in XeroContact.objects.filter(entity_id=entity.id, xero_org_id=str(entity.xero_org_id),
                                                       status="ACTIVE")
                 if _key(c.name) == _key(name) and c.xero_guid not in held), None)
    if copy is not None:
        return copy.xero_guid, copy.name
    try:
        found = xero_client.get(token, entity.xero_org_id, "Contacts",
                                {"searchTerm": " ".join(name.split()), "page": 1}).get("Contacts") or []
    except xero_client.XeroUnanswered:
        raise _Stop(UNANSWERED, 502) from None
    match = next((c for c in found
                  if c.get("ContactID") and _key(c.get("Name")) == _key(name) and c["ContactID"] not in held
                  and str(c.get("ContactStatus") or "ACTIVE").upper() == "ACTIVE"), None)
    if match is not None:
        return match["ContactID"], match.get("Name") or name
    made = _send(token, entity, "POST", "Contacts",
                 {"Contacts": [{"Name": name, "IsCustomer": False, "IsSupplier": True}]})
    xero_id = ((made.get("Contacts") or [{}])[0]).get("ContactID")
    if not xero_id:
        logger.error("chart: Xero made contact %r for entity %s but sent no id", name, entity.id)
        raise _Stop(UNANSWERED, 502)
    return xero_id, name


def _xero_name(entity: Entity, contact: XeroContactSync, xero_name: str) -> None:
    """A contact linked to a Xero contact takes Xero's name at the link (the user, 2026-11-27: linked
    contacts are changed in Xero and Minty follows - never a silent flip at the next sync); the bills
    on it follow at once."""
    from minty_db.models import Bill

    name = (xero_name or "").strip()[:CONTACT_MAX]
    if not name or name == contact.name:
        return
    XeroContactSync.objects.filter(id=contact.id).update(name=name)
    contact.name = name
    Bill.objects.filter(entity_id=entity.id, contact_id=contact.id).exclude(contact_name=name[:100]).update(
        contact_name=name[:100])


def find_or_make_contact(user_id, entity: Entity, name: str, token: str | None) -> tuple[XeroContactSync, str]:
    """The company's contact of that name, else a new one of its own - linked to the Xero contact
    of the name when one is stored, else created in Xero, for a live company.
    The one way a contact is added (Settings, and the pickers' "+ Add" through ``create_contact``).
    ``(contact, how)``: ``back`` (a deleted one brought back), ``found`` or ``made``."""
    back = _bring_contact_back(entity, name, token)
    if back is not None:
        return back, "back"
    holder = _holder(entity.id, name)
    if holder is not None:
        if token and not holder.xero_contact_id:
            # made in Minty while offline: into Xero now, so the picker's answer is usable there
            xero_id, xero_name = _xero_contact_for(token, entity, holder.name)
            XeroContactSync.objects.filter(id=holder.id).update(xero_contact_id=xero_id,
                                                                xero_org_id=str(entity.xero_org_id))
            holder.xero_contact_id = xero_id
            _xero_name(entity, holder, xero_name)
        return holder, "found"
    xero_id, xero_name = _xero_contact_for(token, entity, name) if token else (None, name)
    contact = XeroContactSync.objects.create(
        entity_id=entity.id, name=(xero_name or name)[:CONTACT_MAX], source="user", created_by=user_id,
        category="expense_contact",
        xero_contact_id=xero_id, xero_org_id=str(entity.xero_org_id) if xero_id else None)
    return contact, "made"


def _bring_contact_back(entity: Entity, name: str, token) -> XeroContactSync | None:
    """The contact the company deleted with this name, brought back: its archived row active again,
    or - its row gone - Xero's archived contact of the name linked to a new row as it stands. None
    when there is none. Minty never restores anything in the customer's Xero (owner's decisions doc
    C4, 2026-11-19): one Xero still holds archived is asked about at publish."""
    key = _key(name)
    if _holder(entity.id, name) is not None:
        return None
    gone = sorted((c for c in XeroContactSync.objects.filter(entity_id=entity.id, record_status="archived",
                                                             merged_into__isnull=True) if _key(c.name) == key),
                  key=lambda c: not c.xero_contact_id)
    if gone:
        row = gone[0]
        XeroContactSync.objects.filter(id=row.id).update(record_status="active")
        row.record_status = "active"
        logger.info("chart: entity %s brought contact %s back", entity.id, row.id)
        return row
    if token is None:
        return None
    held = set(XeroContactSync.objects.filter(entity_id=entity.id, xero_contact_id__isnull=False)
               .values_list("xero_contact_id", flat=True))
    copy = next((c for c in XeroContact.objects.filter(entity_id=entity.id, xero_org_id=str(entity.xero_org_id))
                 .exclude(status="ACTIVE") if _key(c.name) == key and c.xero_guid not in held), None)
    if copy is None:
        return None
    row = XeroContactSync.objects.create(entity_id=entity.id, name=copy.name[:150], source="xero",
                                         xero_contact_id=copy.xero_guid, xero_org_id=str(entity.xero_org_id),
                                         xero_contact_link_id=copy.id, category="expense_contact")
    logger.info("chart: entity %s linked Xero's archived contact %s again, as it stands", entity.id, copy.xero_guid)
    return row


@_answer
def add_contact(user_id, entity_id, payload: dict) -> tuple[dict, int]:
    entity = _gate(user_id, entity_id, Permission.COA_CREATE)
    name = _contact_name(payload)
    _taken(entity_id, name)
    contact, how = find_or_make_contact(user_id, entity, name, _xero(entity))
    accounting.mark_offline_change(entity, contact)
    logger.info("chart: entity %s %s contact %s (in Xero: %s)", entity_id, how, contact.id,
                bool(contact.xero_contact_id))
    said = f"{contact.name} is back." if how == "back" else f"{contact.name} added."
    return {"contact": _contact_row(contact, contact.id in _contacts_used(entity_id)), "message": said}, 201


@_answer
def edit_contact(user_id, entity_id, contact_id, payload: dict) -> tuple[dict, int]:
    """The company's contact renamed. One linked to Xero is changed in Xero only (the user,
    2026-11-19, as for accounts)."""
    entity = _gate(user_id, entity_id, Permission.COA_UPDATE)
    contact = _own_contact(entity_id, contact_id)
    _linked_contact_waits(entity, contact)
    name = _contact_name(payload)
    _taken(entity_id, name, exclude=contact.id)
    contact.name = name
    contact.save(update_fields=["name"])
    accounting.mark_offline_change(entity, contact)
    used = contact.id in _contacts_used(entity_id)
    return {"contact": _contact_row(contact, used), "message": f"{name} saved."}, 200


@_answer
def delete_contact(user_id, entity_id, contact_id) -> tuple[dict, int]:
    """The company's contact taken away: deleted. A used one is refused (owner, 2026-11-18); one
    linked to Xero is refused too - it is changed in Xero only (the user, 2026-11-19, as for
    accounts)."""
    entity = _gate(user_id, entity_id, Permission.COA_DELETE)
    contact = _own_contact(entity_id, contact_id)
    _linked_contact_waits(entity, contact)
    if contact.id in _contacts_used(entity_id):
        _refuse_used(contact.name, "contact", *_contact_uses(entity_id, [contact.id])[contact.id])
    contact.delete()
    logger.info("chart: entity %s deleted contact %s", entity_id, contact_id)
    return {"message": f"{contact.name} deleted."}, 200


@_answer
def archive_contact(user_id, entity_id, contact_id) -> tuple[dict, int]:
    """Archived (the user, 2026-11-27): off the Petty Cash and Payment Request supplier lists, still
    shown on old records. A contact linked to Xero is archived in Xero while live; not one daily
    closing uses."""
    entity = _gate(user_id, entity_id, Permission.COA_DELETE)
    contact = _own_contact(entity_id, contact_id)
    _linked_contact_waits(entity, contact)
    if _contact_in_settings(entity_id, contact.id):
        _refuse_used(contact.name, "contact", 0, 0, True)
    XeroContactSync.objects.filter(id=contact.id).update(record_status="archived")
    logger.info("chart: entity %s archived contact %s", entity_id, contact.id)
    return {"message": f"{contact.name} archived."}, 200


@_answer
def restore_contact(user_id, entity_id, contact_id) -> tuple[dict, int]:
    """A contact the company archived, back on the lists - unless another took its name meanwhile."""
    entity = _gate(user_id, entity_id, Permission.COA_CREATE)
    contact = next((c for c in _archived_contacts(entity_id) if str(c.id) == str(contact_id)), None)
    if contact is None:
        raise _Stop(CONTACT_GONE, 404)
    _linked_contact_waits(entity, contact)
    _taken(entity_id, contact.name, exclude=contact.id)
    XeroContactSync.objects.filter(id=contact.id).update(record_status="active")
    contact.record_status = "active"
    logger.info("chart: entity %s restored contact %s", entity_id, contact.id)
    return {"contact": _contact_row(contact, contact.id in _contacts_used(entity_id)),
            "message": f"{contact.name} is back."}, 200
