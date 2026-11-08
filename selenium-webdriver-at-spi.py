# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2021-2023 Harald Sitter <sitter@kde.org>
# SPDX-FileCopyrightText: 2026 kdenv2604 contributors
# Modified 2026: private attachment scope, Avalonia semantics, and atomic readonly Name.

import base64
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from datetime import datetime, timedelta
from typing import cast

import gi
import pyatspi
from flask import Flask, Response, jsonify, request
from lxml import etree
from werkzeug.exceptions import HTTPException

from app_roles import ROLE_NAMES

import logging
logging.getLogger("werkzeug").disabled = True
from gi.repository import Gio, GLib

from private_scope import PrivateScope, ScopeError, TreeBudget, fresh, require

# Exposes AT-SPI as a webdriver. This is written in python because C sucks and pyatspi is a first class binding so
# we lose nothing but gain the reduced sucking of python.

# https://github.com/SeleniumHQ/selenium/wiki/JsonWireProtocol#WebElement_JSON_Object.md
# https://www.w3.org/TR/webdriver
# https://github.com/microsoft/WinAppDriver/blob/master/Docs/SupportedAPIs.md
# https://www.freedesktop.org/wiki/Accessibility/PyAtSpi2Example/

class SeleniumSpecialKeyError(RuntimeError): ...

EVENTLOOP_TIME = 0.1
EVENTLOOP_TIME_LONG = 0.5
sys.stdout = sys.stderr
sessions = {}  # global dict of open sessions

logger = logging.Logger("selenium-webdriver-at-spi", logging.INFO)

# Give the GUI enough time to react. tests run on the CI won't always be responsive in the tight schedule established by at-spi2 (800ms) and run risk
# of timing out on (e.g.) click events. The second value is the timeout for app startup, we keep that the same as upstream.
pyatspi.setTimeout(4000, 15000)

# Register a superfluous event listener so at-spi2 enables accessibility status.
# This makes sure apps actually show up on the bus.
# See https://gitlab.gnome.org/GNOME/at-spi2-core/-/commit/c674dc9740ca3858cd90f4206d2ee4309fba7388 which reworked the heuristic
def _a11y_event_listener(event):
    pass

pyatspi.Registry.registerEventListener(
    _a11y_event_listener,
    "window:activate"
)

# Using flask because I know nothing about writing REST in python and it seemed the most straight-forward framework.
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 65536
private_scope = None
source_materials = None


def configure_private(config, token, offer):
    global private_scope, source_materials
    require(private_scope is None, 'session not created', 'Server scope уже установлен.')
    source_materials = offer
    private_scope = PrivateScope(config, token, pyatspi, Gio, GLib)
    return private_scope.descriptor()


@app.before_request
def guard_private_request():
    require(private_scope is not None, 'session not created', 'Нужен private server bootstrap.')
    return private_scope.guard(request, sessions)


@app.errorhandler(ScopeError)
def scope_error(error):
    return jsonify({'value': {'error': error.code, 'message': str(error), 'stacktrace': ''}}), error.status


@app.route('/source', methods=['GET'])
def source_offer():
    return jsonify(source_materials.listing())


@app.route('/source/<source_id>', methods=['GET'])
def source_download(source_id):
    return Response(source_materials.download(source_id), mimetype='application/octet-stream')


@app.route('/session/<session_id>/timeouts', methods=['GET', 'POST'])
def session_timeouts(session_id):
    current = sessions[session_id].timeouts
    if request.method == 'POST':
        requested = json.loads(request.data)
        require(isinstance(requested, dict) and set(requested) <= set(current),
                'invalid argument', 'Неверные W3C timeouts.')
        require(all(type(value) is int and 0 <= value <= 300000 for value in requested.values()),
                'invalid argument', 'Timeout вышел за предел.')
        current.update(requested)
        return jsonify({'value': None})
    return jsonify({'value': current})



@app.errorhandler(Exception)
def unknown_error(e):
    if isinstance(e, HTTPException):
        return e

    return errorFromException(error='unknown error', exception=e), 500

@app.route('/status', methods=['GET'])
def status():
    body = {
        'value': {
            'ready': private_scope is not None,
            'message': 'Modified KDE AT-SPI WebDriver, AGPL-3.0-or-later. Corresponding source: /source',
            'source': {'url': '/source', 'manifestSha256': source_materials.manifest_sha256}
        }
    }
    return json.dumps(body), 200, {'content-type': 'application/json'}


def maybe_special_key_error(text):
    for c in text:
        if c >= '\ue000': # first selenium special key
            raise SeleniumSpecialKeyError("special keys not supported in direct text input")
    return


