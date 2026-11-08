# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 kdenv2604 contributors
"""Граница private attach для изменённого KDE WebDriver; не второй UI backend."""
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid

MARKER_ID = re.compile(r'PrivateAutomation\.Readiness\.v1/[0-9a-f]{32}\Z')
UNIQUE_NAME = re.compile(r':[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+\Z')
OBJECT_PATH = re.compile(r'/(?:[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)*)?\Z')
ERROR_STATUS = {'invalid session id': 404, 'no such window': 404, 'no such element': 404,
                'unknown command': 404,
                'stale element reference': 404, 'unknown error': 500, 'session not created': 500,
                'unsupported operation': 500}


class ScopeError(Exception):
    def __init__(self, code, message, status=None):
        super().__init__(message)
        self.code = code
        self.status = ERROR_STATUS.get(code, 400) if status is None else status


def require(condition, code, message):
    if not condition:
        raise ScopeError(code, message)


def fresh(node):
    node.clear_cache()
    return node


def unique_json(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'invalid argument', 'Повтор JSON key запрещён.')
        result[key] = value
    return result


def private_config(path):
    path = Path(path)
    info = path.lstat()
    require(path.is_absolute() and stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and info.st_mode & 0o077 == 0 and info.st_size <= 65536,
            'invalid argument', 'Config должен быть частным обычным файлом текущего UID.')
    config = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique_json)
    require(set(config) == {'targetPid', 'rootAutomationId', 'busAddress', 'tokenFile', 'readyFile'},
            'invalid argument', 'Неверный private config.')
    address = config['busAddress']
    require(isinstance(address, str) and address.startswith('unix:path=/') and
            all(os.environ.get(key) == address for key in ('AT_SPI_BUS_ADDRESS', 'DBUS_SESSION_BUS_ADDRESS')),
            'invalid argument', 'Нужна явно выбранная private bus без fallback.')
    return config


def process_identity(pid):
    require(type(pid) is int and 0 < pid <= 2147483647, 'invalid argument', 'Неверный target PID.')
    if sys.platform.startswith('linux'):
        status = Path(f'/proc/{pid}/status').read_text()
        uid = int(next(line for line in status.splitlines() if line.startswith('Uid:')).split()[1])
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        require(fields[0] != 'Z', 'no such window', 'Worker завершён.')
        stamp = Path('/proc/sys/kernel/random/boot_id').read_text().strip() + ':' + fields[19]
    elif sys.platform.startswith('freebsd'):
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'uid=', '-o', 'lstart='],
                                capture_output=True, text=True, timeout=2, check=True)
        fields = result.stdout.strip().split(maxsplit=1)
        require(len(fields) == 2, 'no such window', 'Нет start stamp worker.')
        uid, stamp = int(fields[0]), fields[1]
    else:
        raise ScopeError('unsupported operation', 'Native environment ещё не поддержан.')
    require(uid == os.getuid(), 'invalid argument', 'Чужой UID worker запрещён.')
    # Start stamp дополняет удерживаемый unique D-Bus peer, а не заменяет его.
    return hashlib.sha256(f'{sys.platform}:{pid}:{uid}:{stamp}'.encode()).hexdigest()


class TreeBudget:
    def __init__(self):
        self.visited = []
        self.deadline = time.monotonic() + 3

    def visit(self, node, depth):
        require(node is not None and depth <= 64 and len(self.visited) < 512
                and time.monotonic() < self.deadline and node not in self.visited,
                'unknown error', 'Цикл или превышение предела живого дерева.')
        self.visited.append(node)


@dataclass(frozen=True)
class MarkerPin:
    connection: object
    guid: str
    local_name: str
    sender: str
    path: str


