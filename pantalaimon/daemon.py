#!/usr/bin/env python3

import attr
import asyncio
import aiohttp
import os
import json

import click
from ipaddress import ip_address
from urllib.parse import urlparse

from aiohttp import web, ClientSession
from nio import (
    LoginResponse,
    KeysQueryResponse,
    GroupEncryptionError,
    SyncResponse
)
from appdirs import user_data_dir
from json import JSONDecodeError
from multidict import CIMultiDict

from pantalaimon.client import PantaClient


@attr.s
class ProxyDaemon:
    homeserver = attr.ib()
    data_dir = attr.ib()
    proxy = attr.ib(default=None)
    ssl = attr.ib(default=None)

    client_sessions = attr.ib(init=False, default=attr.Factory(dict))
    default_session = attr.ib(init=False, default=None)

    def get_access_token(self, request):
        # type: (aiohttp.BaseRequest) -> str
        """Extract the access token from the request.

        This method extracts the access token either from the query string or
        from the Authorization header of the request.

        Returns the access token if it was found.
        """
        access_token = request.query.get("access_token", "")

        if not access_token:
            access_token = request.headers.get(
                "Authorization",
                ""
            ).strip("Bearer ")

        return access_token

    async def forward_request(self, request, session):
        path = request.path
        method = request.method
        data = await request.text()

        headers = CIMultiDict(request.headers)
        headers.pop("Host", None)

        params = request.query

        return await session.request(
            method,
            self.homeserver + path,
            data=data,
            params=params,
            headers=headers,
            proxy=self.proxy,
            ssl=False
        )

    async def router(self, request):
        session = None

        token = self.get_access_token(request)
        client = self.client_sessions.get(token, None)

        if client:
            session = client.client_session
        else:
            if not self.default_session:
                self.default_session = ClientSession()
            session = self.default_session

        resp = await self.forward_request(request, session)
        return(web.Response(text=await resp.text()))

    def _get_login_user(self, body):
        identifier = body.get("identifier", None)

        if identifier:
            user = identifier.get("user", None)

            if not user:
                user = body.get("user", "")
        else:
            user = body.get("user", "")

        return user


    async def login(self, request):
        try:
            body = await request.json()
        except JSONDecodeError:
            # After a long debugging session the culprit ended up being aiohttp
            # and a similar bug to
            # https://github.com/aio-libs/aiohttp/issues/2277 but in the server
            # part of aiohttp. The bug is fixed in the latest master of
            # aiohttp.
            # Return 500 here for now since quaternion doesn't work otherwise.
            # After aiohttp 4.0 gets replace this with a 400 M_NOT_JSON
            # response.
            return web.Response(
                status=500,
                text=json.dumps({
                    "errcode": "M_NOT_JSON",
                    "error": "Request did not contain valid JSON."
                })
            )

        user = self._get_login_user(body)
        password = body.get("password", "")
        device_id = body.get("device_id", "")
        device_name = body.get("initial_device_display_name", "pantalaimon")

        client = PantaClient(
            self.homeserver,
            user,
            device_id,
            store_path=self.data_dir,
            ssl=self.ssl,
            proxy=self.proxy
        )

        response = await client.login(password, device_name)

        if isinstance(response, LoginResponse):
            self.client_sessions[response.access_token] = client
        else:
            await client.close()

        return web.Response(
            status=response.transport_response.status,
            text=await response.transport_response.text()
        )

    @property
    def _missing_token(self):
        return web.Response(
            status=401,
            text=json.dumps({
                "errcode": "M_MISSING_TOKEN",
                "error": "Missing access token."
            })
        )

    @property
    def _unknown_token(self):
        return web.Response(
                status=401,
                text=json.dumps({
                    "errcode": "M_UNKNOWN_TOKEN",
                    "error": "Unrecognised access token."
                })
        )

    @property
    def _not_json(self):
        return web.Response(
            status=400,
            text=json.dumps({
                "errcode": "M_NOT_JSON",
                "error": "Request did not contain valid JSON."
            })
        )

    async def sync(self, request):
        access_token = self.get_access_token(request)

        if not access_token:
            return self._missing_token

        try:
            client = self.client_sessions[access_token]
        except KeyError:
            return self._unknown_token

        sync_filter = request.query.get("filter", None)
        timeout = request.query.get("timeout", None)

        try:
            sync_filter = json.loads(sync_filter)
        except (JSONDecodeError, TypeError):
            pass

        if isinstance(sync_filter, int):
            sync_filter = None

        # TODO edit the sync filter to not filter encrypted messages
        # TODO do the same with an uploaded filter

        # room_filter = sync_filter.get("room", None)

        # if room_filter:
        #     timeline_filter = room_filter.get("timeline", None)
        #     if timeline_filter:
        #         types_filter = timeline_filter.get("types", None)

        response = await client.sync(timeout, sync_filter)

        if not isinstance(response, SyncResponse):
            return web.Response(
                status=response.transport_response.status,
                text=await response.text()
            )

        if client.should_upload_keys:
            await client.keys_upload()

        if client.should_query_keys:
            key_query_response = await client.keys_query()

            # Verify new devices automatically for now.
            if isinstance(key_query_response, KeysQueryResponse):
                for user_id, device_dict in key_query_response.changed.items():
                    for device in device_dict.values():
                        if device.deleted:
                            continue

                        print("Automatically verifying device {}".format(
                            device.id
                        ))
                        client.verify_device(device)

        json_response = await response.transport_response.json()

        decrypted_response = client.decrypt_sync_body(json_response)

        return web.Response(
            status=response.transport_response.status,
            text=json.dumps(decrypted_response)
        )

    async def send_message(self, request):
        access_token = self.get_access_token(request)

        if not access_token:
            return self._missing_token

        try:
            client = self.client_sessions[access_token]
        except KeyError:
            return self._unknown_token

        msgtype = request.match_info["event_type"]
        room_id = request.match_info["room_id"]
        txnid = request.match_info["txnid"]

        try:
            content = await request.json()
        except JSONDecodeError:
            return self._not_json

        try:
            response = await client.room_send(room_id, msgtype, content, txnid)
        except GroupEncryptionError:
            await client.share_group_session(room_id)
            response = await client.room_send(room_id, msgtype, content, txnid)

        return web.Response(
            status=response.transport_response.status,
            text=await response.transport_response.text()
        )

    async def shutdown(self, app):
        """Shut the daemon down closing all the client sessions it has.

        This method is called when we shut the whole app down
        """
        for client in self.client_sessions.values():
            await client.close()

        if self.default_session:
            await self.default_session.close()
            self.default_session = None


