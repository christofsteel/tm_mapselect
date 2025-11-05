from typing import Optional
from xmlrpc.client import Fault
from dataclasses import dataclass
from flask import Flask, request, render_template
from requests import get
from argparse import ArgumentParser
import os

from .gbxremote import DedicatedRemote


@dataclass
class Map:
    ID: int
    UId: str
    Name: str
    FileName: str
    Environnement: str
    Author: str
    AuthorNickname: str
    GoldTime: int
    CopperPrice: int
    MapType: str
    MapStyle: str


@dataclass
class ServerState:
    server_name: str
    maps: list[Map]
    players: list[str]
    current_map_index: int
    modescript_settings: dict[str, int | str | bool]

    @property
    def current_map(self) -> Map | None:
        if 0 <= self.current_map_index < len(self.maps):
            return self.maps[self.current_map_index]
        return None


class ServerController:
    def __init__(self, host: str, port: int, username: str, password: str):
        self.remote = DedicatedRemote(
            host,
            port,
            username,
            password,
            apiVersion="2022-03-21",
        )
        self.state: Optional[ServerState] = None
        self._uid_to_id_cache: dict[str, int] = {}

    def get_connection_string(self) -> str:
        if not self.remote.connalive:
            raise RuntimeError("Not connected to the dedicated server.")

        return f"#join={self.remote.host}:2350@Trackmania"

    def get_server_name(self) -> str:
        if not self.remote.connalive:
            raise RuntimeError("Not connected to the dedicated server.")
        name = self.remote.call("GetServerName")
        if not isinstance(name, str):
            raise RuntimeError("Failed to retrieve server name.")
        return name

    def connect(self) -> bool:
        print("Connecting to the dedicated server...")
        connected = self.remote.connect()
        if connected:
            print("Collecting map infos...")
            maps = self.get_map_list()
            current_map_index = self.get_current_map_index()
            mode_script_settings = self.get_mode_script_settings()
            server_name = self.get_server_name()
            self.state = ServerState(
                server_name=server_name,
                maps=maps,
                players=[],
                current_map_index=current_map_index,
                modescript_settings=mode_script_settings,
            )
        return connected

    def get_mode_script_settings(self) -> dict:
        if not self.remote.connalive:
            raise RuntimeError("Not connected to the dedicated server.")
        options = self.remote.call("GetModeScriptSettings")
        if not isinstance(options, dict):
            raise RuntimeError("Failed to retrieve mode script settings.")
        return options

    def set_mode_script_setting(self, key: str, value: bool | int | str) -> bool:
        if not self.remote.connalive:
            raise RuntimeError("Not connected to the dedicated server.")
        try:
            current_mode_settings = self.get_mode_script_settings()
            current_mode_settings[key] = value
            result = self.remote.call("SetModeScriptSettings", current_mode_settings)
            if not isinstance(result, bool):
                raise RuntimeError("Failed to set mode script settings.")
        except Fault as e:
            raise RuntimeError(f"Failed to set mode script setting: {e}") from e
        return result

    def disconnect(self) -> None:
        self.remote.stop()

    def get_current_map_index(self) -> int:
        if not self.remote.connalive:
            raise RuntimeError("Not connected to the dedicated server.")
        idx = self.remote.call("GetCurrentMapIndex")
        if not isinstance(idx, int):
            raise RuntimeError("Failed to retrieve current map index.")
        return idx

    def get_tmx_id(self, map_uid: str) -> int:
        if map_uid in self._uid_to_id_cache:
            return self._uid_to_id_cache[map_uid]
        response = get(
            f"https://trackmania.exchange/api/maps?uid={map_uid}&fields=MapId"
        )
        if response.status_code != 200:
            raise RuntimeError(f"Failed to retrieve TMX info for map UID {map_uid}.")
        id = response.json()["Results"][0]["MapId"]
        self._uid_to_id_cache[map_uid] = id
        return id

    def get_map_list(self, map_chunks: int = 5) -> list[Map]:
        if not self.remote.connalive:
            raise RuntimeError("Not connected to the dedicated server.")
        offset = 0
        maps = []
        new_maps = self.remote.call("GetMapList", map_chunks, offset)
        if not isinstance(new_maps, list):
            return []
        maps += new_maps
        offset += map_chunks
        while len(new_maps) >= map_chunks:
            new_maps = self.remote.call("GetMapList", map_chunks, offset)
            if not isinstance(new_maps, list):
                break
            maps += new_maps
            offset += map_chunks
        tmx_infos = [{"ID": self.get_tmx_id(map_data["UId"])} for map_data in maps]
        maps = [map_data | tmx_info for map_data, tmx_info in zip(maps, tmx_infos)]
        return [Map(**map_data) for map_data in maps]

    def set_current_map_index(self, index: int) -> None:
        if not self.remote.connalive:
            raise RuntimeError("Not connected to the dedicated server.")
        try:
            self.remote.call("JumpToMapIndex", index)
        except Fault as e:
            raise RuntimeError(f"Failed to set current map index: {e}") from e

    def update_map_list(self) -> None:
        if self.state:
            self.state.maps = self.get_map_list()

    def update_current_map_index(self) -> None:
        if self.state:
            self.state.current_map_index = self.get_current_map_index()

    def update_mode_script_settings(self) -> None:
        if self.state:
            self.state.modescript_settings = self.get_mode_script_settings()

    def update_state(self) -> None:
        self.update_map_list()
        self.update_current_map_index()
        self.update_mode_script_settings()


