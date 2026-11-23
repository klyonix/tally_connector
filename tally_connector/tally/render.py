"""
Model -> Tally XML. Pure: no frappe, no database, no network.

Everything this module knows about Tally is written down in `docs` strings and
in `ASSUMPTIONS` below, because each of those cost a failed import to learn.

Deterministic by construction - stable ordering, fixed decimal formatting, no
timestamps in the payload - which is what makes a golden file a useful test
rather than a flaky one.
"""

from __future__ import annotations

import datetime

from decimal import Decimal

from lxml import etree

from tally_connector.tally.extract_rules import tally_key

from tally_connector.tally.model import (
    ADVANCE,
    AGAINST_REF,
    INVOICE_KINDS,
    NEW_REF,
    Export,
    Masters,
    Voucher,
    money,
)

# Learned the hard way; each line is an import that failed until it was obeyed.
ASSUMPTIONS = {
    "abort": "Tally aborts at the first line it cannot parse and reports only "
             "that line. One bad record loses the whole file.",
    "order": "Masters must import before vouchers, as two files: REPORTNAME "
             "'All Masters' then 'Vouchers'. A masters file is finished only "
             "when Tally.imp says Errors: 0 - a Created count is not.",
    "message": "One TALLYMESSAGE per object.",
    "date": "Dates are YYYYMMDD with no separators, and must fall inside the "
            "target company's open books.",
    "sign": "Negative amount is a debit, positive is a credit, and a voucher's "
            "amounts must sum to zero.",
    "deemed": "ISDEEMEDPOSITIVE flags the debit side. It is derived from the "
              "sign, never stored separately - storing both is how they disagree.",
    "invoice_view": "A voucher carrying stock must declare ISINVOICE and the "
                    "invoice view, or Tally answers 'Can't mix Stock Items and "
                    "Accounting Entries'.",
    "rate": "Rate carries its unit: 15000.00/nos. Quantity is '1 nos'.",
    "billwise": "A ledger with ISBILLWISEON obliges its vouchers to carry "
                "BILLALLOCATIONS.LIST, else the amount posts to On Account.",
    "remoteid": "REMOTEID is optional and not written. A working import "
                "carried the ERPNext name in it and Tally took it; it was "
                "never the cause of a refusal. Correlation is VOUCHERNUMBER.",
    "guid": "GUID is what TallyPrime's 'Overwrite voucher when a voucher with "
            "same GUID exists' matches on, so a second import of the same file "
            "replaces rather than duplicates. Derived from the ERPNext "
            "document, because a value that changes each run buys nothing.",
    "opening": "OPENINGBALANCE on a ledger carries the position before the "
               "exported vouchers begin, in Tally's own sign. Without it a "
               "company built from these masters starts every ledger at nought.",
    "gstrate": "An item's GST rate is GSTDETAILS.LIST > STATEWISEDETAILS.LIST "
               "> RATEDETAILS.LIST, one block per duty head, integrated whole "
               "and central and state each half. Missing, the import succeeds "
               "and the voucher lands in Uncertain Transactions, out of GSTR-1 "
               "until somebody fixes it by hand.",
    "company": "SVCURRENTCOMPANY must match the Tally company name exactly.",
    "escaping": "& and < must be escaped. lxml does this; hand-built XML is "
                "where it breaks.",
}

# How long a NARRATION Tally will take.
#
# Measured off TallyPrime's own export of a real company: 514 narrations, the
# longest 120 characters and the **median also 120**, which is what truncation
# looks like from outside. This connector was sending up to 1,456 - a payment
# with every allocation written out - and a live TallyPrime answered
# `Software Exception c0000005 (Memory Access Violation)` while importing
# vouchers, which is what a long string into a fixed buffer does.
#
# Truncated rather than dropped, and marked, because a narration that stops
# mid-sentence with no sign of it is worse than one that says it was cut.
NARRATION_MAX = 120
NARRATION_CUT = "..."

