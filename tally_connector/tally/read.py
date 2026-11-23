"""
Tally XML back into the model, so the validator can check a file that already
exists.

This is what makes stage 2 useful before stage 3. The live export still runs
through the original generators, which do not know the model - but their output
is Tally XML, and Tally XML parses into the model perfectly well. So there is one
validator with two ways in:

    fixtures ------------> model -> validate     (offline, no site)
    generated XML -> read -> model -> validate   (the real export, today)

Pure, like everything else here. It reads what an exporter wrote; it does not
care which exporter wrote it.
"""

from __future__ import annotations

import datetime
import re

from lxml import etree

from tally_connector.tally.model import (
    Address,
    CostCentre,
    Godown,
    BillRef,
    CostCentreAllocation,
    Export,
    InventoryLine,
    LedgerLine,
    Ledger,
    Masters,
    StockGroup,
    StockItem,
    Unit,
    Group,
    Voucher,
    money,
)

# `10.00 Nos` or `10 Nos`; and `1000.00/Nos`.
QTY = re.compile(r"^\s*(-?[\d.]+)\s*(.*)$")
RATE = re.compile(r"^\s*(-?[\d.]+)\s*/\s*(.*)$")


def _text(el, tag, default=""):
    found = el.findtext(tag)
    return default if found is None else found


def _amount(el, tag="AMOUNT"):
    """Tally's own numbers, taken as written.

    A malformed value - `--1234.50`, the shape a return produced - is kept as zero here
    and reported by the amount check, rather than raising. A reader that throws
    tells you one thing about one line; the validator's job is the whole list.
    """
    raw = (el.findtext(tag) or "0").strip()
    try:
        return money(raw)
    except Exception:
        return money(0)


def malformed_amounts(xml: str) -> list[str]:
    """Values in the file that are not numbers at all.

    Kept separate because `_amount` cannot report them - by the time it has
    coerced one to zero, the evidence is gone.
    """
    root = etree.fromstring(xml.encode("utf-8"))
    bad = []
    for el in root.iter("AMOUNT"):
        raw = (el.text or "0").strip()
        try:
            money(raw)
        except Exception:
            bad.append(raw)
    return bad


def _date(value):
    value = (value or "").strip()
    if len(value) == 8 and value.isdigit():
        return datetime.date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    return None


def _split_qty(raw):
    m = QTY.match(raw or "")
    if not m:
        return money(0), ""
    return money(m.group(1) or 0), (m.group(2) or "").strip()


def _split_rate(raw):
    m = RATE.match(raw or "")
    if not m:
        return money(0), ""
    return money(m.group(1) or 0), (m.group(2) or "").strip()


def _address(el):
    # A voucher carries the buyer's address, a ledger carries its own mailing
    # address, and both spellings are read so one reader serves both files.
    lines = [e.text for e in el.findall("ADDRESS.LIST/ADDRESS") if e.text]
    lines += [e.text for e in el.findall("BASICBUYERADDRESS.LIST/BASICBUYERADDRESS")
              if e.text]
    lines += [e.text for e in el.findall("LEDMAILINGDETAILS.LIST/ADDRESS.LIST/ADDRESS") if e.text]
    return Address(
        lines=lines,
        state=_text(el, "PRIORSTATENAME") or _text(el, "STATENAME")
              or _text(el, "LEDMAILINGDETAILS.LIST/STATE"),
        country=_text(el, "COUNTRYOFRESIDENCE") or _text(el, "LEDMAILINGDETAILS.LIST/COUNTRY"),
        pincode=_text(el, "LEDMAILINGDETAILS.LIST/PINCODE"),
        gstin=_text(el, "LEDGSTREGDETAILS.LIST/GSTIN") or _text(el, "PARTYGSTIN"),
        registration_type=_text(el, "LEDGSTREGDETAILS.LIST/GSTREGISTRATIONTYPE")
                          or _text(el, "GSTREGISTRATIONTYPE")
                          or _text(el, "PARTYTAXREGISTRATIONTYPE"),
    )