class AtomicName:
    def __init__(self, gio, glib, address):
        self.gio, self.glib = gio, glib
        flags = gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
        self.connection = gio.DBusConnection.new_for_address_sync(address, flags, None, None)
        self.guid, self.local_name = self.connection.get_guid(), self.connection.get_unique_name()
        require(self.guid and self.local_name, 'unknown error', 'Нет private connection identity.')

    def assert_connection(self):
        require(not self.connection.is_closed() and self.connection.get_guid() == self.guid
                and self.connection.get_unique_name() == self.local_name,
                'stale element reference', 'Private bus connection закрыта или заменена.')

    def pin(self, node, sender, pid):
        self.assert_connection()
        peer, path = node.app.bus_name, node.path
        require(isinstance(peer, str) and peer == sender and UNIQUE_NAME.fullmatch(peer)
                and isinstance(path, str) and len(path) <= 1024 and OBJECT_PATH.fullmatch(path),
                'stale element reference', 'Marker вне закреплённого peer/path.')
        owner = self.connection.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus',
            'org.freedesktop.DBus', 'GetConnectionUnixProcessID', self.glib.Variant('(s)', (peer,)),
            self.glib.VariantType.new('(u)'), self.gio.DBusCallFlags.NO_AUTO_START, 1000, None)
        require(owner.get_type_string() == '(u)' and owner.unpack() == (pid,),
                'stale element reference', 'Marker peer принадлежит другому PID.')
        return MarkerPin(self.connection, self.connection.get_guid(), self.connection.get_unique_name(), peer, path)

    def read(self, pin):
        # Здесь нет libatspi getters, pump, Peer.Ping, reconnect или повторной привязки.
        require(self.connection is pin.connection and not self.connection.is_closed()
                and self.connection.get_guid() == pin.guid
                and self.connection.get_unique_name() == pin.local_name,
                'stale element reference', 'Закреплённая private connection потеряна.')
        message = self.gio.DBusMessage.new_method_call(pin.sender, pin.path,
                                                    'org.freedesktop.DBus.Properties', 'Get')
        message.set_flags(self.gio.DBusMessageFlags.NO_AUTO_START)
        message.set_body(self.glib.Variant('(ss)', ('org.a11y.atspi.Accessible', 'Name')))
        reply, _serial = self.connection.send_message_with_reply_sync(
            message, self.gio.DBusSendMessageFlags.NONE, 1000, None)
        require(reply.get_message_type() == self.gio.DBusMessageType.METHOD_RETURN
                and reply.get_sender() == pin.sender,
                'stale element reference', 'Atomic Name reply не принадлежит закреплённому peer.')
        body = reply.get_body()
        require(body is not None and body.get_type_string() == '(v)', 'unknown error', 'Неверный Atomic Name reply.')
        value = body.get_child_value(0).get_variant()
        require(value.get_type_string() == 's', 'unknown error', 'Name не является строкой.')
        text = value.get_string()
        require(isinstance(text, str) and len(text) <= 4096, 'unknown error', 'Name превышает предел.')
        return text

    def close(self):
        self.connection.close_sync(None)


