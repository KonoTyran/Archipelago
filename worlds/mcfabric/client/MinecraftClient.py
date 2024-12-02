import argparse
import io
import json
import os.path
import pkgutil
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from enum import Enum
from math import floor, log
from queue import Queue

from tkinter import filedialog, Canvas
from typing import List, Optional, TypedDict
from urllib.parse import urlparse

from kivy import Config
from kivy.app import App
from kivy.clock import Clock, mainthread
from kivy.core.image import Image as CoreImage
from kivy.core.window import Window
from kivy.lang import Builder
from kivy.network.urlrequest import UrlRequest, UrlRequestUrllib
from kivy.properties import StringProperty, ObjectProperty, NumericProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.recycleview import RecycleView
from kivy.uix.stacklayout import StackLayout
from kivy.uix.textinput import TextInput
from kivy.uix.widget import Widget
from kivy.uix.screenmanager import ScreenManager, Screen, NoTransition
from kivy.utils import escape_markup

import Utils

version_file_endpoint = "https://raw.githubusercontent.com/KonoTyran/archipelago-randomizer-fabric/main/versions/fabric_versions.json"
fabric_server_url = "https://meta.fabricmc.net/v2/versions/loader/[minecraft]/[fabric]/1.0.1/server/jar"

options = Utils.get_settings()["mcfabric_options"]

os.environ["KIVY_NO_CONSOLELOG"] = "1"
os.environ["KIVY_NO_FILELOG"] = "1"
os.environ["KIVY_NO_ARGS"] = "1"
os.environ["KIVY_LOG_ENABLE"] = "0"

Config.set("input", "mouse", "mouse,disable_multitouch")
Config.set("kivy", "exit_on_escape", "0")
Config.set("graphics", "multisamples", "0")

parser = argparse.ArgumentParser()
parser.add_argument("apmc_file", default=None, nargs='?', help="Path to an Archipelago Minecraft data file (.apmc)")

args, rest = parser.parse_known_args()


def load_text(*path: str):
    return pkgutil.get_data(__name__, "/".join(path)).decode()


def load_image(*path: str):
    data = io.BytesIO(pkgutil.get_data(__name__, "/".join(path)))
    texture = CoreImage(data, ext="png")
    return texture


def format_bytes(size):
    power = 0 if size <= 0 else floor(log(size, 1024))
    return f"{round(size / 1024 ** power, 2)} {['B', 'KB', 'MB', 'GB', 'TB'][int(power)]}"


class APMC(TypedDict):
    world_seed: str
    seed_name: str
    player_name: str
    player_id: int
    client_version: int
    structures: dict[str, str]
    advancement_goal: int
    egg_shards_required: int
    egg_shards_available: int
    required_bosses: str
    MC35: bool
    death_link: bool
    starting_items: list
    race: bool
    server: Optional[str]
    port: Optional[int]


class Version(TypedDict):
    version: int
    java: int
    fabric: str
    minecraft: str
    url: str


class ServerStatus(Enum):
    STOPPED = 0
    STARTING = 1
    RUNNING = 2

    def __lt__(self, other):
        return self.value < other.value

    def __gt__(self, other):
        return self.value > other.value

    def __eq__(self, other):
        return self.value == other.value


