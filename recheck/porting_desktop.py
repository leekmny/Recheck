"""Process-scoped desktop accessibility drivers for dedicated GUI workers."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import contextmanager, suppress
import os
import sys
from threading import Thread

import psutil

from .porting_drivers import (
    DriverConfigurationError, DriverError, DriverUnavailable, MAX_BYTES, ObservationError,
    ObservationTimeout, remaining, text_field,
)


MAX_UI_NODES = 1500


class Desktop:
    def __init__(self, pid: int | None):
        if type(pid) is not int or pid <= 0:
            raise DriverConfigurationError('Desktop observation requires the launched target PID')
        try:
            self.process = psutil.Process(pid)
        except psutil.Error as exc:
            raise ObservationError('Desktop target process is unavailable') from exc

    async def run(self, action: str, step: dict, deadline: float):
        if action not in {'ready', 'fill', 'click', 'wait', 'read'}:
            raise DriverConfigurationError(f'Unsupported desktop action: {action}')
        locator = step.get('locator')
        if (not isinstance(locator, dict) or not locator or
                set(locator) - {'window', 'name', 'id', 'role'}):
            raise DriverConfigurationError('Desktop locator requires window, name, id or role')
        locator = {key: text_field(value, key) for key, value in locator.items()}
        if action == 'fill':
            text_field(step.get('value'), 'value', empty=True)
        state = step.get('state', 'visible')
        if action in {'ready', 'wait'} and state not in {'visible', 'hidden', 'enabled', 'exists', 'missing'}:
            raise DriverConfigurationError('Unsupported desktop wait state')
        if 'text' in step:
            text_field(step['text'], 'text', empty=True)
        while True:
            try:
                timeout = remaining(deadline)
                pending = _native_operation(self._once, action, locator, step, deadline)
                found, result = await asyncio.wait_for(pending, timeout)
            except (TimeoutError, ObservationTimeout) as exc:
                raise ObservationTimeout(f'Desktop {action} timed out') from exc
            if found:
                return result
            await asyncio.sleep(0.075)

    def _once(self, action, locator, step, deadline):
        if not self.process.is_running():
            raise ObservationError('Desktop target process exited')
        try:
            children = self.process.children(recursive=True)
            if len(children) > 128:
                raise ObservationError('Desktop target process tree exceeds the process limit')
            pids = {self.process.pid, *(child.pid for child in children if child.is_running())}
            with _platform_backend(pids) as platform:
                node = _find(platform, locator, deadline)
                if action in {'ready', 'wait'}:
                    state = step.get('state', 'visible')
                    if state == 'missing':
                        satisfied = node is None
                    elif state == 'hidden':
                        satisfied = node is None or not platform.read(node, 'visible')
                    else:
                        satisfied = node is not None and (state == 'exists' or platform.read(node, state))
                    if 'text' in step:
                        satisfied = satisfied and node is not None and platform.read(node, 'text') == step['text']
                    return satisfied, {'ready': True} if action == 'ready' else {'completed': True}
                if node is None:
                    return False, None
                if action == 'read':
                    prop = step.get('property', 'text')
                    if prop not in ('name', 'text', 'value', 'visible', 'enabled'):
                        raise DriverConfigurationError('Unsupported desktop read property')
                    value = platform.read(node, prop)
                    if isinstance(value, str) and len(value.encode('utf-8')) > MAX_BYTES:
                        raise ObservationError('Desktop value exceeds the byte limit')
                    return True, value
                if action == 'fill':
                    platform.fill(node, step['value'])
                else:
                    platform.click(node)
                return True, {'completed': True}
        except DriverError:
            raise
        except Exception as exc:
            raise ObservationError(f'Desktop accessibility operation failed: {type(exc).__name__}: {str(exc)[:500]}') from exc


def _native_operation(operation, *args):
    # A blocked native call must not keep asyncio.run() waiting for its default executor.
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def deliver(result, error):
        if not future.done():
            if error is None:
                future.set_result(result)
            else:
                future.set_exception(error)

    def invoke():
        try:
            result, failure = operation(*args), None
        except Exception as exc:
            result, failure = None, exc
        with suppress(RuntimeError):  # The worker's event loop may already have terminated.
            loop.call_soon_threadsafe(deliver, result, failure)

    Thread(target=invoke, daemon=True, name='recheck-accessibility').start()
    return future


def _find(platform, locator, deadline):
    pending = deque(platform.windows())
    name = locator.get('window')
    if name is not None:
        pending = deque(node for node in pending if platform.property(node, 'name') == name)
    matching = []
    visited = 0
    fields = [(key, value) for key, value in locator.items() if key != 'window']
    while pending:
        remaining(deadline)
        node = pending.popleft()
        if node is None or not platform.owns(node):
            continue
        visited += 1
        if visited > MAX_UI_NODES:
            raise ObservationError('Desktop accessibility tree exceeds the node limit')
        if all(platform.property(node, key) == value for key, value in fields):
            matching.append(node)
            if len(matching) > 1:
                raise ObservationError('Desktop locator matches multiple controls')
        if fields:
            pending.extend(platform.children(node))
        if len(pending) > MAX_UI_NODES:
            raise ObservationError('Desktop accessibility tree exceeds the node limit')
    return matching[0] if matching else None


@contextmanager
def _platform_backend(pids):
    if sys.platform == 'win32':
        try:
            from pywinauto.controls.uiawrapper import UIAWrapper
            from pywinauto.uia_element_info import UIAElementInfo
            import comtypes
            from pywintypes import error as windows_error
            from win32gui import EnumWindows
            from win32process import GetWindowThreadProcessId
        except (ImportError, OSError) as exc:
            raise DriverUnavailable('Install pywinauto on the interactive Windows worker') from exc

        def window_pid(handle):
            with suppress(windows_error):
                return GetWindowThreadProcessId(handle)[1]

        def resolve_window(handle):
            with suppress(comtypes.COMError):
                return UIAWrapper(UIAElementInfo(handle))

        comtypes.CoInitializeEx()
        try:
            yield WindowsBackend(pids, EnumWindows, window_pid, resolve_window)
        finally:
            comtypes.CoUninitialize()
    elif sys.platform.startswith('linux'):
        if not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
            raise DriverUnavailable('Linux desktop observation requires an interactive display and AT-SPI bus')
        try:
            import gi

            gi.require_version('Atspi', '2.0')
            from gi.repository import Atspi
        except (ImportError, ValueError) as exc:
            raise DriverUnavailable('Install PyGObject and the AT-SPI 2 typelib on the Linux worker') from exc
        yield LinuxBackend(Atspi, pids)
    elif sys.platform == 'darwin':
        try:
            import ApplicationServices
        except (ImportError, OSError) as exc:
            raise DriverUnavailable('Install pyobjc-framework-ApplicationServices on the macOS worker') from exc
        yield MacBackend(ApplicationServices, pids)
    else:
        raise DriverUnavailable('No desktop accessibility adapter is available for this worker')


class WindowsBackend:
    def __init__(self, pids, enumerate_windows, window_pid, resolve_window):
        self.pids = pids
        self.enumerate_windows = enumerate_windows
        self.window_pid = window_pid
        self.resolve_window = resolve_window

    def windows(self):
        handles = []

        def collect(handle, _):
            if self.window_pid(handle) in self.pids:
                handles.append(handle)
            return True

        # Traverse Win32 HWNDs before calling UIA so unrelated providers cannot
        # block a desktop-root accessibility search for an owned window.
        self.enumerate_windows(collect, None)
        if len(handles) > MAX_UI_NODES:
            raise ObservationError('Desktop accessibility tree exceeds the node limit')

        result = []
        for handle in handles:
            if self.window_pid(handle) not in self.pids:
                continue
            node = self.resolve_window(handle)
            if node is not None and self.owns(node):
                result.append(node)
        return result

    def owns(self, node):
        return node.element_info.process_id in self.pids

    def children(self, node):
        return node.children()

    def property(self, node, prop):
        field = {'name': 'name', 'id': 'automation_id', 'role': 'control_type'}[prop]
        return getattr(node.element_info, field)

    def read(self, node, prop):
        if prop in ('name', 'text'):
            return node.window_text()
        if prop == 'value':
            return node.iface_value.CurrentValue
        if prop == 'visible':
            return node.is_visible()
        return node.is_enabled()

    def fill(self, node, value):
        node.set_edit_text(value)

    def click(self, node):
        node.invoke()


class LinuxBackend:
    def __init__(self, api, pids):
        self.api, self.pids = api, pids
        api.init()
        api.set_timeout(400, 400)

    def windows(self):
        desktop = self.api.get_desktop(0)
        if desktop is None:
            raise DriverUnavailable('The interactive Linux worker has no accessible desktop')
        return [window for app in self.children(desktop) if app is not None and self.owns(app)
                for window in self.children(app)]

    def owns(self, node):
        return node.get_process_id() in self.pids

    def children(self, node):
        count = node.get_child_count()
        if count > MAX_UI_NODES:
            raise ObservationError('AT-SPI tree exceeds the node limit')
        return [node.get_child_at_index(index) for index in range(count)]

    def property(self, node, prop):
        field = {'name': 'get_name', 'id': 'get_accessible_id', 'role': 'get_role_name'}[prop]
        return getattr(node, field)()

    def read(self, node, prop):
        if prop == 'name':
            return node.get_name()
        if prop in ('text', 'value'):
            accessor = node.get_text_iface()
            if accessor:
                if accessor.get_character_count() > MAX_BYTES:
                    raise ObservationError('AT-SPI text exceeds the observation byte limit')
                return accessor.get_text(0, -1)
            return node.get_name() if prop == 'text' else None
        state = self.api.StateType.SHOWING if prop == 'visible' else self.api.StateType.ENABLED
        return node.get_state_set().contains(state)

    def fill(self, node, value):
        edit = node.get_editable_text_iface()
        if not edit or not edit.set_text_contents(value):
            raise ObservationError('AT-SPI control does not accept text')

    def click(self, node):
        action = node.get_action_iface()
        if action:
            for index in range(min(32, action.get_n_actions())):
                if (action.get_action_name(index) or '').casefold() in {'click', 'press', 'activate'} and action.do_action(index):
                    return
        raise ObservationError('AT-SPI control does not expose a click or press action')


class MacBackend:
    def __init__(self, api, pids):
        self.api, self.pids = api, pids
        if not api.AXIsProcessTrusted():
            raise DriverUnavailable('The macOS worker needs Accessibility permission for its Python process')

    def _attribute(self, node, attr):
        code, value = self.api.AXUIElementCopyAttributeValue(node, attr, None)
        return value if code == 0 else None

    def windows(self):
        result = []
        for pid in sorted(self.pids):
            app = self.api.AXUIElementCreateApplication(pid)
            self.api.AXUIElementSetMessagingTimeout(app, 0.4)
            result.extend(self._attribute(app, 'AXWindows') or [])
        return result

    def owns(self, node):
        code, pid = self.api.AXUIElementGetPid(node, None)
        return code == 0 and pid in self.pids

    def children(self, node):
        return self._attribute(node, 'AXChildren') or []

    def property(self, node, prop):
        attr = {'name': 'AXTitle', 'id': 'AXIdentifier', 'role': 'AXRole'}[prop]
        value = self._attribute(node, attr)
        if prop == 'name' and not value:
            return self._attribute(node, 'AXDescription') or self._attribute(node, 'AXValue')
        return value

    def read(self, node, prop):
        if prop == 'visible':
            window = self._attribute(node, 'AXWindow') or node
            visible = self._attribute(node, 'AXHidden')
            return not (visible or self._attribute(window, 'AXMinimized'))
        if prop == 'enabled':
            return self._attribute(node, 'AXEnabled') is True
        if prop == 'name':
            return self.property(node, 'name')
        value = self._attribute(node, 'AXValue')
        if value is None and prop == 'text':
            return self.property(node, 'name')
        if value is None or isinstance(value, (str, float, int)):
            return value
        raise ObservationError('Unsupported macOS AX value type')

    def fill(self, node, value):
        if self.api.AXUIElementSetAttributeValue(node, 'AXValue', value) != 0:
            raise ObservationError('macOS AX control does not accept text')

    def click(self, node):
        if self.api.AXUIElementPerformAction(node, 'AXPress') != 0:
            raise ObservationError('macOS AX control does not expose a press action')
