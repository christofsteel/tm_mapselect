from threading import Thread, Event
from requests_oauthlib import OAuth2Session
from typing import Any, Callable, Concatenate, Literal, Optional, Self, cast
from xmlrpc.client import Fault
from dataclasses import dataclass
from flask import Flask, request, render_template, session, redirect
from requests import get
from argparse import ArgumentParser
import os
import sqlite3

from tm_mapselect.tmcolors import word_to_html

from .gbxremote import DedicatedRemote


@dataclass
class MapUserData:
    record: int
    medal: (
        Literal["Author"]
        | Literal["Gold"]
        | Literal["Silver"]
        | Literal["Bronze"]
        | None
    )

    @classmethod
    def from_record(cls, record: int, medals: dict[str, int]) -> Self:
        data = cls(record, None)
        if record < medals["Bronze"]:
            data.medal = "Bronze"
        if record < medals["Silver"]:
            data.medal = "Silver"
        if record < medals["Gold"]:
            data.medal = "Gold"
        if record < medals["Author"]:
            data.medal = "Author"
        return data


@dataclass
class Map:
    ID: int
    OnlineID: str
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
    Medals: dict[str, int]


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


@dataclass
class DBEntry:
    uid: str
    id: int
    online_id: str
    author_medal: int
    gold_medal: int
    silver_medal: int
    bronze_medal: int

    @classmethod
    def create_table_sql(cls, cursor) -> None:
        # get entries and type hints
        fields = cls.__annotations__

        sql_type_mapping = {str: "TEXT", int: "INTEGER"}

        rows = ", ".join(
            f"{field} {sql_type_mapping[typ]}"
            + (" PRIMARY KEY" if field == "id" else "")
            for field, typ in fields.items()
        )

        cursor.execute(f"CREATE TABLE IF NOT EXISTS map_uid_to_id ({rows})")

    def insert_or_replace(self, cursor) -> None:
        self_tuple = tuple(value for value in self.__dict__.values())
        entry_strings = ", ".join("?" for _ in self_tuple)
        field_names = ", ".join(self.__dict__.keys())

        return cursor.execute(
            f"INSERT OR REPLACE INTO map_uid_to_id ({field_names}) VALUES ({entry_strings})",
            self_tuple,
        )

    @classmethod
    def select_all(cls, cursor) -> list["DBEntry"]:
        cursor.execute("SELECT * FROM map_uid_to_id")
        rows = cursor.fetchall()
        return [cls(*row) for row in rows]


class DB:
    def __init__(self, db_path: str) -> None:
        self.conn = sqlite3.connect(db_path)
        self._initialize_db()

    def _initialize_db(self) -> None:
        cursor = self.conn.cursor()
        DBEntry.create_table_sql(cursor)
        self.conn.commit()

    def db_add_entry(self, entry: DBEntry) -> None:
        cursor = self.conn.cursor()
        entry.insert_or_replace(cursor)
        self.conn.commit()

    def get_all_entries(self) -> dict[str, tuple[int, str, dict[str, int]]]:
        cursor = self.conn.cursor()
        entry = DBEntry.select_all(cursor)
        return {
            e.uid: (
                e.id,
                e.online_id,
                {
                    "Author": e.author_medal,
                    "Gold": e.gold_medal,
                    "Silver": e.silver_medal,
                    "Bronze": e.bronze_medal,
                },
            )
            for e in entry
        }