def _createNode2(accessible, parentElement, indexInParents=None, budget=None):
    if not accessible:
        return
    indexInParents = [] if indexInParents is None else indexInParents
    budget = TreeBudget() if budget is None else budget
    budget.visit(accessible, len(indexInParents))
    private_scope.assert_member(accessible)
    fresh(accessible)
    # A bit of aggressive filtering to not introspect chromium and firefox and the likes when using the desktop root.
    if accessible.toolkitName not in ("Avalonia", "Qt", "at-spi-registry"):
        return

    roleName = accessible.getRoleName()
    e = None
    if roleName:
        e = etree.Element(roleName.replace(" ", "_"))
    else:
        e = etree.Element("accessible")

    e.set("name", accessible.name)
    e.set("role", str(int(accessible.getRole())))
    e.set("description", accessible.description)
    if accessible.accessibleId != None:
        e.set("accessibility-id", accessible.accessibleId)
    # NB: pyatspi.getPath is bugged when the QObject has no QObject parent. Instead manually keep track of indexes.
    # while generating the xml.
    # path = pyatspi.getPath(accessible)
    # path is a list of ints for the indexes within the parents
    path_strs = [str(x) for x in indexInParents]
    e.set("path", ' '.join(path_strs))

    states = []
    for state in accessible.getState().getStates():
        states.append(pyatspi.stateToString(state))
    e.set("states", ', '.join(states))

    child_count = accessible.childCount
    require(0 <= child_count <= 512, 'unknown error', 'Дерево превысило предел детей.')
    for i in range(child_count):
        newIndex = indexInParents.copy()
        newIndex.append(i)
        _createNode2(accessible.getChildAtIndex(i), e, newIndex, budget)

    if parentElement != None:
        parentElement.append(e)
    else:
        return e


def errorFromMessage(error, message):
    return jsonify({'value': {'error': error, 'message': message}})


def errorFromException(error, exception):
    return jsonify({'value': {'error': error, 'message': str(exception), 'stacktrace': traceback.format_exc()}})


@app.route('/')
def index():
    return 'Modified KDE AT-SPI WebDriver — AGPL-3.0-or-later. Corresponding source: <a href="/source">/source</a>'

# Encapsulates a Session object. Sessions are opened by the client and contain elements. A session is generally speaking
# an app.
# TODO: should we expose the root scope somehow? requires a special variant of session and moving logic from the
#   REST app functions to the Session object.


class Session:
    def __init__(self) -> None:
        self.id = str(uuid.uuid4())
        self.elements = {}
        self.timeouts = {'script': 30000, 'pageLoad': 300000, 'implicit': 5000}
        self.launched = False
        self.pid = private_scope.pid
        private_scope.claim(self.id, json.loads(request.data), self.timeouts)
        self.browsing_context = private_scope.root

    def close(self) -> None:
        # Это attach к уже защищённому worker, не право завершать его процесс.
        private_scope.release(self.id)
        self.elements.clear()


@app.route('/session', methods=['GET', 'POST', 'DELETE'])
def session():
    if request.method == 'POST':
        # TODO:
        # https://www.w3.org/TR/webdriver1/#new-session
        # 1, 3, 4, 5, 8, 9, 11, 12, 13, 14
        try:
            session = Session()
        except Exception as e:
            return errorFromException(error='session not created', exception=e), 500
        sessions[session.id] = session

        if session.browsing_context is None:
            return errorFromMessage(error='session not created',
                                    message='Application was not found on the a11y bus for unknown reasons. It probably failed to register on the bus.'), 500

        return json.dumps({'value': {'sessionId': session.id, 'capabilities': {**private_scope.session_capabilities()}}}), 200, {'content-type': 'application/json'}
    elif request.method == 'GET':
        # TODO impl
        print("GET called when not expected", request)
    elif request.method == 'DELETE':
        # TODO spec review
        return json.dumps({'value': None}), 200, {'content-type': 'application/json'}


@app.route('/session/<session_id>', methods=['DELETE'])
def session_delete(session_id):
    if request.method == 'DELETE':
        # TODO spec review
        session = sessions[session_id]
        if not session:
            return json.dumps({'value': {'error': 'no such window'}}), 404, {'content-type': 'application/json'}

        session.close()
        sessions.pop(session_id, None)
        return json.dumps({'value': None}), 200, {'content-type': 'application/json'}


@app.route('/session/<session_id>/timeouts/implicit_wait', methods=['POST'])
def session_implicit_wait(session_id):
    session = sessions[session_id]
    if not session:
        return json.dumps({'value': {'error': 'no such window'}}), 404, {'content-type': 'application/json'}

    blob = json.loads(request.data)
    ms = blob['ms']

    session.timeouts['implicit'] = ms
    return json.dumps({'value': None})


@app.route('/session/<session_id>/source', methods=['GET'])
def session_source(session_id):
    session = sessions[session_id]
    if not session:
        return json.dumps({'value': {'error': 'no such window'}}), 404, {'content-type': 'application/json'}

    doc = _createNode2(session.browsing_context, None)
    return json.dumps({'value': etree.tostring(doc, pretty_print=False).decode("utf-8")}), 200, {'content-type': 'application/json'}


# NB: custom method to get the source without json wrapper
@app.route('/session/<session_id>/sourceRaw', methods=['GET'])
def session_source_raw(session_id):
    session = sessions[session_id]
    if not session:
        return json.dumps({'value': {'error': 'no such window'}}), 404, {'content-type': 'application/json'}

    doc = _createNode2(session.browsing_context, None)
    return etree.tostring(doc, pretty_print=True).decode("utf-8"), 200, {'content-type': 'application/xml'}

