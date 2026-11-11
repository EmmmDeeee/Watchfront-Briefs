# Country Event Map tools

Copies of `Backend/country_events.py` (feed validator) and
`Backend/country_event_edition.py` (deterministic expiry and edition builder)
from the private Watchfront repository. They run the automated checks in this
repository's workflows. When either changes in Watchfront, copy it here in the
same pull request series.

- `.github/workflows/country-events-expiry.yml` (schedule or manual start; the
  only workflow with write permissions): once a day removes events that have
  left the 30-day window, and nothing else, validates the committed result and
  proposes it as a pull request for the owner to merge. It starts the feed check
  on its own proposal, because pull requests opened with the built-in Actions
  token do not trigger workflows.
- `.github/workflows/country-events-expiry-check.yml` (read-only): tests the
  expiry tool on pull requests that change it.
- `.github/workflows/country-events-check.yml` (read-only): validates every pull
  request that changes `country-events/` against the published feed.

The country border file used by the validator lives in the app repository, so
border checks are skipped here (with a warning). New coordinates are checked
in Watchfront before anything is proposed.