class MinecraftClient(App):
    stop = threading.Event()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.welcome_window: Optional[WelcomeWindow] = None
        self.window_manager: Optional[WindowManager] = None
        self.server_window: Optional[ServerWindow] = None
        self.minecraft_versions: dict[str, list[Version]] = {}
        self.apmc: Optional[APMC] = None
        self.version: Optional[Version] = None
        self.server = None
        self.java_url = None
        self.download: Optional[UrlRequestUrllib] = None
        self.status: ServerStatus = ServerStatus.STOPPED
        self.apmc_path = None

    def build(self):
        Builder.load_string(load_text("layouts", "minecraft.kv"))
        self.window_manager = WindowManager(transition=NoTransition())
        self.welcome_window = WelcomeWindow(self)
        self.server_window = ServerWindow(self)
        self.window_manager.add_widget(self.welcome_window)
        self.window_manager.add_widget(self.server_window)
        Clock.schedule_once(self.init, 1)

        Window.bind(on_request_close=self.on_request_close)

        # send our request out to fetch the versions file
        UrlRequest(version_file_endpoint, self.process_versions, on_failure=self.process_local_versions)

        return self.window_manager

    def on_request_close(self, *arg):
        self.send_command("stop")
        Clock.schedule_interval(self.close, 1 / 60)
        return True

    def close(self, dt):
        if self.stop.is_set():
            sys.exit()
            pass

    def get_application_icon(self):
        return load_image("assets", "icon.png")

    def get_java_url(self) -> Optional[str]:
        if Utils.is_windows:
            return f"https://corretto.aws/downloads/latest/amazon-corretto-{self.version["java"]}-x64-windows-jdk.zip"
        elif Utils.is_linux:
            if platform.machine() == "aarch64":
                return f"https://corretto.aws/downloads/latest/amazon-corretto-{self.version["java"]}-aarch64-linux-jdk.tar.gz"
            else:
                return f"https://corretto.aws/downloads/latest/amazon-corretto-{self.version["java"]}-x64-linux-jdk.tar.gz"
        return None

    def get_jdk(self) -> Optional[str]:
        jdk_exe = os.path.join(options.server_directory, f"jdk{self.version["java"]}", "bin")
        if Utils.is_windows:
            jdk_exe = os.path.join(jdk_exe, "java.exe")
        else:
            jdk_exe = os.path.join(jdk_exe, "java")

        if os.path.isfile(jdk_exe):
            return jdk_exe

        return None

    def get_server_jar_name(self):
        return f"fabric-server-mc.{self.version["minecraft"]}.{self.version["fabric"]}.jar"

    def get_server_jar(self) -> Optional[str]:
        server_jar = os.path.join(options.server_directory, self.get_server_jar_name())

        if os.path.isfile(server_jar):
            return server_jar

        return None

    def init(self, dt=None):
        layout: Widget = self.welcome_window.ids.saves
        layout.clear_widgets()
        saves = self.get_recent_items()
        if len(saves) == 0:
            layout.add_widget(Label(text="No saves"))
        else:
            for name, path in saves:
                layout.add_widget(RecentItem(name=name, path=path, client=self))

        ids: StackLayout = self.welcome_window.ids
        ids.path.value = options.server_directory
        ids.max_memory.value = options.max_heap_size
        ids.min_memory.value = options.min_heap_size

    def process_versions(self, response: UrlRequestUrllib, result):
        self.minecraft_versions: dict[str, Version] = json.loads(result)
        with open(os.path.join(options.server_directory, "minecraft_versions.json"), 'w') as f:
            json.dump(self.minecraft_versions, f)
        self.auto_start_server()

    def process_local_versions(self, response: UrlRequestUrllib, result: str):
        self.log_warn(f"unable to fetch remote versions due to {result}. falling back to local cache.")
        if os.path.isfile(os.path.join(options.server_directory, "minecraft_versions.json")):
            with open(os.path.join(options.server_directory, "minecraft_versions.json"), 'r') as f:
                self.minecraft_versions = json.load(f)
            self.auto_start_server()
        else:
            self.apmc_path = None
            info_dialog(title="Error",
                        content=f"Unable to find Versions file. Must be connected to the internet on initial startup to fetch version and mod info.")
            self.log_error("No versions file found. Must be connected to the internet on initial startup to fetch version and mod info.")

    def get_recent_items(self) -> List:
        directory = os.path.abspath(options.server_directory)
        saves = []
        for directory in os.listdir(directory):
            if directory.startswith("Archipelago-"):
                saves.append(("test", directory))
        return saves

    def auto_start_server(self):
        self.apmc_path = os.path.abspath(args.apmc_file) if args.apmc_file else None
        if self.apmc_path:
            self.open_apmc(path=self.apmc_path)

    def open_apmc(self, path=None):
        self.apmc_path = path
        if self.apmc_path is None:
            self.apmc_path = filedialog.askopenfilename(title="Choose AP Minecraft file",
                                              filetypes=(("Archipelago Minecraft", "*.apmc"),))
        apmc: APMC
        if self.apmc_path is None or self.apmc_path == "" or os.path.isfile(self.apmc_path) is False:
            return
        with open(self.apmc_path, "r") as f:
            data = f.read()

            if data.startswith("e"):
                from base64 import b64decode
                apmc = json.loads(b64decode(data))
            elif data.startswith("{"):
                apmc = json.loads(data)

        if apmc is not None:
            self.start_server(apmc)

    def start_server(self, apmc: APMC) -> None:
        self.apmc = apmc

        try:
            self.version: Version = next(filter(lambda entry: entry['version'] == self.apmc["client_version"],
                                                self.minecraft_versions[options.release_channel]))
            self.server_window.status.text = f"Initializing {self.version["minecraft"]}"

            self.window_manager.current = "Server"
            self.start_server_check_jdk()

        except KeyError:
            self.log_error(f"unable to find version {self.apmc['client_version']} in {options.release_channel}")
            info_dialog(title="Error", content=f"Unable to find version {self.apmc['client_version']} in the {options.release_channel} channel.")
            self.apmc_path = None
            return



    @mainthread
    def start_server_check_jdk(self):
        # check jdk
        if self.get_jdk() is None:
            self.download_jdk(self.get_java_url())
        else:
            self.start_server_check_server_jar()

    @mainthread
    def start_server_check_server_jar(self):
        if self.get_server_jar() is None:
            self.download_server_jar(fabric_server_url.replace("[minecraft]", self.version["minecraft"]).replace("[fabric]", self.version["fabric"]))
        else:
            threading.Thread(target=self.server_thread).start()

    def download_server_jar(self, url):
        self.download = UrlRequest(url, on_progress=self.server_jar_download_progress, on_finish=self.server_jar_finished,
                                   on_success=self.server_jar_run, on_error=self.server_jar_error, on_redirect=self.server_jar_redirect,
                                   chunk_size=102400)
        self.server_window.close_progress_bar_dialog()
        self.server_window.show_progress_bar_dialog("Downloading Server Jar", f"Downloading Fabric {self.version["fabric"]} for Minecraft {self.version["minecraft"]}", 100)
        self.log_info(f"downloading server jar from {url}")

    def server_jar_download_progress(self, request, current_size, total_size):
        if self.server_window.progress_popup is None:
            return
        if total_size > 0:
            self.server_window.progress_popup.progress = current_size / total_size * 100
            self.server_window.progress_popup.progress_text = f"Downloading... {format_bytes(current_size)} / {format_bytes(total_size)}"

    def server_jar_redirect(self, request: UrlRequestUrllib, result: str):
        old_url = urlparse(request.url)
        loc = request.resp_headers['Location']
        url = f"{old_url.scheme}://{old_url.netloc}{loc}"
        self.download_server_jar(url)

    def server_jar_error(self, request, error):
        info_dialog(title="Error", content=f"There was an error downloading Server Jar \n {error}")
        self.log_error(f"download error: {error}")

    def server_jar_run(self, request: UrlRequestUrllib, result):
        self.server_window.close_progress_bar_dialog()
        self.log_info(f"server jar downloaded to {options.server_directory}")

        try:
            with open(os.path.join(options.server_directory, self.get_server_jar_name()), 'wb') as f:
                f.write(result)
        except Exception as e:
            self.log_error(f"error writing server jar: {e}")
            info_dialog(title="Error", content=f"Error writing Fabric to {options.server_directory}")
            return

        self.start_server_check_server_jar()

    def server_jar_finished(self, request: UrlRequestUrllib):
        if request.resp_status == 200:
            self.server_window.close_progress_bar_dialog()

    def download_jdk(self, url):
        self.download = UrlRequest(url, on_progress=self.jdk_download_progress, on_finish=self.jdk_finished,
                                   on_success=self.jdk_extract, on_error=self.jdk_error, on_redirect=self.jdk_redirect,
                                   chunk_size=102400)
        self.server_window.close_progress_bar_dialog()
        self.server_window.show_progress_bar_dialog("Downloading JDK", f"Downloading Java {self.version["java"]}", 100)
        self.log_info(f"downloading jdk from {url}")

    def jdk_download_progress(self, request, current_size, total_size):
        if self.server_window.progress_popup is None:
            return
        if total_size > 0:
            self.server_window.progress_popup.progress = current_size / total_size * 100
            self.server_window.progress_popup.progress_text = f"Downloading... {format_bytes(current_size)} / {format_bytes(total_size)}"

    def jdk_redirect(self, request: UrlRequestUrllib, result: str):
        old_url = urlparse(request.url)
        loc = request.resp_headers['Location']
        url = f"{old_url.scheme}://{old_url.netloc}{loc}"
        self.download_jdk(url)

    def jdk_error(self, request, error):
        info_dialog(title="Error", content=f"There was an error downloading Java \n {error}")
        self.log_error(f"download error: {error}")

    def jdk_extract(self, request: UrlRequestUrllib, result):
        self.server_window.close_progress_bar_dialog()
        self.log_info(f"extracting jdk to {options.server_directory}")

        def extract():
            # extract the jdk
            import zipfile
            from io import BytesIO

            jdk_dir = os.path.join(options.server_directory, f"jdk{self.version["java"]}")

            with zipfile.ZipFile(BytesIO(result)) as archive:
                # filter out all the directories
                file_list = [name for name in archive.namelist() if not archive.getinfo(name).is_dir()]
                self.open_progress_bar_dialog("Extracting JDK", f"Extracting Java {self.version["java"]}",
                                              len(file_list))
                for index, full_name in enumerate(file_list):
                    file_path = list(filter(bool, full_name.split("/")))
                    del file_path[0]

                    target_path = os.path.join(jdk_dir, *file_path)
                    target_dir = os.path.dirname(target_path)
                    os.makedirs(target_dir, exist_ok=True)

                    self.set_progress(index)
                    with archive.open(full_name) as source:
                        with open(target_path, 'wb') as dest:
                            shutil.copyfileobj(source, dest)
            self.finish_jdk_extract()

        threading.Thread(target=extract).start()

    @mainthread
    def finish_jdk_extract(self):
        self.log_info(f"jdk extracted to {options.server_directory}")
        self.server_window.close_progress_bar_dialog()
        self.start_server_check_server_jar()

    @mainthread
    def open_progress_bar_dialog(self, title, content, max):
        self.server_window.show_progress_bar_dialog(title, content, max)

    @mainthread
    def set_progress(self, value):
        if self.server_window.progress_popup is not None:
            self.server_window.progress_popup.progress = value

    def jdk_finished(self, request: UrlRequestUrllib):
        if request.resp_status == 200:
            self.server_window.close_progress_bar_dialog()
        self.download = None

    def server_thread(self):

        self.status = ServerStatus.STOPPED
        self.server_window.background_color = (.5, .1, .1, 1)
        world_name = f"Archipelago-{self.apmc['seed_name']}-P{self.apmc['player_id']}"
        world_dir = os.path.join(options.server_directory, world_name)
        if not os.path.isdir(world_dir):
            os.makedirs(world_dir)
        save_path = os.path.join(world_dir, "save.apmc")
        if save_path != self.apmc_path:
            shutil.copyfile(self.apmc_path, save_path)
        os.environ["JAVA_OPTS"] = ""
        self.server = subprocess.Popen((self.get_jdk(),
                                        "-jar",
                                        self.get_server_jar(),
                                        "--nogui",
                                        "--world",
                                        world_name,
                                        ),
                                       stderr=subprocess.PIPE,
                                       stdout=subprocess.PIPE,
                                       stdin=subprocess.PIPE,
                                       encoding="utf-8",
                                       text=True,
                                       cwd=options.server_directory
                                       )

        server_queue = Queue()
        stream_server_output(self.server.stdout, server_queue, self.server)
        stream_server_output(self.server.stderr, server_queue, self.server)

        while not self.stop.is_set():
            if self.server.poll() is not None:
                self.log_raw("[color=FFFF00]Minecraft server has exited.[/color]")
                self.stop.set()

            while not server_queue.empty():
                raw_message: str = server_queue.get()

                match = re.match("^\[[0-9:]+] \[.+/(WARN|INFO|ERROR)] \[.+]: (.*)", raw_message)
                if match:
                    level = match.group(1)
                    msg = escape_markup(match.group(2))

                    if level == "WARN":
                        self.log_warn(msg)
                    elif level == "ERROR":
                        self.log_error(msg)
                    elif level == "INFO":
                        self.log_info(msg)
                else:
                    self.log_info(raw_message)

                if self.status < ServerStatus.RUNNING:

                    server_starting_match = re.match("^\[[0-9:]+] \[main/INFO]: Loading Minecraft ([0-9.]+)",
                                                     raw_message)
                    if server_starting_match:
                        self.log_info(f"Starting Minecraft {server_starting_match.group(1)}")
                        self.server_window.status.text = f"Starting Server for {server_starting_match.group(1)}"
                        self.server_window.background_color = (.5, .5, .0, 1)
                        self.version["minecraft"] = server_starting_match.group(1)
                        self.status = ServerStatus.STARTING

                    server_started_match = re.match(
                        "^\[[0-9:]+] \[Server thread/INFO]: Done \([0-9.]+s\)! For help, type \"help\"", raw_message)
                    if server_started_match:
                        self.server_window.status.text = f"Server Running. Connect to `127.0.0.1` in Minecraft {self.version["minecraft"]}"
                        self.server_window.background_color = (.1, .5, .1, 1)
                        self.status = ServerStatus.RUNNING

                server_queue.task_done()
            time.sleep(0.01)

    def send_command(self, cmd):
        try:
            self.server.stdin.write(f'{cmd}\n')
            self.server.stdin.flush()
        except AttributeError:
            sys.exit()

    @mainthread
    def log_info(self, msg):
        self.server_window.log.on_message_markup(f"[b][INFO][/b] {escape_markup(msg)}")

    @mainthread
    def log_warn(self, msg):
        self.server_window.log.on_message_markup(f"[color=FFFF00][b][WARN][/b][/color] {escape_markup(msg)}")

    @mainthread
    def log_error(self, msg):
        self.server_window.log.on_message_markup(f"[color=FFFF00][b][ERROR][/b][/color] {escape_markup(msg)}")

    @mainthread
    def log_raw(self, msg):
        self.server_window.log.on_message_markup(msg)


