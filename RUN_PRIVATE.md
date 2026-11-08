<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 kdenv2604 contributors -->
# Private-scope mode

This document describes the fork's source-level private-scope mode. It is not
a deployment recommendation or a supported release procedure.

`run_private.py` starts the modified server only on literal `127.0.0.1` and
requires an absolute, private configuration file with these fields:

```json
{
  "targetPid": 123,
  "rootAutomationId": "Window",
  "busAddress": "unix:path=/absolute/private-bus",
  "tokenFile": "/absolute/new-token",
  "readyFile": "/absolute/new-ready.json"
}
```

The file must be owned by the invoking user, mode 0600 or stricter, and must
not be committed. The process verifies matching private AT-SPI and D-Bus
addresses, issues a fresh bearer token into `tokenFile`, and writes the
loopback WebDriver endpoint to `readyFile`.

The runtime requires a generated
`source-offer/source-offer-manifest.json` next to the installed component.
That directory must contain only component source and licensing material,
never secrets, tokens, private configuration, or unrelated files.
