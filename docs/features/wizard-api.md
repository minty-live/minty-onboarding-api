# The wizard's API — `/api/onboarding/*`

Everything the onboarding wizard (`../minty-onboarding-web`) calls, served here byte-for-byte on the
same paths Minty's Flask still answers (`Minty/blueprints/entity/routes/create.py`; not
`payment-method`, `billing/*` or `finalize` since 2026-10-06) — the
wizard's `lib/apiRoutes.ts` decides which service gets each path. Routers:
`onboarding/api_reference.py`, `api_state.py`, `api_entity.py`, `api_pettycash.py`,
`api_invites.py`, `api_modules.py`, `api_billing.py`, `api_me.py`; services under
`onboarding/services/`. The migration groups (A–G) and the deliberate divergences from
Flask are in the repo `README.md`.

## Reference (public)

`GET /server-time` — "today" in Hong Kong, server-authoritative, so a date the wizard
defaults to never comes from the browser's clock. `GET /currencies`, `GET /countries` —
the registries Step 1 needs (`currency_info`, `country_info`), read from the database. Each
currency carries `decimal_places` (Step 5 types the opening balance in it; JPY is 0) and each
country its own `currency_id` (or null), which Step 1 preselects when the country is picked.
`GET /plans` — the module price list for Step 2's summary, **from `billing_plan`, not
Stripe** (`services/plans.py`).

## Resume: `/state` and `/saved-step` (`services/state.py`, `steps.py`)

**The `entities` row is the source of truth, not the browser.** `GET /state?entity_id=`
rebuilds the whole picture from the database — modules, Xero and the opening balance from ONE
`entity_setup_summary` row (a Postgres view, so the tests that read `/state` run in the
Postgres pass only; the live Xero revoke check runs first), and the Step 6 flag from
`entity_pettycash_settings.pettycash_account_id` (the view has no such column; c5a05 added
one and was withdrawn 2026-10-14) — entity, name, country, currency, phone,
email, status, `modules`, `xero` (connected? tenant), `sales_methods`, `opening_balance`,
`invites` (best-effort: a cashier who may not list invitations still gets a state),
`saved_step`, `current_step`, `max_reached` — so a cold resume
(new browser, cleared storage) lands correctly.

Two numbers, two meanings, one 1–9 scale:
- `saved_step` — the frontend's step id, stored verbatim by `POST /saved-step` ("Save and
  Exit"); `is_valid_step` refuses anything but an integer 1–9 (stricter than Flask, which
  truncated `2.5`).
- `current_step` — `derive_current_step`: the furthest step the **saved data** justifies
  (live company → All Set; no modules → Modules; no Xero → Accounting; Petty Cash chosen
  but account codes missing → Sales; Bills chosen → Bills; else Invite). The wizard uses
  it only to raise the ceiling of unlocked steps — its own landing order differs on
  purpose, and the module docstring records the bug that trusting it caused.

Step ids: 1 Basic, 2 Modules, 3 Invite, 4 Accounting, 5 Sales, 6 Account code, 7 Others,
8 Bills, 9 All Set.

## The company (`services/entity_create.py`, `resolve.py`)