MASTERS_REPORT = "All Masters"
# "Vouchers", which is what Tally's documentation names as the import
# definition for vouchers, and what a working import used: 16,261 vouchers
# created under it on a real company's books, 0 errors.
#
# This was changed to "All Masters" on 9 September on the reasoning that
# Tally's own voucher *export* declares it, with a comment saying "Vouchers"
# imports nothing. That comment was wrong, and so was the reasoning - an export
# file's envelope is not evidence about the import request. The vouchers imports
# were failing because the masters file had never finished, which nobody could
# see until Tally.imp was read.
VOUCHERS_REPORT = "Vouchers"

# What Tally does when an imported master already exists.
#
# **The default is to send nothing**, and that is a correction rather than a
# preference. This used to default to `@@DUPIGNORECOMBINE`, a string that
# appears in no TallyPrime export, no Tally sample, no Tally documentation and
# no working implementation - it was measured to exist in exactly four files on
# the machine that produced it, and all four were this connector's own output.
# It reads as two option names run together.
#
# What a real masters import did with it is on the record: `Created: 9,626,
# Altered: 9,694` against a customer's live company. Nine and a half thousand
# masters an accountant maintains were overwritten by ours, which is what an
# unhonoured "ignore" directive looks like from the outside.
#
# Omitting the directive is the only configuration with positive evidence
# behind it: the reference implementation that imported 16,261 vouchers sends
# no IMPORTDUPS at all. The two tokens below follow Tally's documented
# `@@DUP<WORD>` form and stay available as explicit choices, for somebody who
# has decided they want that and can watch what it does.
#
# The real answer is not to send masters that already exist, which is what the
# destination snapshot is for.
DUP_NONE = ""                       # send no directive - the default
DUP_MODIFY = "@@DUPMODIFY"          # replace it with what is being imported
DUP_COMBINE = "@@DUPCOMBINE"        # add the opening balances together
DUP_IGNORE = DUP_NONE               # kept so existing callers keep working
DUPLICATE_BEHAVIOURS = (DUP_NONE, DUP_MODIFY, DUP_COMBINE)

CREATE = "Create"
ALTER = "Alter"

# There is no Cancel and no Delete, deliberately.
#
# Only a submitted document goes to Tally, so a cancelled
# one is never sent in the first place. A file that cancels vouchers Tally
# already holds was built and removed the same day: it stops accounting
# somebody may be relying on, and nothing here has ever been proved against a
# real Tally. What remains is the knowing - `sync_ledger.stale()` reports a
# voucher Tally holds that ERPNext has withdrawn - and a person cancels it in
# Tally, where they can see what it affects.

# Tally has no upsert.
#
# `Create` always creates and `Alter` addresses a voucher that already exists,
# by TAGNAME and TAGVALUE, failing on one Tally has never seen. So no single
# mode is right for both a first import and a re-import, and pretending
# otherwise is how a ledger ends up with everything twice. The mode is chosen
# deliberately.
#
# Two things catch a repeat, and this used to name only the second. **The GUID**
# every voucher carries, derived from its ERPNext document: TallyPrime's
# "Overwrite voucher when a voucher with same GUID exists" reads it, and with
# that on a second `Create` of the same file replaces rather than duplicates.
# And **the target's own configuration** - the voucher type numbered Manually
# with Prevent Duplicates set - which `check_target_company` still reports,
# because it is the fallback where nobody has switched the first one on.
VOUCHER_ACTIONS = (CREATE, ALTER)


def _amount(value: Decimal) -> str:
    """Tally reads the sign off the number itself."""
    return f"{money(value):.2f}"


def _qty(quantity: Decimal, unit: str) -> str:
    return f"{money(quantity):.2f} {unit}".strip()


def _tally_date(value) -> str:
    return value.strftime("%Y%m%d")