class NadeoAPI:
    LOGIN_URL = "https://api.trackmania.com/oauth/authorize"
    TOKEN_URL = "https://api.trackmania.com/api/access_token"
    API_BASE_URL = "https://api.trackmania.com/"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri="http://localhost:8080/.auth/callback",
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        # self.session: OAuth2Session = OAuth2Session(
        #     client_id, redirect_uri=redirect_uri
        # )

    def get_authorization_url(self) -> str:
        session = OAuth2Session(
            self.client_id, redirect_uri=self.redirect_uri, scope=["read_favorite"]
        )

        authorization_url, state = session.authorization_url(self.LOGIN_URL)
        return authorization_url

    def get_token(self, code: str) -> dict[str, Any]:
        session = OAuth2Session(self.client_id, redirect_uri=self.redirect_uri)
        token = session.fetch_token(
            self.TOKEN_URL,
            code=code,
            client_secret=self.client_secret,
        )
        return token

    def get_user(self, token: dict[str, Any]) -> dict[str, Any]:
        session = OAuth2Session(
            self.client_id,
            token=token,
        )
        response = session.get(f"{self.API_BASE_URL}api/user")
        response.raise_for_status()
        return response.json()

    def get_records(
        self, token: dict[str, Any], maps: list[Map]
    ) -> dict[str, MapUserData]:
        session = OAuth2Session(
            self.client_id,
            token=token,
        )

        dict_of_maps = {m.OnlineID: m for m in maps}
        records: dict[str, MapUserData] = {}
        chunked_maps = [maps[i : i + 20] for i in range(0, len(maps), 20)]
        for chunk in chunked_maps:
            response = session.get(
                f"{self.API_BASE_URL}api/user/map-records",
                params={"mapId[]": map(lambda m: m.OnlineID, chunk)},
            )
            response.raise_for_status()
            records |= {
                record["mapId"]: MapUserData.from_record(
                    record["time"], dict_of_maps[record["mapId"]].Medals
                )
                for record in response.json()
            }
        return records

    def get_favorite_maps(self, token: dict[str, Any]) -> dict[str, Any]:
        session = OAuth2Session(
            self.client_id,
            token=token,
        )
        response = session.get(f"{self.API_BASE_URL}api/user/maps/favorite")
        response.raise_for_status()
        return response.json()

    def get_account_ids(self, token: dict[str, Any]) -> dict[str, Any]:
        session = OAuth2Session(
            self.client_id,
            token=token,
        )
        response = session.get(
            f"{self.API_BASE_URL}api/display-names/account-ids",
            params={"displayName[]": "christ.of.steel"},
        )
        response.raise_for_status()
        return response.json()


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
        self._uid_to_id_cache: dict[str, tuple[int, str, dict[str, int]]] = {}
        self.update_thread: Thread = Thread(target=self._periodic_update, daemon=True)
        self.update_event: Event = Event()
        self.sqlite_cache_path = sqlite_cache_path

    def _periodic_update(self) -> None:
        self._db = DB(self.sqlite_cache_path)
        self._uid_to_id_cache = self._db.get_all_entries()
        while True:
            # try:
            self.update_state()
            # except Exception as e:
            # print(f"Error during periodic update: {e}")
            self.update_event.wait(timeout=60)
            self.update_event.clear()  # Race condition, but I don't care

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
            maps = []
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

    def get_tmx_ids(self, map_uid: str) -> tuple[int, str, dict[str, int]]:
        if map_uid in self._uid_to_id_cache:
            return self._uid_to_id_cache[map_uid]
        response = get(
            f"https://trackmania.exchange/api/maps?uid={map_uid}",
            params={
                "fields": "MapId,OnlineMapId,Medals.Author,Medals.Gold,Medals.Silver,Medals.Bronze"
            },
        )
        if response.status_code != 200:
            print(response.url)
            raise RuntimeError(f"Failed to retrieve TMX info for map UID {map_uid}.")

        id = response.json()["Results"][0]["MapId"]
        online_map_id = response.json()["Results"][0]["OnlineMapId"]
        medals = response.json()["Results"][0]["Medals"]

        self._uid_to_id_cache[map_uid] = (id, online_map_id, medals)
        self._db.db_add_entry(
            DBEntry(
                uid=map_uid,
                id=id,
                online_id=online_map_id,
                author_medal=medals.get("Author", 0),
                gold_medal=medals.get("Gold", 0),
                silver_medal=medals.get("Silver", 0),
                bronze_medal=medals.get("Bronze", 0),
            )
        )

        return id, online_map_id, medals

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
        for map_data in maps:
            tmx_id, online_id, medals = self.get_tmx_ids(map_data["UId"])
            map_data["ID"] = tmx_id
            map_data["OnlineID"] = online_id
            map_data["Medals"] = medals

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