`POST /create` — creates the company owned by the caller with its default settings (sales
methods: **Cash only** — no electronic or delivery method is seeded, so the wizard's lists start
empty until its Auto Fill);
idempotent on (member, name, `status = onboarding`) so a retry resumes the abandoned row
(`created: false`). `PUT /entity/{id}` edits an in-progress one. Both set
`entities.timezone` from the country (`minty_shared.time.country_timezone`, the tracker's map;
owner, 2026-11-04) - a sent `timezone` is ignored, and a country the map lacks keeps the stored
zone with a warning in the log. Create derives the currency from the country when none is
sent (as edit always has) and sets the creator's `user_entity.joined_at`. **The creator is the
company's subscriber** (owner, 2026-11-13; minty-db v0.9 `entities.subscriber_user_id`). Only
they may activate or change its subscription until they hand it over, and minty-subscription-api
enforces that. A resumed company with no subscriber gets its creator, never over one already set. **Create requires a
country and a currency** (owner, 2026-10-19: Step 1 no longer pre-fills Hong Kong / HKD): no
country is a 400 `"Choose the company's country."`, and no currency sent or derivable from the
country's registry row is a 400 `"Choose the company's currency."` — both before the company is
created. A name over 100 characters (after the trim) is a 400 `"Entity name must be 100
characters or fewer."` on create as on rename (`NAME_MAX`, 2026-10-26; create had no limit
before). **Names need not be unique** (owner, 2026-11-16, as in Xero: a company is its id, the
name a label; it was unique per person from 2026-11-03 and across every customer before):
create and rename never refuse a name another company already has, the acting user's own
included. Create still resumes an in-progress (`onboarding`, unarchived) company of the same
name and member rather than making a twin (below). `PUT /entity/{id}` does not require a country or currency (a partial edit stays valid). **While Xero is linked the currency is locked** (2026-11-24; `xero_lists.xero_connected`: an organisation held and not `disconnected`, so a disconnected company or one mid-matching is not locked): a different currency is a 409 `"This company is connected to Xero, so its currency stays the same as Xero's. To change it, disconnect Xero first."` (`CURRENCY_LOCKED`, accounts-api's Settings sentence), the same one sent back is fine, and a country change keeps the currency (no derivation, no denomination swap). `GET /state`'s `entity.currency_locked` says so for Step 1. accounts-api's connect refuses an organisation whose currency isn't the company's. Country and currency
are **resolved against the registries** (`resolve.country_code` accepts alpha-2, alpha-3
or a unique name; `currency_id` only queries once the value is a uuid) and never stored
unvalidated — they are foreign keys; phone and email are checked the way the wizard does.
The phone (`resolve.contact_phone`, since 2026-10-12) is **E.164**: `+<dial code><number>`,
spaces, dashes, dots and brackets dropped, the domestic trunk 0 dropped (except +39/+378), and
the code must be a `country_info.phone_code` (so a database without
the dial codes in `Minty/scripts/schema_migration/pettycashv3_data_post_build.sql` refuses every number). Only Hong Kong's length
is exact: 8 digits for +852, the one market Minty sells in (`PHONE_LENGTHS`, the wizard's table,
so change both together; add a code when its market opens). Every other code: at least 4
national digits and at most 15 in all. Stored as
`+85291234567`. Any miss is a 400 `"Please check the phone number and its country code."`. A
digits-only value (the pre-2026-10-12 shape) is refused. Stored rows are not rewritten: the
wizard splits an old one under the company's Country when Step 1 opens. `GET /countries`
carries `phone_code` ("+852", "" when the row has none).
The business email (`resolve.business_email`, `POST /create` and `PUT /entity/{id}`) is
English only since 2026-10-01: `resolve.EMAIL_RE` = printable ASCII minus "@", one "@", a dot
in the domain (still shallow: `a+b@sub.domain.museum` passes). A non-ASCII address is a 400
`"Email can only contain English letters, numbers and symbols."`; any other miss is a 400
`"Please enter a valid business email."` (a dotless domain is one since that date). Stored rows
are not rewritten. Invitation emails are not checked here: `POST /invite` forwards to Flask,
which refuses non-ASCII itself, and its 400 reaches the wizard unchanged (`minty_client.proxy`).

The optional business email (`entities.business_email`) is not just stored (2026-09-30): the
billing engine (`minty-subscription-api`, `notify.address_for`; Minty's copy deleted 2026-10-06) sends the company's trial
ending warning there, and a billing account with no billing email of its own mails its payment
emails - and prints its invoices' Bill to - to the business email when every company on the
account shares it. Blank means the payer's own address is used instead.

## Petty-cash setup (`api_pettycash.py`)

- `GET/POST /sales-methods` — the electronic and delivery methods, **reconciled** to the
  submitted name lists, never delete-and-reinsert (`services/sales_methods.py`; catalogue
  rows are shared and get-or-created; permission `SALES_METHOD_CREATE`). Free lists with no
  account link (owner, 2026-11-11: the one-day link to step 4's sales categories is gone, and
  the column, an unreleased minty-db v0.6, was dropped again). Which lists follow the business type
  (`sales_methods.groups`): `GET` (and `/state`'s `sales_methods`) carries `groups` -
  `non_cash` for a retail shop, a service or a general business (step 5 shows one Non-cash list,
  posted as `electronic` with `delivery: []`), `electronic_delivery` for Restaurant & Food and a
  company onboarding never asked. Petty Cash groups the same way (minty-petty-cash-api's
  `sales_groups`).
- `POST /opening-balance` — the starting cash and the date reporting starts, saved as
  `entity_pettycash_settings.opening_balance` / `start_date` (the row is created if Step 6
  has not made it yet; `services/opening_balance.py`). No report is written: Minty's first
  report opens at the amount, defaults to the date and may not start before it. Refused
  with 409 once any report is posted; a future date, a negative or non-numeric amount, an
  amount finer than the currency's `decimal_places`, or a company with no currency yet is
  400. A **blank amount is refused, not saved as 0** (2026-10-26): no amount key, `null`, `""`
  or whitespace is 400 `"The opening balance is still empty. Add the amount to continue."`
  (the wizard skips the call while its field is blank). Flask's fallback (`save_opening_balance`) does the same.
  A save also clears `opening_skipped_at`.
- `POST /opening-balance/skip` (2026-11-26, the user) — step 5's "Skip for now": the balance stays
  **not set** (`opening_balance` / `start_date` NULL, never 0) and `entity_pettycash_settings.opening_skipped_at`
  (minty-db v0.20) records the skip, under a row lock. `{skipped: true}`; a company that already has
  a balance keeps it and gets `{skipped: false}`. `GET /state` carries `opening_skipped`, and step 5
  counts as done when the balance is saved **or** skipped, so the wizard resumes past it. The first
  petty cash report then asks for the date and the amount (minty-petty-cash-api `PUT /opening-balance`).
- `GET/POST /account-codes`, `POST /contacts`, `POST /contacts/create` (a new Xero
  contact), `GET/POST /bill-codes` — the expense accounts, the petty-cash contact mapping
  and the bills' account codes (`services/xero_lists.py`; ported from Flask 2026-11-06, Phase
  4 of the Flask retirement - proxied until then). Flask's answers, statuses and sentences;
  `COA_VIEW` to read, `COA_UPDATE` to save, `CONTACT_CREATE` for a new contact (403
  `{"error": "Access denied"}`); no Xero yet or no working token is 409
  `{error, connected: false}`. The token comes from accounts-api and Xero is called through
  `core/xero_client.py` - this service never refreshes one.
  - `GET /account-codes` asks Xero live (accounts and every page of contacts), as Flask did,
    because only Xero's answer marks the system accounts it leaves out (the Director list, the
    codes). Xero not answering is a 502 with a sentence (Flask showed empty lists).
  - `POST /account-codes` asks accounts-api's one sync first (`POST /api/internal/xero/sync`),
    then resolves each chosen Xero id to its stored row (a choice still missing is 409 naming
    it; Flask kept the old value silently); a key absent keeps its column, an empty one clears
    it; the discrepancy bank mirrors the petty cash bank; the ticked codes are exactly the
    posted ones, at least one when the company has any. With no `expense_codes` key (the Xero
    path's step 6, 2026-11-11: X3 chose them) only the mapping is saved, the ticks stay as
    they are and the answer's `expense_codes` is null.
  - `POST /contacts` resolves from the stored contacts, asking the sync once only when one is
    missing. A company banking all its sales (step 4) keeps no cash-sale contact: it is cleared
    whatever is posted (2026-11-11). `POST /contacts/create` makes a supplier in Xero and stores it
    (`category = expense_contact`); Xero refusing is 502.
  - **`GET`/`POST /petty-cash-codes` (owner, 2026-11-18):** Petty Cash Settings' Account Code for a company keeping its books in Minty (a Xero company is 409: it chose them in X3). Step 4's expense accounts with a code (`_payment_list`, as `/bill-codes`) as `{codes: [{code, name}], selected_codes}`, ticked from Petty Cash's `account_module` rows, else the old `entity_account_xero.is_active` (as the sheet set them). The save ticks exactly `selected_codes` in both tables (minty-petty-cash-api reads `entity_account_xero`): none is 400 "Pick at least one account code.", a code not listed 400, no expense account 409. COA_VIEW / COA_UPDATE. Tests: `test_petty_cash_codes.py`.
  - **`/bill-codes` follows Accounting Setup (owner, 2026-11-11):** the list is step 4's expense
    accounts (`accounting.payment_accounts`) - a Minty company's own (4.4), a Xero company's ticked
    in X3 (or by the old wizard) - with no Xero call. Each is ticked by its old
    `entity_bill_account_xero` row when one exists (what minty-payment-request-api reads), else by
    Payment Request's `account_module` row; `default_all` is true only when none has either.
    `POST` ticks exactly the posted codes among them (at least one: "Pick at least one account
    code.") and writes **both** tables - `account_module` PAYMENT_REQUEST (`is_active`, made
    for a Xero account that has none) and the old rows (made when missing, and turned off for any
    account step 4 doesn't list). A company with no listed account is 409 "Add an expense account
    in Accounting Setup first." **No default account code (owner, 2026-11-18):** neither route reads
    or writes `is_default`; a posted `default_code` is ignored.

## Accounting Setup - step 4 with Xero optional (`api_accounting.py`, `services/accounting.py`)

New 2026-11-10 (Figma `LGkVZqcyxSIh6pp5ihDjXw` node 348:1007; the design is Minty's
`docs/modernisation/xero_optional_database.md`; the columns are minty-db v0.5's EXPAND). Step 4
asks how Minty keeps the books; on its own, the company answers two questions and picks its own
accounts, with no Xero at all. Every route passes `entity_for_member` first, then the role check
(`COA_VIEW` to read, `COA_CREATE`/`COA_UPDATE`/`COA_DELETE` to change; refused is 403
`{"error": "Access denied"}`). Refusals are `{"error", "fields"?}` with Part 11 sentences: 400 for a
field, 409 for a code or name another active account holds.

| Route | Screen | What |
|---|---|---|
| `GET /accounting?entity_id=` | all of step 4 | the answers (`mode`, `business_type`, `uses_sales_cash`, `business_answered`), `accounts.sales`/`accounts.expense` (hidden system accounts left out; each row's `auto_filled` = an Auto Fill row nothing uses yet, what Revert would take out - 2026-11-11: a kept Cash Over / Short made Revert look as if it filled again), the five `slots` with their `options` (right types only), `slots_ready`, `next_code` per type, `types` |
| `POST /accounting/mode {mode}` | 4.1 | `minty` or `xero` -> `entities.setup_mode`. `minty` is refused (409) while a Xero org is linked. `xero` is free during onboarding (owner, 2026-11-11, no question asked): it removes every account Minty made for the company (`source` template or user, not the hidden system rows - the business type's set, Auto Fill's and the person's own; `_clear_own_chart`) and the daily-closing choices that pointed at them, in one transaction. Refused (409, nothing changed) only when a report expense or bill line already uses one: _This company's reports or bills already use its accounts in Minty, so it keeps using Minty on its own for now. Connect Xero from Settings once setup is finished._ (the hub's matching) |
| `POST /accounting/business {business_type, uses_sales_cash}` | 4.2 | `fnb`/`retail`/`service`/`general`, or null = "None of these"; the cash question is required for every company and "Not sure" arrives as `true`. On the Minty path the template's shared system rows (Petty Cash, Bank, Amount Due to Director) are added then, hidden, and fill their daily-closing accounts. Since 2026-11-11 (owner: no sales screen) the set's ticked sales accounts are added too (`_sales_from_set`), so Cash Sales fills its daily-closing account; changing the answer removes the old set's unused sales rows (and releases its cash-sale slot) before adding the new set's. The company's Cash sales method (`entity_sale_setting` for `sale_info.value_name = 'cash_sales'`) follows the answer: `false` ("We bank all sales") turns it off, `true` on (linked if missing); such a company's `GET /accounting` has no `cash_sale` slot (owner's brief, 2026-11-10) |
| `POST /accounting/sales-accounts {cash, electronic, delivery}` | the sales accounts screen (owner, 2026-11-17; Figma 464:73 row 1) | Three booleans, each required (400 otherwise); none true is 400 "No sales account is ticked yet. Tick at least one to continue." (`fields.sales_accounts`). COA_UPDATE. Screen only: no account is made or removed. `cash` is `uses_sales_cash` - a change runs 4.2's cash effects (the Cash method, and on the Minty path the set's sales rows and cash-sale slot); `electronic`/`delivery` are `entities.sells_electronic`/`sells_delivery` (minty-db v0.11). An unticked list's methods go as step 5's save would take them out (Minty's switched off, the company's own deleted unless a report used it). Answers the step's answers. `GET /accounting`, `/state`'s `accounting` and the sales-methods read carry `sells_cash`, `sells_electronic`, `sells_delivery` (in effect: NULL = the business type picks - Restaurant & Food both, retail/service/general Electronic only, "None of these" (no type, the cash question answered) neither - owner 2026-11-18; a company from before the questions both) and `sales_chosen`. On the Minty path each ticked account with no code yet is linked to its code (minty-db v0.12, owner 2026-11-17): Electronic Payment to 4001, Delivery to 4002 (4004 first for General), the sheet's row added when the set did not add it; a business-type change releases the set's links with its rows and links the new set's; a service business (no delivery row) links none |
| `GET /accounting/autofill?kind=` / `POST {kind, template_ids}` / `POST /accounting/autofill/revert {kind}` | 4.3, 4.4 | the company's template set for `sales` or `expense` (its business type, or General for "None of these"; its cash answer; its country or every country), ticked where the template says; adding skips a row already there and refuses one whose code or name is taken; each added account shows in the modules its row ticks (`pettycash_default` / `payment_request_default` -> `account_module` and the old `entity_account_xero` / `entity_bill_account_xero` rows, inactive where unticked), and a row with a `pettycash_role` (e.g. Cash Sales) fills that daily-closing account when it is still empty (2026-11-10, the accounting team's sheet); an account the person adds is in both modules; Revert removes the Auto Fill rows nothing uses. The templates are Minty's `docs/schema/seed_account_templates.sql` |
| `POST /accounting/accounts {code, name, type, description?, role?}` / `PATCH /accounting/accounts/{id}` / `DELETE /accounting/accounts/{id}` | 4.3b, 4.4a, 4.5a | one account: code (letters, numbers, dots, dashes; up to 10; a bank account may have none), name (up to 80 until CONTRACT widens it), type; `role` also chooses it for that daily-closing account. An account from Xero is changed in Xero (409). Removing deletes an unused account and refuses a used one (409, owner 2026-11-18); a system account can't be removed |
| `PUT /accounting/accounts {kind, template_ids, accounts: [{id?, code, name, type, description?}]}` | 4.4's Save & Next (2026-11-20; the screen holds every change until then, as X3 does) | The list as the screen holds it, applied in ONE transaction (`accounting.save_accounts`), reusing the per-change rules: every Minty-made account of `kind` (not a hidden system row, not from Xero) left out of `accounts` is removed as `DELETE` removes it - a used one refuses the WHOLE save, 409 with `USED_ACCOUNT` ("<name> is already used in your records or Petty Cash Settings, so it can't be deleted.", D1/D2); a row with its `id` is kept and, when a field changed, edited as `PATCH` edits it (a type outside `kind` is 400 "That type can't be used for this account. Choose one from the list."); `template_ids` are added as `POST /autofill` adds them (one already there is skipped); a row without an `id` is added as `POST /accounting/accounts` adds it. Order: removals, edits, Auto Fill rows, new rows (a code a removal frees can be taken). Any refusal - a field (400), a code or name taken (409), an id not among the company's own accounts of `kind` (404 "That account isn't in this company. Reload the list and try again."), a template not offered or a malformed body (400) - writes nothing. COA_UPDATE always, plus COA_DELETE when something is removed and COA_CREATE when something is added (the per-change routes' permissions). Answers the whole step (`GET /accounting`). The per-change routes stay |
| `POST /accounting/xero-accounts {kind, account_ids}` | X3 (Xero path, 2026-11-10; X2's sales screen removed 2026-11-11, the route still takes `sales`) | Exactly these of the synced Xero `sales`/`expense` accounts are Minty's: Petty Cash's `account_module` rows (`is_active`), and for expenses `entity_account_xero.is_active` too (`xero_lists._tick_codes`, the apps' old reader). 400 for none ticked ("Tick at least one sales account to continue."), a row of the wrong kind, a Minty-made or another company's row; 409 when not connected (a kept org after a disconnect is not). Answers the whole step (`GET /accounting`), whose rows now carry `used` |
| `POST /accounting/xero-refresh` | Refresh from Xero (X2-X4) | accounts-api's one sync (`core/xero_client.sync`), then the whole step again; 502 _Xero didn't send your accounts just now. Try Refresh again in a moment._; 409 when not connected |
| `POST /accounting/slots {role, account_id \| null}` | (4.5, removed from the wizard 2026-11-11; kept for the route's callers) | `petty_cash`, `bank`, `cash_sale`, `discrepancy`, `director` -> `entity_pettycash_settings`; an account of the wrong type is refused, and petty cash and the bank can't be one account. Petty cash also fills `discrepancy_bank_account_id` (as the old step 6 did; Petty Cash and finalize read it). Missing ones only warn: Petty Cash asks again before the first closing |

Expense accounts show under two headings from their type (`group`: `direct_costs` =
`cost_of_sales`, "Cost of what you sell"; `expense`/`overheads` = `running_costs`), never a stored
group (decision 19). **Until each module's own release moves it, an account made here is written
for the old readers too:** `account_info`'s old `type` (Xero's spelling), `xero_code` and `status`
beside Minty's `code`/`account_type`/`record_status`/`source`, and an expense account also gets
Petty Cash's tick (`entity_account_xero`), a Payment Request bill code (`entity_bill_account_xero`
with no Xero id, like a hand-added code) and its `account_module` rows.

The rest of the wizard follows `setup_mode`: `/state` carries `accounting` (the answers) and
moves a Minty-only company past step 4 once the business questions are answered (its accounts
are chosen inside step 4); finalize asks it for neither Xero nor the Xero-only accounts and
contacts, only the business answers, and it goes live as `disconnected` (the only live value
before CONTRACT) while the trigger keeps `xero_status = 'not_connected'`; step 8's bill codes are
its own expense accounts, with no Xero call. With a mode chosen (a Xero org sets `xero`), `/state`
counts Petty Cash's step (5) done once its opening balance is saved, since step 4 chose the
accounts (2026-11-11). Step 5's sales methods refuse a method named Cash (400 "Cash is set by your
answer in Accounting Setup."): Cash follows step 4's cash answer only.

## Invitations (`services/invites.py`)

`GET /invite` lists pending invitations (`USER_VIEW_ALL`) with `expires_at` and `expired`
(Step 3 shows "Expires in N days" / "Expired"), `POST /invite/cancel` marks one `revoked`
(never deleted). **Sending stays in Flask** (`POST /invite` and `POST /invite/resend` proxy -
resend gives a fresh link and expiry, with Flask's cooldown as a 429 `{error, retry_after}`)
— mail has no single-writer constraint, but the templates and the accept flow live there.

## Proxied to Flask (`api_modules.py`, `api_billing.py`, `core/minty_client.py`)

`POST /modules` (the map is a projection of subscription state — Flask owns it) and
`POST /xero/disconnect` are forwarded as the caller with Flask's own status code.

## Proxied to minty-subscription-api (`api_billing.py`, `core/subscription_client.py`)

Since 2026-10-06 `GET /payment-method` and the seven `billing/*` routes (`payment-methods`
GET, `payment-methods/setup-intent`, `payment-methods/confirm`, `payment-methods/default`,
`accounts` GET/POST, `authorize` — Stripe customers, SetupIntents, cards, consent; every card
is confirmed in-app onto a billing account; the hosted-Checkout `payment-method/setup` and
`/complete` proxies were removed 2026-10-01) go to `SUBSCRIPTION_API_URL` on the same paths.
`core/subscription_client.py` is the only module that calls that service; it reuses
`minty_client`'s transport (`base=`): the caller's bearer forwarded verbatim, the same timeout,
the same 502/504 answers.

## The sidebar's My Profile sheet (`api_me.py`, proxied since 2026-11-03)

The wizard draws `@minty/shared`'s sidebar, whose My Profile sheet reads and saves the
person's own profile and lists their subscriptions, and its cat button sends "Talk to Minty" messages. Neither Flask's nor the subscription API's
CORS admits the onboarding origin, so the wizard reaches them through four pass-through
proxies here (the caller's bearer forwarded, the upstream's status and body relayed unchanged;
nothing is validated here):

- `GET /me/profile[?entity=]` → Flask `GET /api/me/profile` (`entity` forwarded only when sent;
  Flask answers 403 for a company the person may not see).
- `PATCH /me/profile[?entity=]` → Flask `PATCH /api/me/profile`, the JSON body
  (`{first_name?, last_name?, email?}`) forwarded as is; Flask validates (400/422 with the
  sentence to show).
- `GET /me/subscriptions[?page=&per_page=]` → minty-subscription-api `GET /api/me/subscriptions`
  (paging forwarded only when sent, for that service to validate).
- `POST /me/contact` → Flask `POST /api/me/contact`, the "Talk to Minty" chat's message
  (`{topic, message, app, entity?}`) forwarded as is; Flask takes the sender from the token,
  validates and mails it (403/422/429/502 with the sentence to show).

They are not Flask onboarding paths. Both upstreams accept the onboarding-scoped token (Flask's
`hub_api.guard` and the subscription API's `SelfBearerAuth` check the signature and the person,
not `scope`).

## Finalize (`api_billing.py`, implemented here since 2026-10-06)

`POST /finalize {entity_id}` — called on arrival at All Set. Membership via
`entity_for_member` (400/403/404). First the setup rule (`setup_gaps`, from
`entity_setup_summary`, only when a module is on): a company keeping its books in Minty on its
own (Accounting Setup, above) needs only its business answers; otherwise Petty Cash needs Xero
plus daily closing's accounts (four for a company banking all its sales, else five; the
discrepancy bank follows petty cash) -> step 6, Account Code (2026-11-11: step 4 no longer maps
them), and the director, discrepancy and (with
cash sales only) cash-sale contacts -> step 7, read from `entity_pettycash_settings` (2026-11-11); Payment Request needs Xero plus at least one ticked bill account code (no default code since
2026-11-18). Anything missing is a 409 `{"error", "missing": [{"step",
"message"}]}` and the company stays `onboarding` - All Set lists each with a way back to its
step. Then a company still `onboarding` flips to `connected` (a Xero
org is linked) or `disconnected`; then **every** finalize calls the subscription API's
`POST /api/onboarding/trials/start {entity_id}`, so a retry reaches it. A non-200 answer fails
finalize with that service's own status and `error` sentence (the company stays live; the
wizard's Try again redoes the trial start); success is `{"status": "success", "trial_end":
iso | null}`. Both halves are idempotent. Flask's `/api/onboarding/finalize` no longer exists.

The trials it starts have **no subscriber** (2026-10-08): `trials/start` establishes no
payer, because a free trial commits nobody. The wizard's door onto the billing relationship
is the proxied `POST /billing/authorize` on step 2, and it may be skipped - nothing here
changes, but a company that skipped it goes live with trials that expire rather than convert
until an admin confirms billing in minty-web.


## Settings' accounts and contacts (`onboarding/internal.py`, `services/chart.py`; owner 2026-11-12)

The ONE implementation of a company's accounts and contacts add / edit / delete - the wizard's
account steps and the hub's Settings share it (`accounting._create`, `_apply_update`, `_retire`:
the checks, the old readers' rows, the module rows, the daily-closing slots). The hub reaches it
through accounts-api (`/api/me/company/accounts|contacts`), which calls
`/api/internal/chart/{accounts,contacts}[/<id>]` with a service assertion (scope `company-chart`,
claims `entity_id` and `user_id`; anything else 401). The person's rights are checked again here.

- **Who owns what.** Every row is the company's own copy, so any is edited or deleted (owner,
  2026-11-12): `source` `user`, `xero`, and `template` (its copies of Minty's starter chart and
  template contacts; Minty's `account_template` is never touched). Only the hidden system rows stay
  out. The read marks each row `own`, `source`, `in_xero`, `used`, and carries both codes (the user,
  2026-11-24 - Minty Code + Xero Code): `code` is Minty's, `xero_code` Xero's once the account is linked
  (empty otherwise); it also sends the types and each type's next free code (`minty_shared.accounts`).
- **Minty's accounts** (owner, 2026-11-12): the read's `minty_accounts` are the starter chart
  (`account_template`) for the company's country, every business type's, one per name, less the
  hidden system rows and any the company has; `POST {template_id}` adds one as Minty's copy
  (`accounting._from_template`: its module and daily-closing defaults, as Auto Fill); a code
  already taken is refused (409), one no longer offered is 404.
- **Live on Xero, no account is added, changed or deleted here** (the user, 2026-11-23): while
  `xero_connected`, `chart.add_account` / `edit_account` / `delete_account` and the wizard's
  `POST`/`PATCH`/`DELETE`/`PUT /accounting/accounts` answer 409 "This company is connected to Xero,
  so accounts are added and changed in Xero. Make the change there, and Minty follows." with no Xero
  call. The reads still carry the role's `permissions`; the page reads `xero_live` to hide the
  buttons. Accounts are never merged (the user, 2026-11-24); contacts keep accounts-api's Merge.
  Contacts are unaffected (below).
- **With or without Xero.** A company live on Xero (`xero_connected` + a token) gets each change in
  Xero first - `PUT /Accounts`, `POST /Accounts/{id}` (an edit, or `Status: ARCHIVED`), `POST
  /Contacts` (a new one, a rename or `ContactStatus: ARCHIVED`); Xero's refusal is a sentence
  (422, `core/xero_client.write`), unanswered 502, and nothing changes in Minty. A live company with
  no token: 409, reconnect. Not live: the company's own Minty rows; a row already linked to Xero
  waits for a reconnect (409).
- **A used account or contact can't be deleted** (owner, 2026-11-18): refused with 409, checked before
  Xero is told anything. Settings' refusal says what uses it and offers Archive (the user, 2026-11-27):
  "<name> is used by 12 expenses and 3 bills. Archive it instead." with `can_archive: true` (a bill counts
  once however many lines name it), or "<name> is used in Petty Cash Settings. Choose another account
  (contact) there first." with no archive; the reads carry the same sentence per used row (`used_note`,
  `can_archive`). The wizard's own delete keeps `USED_ACCOUNT` ("<name> is already used in your records
  or Petty Cash Settings, so it can't be deleted."). "Used" is accounts-api's rule (`xero/services/usage.py`): an expense line,
  a bill line (by id, or by code on a line saved before `bill_line.account_id`), daily closing's
  accounts or contacts, or a record combined into it (`merged_into_id`); accounting's `_used`/`_held`
  and chart's `_contacts_used`. An unused one is deleted (archived in Xero when linked). Rows
  archived before that day (the old rule) still come back when added again.
- **No duplicate contacts.** Names are unique per company among active ones (case and inner spaces
  ignored; 409 naming the existing one). A live company's new contact links to Xero's contact of
  that name - the stored copies, then a live `GET /Contacts?searchTerm=` narrowed to the exact name
  (owner's decisions doc C5, 2026-11-19) - before making one there as a supplier; a Minty-made
  contact met while live goes into Xero then (`find_or_make_contact`, also behind the wizard's
  `/contacts/create`). Xero not answering the search adds nothing (502).
- **Deleted, then added again: brought back, never a twin** (owner, 2026-11-12). An account added
  with the code, name and type of one the company deleted - or a contact with the name of one - is
  that one: Minty's archived row restored (its name kept; modules and old-reader rows on again), or,
  its row gone, the archived Xero copy linked to a new row; set ACTIVE in Xero when it is there
  ("<name> is back."). A delete marks Minty's copy (`xero_account` / `xero_contact`) archived at
  once, so nothing links to it meanwhile. Offline, a row still linked to Xero is refused: "<name>
  was archived in Xero. Reconnect Xero to bring it back."
- **Archive and restore** (the user, 2026-11-27): `POST .../accounts/<id>/archive` (COA_DELETE) sets
  `record_status = 'archived'` - Xero's `status` untouched, so it is never taken for an account Xero
  archived (A3) - and turns off its Petty Cash tick, bill code and module rows: out of every picker, still
  shown on old records (they read it by id). `.../restore` (COA_CREATE) brings it back, refused 409 when
  another account took its code or name. Contacts the same (`.../contacts/<id>/archive|restore`): off the
  supplier lists, refused 409 on a name another contact holds. Not while live on Xero (a linked contact
  while live: changed in Xero), nor one daily closing uses. The reads list them apart (`archived_accounts`,
  `archived_contacts`). A connected company's chart is Xero's: an account or contact archived while
  disconnected comes back at the sync (accounts-api `sync.apply_accounts` / `apply_contacts`).
- Tests: `tests/test_chart.py`.

## Tests

`tests/test_state.py`, `test_entity.py`, `test_pettycash.py`, `test_invites.py`,
`test_reference.py`, `test_accounting.py` (Accounting Setup; `PUT /accounting/accounts`: one call applies every change, any refusal writes nothing, Xero and system rows left alone, a cashier refused), `test_routes.py` (every path is
registered; 30 ported, 17 proxied, 47 in all, 2026-11-10), `test_me_proxies.py` (the My Profile proxies), `test_finalize.py`, `test_char_schema.py`
and `test_schema_name.py` (the schema), `test_settings_guard.py`; 399 passed + 1 skipped on
2026-10-06 (SQLite and Postgres) against Minty's `01_schema_rebased.sql` (`MINTY_REPO`). End to end:
`minty-onboarding-web/e2e` (`stack`, `resume`, `xero`, `walk`).

**Connecting Xero later (Settings Phase 2, 2026-11-11).** `accounting.minty_on_its_own` is
`setup_mode = 'minty'` alone - an organisation recorded while minty-accounts-api matches the
company's accounts leaves it Minty's until matching's Finish sets `'xero'`. Finalize never marks such
a company `connected` (only matching does, once its accounts and suppliers are in Xero).

## Accounts stay in Minty (the owner's decisions doc A1, A3, C4, 2026-11-19)

- **A1**: an account added while Xero is off (Settings' General, `chart.add_account`) is never created in
  Xero - it is mapped on the matching screen. Xero's archived account of the same code and name is neither
  restored nor linked. While live, no account is added here at all (the user, 2026-11-23).
- **Disconnected = Minty-only** (the user, 2026-11-27; replaces 2026-11-19's "linked accounts are changed
  in Xero only"): while the company is not live, `chart.edit_account` / `delete_account` and the wizard's
  `update_account` / `delete_account` change and delete a linked account like Minty's own. The stored link
  stays (`xero_account_id`, and Xero's code: `_apply_update` changes only Minty's code, `_bill_row` only
  the name and type of Xero's bill-code row) for the matching at reconnect, where Xero's details win.
  While a matching is open a linked row waits: 409 `MATCHING_OPEN` ("The Xero matching is still open.
  Finish or close it in Settings, under Integration, then try again."). The reads carry `matching_open`.
  Every add or edit while the company holds an organisation with Xero off marks the row
  (`minty_changed_at`, minty-db v0.21; `accounting.mark_offline_change`), so the reconnect opens the
  matching for it (accounts-api `matching.needs_matching`).
- **A3**: `GET .../accounts` rows carry `gone_from_xero`; a linked account Xero archived or removed (the
  sync archives it here too - a trigger follows its status) stays in the list while records use it, for
  Settings to ask the person to map it.
- **C4**: Minty never restores anything in Xero - a deleted account or contact added again comes back in
  Minty only; Xero's archived contact of the name is linked as it stands.
- **Linked contacts are changed in Xero only while live** (the user, 2026-11-19): `chart.edit_contact` /
  `delete_contact` refuse a contact with a Xero id while live - 409 "This contact comes from Xero. Change
  it in Xero, and Minty follows." With Xero off they are renamed and deleted here, marked as accounts are
  (the user, 2026-11-27); while a matching is open they wait (`MATCHING_OPEN`).