def stream_server_output(pipe, queue, process):
    def queuer():
        while process.poll() is None:
            text = pipe.readline().rstrip().expandtabs()
            if text:
                queue.put_nowait(text)

    thread = threading.Thread(target=queuer, name="Minecraft Output Queue", daemon=True)
    thread.start()
    return thread


class TextOption(GridLayout):
    value = StringProperty()
    label = StringProperty()
    button_label = StringProperty()


class FolderOption(TextOption):

    def button_press(self):
        new_dir = filedialog.askdirectory(title="Choose Server Directory", initialdir=options.server_directory)
        if new_dir:
            self.value = new_dir


class RecentItem(BoxLayout):
    name = StringProperty()
    path = StringProperty()
    client: MinecraftClient = ObjectProperty()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        icon_delete = load_image("assets", "delete.png")
        icon_edit = load_image("assets", "edit.png")
        self.ids.delete_icon.texture = icon_delete.texture
        self.ids.rename_icon.texture = icon_edit.texture

    def load(self):
        print(f"Load path: {self.path}")
        save_path = os.path.join(options.server_directory, self.path, "save.apmc")
        if os.path.isfile(save_path):
            self.client.open_apmc(save_path)
        else:
            info_dialog(title="Error", content=f"Unable to find save file for world {self.path}")

    def delete(self):
        self.client.welcome_window.confirm_delete(target=self.path, title="Confirm Delete",
                                                  content=f"Delete {self.name}?\nThis Action is permanent.")

    def rename(self):
        self.client.welcome_window.edit(title="Confirm Edit", content=f"Rename {self.name}", edit=self)