def check_requires_button_compat():
    major, minor, _micro = pyatspi.Atspi.get_version()
    return major > 2 or (major == 2 and minor >= 53)
requires_button_compat = check_requires_button_compat()

def locator(session, strategy, selector, start, findAll=False):
    require(strategy in ('xpath', 'accessibility id', 'class name', 'name', 'description'),
            'invalid selector', 'Неподдерживаемый locator strategy.')
    require(isinstance(selector, str) and 0 < len(selector) <= 4096,
            'invalid argument', 'Нужен непустой ограниченный selector.')
    end_time = time.monotonic() + session.timeouts['implicit'] / 1000
    while True:
        private_scope.assert_live()
        private_scope.assert_member(start)
        results = []
        if strategy == 'xpath':
            doc = _createNode2(start, None)
            try:
                selected = doc.xpath(selector)
            except etree.XPathError as error:
                raise ScopeError('invalid selector', 'Неверный XPath.') from error
            require(isinstance(selected, list), 'invalid selector', 'XPath должен выбирать элементы.')
            for item_xml in selected:
                require(hasattr(item_xml, 'get') and item_xml.get('path') is not None,
                        'invalid selector', 'XPath должен выбирать элементы дерева.')
                item = start
                for index in map(int, item_xml.get('path').split()):
                    item = item.getChildAtIndex(index)
                private_scope.assert_member(item)
                fresh(item)
                if item_xml.get('name') != item.name or item_xml.get('description') != item.description:
                    raise ScopeError('stale element reference', 'Дерево изменилось во время XPath.')
                results.append(item)
        else:
            if requires_button_compat and '[push button |' in selector:
                selector = selector.replace('[push button |', '[button |')
            for item in private_scope.walk(start, include_start=start == session.browsing_context):
                if strategy == 'accessibility id':
                    matches = item.accessibleId == selector
                elif strategy == 'class name':
                    matches = str(item) == selector and (item.getState().contains(pyatspi.STATE_VISIBLE) or item.getState().contains(pyatspi.STATE_SENSITIVE))
                elif strategy == 'name':
                    matches = item.name == selector and (item.getState().contains(pyatspi.STATE_VISIBLE) or item.getState().contains(pyatspi.STATE_SENSITIVE))
                else:
                    matches = item.description == selector and (item.getState().contains(pyatspi.STATE_VISIBLE) or item.getState().contains(pyatspi.STATE_SENSITIVE))
                if matches:
                    results.append(item)
                    if not findAll and strategy != 'accessibility id':
                        break
            if strategy == 'accessibility id':
                require(len(results) <= 1, 'invalid selector', 'AutomationId неоднозначен внутри окна.')
        if results or time.monotonic() >= end_time:
            return results if findAll else results[:1]
        time.sleep(min(EVENTLOOP_TIME, max(0, end_time - time.monotonic())))


@app.route('/session/<session_id>/element', methods=['GET', 'POST'])
def session_element(session_id=None):
    # https://www.w3.org/TR/webdriver1/#dfn-find-element

    # TODO scope elements to session somehow when the session gets closed we can throw away the references
    session = sessions[session_id]
    blob = json.loads(request.data)

    strategy = blob['using']
    selector = blob['value']
    if not strategy or not selector:
        return json.dumps({'value': {'error': 'invalid argument'}}), 404, {'content-type': 'application/json'}

    start = session.browsing_context
    if not start: # browsing context (no longer) valid
        return json.dumps({'value': {'error': 'no such window'}}), 404, {'content-type': 'application/json'}

    results = locator(session, strategy, selector, start)

    if not results:
        return json.dumps({'value': {'error': 'no such element'}}), 404, {'content-type': 'application/json'}

    result = results[0]
    unique_id = private_scope.remember(session, result)
    session.elements[unique_id] = result
    return json.dumps({'value': {'element-6066-11e4-a52e-4f735466cecf': unique_id}}), 200, {'content-type': 'application/json'}


@app.route('/session/<session_id>/elements', methods=['GET', 'POST'])
def session_element2(session_id=None):
    # https://www.w3.org/TR/webdriver1/#dfn-find-elements

    # TODO scope elements to session somehow when the session gets closed we can throw away the references
    session = sessions[session_id]
    blob = json.loads(request.data)

    strategy = blob['using']
    selector = blob['value']
    if not strategy or not selector:
        return json.dumps({'value': {'error': 'invalid argument'}}), 404, {'content-type': 'application/json'}

    start = session.browsing_context
    if not start:  # browsing context (no longer) valid
        return json.dumps({'value': {'error': 'no such window'}}), 404, {'content-type': 'application/json'}

    results = locator(session, strategy, selector, start, findAll = True)

    if not results:
        return json.dumps({'value': results}), 200, {'content-type': 'application/json'}

    serializations = []
    for result in results:
        unique_id = private_scope.remember(session, result)
        session.elements[unique_id] = result
        serializations.append({'element-6066-11e4-a52e-4f735466cecf': unique_id})

    return json.dumps({'value': serializations}), 200, {'content-type': 'application/json'}


