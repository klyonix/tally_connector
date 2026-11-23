"""Running a test as though the site were set to send item detail.

`Send item detail on vouchers` is a site switch, and a site with it unticked
has `extract` fold every stock line onto its
ledger and the export carries no stock item, no stock group and no unit.

A test about how a stock line is priced, how a unit symbol is sanitised or how
`ALLINVENTORYENTRIES.LIST` is rendered is testing the reader and the renderer.
None of that changes with the switch, and a test of it that silently stops
running when somebody ticks a box in Tally Settings is the shape this project
keeps paying for: it finds nothing, skips, and passes with the defect planted
back in. So such a test asks for the stock lines instead of taking whatever
the site happens to be set to.

Use it the other way round - `without_inventory` - for a test that is about
the fold itself over real data.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch


@contextmanager
def inventory_sent():
    """`extract` and everything built on it carry their stock lines."""
    with patch("tally_connector.tally.extract.sending_inventory",
               return_value=True):
        yield


@contextmanager
def inventory_folded():
    """`extract` folds every stock line onto the ledger it posted to."""
    with patch("tally_connector.tally.extract.sending_inventory",
               return_value=False):
        yield