class ConfirmDialog(Popup):
    text = StringProperty()
    confirm_text = StringProperty()
    cancel_text = StringProperty()


class InfoDialog(Popup):
    text = StringProperty()
    button_text = StringProperty()


class ProgressBarDialog(Popup):
    text = StringProperty("")
    progress_text = StringProperty("")
    progress = NumericProperty(0)
    max = NumericProperty(100)

    def __init__(self, max, **kwargs):
        super().__init__(**kwargs)
        self.max = max


def confirm_prompt(confirm=None, title="Prompt", content="Are you sure?", cancel=None):
    popup = ConfirmDialog(title=title, text=content, confirm_text="Yes", cancel_text="No")
    popup.open()

    if cancel is not None:
        popup.ids.cancel.bind(on_press=cancel)

    if confirm is not None:
        popup.ids.confirm.bind(on_press=confirm)


def info_dialog(title="Prompt", content="Are you sure?", cancel=None):
    popup = InfoDialog(title=title, text=content, button_text="OK")
    popup.open()


def edit_prompt(confirm, title="Prompt", content="Are you sure?", cancel=None, edit: RecentItem = None):
    popup = ConfirmDialog(title=title, text=content, confirm_text="Confirm", cancel_text="Cancel")
    popup.open()

    content: Widget = popup.ids.content

    textinput = TextInput(text=edit.name,
                          size_hint=(1, None),
                          height=30,
                          multiline=False,
                          )
    content.add_widget(textinput)
    textinput.bind(on_text_validate=lambda _: confirm(edit, textinput.text))
    textinput.bind(on_text_validate=popup.dismiss)

    if cancel is not None:
        popup.ids.cancel.bind(on_press=cancel)

    popup.ids.confirm.bind(on_press=lambda _: confirm(edit, textinput.text))