@app.route('/session/<session_id>/element/<element_id>/click', methods=['GET', 'POST'])
def session_element_click(session_id, element_id):
    element = sessions[session_id].elements[element_id]
    private_scope.require_interactable(element)
    try:
        action = element.queryAction()
        available = [action.getName(index) for index in range(action.nActions)]
        # Avalonia публикует точный семантический click, без pointer/focus fallback.
        names = ('click',) if element.toolkitName == 'Avalonia' else ('Press', 'Toggle', 'ShowMenu')
        candidates = [index for index, name in enumerate(available) if name in names]
        require(len(candidates) == 1, 'unsupported operation', 'Нет одного поддерживаемого click action.')
        require(action.doAction(candidates[0]) is True, 'invalid element state', 'AT-SPI action отклонён.')
    except NotImplementedError as error:
        raise ScopeError('unsupported operation', 'Нет настоящего Action interface.') from error
    return json.dumps({'value': None}), 200, {'content-type': 'application/json'}


@app.route('/session/<session_id>/element/<element_id>/text', methods=['GET'])
def session_element_text(session_id, element_id):
    session = sessions[session_id]
    element = session.elements[element_id]
    try:
        textElement = element.queryText()
        return json.dumps({'value': textElement.getText(0, -1)}), 200, {'content-type': 'application/json'}
    except NotImplementedError:
        return json.dumps({'value': element.name}), 200, {'content-type': 'application/json'}


@app.route('/session/<session_id>/element/<element_id>/enabled', methods=['GET'])
def session_element_enabled(session_id, element_id):
    session = sessions[session_id]
    element = session.elements[element_id]
    return json.dumps({'value': element.getState().contains(pyatspi.STATE_ENABLED)}), 200, {'content-type': 'application/json'}


@app.route('/session/<session_id>/element/<element_id>/rect', methods=['GET'])
def session_element_rect(session_id, element_id):
    session = sessions[session_id]
    element = session.elements[element_id]
    x, y = element.queryComponent().getPosition(pyatspi.XY_SCREEN)
    w, h = element.queryComponent().getSize()
    return json.dumps({'value': {'x': x, 'y': y, 'width': w, 'height': h} }), 200, {'content-type': 'application/json'}


@app.route('/session/<session_id>/element/<element_id>/displayed', methods=['GET'])
def session_element_displayed(session_id, element_id):
    session = sessions[session_id]
    element = session.elements[element_id]
    return json.dumps({'value': element.getState().contains(pyatspi.STATE_VISIBLE) and element.getState().contains(pyatspi.STATE_SHOWING)}), 200, {'content-type': 'application/json'}


@app.route('/session/<session_id>/element/<element_id>/selected', methods=['GET'])
def session_element_selected(session_id, element_id):
    session = sessions[session_id]
    element = session.elements[element_id]
    return json.dumps({'value': (element.getState().contains(pyatspi.STATE_SELECTED) or element.getState().contains(pyatspi.STATE_FOCUSED))}), 200, {'content-type': 'application/json'}


@app.route('/session/<session_id>/element/<element_id>/attribute/<name>', methods=['GET'])
def session_element_attribute(session_id, element_id, name):
    session = sessions[session_id]
    if not session:
        return json.dumps({'value': {'error': 'no such window'}}), 404, {'content-type': 'application/json'}

    element = session.elements[element_id]
    if not element:
        return json.dumps({'value': {'error': 'no such element'}}), 404, {'content-type': 'application/json'}

    if name in ('Name', 'name') and private_scope.is_marker(element_id):
        return json.dumps({'value': private_scope.marker_name(element_id)}), 200, {'content-type': 'application/json'}
    if name in ('AutomationId', 'accessibility-id'):
        return json.dumps({'value': element.accessibleId}), 200, {'content-type': 'application/json'}

    if name in ("Name", "name"):
        return json.dumps({'value': element.name}), 200, {'content-type': 'application/json'}

    if name == "value":
        elementValue = element.queryValue()
        return json.dumps({'value': elementValue.currentValue}), 200, {'content-type': 'application/json'}

    result = None
    for value, string in pyatspi.STATE_VALUE_TO_NAME.items():
        if string == name:
            result = element.getState().contains(value)
            break

    return json.dumps({'value': result}), 200, {'content-type': 'application/json'}


@app.route('/session/<session_id>/element/<element_id>/element', methods=['POST'])
def session_element_element(session_id, element_id):
    session = sessions[session_id]
    if not session:
        return json.dumps({'value': {'error': 'no such window'}}), 404, {'content-type': 'application/json'}

    blob = json.loads(request.data)
    strategy = blob['using']
    selector = blob['value']
    if not strategy or not selector:
        return json.dumps({'value': {'error': 'invalid argument'}}), 404, {'content-type': 'application/json'}

    start = session.elements[element_id]
    if not start:  # browsing context (no longer) valid
        return json.dumps({'value': {'error': 'no such element'}}), 404, {'content-type': 'application/json'}

    results = locator(session, strategy, selector, start)

    if not results:
        return json.dumps({'value': {'error': 'no such element'}}), 404, {'content-type': 'application/json'}

    result = results[0]
    unique_id = private_scope.remember(session, result)
    session.elements[unique_id] = result
    return json.dumps({'value': {'element-6066-11e4-a52e-4f735466cecf': unique_id}}), 200, {'content-type': 'application/json'}

