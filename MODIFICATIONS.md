<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 kdenv2604 contributors -->
# Modifications to KDE AT-SPI WebDriver

This fork is based on KDE `selenium-webdriver-at-spi` commit
`4199d3ea3ccdcaa175b5a06ed3ad8f1af498f38a`. Original SPDX notices and
copyright attribution are preserved. Fork changes are
AGPL-3.0-or-later.

## Scope

The fork adds a deliberately narrow private-scope mode for a pre-existing
desktop process:

- loopback-only startup with a freshly generated bearer token;
- attachment to one configured UID, PID, D-Bus peer, accessibility root, and
  process incarnation;
- a small W3C WebDriver route allowlist, without arbitrary launch,
  desktop-root, global input, clipboard, screenshot, image-comparison,
  execute, or termination routes;
- bounded tree traversal and strict action/edit success checks;
- a pinned direct D-Bus `Name` read for a pre-discovered readiness marker;
- a component-only `/source` endpoint whose manifest is checked against the
  installed source bytes;
- malformed JSON is reported as W3C `invalid argument`, rather than an
  internal server error.

The added modules are `private_scope.py`, `source_offer.py`, and
`run_private.py`. The primary upstream server remains
`selenium-webdriver-at-spi.py`; this is a modified version, not a replacement
claimed as original work.

## Limits

This is an experimental source fork, not a release or a claim of complete
platform, dependency, or security validation. A distributor or network
operator remains responsible for the Corresponding Source and other
AGPL-3.0-or-later obligations of the actual deployment.