def _sub(parent, tag, text=None, **attrib):
    el = etree.SubElement(parent, tag, **attrib)
    if text is not None and text != "":
        el.text = str(text)
    return el


def _envelope(company: str, report: str, duplicates: str | None = None):
    """The ENVELOPE, returning the node objects hang off."""
    root = etree.Element("ENVELOPE")
    header = etree.SubElement(root, "HEADER")
    _sub(header, "TALLYREQUEST", "Import Data")
    body = etree.SubElement(root, "BODY")
    importdata = etree.SubElement(body, "IMPORTDATA")
    desc = etree.SubElement(importdata, "REQUESTDESC")
    _sub(desc, "REPORTNAME", report)
    static = etree.SubElement(desc, "STATICVARIABLES")
    if duplicates:
        _sub(static, "IMPORTDUPS", duplicates)
    _sub(static, "SVCURRENTCOMPANY", company)
    return root, etree.SubElement(importdata, "REQUESTDATA")


def _message(requestdata):
    return etree.SubElement(requestdata, "TALLYMESSAGE", nsmap={"UDF": "TallyUDF"})


def _to_string(root) -> str:
    return etree.tostring(
        root, pretty_print=True, xml_declaration=True, encoding="UTF-8"
    ).decode("utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Addresses
# ─────────────────────────────────────────────────────────────────────────────

def _address_block(parent, address, list_tag, line_tag):
    if not address or not address.lines:
        return
    el = etree.SubElement(parent, list_tag, TYPE="String")
    for line in address.lines:
        _sub(el, line_tag, line)


# Tally keeps a ledger's GST registration, its mailing details, and GST and HSN
# details as dated collections, and every filled one opens with the date it
# takes effect from: 10,969 of 10,969 blocks in a real company's whole-year
# export, 0 exceptions. This export had sent none. The floor is the start of the
# financial year GST began in, used only when the extract could not read a
# fiscal year - early is the safe direction, since a voucher dated before a
# registration's APPLICABLEFROM sees no registration at all.
APPLICABLE_FROM_FLOOR = datetime.date(2017, 4, 1)


# What Tally calls each half of GST, and the row that covers every state.
GST_DUTY_HEADS = ("Central Tax", "State Tax", "Integrated Tax")
ANY_STATE = "Any"


def _rate_details(gst, rate):
    """The GST rate, per duty head, the way Tally nests it.

    `GSTDETAILS.LIST > STATEWISEDETAILS.LIST > RATEDETAILS.LIST`, one
    RATEDETAILS block per duty head. Without it an item arrives carrying an HSN
    code and the word `Taxable` and no rate at all. That does not fail the
    import - the voucher's own tax ledgers still carry their amounts, so the
    accounting is right - and it drops the voucher into Tally's Uncertain
    Transactions, where it is left out of GSTR-1 until somebody fixes it by
    hand. A wrong answer nobody sees until filing.

    The integrated rate is the whole of it and the central and state rates are
    each half, which is Tally's arithmetic and india_compliance's alike.
    """
    if rate is None:
        return
    states = etree.SubElement(gst, "STATEWISEDETAILS.LIST")
    _sub(states, "STATENAME", ANY_STATE)
    for head in GST_DUTY_HEADS:
        share = rate if head == "Integrated Tax" else rate / 2
        details = etree.SubElement(states, "RATEDETAILS.LIST")
        _sub(details, "GSTRATEDUTYHEAD", head)
        _sub(details, "GSTRATEVALUATIONTYPE", "Based on Value")
        _sub(details, "GSTRATE", f"{money(share):.2f}")


def _narration(text):
    """A narration Tally can hold, cut where it has to be and saying so."""
    text = (text or "").strip()
    if len(text) <= NARRATION_MAX:
        return text
    return text[:NARRATION_MAX - len(NARRATION_CUT)].rstrip() + NARRATION_CUT


def _dated(parent, tag, when):
    """A dated collection, opened with the date it applies from."""
    el = etree.SubElement(parent, tag)
    _sub(el, "APPLICABLEFROM", _tally_date(when or APPLICABLE_FROM_FLOOR))
    return el


# ─────────────────────────────────────────────────────────────────────────────
# Masters
# ─────────────────────────────────────────────────────────────────────────────

def _emitter(resolution):
    """Given a master, the ACTION to send it with - or None to send nothing.

    With no resolution everything is a Create, which is what this did before
    the destination was ever asked. With one, a master Tally already holds is
    not in the file at all: that is what ends the 9,694 silent alterations a
    real import recorded against a customer's live company.

    Either way a master Tally cannot name is left out, because it has no
    identity there to be created under. `tally_key` ignores case and every
    non-alphanumeric, so a customer called `....` reduces to the empty string -
    and a live push proved what Tally does with one: `0 errors and 1
    exceptions`, naming nothing, with the rest of the file lost behind it.
    Leaving it out is not a quiet drop; `validate` reports it by name, and
    blocks outright when a voucher posts to it.
    """
    if resolution is None:
        return lambda _kind, name: "Create" if tally_key(name) else None

    emitted = {kind: resolution.emitted(kind)
               for kind in ("groups", "ledgers", "units", "stock_groups",
                            "stock_items", "godowns", "cost_centres")}

    def action(kind, name):
        if not tally_key(name):
            return None
        return emitted.get(kind, {}).get(name)

    return action


def render_masters(company: str, masters: Masters,
                   duplicates: str = DUP_NONE, resolution=None) -> str:
    """Groups, then ledgers, then units, stock groups and items.

    Order matters within the file as well as between the two files: a child
    names its parent, and Tally has to have seen the parent.
    """
    root, data = _envelope(company, MASTERS_REPORT, duplicates)
    action_for = _emitter(resolution)

    for group in masters.groups:
        action = action_for("groups", group.name)
        if not action:
            continue
        el = etree.SubElement(_message(data), "GROUP",
                              attrib={"NAME": group.name, "ACTION": action})
        _sub(el, "NAME", group.name)
        _sub(el, "PARENT", group.parent)

    for unit in masters.units:
        action = action_for("units", unit.symbol)
        if not action:
            continue
        el = etree.SubElement(_message(data), "UNIT",
                              attrib={"NAME": unit.symbol, "ACTION": action})
        _sub(el, "NAME", unit.symbol)
        _sub(el, "ISSIMPLEUNIT", "Yes")
        # FORMALNAME is the long name. This used to put it in ORIGINALNAME,
        # which is Tally's field for the name a master is being renamed *from* -
        # so it read as a rename of a unit the company has never had.
        if unit.formal_name and unit.formal_name != unit.symbol:
            _sub(el, "FORMALNAME", unit.formal_name)
        _sub(el, "DECIMALPLACES", str(unit.decimal_places))

    # Cost centres before any voucher may name one. Tally files them under a
    # cost category and ships exactly one, so the category is referenced rather
    # than created.
    for centre in masters.cost_centres:
        action = action_for("cost_centres", centre.name)
        if not action:
            continue
        el = etree.SubElement(_message(data), "COSTCENTRE",
                              attrib={"NAME": centre.name, "ACTION": action})
        _sub(el, "NAME", centre.name)
        _sub(el, "PARENT", centre.parent)
        _sub(el, "CATEGORY", centre.category)

    for godown in masters.godowns:
        action = action_for("godowns", godown.name)
        if not action:
            continue
        el = etree.SubElement(_message(data), "GODOWN",
                              attrib={"NAME": godown.name, "ACTION": action})
        _sub(el, "NAME", godown.name)
        _sub(el, "PARENT", godown.parent)

    for sg in masters.stock_groups:
        action = action_for("stock_groups", sg.name)
        if not action:
            continue
        el = etree.SubElement(_message(data), "STOCKGROUP",
                              attrib={"NAME": sg.name, "ACTION": action})
        _sub(el, "NAME", sg.name)
        if sg.parent:
            _sub(el, "PARENT", sg.parent)

    for item in masters.stock_items:
        action = action_for("stock_items", item.name)
        if not action:
            continue
        el = etree.SubElement(_message(data), "STOCKITEM",
                              attrib={"NAME": item.name, "ACTION": action})
        _sub(el, "NAME", item.name)
        if item.parent:
            _sub(el, "PARENT", item.parent)
        if item.base_unit:
            _sub(el, "BASEUNITS", item.base_unit)
        _sub(el, "DESCRIPTION", item.description)
        _sub(el, "GSTTYPEOFSUPPLY", item.type_of_supply)
        _sub(el, "GSTAPPLICABLE", "Applicable" if item.gst_applicable else "Not Applicable")
        # Taxability and HSN are two dated collections in TallyPrime, not one.
        # This sent HSNCODE inside GSTDETAILS.LIST, which is the Tally.ERP 9
        # shape: across a real company's whole year HSNCODE appears nowhere in
        # GSTDETAILS, and HSN sits in HSNDETAILS.LIST - 6,088 blocks. Its source
        # has to say the code is given here, or Tally uses the group's instead.
        if item.hsn_code or item.taxability or item.gst_rate is not None:
            gst = _dated(el, "GSTDETAILS.LIST", masters.applicable_from)
            _sub(gst, "TAXABILITY", item.taxability or "Taxable")
            _rate_details(gst, item.gst_rate)
        if item.hsn_code:
            hsn = _dated(el, "HSNDETAILS.LIST", masters.applicable_from)
            _sub(hsn, "SRCOFHSNDETAILS", "Specify Details Here")
            _sub(hsn, "HSNCODE", item.hsn_code)
        _sub(el, "ISDELETED", "No")

    for ledger in masters.ledgers:
        action = action_for("ledgers", ledger.name)
        if not action:
            continue
        el = etree.SubElement(_message(data), "LEDGER",
                              attrib={"NAME": ledger.name, "ACTION": action})
        _sub(el, "NAME", ledger.name)
        _sub(el, "PARENT", ledger.parent)
        # Only when something names one. A ledger with no CURRENCYNAME takes the
        # company's base currency, which is the currency of every figure this
        # export sends - so the ordinary case is to say nothing.
        if ledger.currency:
            _sub(el, "CURRENCYNAME", ledger.currency)
        _sub(el, "ISBILLWISEON", "Yes" if ledger.bill_wise else "No")
        _sub(el, "AFFECTSSTOCK", "Yes" if ledger.affects_stock else "No")
        # Tally refuses a cost centre allocation on a ledger that has not got
        # cost centres switched on, so the ledger has to say so.
        _sub(el, "ISCOSTCENTRESON", "Yes" if ledger.cost_centres_on else "No")
        # Tally's own sign, which is this export's throughout: negative is a
        # debit. Sent only where there is one, because an OPENINGBALANCE of
        # nought and no OPENINGBALANCE at all say the same thing to Tally.
        if ledger.opening_balance:
            _sub(el, "OPENINGBALANCE", _amount(ledger.opening_balance))
        if ledger.tax_type:
            # A GST ledger that does not say so is an ordinary duty ledger to
            # Tally, and its GST reports come back empty however correct the
            # postings are.
            _sub(el, "TAXTYPE", ledger.tax_type)
            if ledger.duty_head:
                _sub(el, "GSTDUTYHEAD", ledger.duty_head)
        address = ledger.address
        if address:
            if address.state:
                _sub(el, "PRIORSTATENAME", address.state)
            if address.country:
                _sub(el, "COUNTRYOFRESIDENCE", address.country)
            if address.gstin:
                gst = _dated(el, "LEDGSTREGDETAILS.LIST", masters.applicable_from)
                _sub(gst, "GSTREGISTRATIONTYPE", address.registration_type or "Regular")
                if address.state:
                    _sub(gst, "PLACEOFSUPPLY", address.state)
                _sub(gst, "GSTIN", address.gstin)
            # MAILINGNAME before the address, which is the order both a real
            # export and a working one use.
            mailing = _dated(el, "LEDMAILINGDETAILS.LIST", masters.applicable_from)
            _sub(mailing, "MAILINGNAME", ledger.name)
            _address_block(mailing, address, "ADDRESS.LIST", "ADDRESS")
            _sub(mailing, "STATE", address.state)
            _sub(mailing, "COUNTRY", address.country)
            _sub(mailing, "PINCODE", address.pincode)

    return _to_string(root)


# ─────────────────────────────────────────────────────────────────────────────
# Vouchers
# ─────────────────────────────────────────────────────────────────────────────

def _ledger_entry(parent, line, tag, no_cost_centres=frozenset()):
    el = etree.SubElement(parent, tag)
    _sub(el, "LEDGERNAME", line.ledger)
    # Derived from the sign, never stored beside it.
    _sub(el, "ISDEEMEDPOSITIVE", "Yes" if line.is_debit else "No")
    if line.is_party:
        _sub(el, "ISPARTYLEDGER", "Yes")
    _sub(el, "AMOUNT", _amount(line.amount))

    for ref in line.bill_refs:
        bill = etree.SubElement(el, "BILLALLOCATIONS.LIST")
        _sub(bill, "NAME", ref.name)
        _sub(bill, "BILLTYPE", ref.kind)
        if ref.kind in (NEW_REF, ADVANCE) and ref.due_date:
            _sub(bill, "BILLCREDITPERIOD", ref.due_date.strftime("%d-%b-%Y"))
        _sub(bill, "AMOUNT", _amount(ref.amount))

    # A cost centre allocation on a ledger whose own setting in Tally is off is
    # refused, and the refusal costs the whole voucher. Our master would have
    # switched the flag on - but a master the destination already holds is not
    # sent any more, so its setting stays theirs. On a real company 313 of 357
    # ledgers have cost centres off, and this is what stopped every voucher
    # importing after the export learnt to reuse masters.
    #
    # The allocation is left out and the voucher goes. It is analysis rather
    # than accounting - the amounts are unchanged - and the validator names
    # every line that lost one, so nothing vanishes quietly.
    for cc in (() if line.ledger in no_cost_centres else line.cost_centres):
        alloc = etree.SubElement(el, "COSTCENTREALLOCATIONS.LIST")
        _sub(alloc, "NAME", cc.name)
        _sub(alloc, "AMOUNT", _amount(cc.amount))

    if line.cleared_on:
        bank = etree.SubElement(el, "BANKALLOCATIONS.LIST")
        _sub(bank, "DATE", _tally_date(line.cleared_on))
        _sub(bank, "BANKERSDATE", _tally_date(line.cleared_on))
        _sub(bank, "TRANSACTIONTYPE", line.instrument or "Cheque/DD")
        _sub(bank, "AMOUNT", _amount(line.amount))
    return el


def render_voucher(parent, voucher: Voucher, action: str = CREATE,
                   with_guid: bool = False, no_cost_centres=frozenset()):
    # OBJVIEW is an attribute here and not a child element, which is where
    # Tally's own export puts it.
    #
    # No REMOTEID. It was removed on a wrong guess while the real failure - a
    # masters file that never finished - was invisible, and a later working
    # import carried an ERPNext name in it without complaint. It stays out
    # because nothing is gained by it: `VOUCHERNUMBER` is the same ERPNext
    # document name, it is what an alteration addresses a voucher by, and it is
    # the one a person reads in Tally.
    attrib = {"VCHTYPE": voucher.kind, "ACTION": action,
              "OBJVIEW": ("Invoice Voucher View" if voucher.is_invoice
                          else "Accounting Voucher View")}
    if action == ALTER:
        # An alteration has to say which voucher it is altering. The voucher
        # number is the ERPNext document name, which is unique and stable, so it
        # addresses exactly one.
        attrib["TAGNAME"] = "Voucher Number"
        attrib["TAGVALUE"] = voucher.number
    el = etree.SubElement(parent, "VOUCHER", attrib=attrib)

    _sub(el, "DATE", _tally_date(voucher.date))
    _sub(el, "EFFECTIVEDATE", _tally_date(voucher.date))
    # Written where Tally writes it, between the date and the type. It is what
    # makes a second import of this file an overwrite rather than a second set
    # of vouchers, on a TallyPrime with "Overwrite voucher when a voucher with
    # same GUID exists" switched on.
    if with_guid and voucher.tally_guid:
        _sub(el, "GUID", voucher.tally_guid)
    _sub(el, "VOUCHERTYPENAME", voucher.kind)
    _sub(el, "VOUCHERNUMBER", voucher.number)
    if voucher.reference:
        _sub(el, "REFERENCE", voucher.reference)

    if voucher.party:
        _sub(el, "PARTYLEDGERNAME", voucher.party)
        _sub(el, "PARTYNAME", voucher.party)
        # What Tally's own export calls the party on an invoice, beside
        # PARTYNAME rather than instead of it.
        _sub(el, "BASICBUYERNAME", voucher.party)

    address = voucher.party_address
    if address:
        if address.country:
            _sub(el, "COUNTRYOFRESIDENCE", address.country)
        if address.state:
            _sub(el, "STATENAME", address.state)
            _sub(el, "PLACEOFSUPPLY", address.state)
        if address.gstin:
            _sub(el, "PARTYGSTIN", address.gstin)
            # GSTREGISTRATIONTYPE, not PARTYTAXREGISTRATIONTYPE. The name this
            # used appears **nought** times in 300 MB of Tally's own export -
            # two files, one demo and one real company's whole year - while
            # GSTREGISTRATIONTYPE appears 1,160 times at voucher level. It was
            # a field of ours, on Tally's object.
            _sub(el, "GSTREGISTRATIONTYPE", address.registration_type or "Regular")
        # BASICBUYERADDRESS.LIST, not ADDRESS.LIST. `Address` is a field of
        # Tally's Ledger object and not of its Voucher, and this put one on
        # every voucher with a party on it. Tally's own voucher export carries
        # BASICBUYERNAME and a GSTBUYERADDRESS.LIST and no bare ADDRESS.LIST at
        # all, and the Excel template calls the column "Buyer/Supplier -
        # Address" - three sources, one answer.
        _address_block(el, address, "BASICBUYERADDRESS.LIST", "BASICBUYERADDRESS")

    _sub(el, "NARRATION", _narration(voucher.narration))

    # An inventory voucher must say so, or Tally refuses to mix stock with
    # accounting entries. An accounting-only voucher must say the opposite.
    if voucher.is_invoice:
        _sub(el, "ISINVOICE", "Yes")
        _sub(el, "PERSISTEDVIEW", "Invoice Voucher View")
    else:
        _sub(el, "PERSISTEDVIEW", "Accounting Voucher View")

    for inv in voucher.inventory:
        entry = etree.SubElement(el, "ALLINVENTORYENTRIES.LIST")
        _sub(entry, "STOCKITEMNAME", inv.item)
        _sub(entry, "ISDEEMEDPOSITIVE", "Yes" if inv.amount < 0 else "No")
        _sub(entry, "RATE", f"{money(inv.rate):.2f}/{inv.unit}")
        _sub(entry, "AMOUNT", _amount(inv.amount))
        _sub(entry, "ACTUALQTY", _qty(inv.quantity, inv.unit))
        _sub(entry, "BILLEDQTY", _qty(inv.quantity, inv.unit))
        if inv.godown or inv.batch:
            batch = etree.SubElement(entry, "BATCHALLOCATIONS.LIST")
            _sub(batch, "GODOWNNAME", inv.godown or "Main Location")
            _sub(batch, "BATCHNAME", inv.batch or "Primary Batch")
            _sub(batch, "AMOUNT", _amount(inv.amount))
            _sub(batch, "ACTUALQTY", _qty(inv.quantity, inv.unit))
            _sub(batch, "BILLEDQTY", _qty(inv.quantity, inv.unit))
        alloc = etree.SubElement(entry, "ACCOUNTINGALLOCATIONS.LIST")
        _sub(alloc, "LEDGERNAME", inv.ledger)
        _sub(alloc, "ISDEEMEDPOSITIVE", "Yes" if inv.amount < 0 else "No")
        _sub(alloc, "AMOUNT", _amount(inv.amount))

    # The ledger tag follows the voucher's mode, because Tally's own writer
    # does and the two modes disagree.
    #
    # On an accounting voucher this was measured against a live TallyPrime -
    # the same voucher, changing nothing but the tag:
    #
    #     accounting, ALLLEDGERENTRIES.LIST   created=1
    #     accounting, LEDGERENTRIES.LIST      created=0, EXCEPTIONS=1
    #
    # and that exception is `Software Exception c0000005 (Memory Access
    # Violation)` - Tally falls over rather than refusing, which is why the
    # gateway returned a count with no LINEERROR and nothing could be read out
    # of it.
    #
    # **That measurement was then over-generalised, by me, to both modes**, and
    # the note claiming so was wrong: both measured lines are accounting
    # vouchers and the invoice case was never tested. Every invoice-mode
    # voucher failed afterwards. What settled it is TallyPrime's own export of
    # the destination company - 4,682 vouchers it wrote itself, split with no
    # exceptions at all:
    #
    #     invoice    (ISINVOICE=Yes)   1,141   LEDGERENTRIES.LIST
    #     accounting                   3,541   ALLLEDGERENTRIES.LIST
    #
    # So the measurement stands for the mode it was taken in, and the other
    # mode takes Tally's own answer rather than the generalisation.
    tag = "LEDGERENTRIES.LIST" if voucher.is_invoice else "ALLLEDGERENTRIES.LIST"
    for line in voucher.lines:
        _ledger_entry(el, line, tag, no_cost_centres)
    return el


def render_vouchers(company: str, vouchers, action: str = CREATE,
                    with_guid: bool = False, no_cost_centres=frozenset()) -> str:
    """The voucher file.

    `with_guid` is off by default, and that is a retreat rather than a
    preference. A GUID is what TallyPrime's "Overwrite voucher when a voucher
    with same GUID exists" matches on, so it is how a re-import is told from a
    second set of vouchers - but the one this connector mints is a bare UUID
    and Tally's own are a UUID followed by an eight-hex suffix
    (`...c5079b964709-0000a46e`). A live TallyPrime crashed importing vouchers
    that carried ours, with `Software Exception c0000005 (Memory Access
    Violation)`, which is the shape of a parser walking off the end of a
    structure it expected to be longer.

    That is a hypothesis and not a proof - the crash has not yet been bisected
    to this field. It is off until it has been, because the alternative is
    sending an unverified structure into something that has already crashed
    once, and the sync ledger prevents duplicates without Tally's help anyway.
    """
    root, data = _envelope(company, VOUCHERS_REPORT)
    for voucher in vouchers:
        render_voucher(_message(data), voucher, action, with_guid=with_guid,
                       no_cost_centres=no_cost_centres)
    return _to_string(root)


def render(export: Export, duplicates: str = DUP_NONE,
           action: str = CREATE) -> tuple[str, str]:
    """The two documents, in the order they must be imported."""
    return (
        render_masters(export.company, export.masters, duplicates),
        render_vouchers(export.company, export.vouchers, action),
    )
