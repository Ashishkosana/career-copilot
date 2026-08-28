# USCIS H-1B Employer Data Hub — sponsorship history

Downloaded 2026-08-27 from the USCIS archive:

    https://www.uscis.gov/archive/h-1b-employer-data-hub-files
    https://www.uscis.gov/sites/default/files/document/data/h1b_datahubexport-<YEAR>.csv

Columns: Fiscal Year · Employer · Initial Approval · Initial Denial ·
         Continuing Approval · Continuing Denial · NAICS · Tax ID · State · City · ZIP

## Why this exists

The screening gates read *words in the job description* to guess sponsorship, which
is why Energy Solutions ("authorization to work in the U.S. indefinitely") and
Addepar ("current or future visa sponsorship... F-1/OPT") both passed as ELIGIBLE
until 2026-08-26. Filing history is a fact; JD phrasing is a guess.

## Fetch notes

- `curl` needs a browser User-Agent or the pages return 403.
- The **archive** page stops at FY2023. FY2024-26 live on
  /tools/reports-and-studies/h-1b-employer-data-hub which blocks curl entirely —
  reachable via a real browser only.
- FY2023 is sufficient for a presence/absence gate: a company that sponsored in
  2023 still does, and one with zero approvals across 2009-2023 does not.
- DOL OFLC LCA disclosure data (job titles + wage levels, so "entry-level SWE"
  can be checked) is at dol.gov/agencies/eta/foreign-labor/performance and 403s
  from curl. Worth adding later via browser; approvals alone are the cheap gate.
