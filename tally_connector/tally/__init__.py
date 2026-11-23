"""
The Tally side of the connector, and the part of it that knows nothing about
ERPNext or Frappe.

Everything in this package is pure Python over plain data. It can be imported,
exercised and asserted against without a site, a database or a running Tally -
which is the whole point of it existing. The layering is:

    ERPNext  --extract-->  model  --map-->  model  --validate-->  report
                                              |
                                              +--render--> XML

Only the extract layer imports `frappe`. If anything in this package grows a
`frappe` import, the offline test suite stops being offline and the reason for
the split has gone.
"""
