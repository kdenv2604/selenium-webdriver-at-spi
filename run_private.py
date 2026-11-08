# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 kdenv2604 contributors
"""Запускает настоящий изменённый KDE Flask W3C server на private loopback."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import secrets

from private_scope import private_config, require
from source_offer import SourceOffer


def write_new(path, contents):
    path = Path(path)
    require(path.is_absolute(), 'invalid argument', 'Output должен иметь абсолютный собственный путь.')
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
        output.write(contents)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    config = private_config(parser.parse_args().config)
    # Валидация source offer идёт до GI/pyatspi import и native listener.
    offer = SourceOffer()
    from werkzeug.serving import make_server
    path = Path(__file__).resolve().parent / 'selenium-webdriver-at-spi.py'
    spec = importlib.util.spec_from_file_location('kde_scoped_webdriver', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    token = secrets.token_urlsafe(32)
    descriptor = module.configure_private(config, token, offer)
    server = None
    try:
        server = make_server('127.0.0.1', 0, module.app, threaded=False)
        write_new(config['tokenFile'], token + '\n')
        descriptor |= {'url': f'http://127.0.0.1:{server.server_port}', 'sourceUrl': '/source',
                       'sourceManifestSha256': offer.manifest_sha256}
        write_new(config['readyFile'], json.dumps(descriptor) + '\n')
        server.serve_forever()
    finally:
        if server is not None:
            server.server_close()
        module.private_scope.close()


if __name__ == '__main__':
    main()