@app.route('/session/<session_id>/element/<element_id>/elements', methods=['POST'])
def session_element_elements(session_id, element_id):
    session = sessions[session_id]
    if not session:
        return json.dumps({'value': {'error': 'no such window'}}), 404, {'content-type': 'application/json'}

    blob = json.loads(request.data)
    strategy = blob['using']
    selector = blob['value']
    if not strategy or not selector:
        return json.dumps({'value': {'error': 'invalid argument'}}), 404, {'content-type': 'application/json'}

    start = session.elements[element_id]
    if not start:  # browsing context (no longer) valid
        return json.dumps({'value': {'error': 'no such element'}}), 404, {'content-type': 'application/json'}

    results = locator(session, strategy, selector, start, findAll = True)

    if not results:
        return json.dumps({'value': {'error': 'no such element'}}), 404, {'content-type': 'application/json'}

    serializations = []
    for result in results:
        unique_id = private_scope.remember(session, result)
        session.elements[unique_id] = result
        serializations.append({'element-6066-11e4-a52e-4f735466cecf': unique_id})

    return json.dumps({'value': serializations}), 200, {'content-type': 'application/json'}

@app.route('/session/<session_id>/element/<element_id>/value', methods=['POST'])
def session_element_value(session_id, element_id):
    element = sessions[session_id].elements[element_id]
    private_scope.require_interactable(element)
    text = json.loads(request.data).get('text')
    require(isinstance(text, str) and len(text) <= 32768, 'invalid argument', 'Неверный текст ввода.')
    try:
        maybe_special_key_error(text)
        offset = element.queryText().caretOffset
        editable = element.queryEditableText()
        require(editable.insertText(offset, text, len(text)) is True,
                'invalid element state', 'EditableText отклонил ввод.')
    except (SeleniumSpecialKeyError, NotImplementedError) as error:
        raise ScopeError('unsupported operation', 'Нужен EditableText; глобальный ввод запрещён.') from error
    return json.dumps({'value': None}), 200, {'content-type': 'application/json'}


@app.route('/session/<session_id>/execute/sync', methods=['POST'])
def session_execute(session_id):
    session = sessions[session_id]
    if not session:
        return json.dumps({'value': {'error': 'no such window'}}), 404, {'content-type': 'application/json'}

    blob = json.loads(request.data)
    script = cast(str, blob['script'])
    args = cast(list, blob['args'])

    logger.info(script)
    logger.info(args)

    match script:
        case "mobile: getClipboard":
            content_type = cast(str, args[0]['contentType'] if 'contentType' in args else 'plaintext')
            data = cast(str, get_clipboard(content_type))
            return json.dumps({'value': base64.b64encode(data.encode('utf-8')).decode('utf-8')}), 200, {'content-type': 'application/json'}
        case "mobile: setClipboard":
            content = args[0]['content']
            content_type = cast(str, args[0]['contentType'])
            set_clipboard(content, content_type)
            return json.dumps({'value': None}), 200, {'content-type': 'application/json'}

    return json.dumps({'value': {'error': 'no such command'}}), 404, {'content-type': 'application/json'}


@app.route('/session/<session_id>/element/<element_id>/clear', methods=['POST'])
def session_element_clear(session_id, element_id):
    element = sessions[session_id].elements[element_id]
    private_scope.require_interactable(element)
    try:
        require(element.queryEditableText().setTextContents('') is True,
                'invalid element state', 'EditableText отклонил очистку.')
    except NotImplementedError as error:
        raise ScopeError('unsupported operation', 'Нужен EditableText; keyboard fallback запрещён.') from error
    return json.dumps({'value': None}), 200, {'content-type': 'application/json'}


@app.route('/session/<session_id>/appium/device/app_state', methods=['POST'])
def session_appium_device_app_state(session_id):
    session = sessions[session_id]
    if not session:
        return json.dumps({'value': {'error': 'no such window'}}), 404, {'content-type': 'application/json'}

    blob = json.loads(request.data)
    appId = blob['appId']

    proc = subprocess.Popen(
        'selenium-webdriver-at-spi-appidlister', stdout=subprocess.PIPE)
    out, err = proc.communicate()

    apps = json.loads(out)
    if appId in apps.values():
        return json.dumps({'value': 4}), 200, {'content-type': 'application/json'}
    # TODO: implement rest of codes
    return json.dumps({'value': 1}), 200, {'content-type': 'application/json'}


@app.route('/session/<session_id>/appium/device/terminate_app', methods=['POST'])
def session_appium_device_terminate_app(session_id):
    session = sessions[session_id]
    if not session:
        return json.dumps({'value': {'error': 'no such window'}}), 404, {'content-type': 'application/json'}

    blob = json.loads(request.data)
    appId = blob['appId']

    proc = subprocess.Popen(
        'selenium-webdriver-at-spi-appidlister', stdout=subprocess.PIPE)
    out, err = proc.communicate()

    apps = json.loads(out)
    if appId in apps.values():
        pid = list(apps.keys())[list(apps.values()).index(appId)]
        os.kill(int(pid), signal.SIGKILL)
    return json.dumps({'value': None}), 200, {'content-type': 'application/json'}