class PrivateScope:
    allowed = {
        'index', 'status', 'source_offer', 'source_download', 'session', 'session_delete',
        'session_implicit_wait', 'session_timeouts', 'session_source', 'session_element', 'session_element2',
        'session_element_click', 'session_element_text', 'session_element_enabled',
        'session_element_rect', 'session_element_displayed', 'session_element_selected',
        'session_element_attribute', 'session_element_element', 'session_element_elements',
        'session_element_value', 'session_element_clear',
    }

    def __init__(self, config, token, atspi, gio, glib, identity_reader=process_identity):
        self.pid = config['targetPid']
        self.root_id = config['rootAutomationId']
        require(type(self.pid) is int and 0 < self.pid <= 2147483647
                and isinstance(self.root_id, str) and 0 < len(self.root_id) <= 512,
                'invalid argument', 'Нужны точные PID/AutomationId окна.')
        self.identity_reader, self.identity = identity_reader, identity_reader(self.pid)
        self.atspi, self.token = atspi, token
        self.binding_id, self.active = str(uuid.uuid4()), None
        self.markers = {}
        self.atomic = AtomicName(gio, glib, config['busAddress'])
        try:
            require(atspi.Registry.getDesktopCount() == 1, 'no such window', 'Private registry неоднозначен.')
            self.desktop = atspi.Registry.getDesktop(0)
            applications = [node for node in self.children(self.desktop) if node.get_process_id() == self.pid]
            require(len(applications) == 1, 'no such window', 'Нет одного application целевого PID.')
            self.application = applications[0]
            self.sender = self.application.app.bus_name
            require(isinstance(self.sender, str) and UNIQUE_NAME.fullmatch(self.sender),
                    'no such window', 'Нет unique peer application.')
            roots = [node for node in self.walk(self.application, check_member=False) if node.accessibleId == self.root_id]
            require(len(roots) == 1, 'no such window', 'Нет одного точного root окна.')
            self.root = roots[0]
            require(fresh(self.root).toolkitName == 'Avalonia', 'session not created', 'Root не является Avalonia.')
            self.assert_live()
        except BaseException:
            self.atomic.close()
            raise

    def descriptor(self):
        return {'bindingId': self.binding_id, 'processIdentity': self.identity, 'targetPid': self.pid,
                'rootAutomationId': self.root_id, 'platformName': 'FreeBSD' if sys.platform.startswith('freebsd') else 'Linux'}

    def session_capabilities(self):
        return {'platformName': self.descriptor()['platformName'],
                **{'appium:' + key: value for key, value in self.descriptor().items() if key != 'platformName'}}

    @staticmethod
    def children(node):
        count = fresh(node).childCount
        require(0 <= count <= 512, 'unknown error', 'Превышен предел детей.')
        return [child for index in range(count) if (child := node.getChildAtIndex(index)) is not None]

    def walk(self, start, include_start=True, check_member=True):
        budget = TreeBudget()
        pending = [(start, 0)] if include_start else [(child, 1) for child in self.children(start)]
        while pending:
            node, depth = pending.pop()
            budget.visit(node, depth)
            fresh(node)
            require(node.get_process_id() == self.pid and node.app.bus_name == self.sender,
                    'stale element reference', 'Узел вышел из PID/unique peer.')
            if check_member:
                self.assert_member(node)
            yield node
            pending.extend((child, depth + 1) for child in reversed(self.children(node)))

    def _within(self, node, ancestor):
        for _ in range(65):
            fresh(node)
            require(node.get_process_id() == self.pid and node.app.bus_name == self.sender,
                    'stale element reference', 'Чужой PID/peer в parent chain.')
            if node == ancestor:
                return
            parent = node.get_parent()
            require(parent is not None and node in self.children(parent),
                    'stale element reference', 'Узел отделён от исходного окна.')
            node = parent
        raise ScopeError('stale element reference', 'Parent chain не достигает root.')

    def assert_member(self, node):
        self._within(node, self.root)

    def identity_check(self):
        require(self.identity_reader(self.pid) == self.identity, 'no such window', 'Incarnation worker изменилась.')
        self.atomic.assert_connection()

    def assert_live(self):
        self.identity_check()
        require(self.application in self.children(self.desktop), 'no such window', 'Исходный application исчез.')
        require(fresh(self.root).accessibleId == self.root_id, 'no such window', 'Исходный root изменился.')
        self._within(self.root, self.application)

    def claim(self, session_id, blob, timeouts):
        require(self.active is None, 'session not created', 'Private target уже имеет сессию.')
        capabilities = blob.get('capabilities', {})
        require(isinstance(capabilities, dict), 'invalid argument', 'Нужны W3C capabilities.')
        always = capabilities.get('alwaysMatch', {})
        first = capabilities.get('firstMatch', [{}])
        require(isinstance(always, dict) and first == [{}], 'invalid argument', 'Нужен один exact capability target.')
        require(not any(key in always for key in ('app', 'appium:app', 'appium:environ')),
                'session not created', 'Launch и desktop Root запрещены.')
        for field, expected in self.descriptor().items():
            key = field if field == 'platformName' else 'appium:' + field
            require(type(always.get(key)) is type(expected) and always[key] == expected,
                    'session not created', 'Target capability не совпал с private descriptor.')
        requested_timeouts = always.get('timeouts', {})
        require(isinstance(requested_timeouts, dict) and set(requested_timeouts) <= set(timeouts),
                'invalid argument', 'Неверные timeouts.')
        for key, value in requested_timeouts.items():
            require(type(value) is int and 0 <= value <= 300000, 'invalid argument', 'Неверный timeout.')
            timeouts[key] = value
        self.assert_live()
        self.active = session_id

    def release(self, session_id):
        require(self.active == session_id, 'invalid session id', 'Чужая сессия.')
        self.active = None
        self.markers.clear()

    def remember(self, session, node):
        self.assert_member(node)
        identifier = next((key for key, existing in session.elements.items() if existing == node), None)
        if identifier is None:
            require(len(session.elements) < 4096, 'unknown error', 'Превышен предел handles.')
            identifier = uuid.uuid4().hex
            session.elements[identifier] = node
        if MARKER_ID.fullmatch(node.accessibleId or '') and identifier not in self.markers:
            self.markers[identifier] = self.atomic.pin(node, self.sender, self.pid)
        return identifier

    def is_marker(self, identifier):
        return identifier in self.markers

    def marker_name(self, identifier):
        # Повторный polling проверяет только OS incarnation и pinned direct Name.
        # Он не читает desktop/root/parent/cache и не запускает GLib event pump.
        self.identity_check()
        pin = self.markers[identifier]
        text = self.atomic.read(pin)
        self.identity_check()
        return text

    def require_interactable(self, node):
        self.assert_member(node)
        states = fresh(node).getState()
        require(all(states.contains(value) for value in (self.atspi.STATE_ENABLED, self.atspi.STATE_SENSITIVE,
                    self.atspi.STATE_VISIBLE, self.atspi.STATE_SHOWING)),
                'element not interactable', 'Контрол не enabled/sensitive/visible/showing.')

    def guard(self, request, sessions):
        require(request.remote_addr == '127.0.0.1' and hmac.compare_digest(
            request.headers.get('Authorization', ''), 'Bearer ' + self.token),
            'invalid argument', 'Нужна авторизация private loopback.',)
        require(request.endpoint in self.allowed, 'unsupported operation', 'Маршрут не входит в semantic scope.')
        if request.method == 'POST':
            require(request.mimetype == 'application/json', 'invalid argument', 'Нужен application/json.')
            try:
                json.loads(request.data, object_pairs_hook=unique_json)
            except json.JSONDecodeError as error:
                # Невалидный клиентский JSON — W3C invalid argument, а не
                # внутренняя ошибка сервера с traceback парсера в ответе.
                raise ScopeError('invalid argument', 'Неверный JSON запроса.') from error
        if request.endpoint in ('index', 'status', 'source_offer', 'source_download'):
            require(request.method == 'GET', 'unsupported operation', 'Метод не поддержан.')
            return
        if request.endpoint == 'session':
            require(request.method == 'POST', 'unsupported operation', 'Поддержан только W3C newSession.')
            return
        arguments = request.view_args or {}
        session_id = arguments.get('session_id')
        require(session_id in sessions and self.active == session_id, 'invalid session id', 'Сессия закрыта или неизвестна.')
        if request.endpoint == 'session_delete':
            require(request.method == 'DELETE', 'unsupported operation', 'Нужен DELETE session.')
            return
        if request.endpoint == 'session_timeouts':
            require(request.method in ('GET', 'POST'), 'unsupported operation', 'Нужен W3C GET/POST timeouts.')
        if request.endpoint in ('session_implicit_wait', 'session_element', 'session_element2',
                'session_element_click', 'session_element_element', 'session_element_elements',
                'session_element_value', 'session_element_clear'):
            require(request.method == 'POST', 'unsupported operation', 'Нужен W3C POST method.')
        identifier = arguments.get('element_id')
        if identifier is not None:
            require(identifier in sessions[session_id].elements, 'stale element reference', 'Неизвестный handle.')
            if self.is_marker(identifier):
                require(request.endpoint == 'session_element_attribute' and arguments.get('name') in ('Name', 'name'),
                        'unsupported operation', 'Marker доступен только через readonly Name.')
                self.identity_check()
                return
        self.assert_live()
        if identifier is not None:
            self.assert_member(sessions[session_id].elements[identifier])
            fresh(sessions[session_id].elements[identifier])

    def close(self):
        self.atomic.close()
