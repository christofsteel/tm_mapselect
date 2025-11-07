from threading import Thread
from typing import Any, Callable, Concatenate, Optional, cast
from xmlrpc.client import Fault
from dataclasses import dataclass
from flask import Flask, request, render_template
from requests import get
from argparse import ArgumentParser
import os
import time
import sqlite3

from tm_mapselect.tmcolors import word_to_html

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
class ModeScriptSettings:
    time_limit: int

    def as_dict(self) -> dict[str, int]:
        return {
            "S_TimeLimit": self.time_limit,
        }

    def update_from_dict(self, settings: dict[str, str | bool | int]) -> None:
        if "S_TimeLimit" in settings:
            self.time_limit = check_cast(settings["S_TimeLimit"], int)

    @classmethod
    def from_dict(cls, settings: dict[str, str | bool | int]) -> "ModeScriptSettings":
        time_limit = check_cast(settings.get("S_TimeLimit", 0), int)
        return cls(time_limit=time_limit)


@dataclass
class ServerState:
    server_name: str
    maps: list[Map]
    players: list[str]
    current_map_index: int
    modescript_settings: ModeScriptSettings
    max_players: int

    @property
    def current_map(self) -> Map | None:
        if 0 <= self.current_map_index < len(self.maps):
            return self.maps[self.current_map_index]
        return None


def validate_connection[T, **P](
    inner: Callable[Concatenate[Any, P], T],
) -> Callable[Concatenate[Any, P], T]:
    def wrapper(self, *args: P.args, **kwargs: P.kwargs) -> T:
        if not self.remote.connalive:
            raise RuntimeError("Not connected to the dedicated server.")
        result = inner(self, *args, **kwargs)
        return result

    return wrapper


def check_cast[T](obj: Any, typ: type[T]) -> T:
    if not isinstance(obj, typ):
        raise TypeError(f"Expected type {typ}, got {type(obj)}")
    return cast(T, obj)