@app.route('/session/<session_id>/appium/device/press_keycode', methods=['POST'])
def session_appium_device_press_keycode(session_id):
    session = sessions[session_id]
    if not session:
        return json.dumps({'value': {'error': 'no such window'}}), 404, {'content-type': 'application/json'}

    blob = json.loads(request.data)
    keycode = blob['keycode']
    # Not doing anything with these for now
    # metastate = blob['metastate']
    # flags = blob['flags']
    # FIXME needs testing
    generate_keyboard_event_text(keycode)
    return json.dumps({'value': None}), 200, {'content-type': 'application/json'}


@app.route('/session/<session_id>/actions', methods=['POST'])
def session_actions(session_id):
    session = sessions[session_id]
    if not session:
        return json.dumps({'value': {'error': 'no such window'}}), 404, {'content-type': 'application/json'}

    blob = json.loads(request.data)

    """
    The following is to support actions that use a specific element
    as the origin. Instead of passing that origin to the inputsynth,
    we instead immediately transform from the coordinate system of the
    element to the viewport one, and then we pass that to the inputsynth.
    """
    try:
        for parent_action in blob["actions"]:
            for action in parent_action["actions"]:
                if not ("origin" in action and isinstance(action["origin"], dict)):
                    continue
                element_id = next(iter(action["origin"].values()))
                action["origin"] = "viewport"
                element = session.elements[element_id]
                x, y = element.queryComponent().getPosition(pyatspi.XY_SCREEN)
                action["x"] += x
                action["y"] += y
    except KeyError:
        pass

    if 'KWIN_PID' in os.environ:
        with tempfile.NamedTemporaryFile() as file:
            file.write(bytes(json.dumps(blob), "utf-8"))
            file.flush()
            subprocess.run(["selenium-webdriver-at-spi-inputsynth", file.name])
    else:
        raise RuntimeError("actions only work with nested kwin, or with the parent kwin pid passed in to KWIN_PID manually!")

    return json.dumps({'value': None}), 200, {'content-type': 'application/json'}


@app.route('/session/<session_id>/appium/device/get_clipboard', methods=['POST'])
def session_appium_device_get_clipboard(session_id):
    session = sessions[session_id]
    if not session:
        return json.dumps({'value': {'error': 'no such window'}}), 404, {'content-type': 'application/json'}

    blob = json.loads(request.data)
    contentType = blob['contentType']
    data = cast(str, get_clipboard(contentType))

    return json.dumps({'value': base64.b64encode(data.encode('utf-8')).decode('utf-8')}), 200, {'content-type': 'application/json'}


@app.route('/session/<session_id>/appium/device/set_clipboard', methods=['POST'])
def session_appium_device_set_clipboard(session_id):
    session = sessions[session_id]
    if not session:
        return json.dumps({'value': {'error': 'no such window'}}), 404, {'content-type': 'application/json'}

    blob = json.loads(request.data)
    contentType = blob['contentType']
    content = blob['content']

    set_clipboard(content, contentType)

    return json.dumps({'value': None}), 200, {'content-type': 'application/json'}


@app.route('/session/<session_id>/appium/element/<element_id>/value', methods=['POST'])
def session_appium_element_value(session_id, element_id):
    session = sessions[session_id]
    if not session:
        return json.dumps({'value': {'error': 'no such window'}}), 404, {'content-type': 'application/json'}

    element = session.elements[element_id]
    if not element:
        return json.dumps({'value': {'error': 'no such element'}}), 404, {'content-type': 'application/json'}

    blob = json.loads(request.data)
    value = blob['text']

    try:
        valueElement = element.queryValue()
        valueElement.currentValue = float(value)
        return json.dumps({'value': None}), 200, {'content-type': 'application/json'}
    except NotImplementedError:
        return json.dumps({'value': None}), 200, {'content-type': 'application/json'}


@app.route('/session/<session_id>/screenshot', methods=['GET'])
def session_appium_screenshot(session_id):
    session = sessions[session_id]
    if not session:
        return json.dumps({'value': {'error': 'no such window'}}), 404, {'content-type': 'application/json'}

    # NB: these values incorrectly do not include the device pixel ratio, so they are off when used on a scaling display
    # position_x, position_y = session.browsing_context.getChildAtIndex(0).queryComponent().getPosition(pyatspi.XY_SCREEN)
    # size_width, size_height = session.browsing_context.getChildAtIndex(0).queryComponent().getSize()

    proc = subprocess.Popen(['selenium-webdriver-at-spi-screenshotter',
                             str(0), str(0), str(0), str(0)],
                            stdout=subprocess.PIPE)
    out, err = proc.communicate()

    if not out:
        return json.dumps({'value': {'error': err}}), 404, {'content-type': 'application/json'}

    return json.dumps({'value': out.decode('utf-8')}), 200, {'content-type': 'application/json'}