async def init(homeserver, http_proxy, ssl):
    """Initialize the proxy and the http server."""
    data_dir = user_data_dir("pantalaimon", "")

    try:
        os.makedirs(data_dir)
    except OSError:
        pass

    proxy = ProxyDaemon(homeserver, data_dir, proxy=http_proxy, ssl=ssl)

    app = web.Application()
    app.add_routes([
        web.post("/_matrix/client/r0/login", proxy.login),
        web.get("/_matrix/client/r0/sync", proxy.sync),
        web.put(
            r"/_matrix/client/r0/rooms/{room_id}/send/{event_type}/{txnid}",
            proxy.send_message
        ),
    ])
    app.router.add_route("*", "/" + "{proxyPath:.*}", proxy.router)
    app.on_shutdown.append(proxy.shutdown)
    return proxy, app


class URL(click.ParamType):
    name = 'url'

    def convert(self, value, param, ctx):
        try:
            value = urlparse(value)

            if value.scheme not in ('http', 'https'):
                self.fail(f"Invalid URL scheme {value.scheme}. Only HTTP(s) "
                          "URLs are allowed")
            value.port
        except ValueError as e:
            self.fail(f"Error parsing URL: {e}")

        return value


class ipaddress(click.ParamType):
    name = "ipaddress"

    def convert(self, value, param, ctx):
        try:
            value = ip_address(value)
        except ValueError as e:
            self.fail(f"Error parsing ip address: {e}")

        return value


@click.command(
    help=("pantalaimon is a reverse proxy for matrix homeservers that "
          "transparently encrypts and decrypts messages for clients that "
          "connect to pantalaimon.\n\n"
          "HOMESERVER - the homeserver that the daemon should connect to.")
)
@click.option(
    "--proxy",
    type=URL(),
    default=None,
    help="A proxy that will be used to connect to the homeserver."
)
@click.option(
    "-k",
    "--ssl-insecure/--no-ssl-insecure",
    default=False,
    help="Disable SSL verification for the homeserver connection."
)
@click.option(
    "-l",
    "--listen-address",
    type=ipaddress(),
    default=ip_address("127.0.0.1"),
    help=("The listening address for incoming client connections "
          "(default: 127.0.0.1)")
)
@click.option(
    "-p",
    "--listen-port",
    type=int,
    default=8009,
    help="The listening port for incoming client connections (default: 8009)"
)
@click.argument(
    "homeserver",
    type=URL(),
)
def main(proxy, ssl_insecure, listen_address, listen_port, homeserver):
    ssl = None if ssl_insecure is False else False

    loop = asyncio.get_event_loop()
    proxy, app = loop.run_until_complete(init(
        homeserver.geturl(),
        proxy.geturl() if proxy else None,
        ssl
    ))

    web.run_app(app, host=str(listen_address), port=listen_port)


if __name__ == "__main__":
    main()