def create_app(tm_server, tm_xml_port, tm_user, tm_password) -> Flask:
    app = Flask(__name__)
    controller = ServerController(tm_server, tm_xml_port, tm_user, tm_password)
    controller.connect()

    @app.route("/")
    def index():
        if not controller.state:
            return "<h1>Error: Not connected to the server.</h1>"
        maps = controller.state.maps
        timelimit = controller.state.modescript_settings.get("S_TimeLimit", 0)

        current_map = controller.state.current_map_index
        server_name = controller.state.server_name

        return render_template(
            "index.html",
            maps=maps,
            current_map_index=current_map,
            timelimit=timelimit,
            server_name=server_name,
        )

    @app.route("/jumpToMap/<int:map_index>")
    def jump_to_map(map_index: int):
        try:
            controller.set_current_map_index(map_index)
            controller.update_state()
            return f"Jumped to map index {map_index}. <a href='/'>Go back</a>"
        except RuntimeError as e:
            return f"Error: {e}. <a href='/'>Go back</a>"

    @app.route("/setTimeLimit", methods=["POST"])
    def set_time_limit():
        try:
            timelimit = int(request.form["timelimit"])
            result = controller.set_mode_script_setting("S_TimeLimit", timelimit)
            if not result:
                raise RuntimeError("Failed to set time limit.")
            controller.update_state()
            return f"Time limit set to {timelimit} seconds. <a href='/'>Go back</a>"
        except (ValueError, RuntimeError) as e:
            return f"Error: {e}. <a href='/'>Go back</a>"

    @app.route("/refresh")
    def refresh():
        controller.update_state()
        return "Server state refreshed. <a href='/'>Go back</a>"

    @app.route("/getsettings")
    def get_settings():
        if not controller.state:
            return "<h1>Error: Not connected to the server.</h1>"
        settings = controller.state.modescript_settings
        settings_list = "".join(
            [f"<li>{key}: {value}</li>" for key, value in settings.items()]
        )

        return f"<h1>Mode Script Settings</h1><ul>{settings_list}</ul><a href='/'>Go back</a>"

    return app


def main():
    server = os.getenv("TM_SERVER", None)
    xml_port = int(os.getenv("TM_XML_PORT", "5000"))
    username = os.getenv("TM_USERNAME", None)
    password = os.getenv("TM_PASSWORD", None)
    host = os.getenv("APP_HOST", "localhost")
    port = int(os.getenv("APP_PORT", "8080"))

    arg_parser = ArgumentParser(description="Trackmania Dedicated Server Controller")
    arg_parser.add_argument(
        "--server",
        "-s",
        type=str,
        required=True if not server else False,
        help="Trackmania server IP address",
        default=server,
    )
    arg_parser.add_argument(
        "--xml-port",
        "-x",
        type=int,
        default=xml_port,
        help="XML-RPC port of the Trackmania server",
    )
    arg_parser.add_argument(
        "--username",
        "-u",
        type=str,
        required=True if not username else False,
        default=username,
        help="Username for XML-RPC authentication",
    )
    arg_parser.add_argument(
        "--password",
        "-p",
        type=str,
        required=True if not password else False,
        default=password,
        help="Password for XML-RPC authentication",
    )
    arg_parser.add_argument(
        "--host",
        "-H",
        type=str,
        default=host,
        help="Bind host for the web server",
    )
    arg_parser.add_argument(
        "--port", "-P", type=int, default=port, help="Bind port for the web server"
    )

    args = arg_parser.parse_args()

    app = create_app(args.server, args.xml_port, args.username, args.password)
    app.run(host=args.host, port=args.port, debug=True)


if __name__ == "__main__":
    main()
