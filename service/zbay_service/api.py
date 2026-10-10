"""What a relay talks to.

Three calls, all carrying the server key as a bearer token. The key is the whole of a
relay's identity: it says which Discord server the request is for, and nothing in the
body can say otherwise.
"""

import json

from aiohttp import web

from . import VERSION

# a batch is at most a couple of hundred short records, and a snapshot a few hundred
# listings. anything much bigger is not a relay.
MAX_BODY = 2 * 1024 * 1024

# how long a relay may hold a request open waiting for a command.
MAX_WAIT = 25

HUB = web.AppKey("hub", object)


def _tenant(request):
    header = request.headers.get("Authorization", "")
    key = header[7:].strip() if header.lower().startswith("bearer ") else ""

    tenant = request.app[HUB].store.tenant_by_key(key)
    if not tenant:
        # the same answer for a missing key and a wrong one.
        raise web.HTTPUnauthorized(text='{"error":"unknown server key"}', content_type="application/json")
    return tenant


async def _json(request):
    try:
        return json.loads(await request.text())
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(text='{"error":"body is not json"}', content_type="application/json")


async def post_events(request):
    tenant = _tenant(request)
    body = await _json(request)

    records = body.get("records") if isinstance(body, dict) else None
    if not isinstance(records, list):
        raise web.HTTPBadRequest(text='{"error":"expected records"}', content_type="application/json")

    seq = await request.app[HUB].ingest(tenant["guild_id"], body.get("boot"), records)
    return web.json_response({"ok": True, "seq": seq})


async def put_snapshot(request):
    tenant = _tenant(request)
    body = await _json(request)

    if not isinstance(body, dict) or not isinstance(body.get("listings"), list):
        raise web.HTTPBadRequest(text='{"error":"expected a snapshot"}', content_type="application/json")

    request.app[HUB].set_snapshot(tenant["guild_id"], json.dumps(body))
    return web.json_response({"ok": True})


async def get_commands(request):
    tenant = _tenant(request)

    try:
        after = int(request.query.get("after", "0"))
        wait = min(max(float(request.query.get("wait", "0")), 0), MAX_WAIT)
    except ValueError:
        raise web.HTTPBadRequest(text='{"error":"after and wait are numbers"}', content_type="application/json")

    commands = await request.app[HUB].commands(tenant["guild_id"], after, wait)
    return web.json_response({"commands": commands})


async def health(request):
    return web.json_response({"ok": True, "v": VERSION})


def make_app(hub):
    app = web.Application(client_max_size=MAX_BODY)
    app[HUB] = hub

    app.router.add_post("/v1/events", post_events)
    app.router.add_put("/v1/snapshot", put_snapshot)
    app.router.add_get("/v1/commands", get_commands)
    app.router.add_get("/healthz", health)

    return app