@app.route('/session/<session_id>/appium/compare_images', methods=['POST'])
def session_appium_compare_images(session_id):
    """
    Reference:
    - https://github.com/appium/python-client/blob/master/appium/webdriver/extensions/images_comparison.py
    - https://github.com/appium/appium/blob/master/packages/opencv/lib/index.js
    """
    session = sessions[session_id]
    if not session:
        return json.dumps({'value': {'error': 'no such window'}}), 404, {'content-type': 'application/json'}

    blob = json.loads(request.data)
    mode: str = blob['mode']
    options: dict = blob['options']
    return_value: dict = {}

    import cv2 as cv  # The extension is slow, so load it on demand
    import numpy as np

    cv_image1 = cv.imdecode(np.frombuffer(base64.b64decode(blob['firstImage']), np.uint8), cv.IMREAD_COLOR)
    cv_image2 = cv.imdecode(np.frombuffer(base64.b64decode(blob['secondImage']), np.uint8), cv.IMREAD_COLOR)

    if mode == 'matchFeatures':
        # https://docs.opencv.org/3.0-beta/doc/py_tutorials/py_feature2d/py_matcher/py_matcher.html
        detectorName: str = options.get('detectorName', 'ORB')
        matchFunc: str = options.get('matchFunc', 'BruteForce')
        goodMatchesFactor: int = options.get('goodMatchesFactor', -1)

        if detectorName == 'AKAZE':
            detector = cv.AKAZE.create()
        elif detectorName == 'AGAST':
            detector = cv.AgastFeatureDetector.create()
        elif detectorName == 'BRISK':
            detector = cv.BRISK.create()
        elif detectorName == 'FAST':
            detector = cv.FastFeatureDetector.create()
        elif detectorName == 'GFTT':
            detector = cv.GFTTDetector.create()
        elif detectorName == 'KAZE':
            detector = cv.KAZE.create()
        elif detectorName == 'MSER':
            detector = cv.SIFT.create()
        else:
            detector = cv.ORB.create()

        if matchFunc == 'FlannBased':
            matcher = cv.FlannBasedMatcher.create()
        elif matchFunc == 'BruteForceL1':
            matcher = cv.BFMatcher.create(cv.NORM_L1, crossCheck=True)
        elif matchFunc == 'BruteForceHamming':
            matcher = cv.BFMatcher.create(cv.NORM_HAMMING, crossCheck=True)
        elif matchFunc == 'BruteForceHammingLut':
            matcher = cv.BFMatcher.create(cv.NORM_HAMMING2, crossCheck=True)
        elif matchFunc == 'BruteForceSL2':
            matcher = cv.BFMatcher.create(cv.NORM_L2, crossCheck=True)
        else:
            matcher = cv.BFMatcher.create(cv.NORM_L2, crossCheck=True)

        # Find the keypoints and descriptors
        kp1, des1 = detector.detectAndCompute(cv_image1, None)
        kp2, des2 = detector.detectAndCompute(cv_image2, None)
        matches = sorted(matcher.match(des1, des2), key=lambda m: m.distance)

        if len(matches) < 1:
            return json.dumps({'value': {'error': 'Could not find any matches between images. Double-check orientation, resolution, or use another detector or matching function.'}}), 404, {'content-type': 'application/json'}

        return_value['count'] = min(len(matches), goodMatchesFactor) if goodMatchesFactor > 0 else len(matches)
        return_value['points1'] = [kp1[m.queryIdx].pt for m in matches]
        return_value['rect1'] = calculate_matched_rect(return_value['points1'])
        return_value['points2'] = [kp2[m.trainIdx].pt for m in matches]
        return_value['rect2'] = calculate_matched_rect(return_value['points2'])

    elif mode == 'matchTemplate':
        threshold: float = options.get('threshold', 0.0)  # Exact match

        matched = cv.matchTemplate(cv_image1, cv_image2, cv.TM_SQDIFF_NORMED)
        min_val, max_val, min_loc, max_loc = cv.minMaxLoc(matched)

        if min_val <= threshold:
            x, y = min_loc
            return_value['rect'] = {
                'x': x,
                "y": y,
                'width': cv_image2.shape[1],
                'height': cv_image2.shape[0],
            }
        else:
            return json.dumps({'value': {'error': 'Cannot find any occurrences of the partial image in the full image.'}}), 404, {'content-type': 'application/json'}

    elif mode == 'getSimilarity':
        if cv_image1.shape != cv_image2.shape:
            return json.dumps({'value': {'error': 'Both images are expected to have the same size in order to calculate the similarity score.'}}), 404, {'content-type': 'application/json'}
        matched = cv.matchTemplate(cv_image1, cv_image2, cv.TM_SQDIFF_NORMED)
        min_val, max_val, min_loc, max_loc = cv.minMaxLoc(matched)
        return_value['score'] = 1.0 - min_val

    else:
        return json.dumps({'value': {'error': 'Mode is not supported'}}), 404, {'content-type': 'application/json'}

    return json.dumps({'value': return_value}), 200, {'content-type': 'application/json'}