def create_app(
    tm_server, tm_xml_port, tm_user, tm_password, client_id, client_secret, redirect_uri
) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.urandom(24)
    controller = ServerController(tm_server, tm_xml_port, tm_user, tm_password)
    controller.connect()
    nadeo_api = NadeoAPI(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
    )

    @app.template_filter()
    def format_tm(word: str) -> str:
        return word_to_html(word)

    @app.template_filter()
    def format_time(ms: int) -> str:
        seconds = ms // 1000
        minutes = seconds // 60
        seconds = seconds % 60
        milliseconds = ms % 1000
        return f"{minutes:02}:{seconds:02}.{milliseconds:03}"

    @app.route("/")
    def index():
        if not controller.state:
            return "<h1>Error: Not connected to the server.</h1>"

        records = {}
        if "token" in session:
            records = nadeo_api.get_records(session["token"], controller.state.maps)

        return render_template(
            "index.html",
            state=controller.state,
            authorization_url=nadeo_api.get_authorization_url(),
            user_name=session.get("displayName", None),
            records=records,
        )

    @app.route("/jumpToMap/<int:map_index>")
    def jump_to_map(map_index: int):
        try:
            controller.set_current_map_index(map_index)
            controller.update_event.set()
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
            controller.update_event.set()
            return "OK"
        except (ValueError, RuntimeError) as e:
            return f"Error: {e}"

    @app.route("/refresh")
    def refresh():
        controller.update_event.set()
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

    @app.route("/.auth/callback")
    def auth_callback():
        code = request.args.get("code")
        if not code:
            return "Error: No code provided."
        state = request.args.get("state")  # TODO: validate state

        token = nadeo_api.get_token(code)
        session["token"] = token

        user_info = nadeo_api.get_user(token)

        session["displayName"] = user_info.get("displayName", "Unknown User")

        return redirect("/")

    @app.route("/logout")
    def logout():
        session.pop("token", None)
        session.pop("displayName", None)
        return redirect("/")

    return app


def main():
    server = os.getenv("TM_SERVER", None)
    xml_port = int(os.getenv("TM_XML_PORT", "5000"))
    username = os.getenv("TM_USERNAME", None)
    password = os.getenv("TM_PASSWORD", None)
    host = os.getenv("APP_HOST", "localhost")
    port = int(os.getenv("APP_PORT", "8080"))
    client_id = os.getenv("NADEO_CLIENT_ID", None)
    client_secret = os.getenv("NADEO_CLIENT_SECRET", None)
    redirect_uri = os.getenv(
        "NADEO_REDIRECT_URI", "http://localhost:8080/.auth/callback"
    )

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
    arg_parser.add_argument(
        "--client-id",
        "-c",
        type=str,
        required=True if not client_id else False,
        default=client_id,
        help="Nadeo API Client ID",
    )
    arg_parser.add_argument(
        "--client-secret",
        "-C",
        type=str,
        required=True if not client_secret else False,
        default=client_secret,
        help="Nadeo API Client Secret",
    )
    arg_parser.add_argument(
        "--redirect-uri",
        "-r",
        type=str,
        default=redirect_uri,
        help="Nadeo API Redirect URI",
    )

    args = arg_parser.parse_args()

    app = create_app(
        args.server,
        args.xml_port,
        args.username,
        args.password,
        args.client_id,
        args.client_secret,
        args.redirect_uri,
    )
    app.run(host=args.host, port=args.port, debug=True)


if __name__ == "__main__":
    main()
