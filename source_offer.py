# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 kdenv2604 contributors
"""Неизменяемый component-only source offer в собственном private endpoint."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat

from private_scope import require, unique_json

SCHEMA = 'kde-atspi-private-source/1'
COMMIT = '4199d3ea3ccdcaa175b5a06ed3ad8f1af498f38a'
INSTALLED = ('selenium-webdriver-at-spi.py', 'app_roles.py', 'private_scope.py',
             'source_offer.py', 'run_private.py', 'upstream-adaptation.patch')
REQUIRED = set(INSTALLED) | {'AGPL-3.0-or-later.txt', 'MODIFICATIONS.md', 'RUN.md',
                             'DEPENDENCY-SOURCES.json', 'requirements-semantic.txt'}


class SourceOffer:
    def __init__(self, installed_root=None):
        installed = Path(__file__).resolve().parent if installed_root is None else Path(installed_root).resolve()
        root = installed / 'source-offer'
        require(not root.is_symlink() and root.is_dir(), 'session not created', 'Нет component source-offer каталога.')
        manifest_path = root / 'source-offer-manifest.json'
        require(not manifest_path.is_symlink() and manifest_path.is_file()
                and manifest_path.stat().st_size <= 1024 * 1024,
                'session not created', 'Нет обычного ограниченного source manifest.')
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw, object_pairs_hook=unique_json)
        require(set(manifest) == {'schema', 'component', 'upstreamCommit', 'coverage', 'files'}
                and manifest['schema'] == SCHEMA and manifest['component'] == 'KDE selenium-webdriver-at-spi private test adaptation'
                and manifest['upstreamCommit'] == COMMIT,
                'session not created', 'Неверная структура/identity source manifest.')
        coverage = manifest['coverage']
        require(isinstance(coverage, dict) and set(coverage) == {'driverFiles', 'nonSystemDependencies', 'systemLibraries', 'releaseAudit'}
                and all(isinstance(value, str) and 0 < len(value) <= 512 for value in coverage.values()),
                'session not created', 'Неверное описание source coverage.')
        require(isinstance(manifest['files'], list) and 1 <= len(manifest['files']) <= 128,
                'session not created', 'Неверный состав source offer.')
        self.files, paths, total = {}, set(), 0
        for item in manifest['files']:
            require(isinstance(item, dict) and set(item) == {'id', 'path', 'sha256'},
                    'session not created', 'Неверная запись source manifest.')
            identifier, relative, expected = item['id'], item['path'], item['sha256']
            require(isinstance(identifier, str) and re.fullmatch(r'[a-z][a-z0-9-]{0,63}', identifier)
                    and identifier not in self.files and isinstance(relative, str) and len(relative) <= 256
                    and '\\' not in relative and ':' not in relative,
                    'session not created', 'Неверный source id/path.')
            parts = PurePosixPath(relative)
            require(not parts.is_absolute() and parts.parts and all(part not in ('.', '..') for part in parts.parts)
                    and relative == parts.as_posix() and relative not in paths,
                    'session not created', 'Source path должен быть уникальным относительным путём.')
            require(not any(word in relative.lower() for word in ('secret', 'token', 'private-config', '.key', '.pem')),
                    'session not created', 'Private материалы не входят в source offer.')
            path = root
            for part in parts.parts:
                path = path / part
                require(not path.is_symlink(), 'session not created', 'Symlink в source offer запрещён.')
            require(path.resolve().is_relative_to(root.resolve()) and stat.S_ISREG(path.stat().st_mode)
                    and path.stat().st_size <= 2 * 1024 * 1024,
                    'session not created', 'Source file выходит за component scope.')
            contents = path.read_bytes()
            total += len(contents)
            require(total <= 16 * 1024 * 1024 and isinstance(expected, str)
                    and re.fullmatch(r'[0-9a-f]{64}', expected)
                    and hashlib.sha256(contents).hexdigest() == expected,
                    'session not created', 'Source SHA или размер не совпал.')
            if relative in INSTALLED:
                require(contents == (installed / relative).read_bytes(),
                        'session not created', 'Предлагаемый source отличается от установленного компонента.')
            paths.add(relative)
            # После startup handler возвращает эти bytes, а не заново открывает путь.
            self.files[identifier] = (relative, contents)
        require(REQUIRED <= paths, 'session not created', 'Не хватает corresponding-source материалов.')
        self.manifest = manifest
        self.manifest_sha256 = hashlib.sha256(raw).hexdigest()

    def listing(self):
        return {'license': 'AGPL-3.0-or-later', 'manifestSha256': self.manifest_sha256,
                'coverage': self.manifest['coverage'],
                'files': [item | {'download': '/source/' + item['id']} for item in self.manifest['files']]}

    def download(self, identifier):
        require(identifier in self.files, 'unknown command', 'Source id отсутствует в allowlist.')
        return self.files[identifier][1]