class ServerController:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        /,
        sqlite_cache_path: str = "cache.sql",
    ) -> None:
        self.remote = DedicatedRemote(
            host,
            port,
            username,
            password,
            apiVersion="2022-03-21",
        )
        self.state: Optional[ServerState] = None
        self._sqlite_cache_conn = sqlite3.connect(sqlite_cache_path)
        self._initialize_cache_db()
        self._uid_to_id_cache: dict[str, int] = self._load_uid_to_id_cache()
        self.update_thread: Thread = Thread(target=self._periodic_update, daemon=True)

    def _periodic_update(self) -> None:
        while True:
            try:
                self.update_state()
            except Exception as e:
                print(f"Error during periodic update: {e}")
            time.sleep(60)

    def _load_uid_to_id_cache(self) -> dict[str, int]:
        cursor = self._sqlite_cache_conn.cursor()
        cursor.execute("SELECT uid, id FROM map_uid_to_id")
        rows = cursor.fetchall()
        return {uid: id for uid, id in rows}

    def _initialize_cache_db(self) -> None:
        cursor = self._sqlite_cache_conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS map_uid_to_id (
                uid TEXT PRIMARY KEY,
                id INTEGER NOT NULL
            )
            """
        )
        self._sqlite_cache_conn.commit()

    @validate_connection
    def get_server_name(self) -> str:
        return check_cast(self.remote.call("GetServerName"), str)

    @validate_connection
    def get_max_players(self) -> int:
        result = check_cast(self.remote.call("GetMaxPlayers"), dict)
        if "CurrentValue" not in result:
            raise RuntimeError("Failed to get max players.")
        return check_cast(result["CurrentValue"], int)

    @validate_connection
    def set_max_players(self, max_players: int) -> None:
        try:
            self.remote.call("SetMaxPlayers", max_players)
        except Fault as e:
            raise RuntimeError(f"Failed to set max players: {e}") from e

    @validate_connection
    def get_player_list(self) -> list[str]:
        players = self.remote.call("GetPlayerList", 100, 0)
        if not isinstance(players, list) or len(players) == 0:
            raise RuntimeError("Failed to get player list.")
        players = players[1:]
        return [check_cast(player["NickName"], str) for player in players]

    def connect(self) -> bool:
        print("Connecting to the dedicated server...")
        connected = self.remote.connect()
        if connected:
            print("Collecting map infos...")
            maps = self.get_map_list()
            current_map_index = self.get_current_map_index()
            mode_script_settings = ModeScriptSettings.from_dict(
                self.get_mode_script_settings()
            )
            server_name = self.get_server_name()
            max_players = self.get_max_players()
            player_list = self.get_player_list()
            self.state = ServerState(
                server_name=server_name,
                maps=maps,
                players=player_list,
                current_map_index=current_map_index,
                modescript_settings=mode_script_settings,
                max_players=max_players,
            )
            self.update_thread.start()
        return connected

    @validate_connection
    def get_mode_script_settings(self) -> dict[str, bool | int | str]:
        return check_cast(self.remote.call("GetModeScriptSettings"), dict)

    @validate_connection
    def set_mode_script_settings(
        self, settings_dict: dict[str, bool | int | str]
    ) -> bool:
        try:
            current_mode_settings = self.get_mode_script_settings()
            current_mode_settings |= settings_dict
            result = self.remote.call("SetModeScriptSettings", current_mode_settings)
            if not isinstance(result, bool):
                raise RuntimeError("Failed to set mode script settings.")
        except Fault as e:
            raise RuntimeError(f"Failed to set mode script setting: {e}") from e
        return result

    def disconnect(self) -> None:
        self.remote.stop()

    @validate_connection
    def get_current_map_index(self) -> int:
        return check_cast(self.remote.call("GetCurrentMapIndex"), int)

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
        cursor = self._sqlite_cache_conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO map_uid_to_id (uid, id) VALUES (?, ?)",
            (map_uid, id),
        )
        self._sqlite_cache_conn.commit()
        return id

    @validate_connection
    def get_map_list(self, map_chunks: int = 5) -> list[Map]:
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

    @validate_connection
    def set_current_map_index(self, index: int) -> None:
        try:
            print(f"Jumping to map index {index}...")
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
            settings = self.get_mode_script_settings()
            self.state.modescript_settings.update_from_dict(settings)

    def update_player_list(self) -> None:
        if self.state:
            self.state.players = self.get_player_list()

    def update_state(self) -> None:
        self.update_map_list()
        self.update_current_map_index()
        self.update_mode_script_settings()
        self.update_player_list()


def create_app(tm_server, tm_xml_port, tm_user, tm_password) -> Flask:
    app = Flask(__name__)
    controller = ServerController(tm_server, tm_xml_port, tm_user, tm_password)
    controller.connect()

    @app.template_filter()
    def format_tm(word: str) -> str:
        return word_to_html(word)

    @app.route("/")
    def index():
        if not controller.state:
            return "<h1>Error: Not connected to the server.</h1>"

        return render_template(
            "index.html",
            state=controller.state,
        )

    @app.route("/jumpToMap/<int:map_index>")
    def jump_to_map(map_index: int):
        try:
            controller.set_current_map_index(map_index)
            controller.update_state()
            return f"Jumped to map index {map_index}. <a href='/'>Go back</a>"
        except RuntimeError as e:
            return f"Error: {e}. <a href='/'>Go back</a>"

    @app.route("/settings", methods=["POST"])
    def update_settings():
        try:
            timelimit = int(request.form["time_limit"])
            max_players = int(request.form["max_players"])
            result = controller.set_mode_script_settings({"S_TimeLimit": timelimit})
            if not result:
                raise RuntimeError("Failed to set time limit.")
            controller.set_max_players(max_players)
            controller.update_state()
            return "OK"
        except (ValueError, RuntimeError) as e:
            return f"Error: {e}"

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
            [f"<li>{key}: {value}</li>" for key, value in settings.as_dict().items()]
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