class WindowManager(ScreenManager):
    pass


class LogEntry(Label):
    pass


class ServerWindow(Screen):

    def __init__(self, client, **kw):
        super().__init__(**kw)
        self.client = client
        self.log: ServerLog = self.ids.log
        self.status: Label = self.ids.status
        self.cmd: TextInput = self.ids.cmd
        self.progress_popup: Optional[ProgressBarDialog] = None
        self.background_color = (.5, .1, .1, 1)

    def send_command(self, value):
        self.client.send_command(value)
        self.cmd.text = ""
        Clock.schedule_once(self.focus_cmd, 0)

    def focus_cmd(self, dv):
        self.cmd.focus = True

    def show_progress_bar_dialog(self, title, content, max):
        self.progress_popup = ProgressBarDialog(title=title, text=content, max=max)
        self.progress_popup.open()

    def close_progress_bar_dialog(self):
        if self.progress_popup is not None:
            self.progress_popup.dismiss()
        self.progress_popup = None


class ServerLog(RecycleView):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.data = []

    def on_log(self, record: str):
        self.data.append({"text": escape_markup(record)})
        self.clean_old()

    def on_message_markup(self, text):
        self.data.append({"text": text})
        self.clean_old()

    def clean_old(self):
        if len(self.data) > self.messages:
            self.data.pop(0)


class WelcomeWindow(Screen):
    version = StringProperty()

    def __init__(self, client: MinecraftClient, **kwargs):
        super().__init__(**kwargs)
        self.client = client
        self.apmc = None
        Window.minimum_width, Window.minimum_height = (400, 300)

    def do_delete(self, target):
        world_path = os.path.join(options.server_directory, target)
        if options.server_directory in world_path and os.path.isdir(world_path):
            print(f"Deleting {world_path}")
            shutil.rmtree(world_path)
            self.client.init()

    def confirm_delete(self, target, title="Confirm Delete", content="This Action is permanent."):
        confirm_prompt(title=title, content=content, confirm=lambda _: self.do_delete(target))

    def edit(self, title, content, edit):
        edit_prompt(title=title, content=content, edit=edit, confirm=self.do_rename)

    def do_rename(self, target, rename):
        target.name = rename

    def save_options(self):
        options.server_directory = self.ids.path.value
        options.max_heap_size = self.ids.max_memory.value
        options.min_heap_size = self.ids.min_memory.value
        Utils.get_settings().save()
        self.client.init()


def launch():
    MinecraftClient().run()