def read_masters(xml: str) -> Masters:
    root = etree.fromstring(xml.encode("utf-8"))
    masters = Masters()
    for el in root.iter("GROUP"):
        masters.groups.append(Group(name=el.get("NAME") or _text(el, "NAME"),
                                    parent=_text(el, "PARENT")))
    for el in root.iter("UNIT"):
        masters.units.append(Unit(
            symbol=el.get("NAME") or _text(el, "NAME"),
            formal_name=_text(el, "FORMALNAME") or _text(el, "ORIGINALNAME"),
            decimal_places=int(_text(el, "DECIMALPLACES", "2") or 2),
        ))
    for el in root.iter("COSTCENTRE"):
        masters.cost_centres.append(CostCentre(
            name=el.get("NAME") or _text(el, "NAME"),
            parent=_text(el, "PARENT"),
            category=_text(el, "CATEGORY") or "Primary Cost Category",
        ))
    for el in root.iter("GODOWN"):
        masters.godowns.append(Godown(name=el.get("NAME") or _text(el, "NAME"),
                                      parent=_text(el, "PARENT")))
    for el in root.iter("STOCKGROUP"):
        masters.stock_groups.append(StockGroup(name=el.get("NAME") or _text(el, "NAME"),
                                               parent=_text(el, "PARENT")))
    for el in root.iter("STOCKITEM"):
        masters.stock_items.append(StockItem(
            name=el.get("NAME") or _text(el, "NAME"),
            parent=_text(el, "PARENT"),
            base_unit=_text(el, "BASEUNITS"),
            description=_text(el, "DESCRIPTION"),
            # HSNDETAILS.LIST is where TallyPrime keeps it and where this
            # writes it; the other two are read so an older file still reads.
            hsn_code=(_text(el, "HSNDETAILS.LIST/HSNCODE")
                      or _text(el, "GSTDETAILS.LIST/HSNCODE")
                      or _text(el, "HSNCODE")),
            taxability=_text(el, "GSTDETAILS.LIST/TAXABILITY") or "Taxable",
        ))
    for el in root.iter("LEDGER"):
        masters.ledgers.append(Ledger(
            name=el.get("NAME") or _text(el, "NAME"),
            parent=_text(el, "PARENT"),
            currency=_text(el, "CURRENCYNAME", ""),
            bill_wise=_text(el, "ISBILLWISEON") == "Yes",
            affects_stock=_text(el, "AFFECTSSTOCK") == "Yes",
            tax_type=_text(el, "TAXTYPE"),
            duty_head=_text(el, "GSTDUTYHEAD"),
            cost_centres_on=_text(el, "ISCOSTCENTRESON") == "Yes",
            address=_address(el),
        ))
    return masters


def _lines(voucher_el):
    out = []
    for tag in ("LEDGERENTRIES.LIST", "ALLLEDGERENTRIES.LIST"):
        for el in voucher_el.findall(tag):
            out.append(LedgerLine(
                ledger=_text(el, "LEDGERNAME"),
                amount=_amount(el),
                is_party=_text(el, "ISPARTYLEDGER") == "Yes",
                bill_refs=[
                    BillRef(name=_text(b, "NAME"),
                            kind=_text(b, "BILLTYPE", "New Ref"),
                            amount=_amount(b))
                    for b in el.findall("BILLALLOCATIONS.LIST")
                ],
                cost_centres=[
                    CostCentreAllocation(name=_text(c, "NAME"), amount=_amount(c))
                    for c in el.findall("COSTCENTREALLOCATIONS.LIST")
                ],
                cleared_on=_date(_text(el, "BANKALLOCATIONS.LIST/BANKERSDATE")),
            ))
    return out


def _inventory(voucher_el):
    out = []
    for el in voucher_el.findall("ALLINVENTORYENTRIES.LIST"):
        qty, qty_unit = _split_qty(_text(el, "ACTUALQTY"))
        rate, rate_unit = _split_rate(_text(el, "RATE"))
        out.append(InventoryLine(
            item=_text(el, "STOCKITEMNAME"),
            quantity=qty,
            unit=qty_unit or rate_unit,
            rate=rate,
            amount=_amount(el),
            ledger=_text(el, "ACCOUNTINGALLOCATIONS.LIST/LEDGERNAME"),
            godown=_text(el, "BATCHALLOCATIONS.LIST/GODOWNNAME") or None,
            batch=_text(el, "BATCHALLOCATIONS.LIST/BATCHNAME") or None,
        ))
    return out


def read_vouchers(xml: str) -> list[Voucher]:
    root = etree.fromstring(xml.encode("utf-8"))
    out = []
    for el in root.iter("VOUCHER"):
        out.append(Voucher(
            kind=_text(el, "VOUCHERTYPENAME") or el.get("VCHTYPE") or "",
            number=_text(el, "VOUCHERNUMBER"),
            date=_date(_text(el, "DATE")) or datetime.date(1900, 1, 1),
            narration=_text(el, "NARRATION"),
            party=_text(el, "PARTYLEDGERNAME") or None,
            party_address=_address(el),
            # This export writes no REMOTEID - it is Tally's own namespace and
            # a value of ours in it is refused - so the correlation comes off
            # the voucher number, which is the ERPNext document name. The
            # attribute is still read where it is present, because a file Tally
            # itself wrote carries one.
            remote_id=el.get("REMOTEID") or _text(el, "VOUCHERNUMBER") or None,
            # Read back so the validator sees what was actually written. It
            # never was, so the "no GUID" finding could not fire on the
            # preflight path whatever the file carried - a check reporting on
            # a field its own reader discards.
            guid=_text(el, "GUID"),
            reference=_text(el, "REFERENCE"),
            lines=_lines(el),
            inventory=_inventory(el),
            # What the file itself says, rather than what the voucher type is
            # called - the name is a setting, so it is not evidence either way.
            is_invoice_kind=_text(el, "ISINVOICE").strip().lower() == "yes",
        ))
    return out


def read_company(xml: str) -> str:
    root = etree.fromstring(xml.encode("utf-8"))
    return root.findtext(".//SVCURRENTCOMPANY") or ""


def read_export(masters_xml: str, vouchers_xml: str) -> Export:
    """The pair of files an export produces, as one model."""
    return Export(
        company=read_company(masters_xml) or read_company(vouchers_xml),
        masters=read_masters(masters_xml),
        vouchers=read_vouchers(vouchers_xml),
    )
