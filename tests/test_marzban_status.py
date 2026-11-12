"""Marzban server status follows the panel's documented system and core statistics."""

from __future__ import annotations

from aiohttp import web
from aiohttp.test_utils import TestServer

from app.services.panels.marzban import MarzbanBackend


async def test_status_uses_boolean_core_flag_and_outgoing_as_upload() -> None:
    async def handle(request: web.Request) -> web.Response:
        if request.path == "/api/system":
            return web.json_response(
                {
                    "version": "0.8.4",
                    "cpu_usage": 1.0,
                    "incoming_bandwidth": 111,
                    "outgoing_bandwidth": 222,
                    "incoming_bandwidth_speed": 11,
                    "outgoing_bandwidth_speed": 22,
                }
            )
        return web.json_response({"version": "25.1", "started": True, "logs_websocket": "/api/core/logs"})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handle)
    async with TestServer(app) as server:
        backend = MarzbanBackend(str(server.make_url("")), api_token="token")
        try:
            status = await backend.server_status()
        finally:
            await backend.close()
    assert status.core_running
    assert status.uptime_sec is None
    assert (status.net_up_bps, status.net_down_bps) == (22, 11)
    assert (status.traffic_sent, status.traffic_recv) == (222, 111)
