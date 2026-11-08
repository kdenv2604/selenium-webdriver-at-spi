<!--
    SPDX-License-Identifier: CC0-1.0
    SPDX-FileCopyrightText: 2022 Harald Sitter <sitter@kde.org>
-->

# selenium-webdriver-at-spi: private-scope fork

This is a source-level fork of KDE's `selenium-webdriver-at-spi`, based on
upstream commit
[`4199d3ea3ccdcaa175b5a06ed3ad8f1af498f38a`](https://github.com/KDE/selenium-webdriver-at-spi/commit/4199d3ea3ccdcaa175b5a06ed3ad8f1af498f38a).

The original SPDX notices, copyright attribution, and files in `LICENSES/`
are retained. Fork changes are identified as
`Copyright 2026 kdenv2604 contributors` and are licensed under
AGPL-3.0-or-later. See [FORK.md](FORK.md) and
[MODIFICATIONS.md](MODIFICATIONS.md).

This repository contains experimental source code, not a supported binary
release. Never commit runtime configuration, tokens, generated source-offer
material, or test data.

---

# GUI Testing Made Easy

selenium-webdriver-at-spi is a [WebDriver](https://www.w3.org/TR/webdriver/) for [Appium](https://appium.io) (based on [Selenium](https://www.selenium.dev/)) using the Linux accessibility API [AT-SPI2](https://www.freedesktop.org/wiki/Accessibility/AT-SPI2/).

It effectively enables us to write selenium-style UI tests that behind the scenes manipulate the UI through the accessibility API.
This allows better blackbox testing than what QTest and other unit test frameworks provide, while also not relying on pixmap comparision like openqa.

Check out the [wiki for documentation](https://community.kde.org/Selenium)