def calculate_matched_rect(matched_points: list[tuple[int, int]]) -> dict[str, int]:
    if len(matched_points) < 2:
        return {
            'x': 0,
            "y": 0,
            'width': 0,
            'height': 0,
        }

    points_sorted_by_distance = sorted(matched_points, key=lambda pt: pt[0] * pt[0] + pt[1] * pt[1])
    first_point = points_sorted_by_distance[0]
    last_point = points_sorted_by_distance[1]

    return {
        'x': min(first_point[0], last_point[0]),
        "y": min(first_point[1], last_point[1]),
        'width': abs(first_point[0] - last_point[0]),
        'height': abs(first_point[1] - last_point[1]),
    }


def generate_keyboard_event_text(text):
    # using a nested kwin. need to synthesize keys into wayland (not supported in atspi right now)
    if 'KWIN_PID' in os.environ:
        with tempfile.NamedTemporaryFile() as fp:
            actions = []
            for ch in text:
                actions.append({'type': 'keyDown', 'value': ch})
                actions.append({'type': 'keyUp', 'value': ch})
            fp.write(json.dumps({'actions': [{'type': 'key', 'id': 'key', 'actions': actions}]}).encode())
            fp.flush()
            subprocess.run(["selenium-webdriver-at-spi-inputsynth", fp.name])
    else:
        for ch in text:
            pyatspi.Registry.generateKeyboardEvent(char_to_keyval(ch), None, pyatspi.KEY_SYM)
            time.sleep(EVENTLOOP_TIME)

def keyval_to_keycode(keyval):
    # Закрытая original global-input ветка; semantic server не импортирует GTK/GDK.
    gi.require_version('Gdk', '3.0')
    gi.require_version('Gtk', '3.0')
    from gi.repository import Gdk, Gtk
    keymap = Gdk.Keymap.get_default()
    ret, keys = keymap.get_entries_for_keyval(keyval)
    if not ret:
        raise RuntimeError("Failed to map key!")
    # FIXME layout 0 is not necessarily the current one (e.g. in the kcm we can configure multiple layouts)
    return keys[0]


def char_to_keyval(ch):
    # Закрытая original global-input ветка; semantic server не импортирует GTK/GDK.
    gi.require_version('Gdk', '3.0')
    gi.require_version('Gtk', '3.0')
    from gi.repository import Gdk, Gtk
    keyval = Gdk.unicode_to_keyval(ord(ch))
    # I Don't know why this doesn't work, also doesn't work with \033 as input. :((
    # https://gitlab.gnome.org/GNOME/gtk/-/blob/gtk-3-24/gdk/gdkkeyuni.c
    # Other useful resources:
    # https://www.cl.cam.ac.uk/~mgk25/ucs/keysymdef.h
    if ch == "\uE00C":
        keyval = 0xff1b # escape
    elif ch == "\ue03d":
        keyval = 0xffeb # left meta
    elif ch == "\ue006":
        keyval = 0xff0d # return
    elif ch == "\ue007":
        keyval = 0xff8d # enter
    elif ch == "\ue003":
        keyval = 0xff08 # backspace
    elif ch == "\ue010":
        keyval = 0xff57 # end
    elif ch == "\ue012":
        keyval = 0xff51 # left
    elif ch == "\ue014":
        keyval = 0xff53 # right
    elif ch == "\ue013":
        keyval = 0xff52 # up
    elif ch == "\ue015":
        keyval = 0xff54 # down
    elif ch == "\ue004":
        keyval = 0xff09 # tab
    return keyval


def get_clipboard(content_type):
    # Закрытая original global-input ветка; semantic server не импортирует GTK/GDK.
    gi.require_version('Gdk', '3.0')
    gi.require_version('Gtk', '3.0')
    from gi.repository import Gdk, Gtk
    # NOTE: need a window because on wayland we must be the active window to manipulate the clipboard (currently anyway)
    window = Gtk.Window()
    window.set_default_size(20, 20)
    window.show()
    display = window.get_display()
    clipboard = Gtk.Clipboard.get_for_display(display, Gdk.SELECTION_CLIPBOARD)

    spin_glib_main_context()

    data = None
    if content_type == 'plaintext':
        data = clipboard.wait_for_text()
    else:
        raise 'content type not currently supported'

    window.close()

    spin_glib_main_context()

    return data


def set_clipboard(content, content_type):
    # Закрытая original global-input ветка; semantic server не импортирует GTK/GDK.
    gi.require_version('Gdk', '3.0')
    gi.require_version('Gtk', '3.0')
    from gi.repository import Gdk, Gtk
    # NOTE: need a window because on wayland we must be the active window to manipulate the clipboard (currently anyway)
    window = Gtk.Window()
    window.set_default_size(20, 20)
    display = window.get_display()
    clipboard = Gtk.Clipboard.get_for_display(display, Gdk.SELECTION_CLIPBOARD)

    if content_type == 'plaintext':
        clipboard.set_text(base64.b64decode(content).decode('utf-8'), -1)
    else:
        raise 'content type not currently supported'

    spin_glib_main_context()

    window.close()

    spin_glib_main_context()


def spin_glib_main_context(repeat: int = 4):
    context = GLib.MainContext.default()
    for _ in range(repeat):
        time.sleep(EVENTLOOP_TIME_LONG)
        while context.pending():
            context.iteration(may_block=False)
