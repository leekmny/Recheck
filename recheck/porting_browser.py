"""Playwright observations in a disposable Chromium context."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from .porting_drivers import (
    DriverConfigurationError, DriverError, DriverUnavailable, MAX_BYTES, ObservationError, ObservationTimeout,
    encode_bytes, read_file, remaining, step_url, text_field,
)


@asynccontextmanager
async def open_browser(origin: str, directory: Path, deadline: float):
    try:
        from playwright import async_api
    except ImportError as exc:
        raise DriverUnavailable('Install Playwright on the worker and provision Chromium') from exc

    def deny_connection(reader, writer):
        writer.close()

    # Playwright routing alone only sees the first request in some redirect chains.
    # The Chromium proxy also sends non-allowlisted HTTP requests to a closed connection.
    # The enclosing worker remains responsible for actual network isolation.
    async with await asyncio.start_server(deny_connection, '127.0.0.1', port=0) as deny_proxy:
        proxy_port = deny_proxy.sockets[0].getsockname()[1]
        async with async_api.async_playwright() as tools:
            with TemporaryDirectory(prefix='recheck-downloads-', dir=directory) as downloads:
                context = None
                failure = None
                try:
                    allowed = urlsplit(origin).netloc
                    browser = await tools.chromium.launch(
                        headless=True, downloads_path=downloads,
                        timeout=remaining(deadline) * 1000,
                        args=[f'--proxy-server=http://127.0.0.1:{proxy_port}',
                              f'--proxy-bypass-list=<-loopback>;http://{allowed};ws://{allowed}',
                              '--force-webrtc-ip-handling-policy=disable_non_proxied_udp'],
                    )
                except async_api.TimeoutError as exc:
                    raise ObservationTimeout('Chromium launch timed out') from exc
                except async_api.Error as exc:
                    raise DriverUnavailable(f'Worker could not launch Chromium: {exc}') from exc
                try:
                    context = await browser.new_context(service_workers='block', accept_downloads=True)
                    if not hasattr(context, 'route_web_socket'):
                        raise DriverUnavailable('Browser observation requires Playwright >= 1.48')
                    origin_fields = urlsplit(origin)

                    def allowed_origin(url, scheme):
                        address = urlsplit(url)
                        return (address.scheme, address.netloc) == (scheme, origin_fields.netloc)

                    async def route_request(route):
                        if allowed_origin(route.request.url, 'http'):
                            await route.continue_()
                        else:
                            await route.abort()

                    async def route_socket(socket):
                        if allowed_origin(socket.url, 'ws'):
                            socket.connect_to_server()
                        else:
                            await socket.close()

                    await context.route('**/*', route_request)
                    await context.route_web_socket('**/*', route_socket)
                    page = await context.new_page()
                    yield Browser(page, origin, async_api)
                except async_api.Error as exc:
                    failure = ObservationError(f'Browser context failed: {str(exc)[:1024]}')
                    raise failure from exc
                except BaseException as exc:
                    failure = exc
                    raise
                finally:
                    errors = []
                    for resource in (context, browser):
                        if resource is not None:
                            try:
                                await asyncio.wait_for(resource.close(), 2)
                            except (TimeoutError, async_api.Error) as exc:
                                errors.append(f'{type(exc).__name__}: {str(exc)[:500]}')
                    if errors and isinstance(failure, DriverError):
                        failure.evidence['cleanup_errors'] = errors
                    elif errors and failure is None:
                        raise ObservationError('Browser cleanup failed', evidence={'cleanup_errors': errors})


class Browser:
    def __init__(self, page, origin, api):
        self.page = page
        self.origin = origin
        self.api = api

    def locator(self, value):
        if isinstance(value, str):
            return self.page.locator(text_field(value, 'locator'))
        if not isinstance(value, dict):
            raise DriverConfigurationError('Browser locator must be a CSS string or selector object')
        fields = set(value)
        if fields in ({'role'}, {'role', 'name'}):
            args = ({'name': text_field(value['name'], 'name'), 'exact': True}
                    if 'name' in value else {})
            return self.page.get_by_role(text_field(value['role'], 'role'), **args)
        if fields == {'label'}:
            return self.page.get_by_label(text_field(value['label'], 'label'), exact=True)
        if fields == {'test_id'}:
            return self.page.get_by_test_id(text_field(value['test_id'], 'test_id'))
        if fields == {'css'}:
            return self.page.locator(text_field(value['css'], 'css'))
        raise DriverConfigurationError('Unsupported browser locator fields')

    async def run(self, action, step, deadline):
        try:
            return await asyncio.wait_for(self._run(action, step, deadline), remaining(deadline))
        except (self.api.TimeoutError, TimeoutError) as exc:
            raise ObservationTimeout(f'Browser {action} timed out') from exc
        except (self.api.Error, AssertionError) as exc:
            raise ObservationError(f'Browser {action} failed: {str(exc)[:1024]}') from exc

    async def _run(self, action, step, deadline):
        timeout = remaining(deadline) * 1000
        if action == 'navigate':
            response = await self.page.goto(step_url(self.origin, step.get('path', '/')),
                                            wait_until='domcontentloaded', timeout=timeout)
            if response is None:
                raise ObservationError('Browser navigation did not receive an HTTP response')
            return {'status': response.status}
        if action == 'screenshot':
            image = await self.page.screenshot(timeout=timeout)
            if len(image) > MAX_BYTES:
                raise ObservationError('Screenshot exceeds the observation byte limit')
            return {'visual_only': True, 'media_type': 'image/png', 'content_b64': encode_bytes(image)}

        locator = self.locator(step.get('locator', step.get('selector')))
        if action in {'ready', 'wait'}:
            state = step.get('state', 'visible')
            if state not in {'attached', 'detached', 'visible', 'hidden'}:
                raise DriverConfigurationError('Unsupported browser wait state')
            await locator.wait_for(state=state, timeout=timeout)
            if 'text' in step:
                text = text_field(step['text'], 'text', empty=True)
                await self.api.expect(locator).to_have_text(text, timeout=remaining(deadline) * 1000)
            return {'ready': True} if action == 'ready' else {'completed': True}
        if action == 'fill':
            await locator.fill(text_field(step.get('value'), 'value', empty=True), timeout=timeout)
            return {'completed': True}
        if action == 'click':
            await locator.click(timeout=timeout)
            return {'completed': True}
        if action == 'download':
            async with self.page.expect_download(timeout=timeout) as pending:
                await locator.click(timeout=timeout)
            download = await pending.value
            path = await download.path()
            return {'name': download.suggested_filename, **read_file(path, deadline)}
        if action == 'read':
            prop = step.get('property', 'text')
            if prop == 'visible':
                return await locator.is_visible()
            if prop == 'attribute':
                value = await locator.get_attribute(text_field(step.get('name'), 'attribute name'),
                                                    timeout=timeout)
            else:
                accessors = {'text': locator.inner_text, 'value': locator.input_value,
                             'checked': locator.is_checked, 'enabled': locator.is_enabled}
                if prop not in accessors:
                    raise DriverConfigurationError('Unsupported browser read property')
                value = await accessors[prop](timeout=timeout)
            if isinstance(value, str) and len(value.encode('utf-8')) > MAX_BYTES:
                raise ObservationError('Browser value exceeds the observation byte limit')
            return value
        raise DriverConfigurationError(f'Unsupported browser action: {action}')
