from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import typing
from queue import Queue
from threading import Thread
from time import strftime

import requests
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.utils import escape_markup

import Utils
import kvui
from CommonClient import logger, ClientCommandProcessor
from MultiServer import CommandProcessor
from .MinecraftUI import MinecraftUI, ConfirmPopup

parser = argparse.ArgumentParser()
parser.add_argument("apmc_file", default=None, nargs='?', help="Path to an Archipelago Minecraft data file (.apmc)")
parser.add_argument('--install', '-i', dest='install', default=False, action='store_true',
                    help="Download and install Java and the Forge server. Does not launch the client afterwards.")
parser.add_argument('--release_channel', '-r', dest="channel", type=str, action='store',
                    help="Specify release channel to use.")
parser.add_argument('--java', '-j', metavar='17', dest='java', type=str, default=False, action='store',
                    help="specify java version.")
parser.add_argument('--forge', '-f', metavar='1.18.2-40.1.0', dest='forge', type=str, default=False, action='store',
                    help="specify forge version. (Minecraft Version-Forge Version)")
parser.add_argument('--version', '-v', metavar='9', dest='data_version', type=int, action='store',
                    help="specify Mod data version to download.")

args, rest = parser.parse_known_args()

minecraft_server_logger = logging.getLogger("MinecraftServer")
minecraft_client_logger = logging.getLogger("Client")

apmc_file = os.path.abspath(args.apmc_file) if args.apmc_file else None

options = Utils.get_settings()
channel = args.channel or options["minecraft_options"]["release_channel"]
apmc_data = None
data_version = args.data_version or None
jdk_executable = None


def get_minecraft_versions(version, release_channel="release"):
    version_file_endpoint = "https://raw.githubusercontent.com/KonoTyran/Minecraft_AP_Randomizer/master/versions/minecraft_versions.json"
    resp = requests.get(version_file_endpoint)
    local = False
    if resp.status_code == 200:  # OK
        try:
            data = resp.json()
        except requests.exceptions.JSONDecodeError:
            logging.warning(
                f"Unable to fetch version update file, using local version. (status code {resp.status_code}).")
            local = True
    else:
        logging.warning(f"Unable to fetch version update file, using local version. (status code {resp.status_code}).")
        local = True

    if local:
        with open(Utils.user_path("minecraft_versions.json"), 'r') as f:
            data = json.load(f)
    else:
        with open(Utils.user_path("minecraft_versions.json"), 'w') as f:
            json.dump(data, f)

    try:
        if version:
            return next(filter(lambda entry: entry["version"] == version, data[release_channel]))
        else:
            return resp.json()[release_channel][0]
    except (StopIteration, KeyError):
        logging.error(f"No compatible mod version found for client version {version} on \"{release_channel}\" channel.")
        if release_channel != "release":
            minecraft_client_logger.error(
                "Consider switching \"release_channel\" to \"release\" in your Host.yaml file")
        else:
            minecraft_client_logger.error(
                "No suitable mod found on the \"release\" channel. Please Contact us on discord to report this error.")


versions = get_minecraft_versions(data_version, channel)

forge_dir = options["minecraft_options"]["forge_directory"]
max_heap = options["minecraft_options"]["max_heap_size"]
forge_version = args.forge or versions["forge"]
java_version = args.java or versions["java"]
mod_url = versions["url"]
jdk_dir = f"jdk{java_version}"


class MinecraftCommandProcessor(CommandProcessor):
    ctx: MinecraftContext

    def output(self, text: str):
        logging.getLogger("Client").info(text)

    def __init__(self, ctx: MinecraftContext):
        self.ctx = ctx

    def _cmd_connect(self, address: str = "") -> bool:
        pass

    def _error_unknown_command(self, raw: str):
        self.ctx.send_command(raw)

    def default(self, raw: str):
        self.ctx.send_command(raw)


class MinecraftContext:
    command_processor = MinecraftCommandProcessor
    ui: MinecraftUI = None
    ui_task: typing.Optional["asyncio.Task[None]"] = None
    input_task: typing.Optional["asyncio.Task[None]"] = None
    server_task: typing.Optional["asyncio.Task[None]"] = None

    # internals
    # current message box through kvui
    _messagebox: typing.Optional["kvui.MessageBox"] = None
    # message box reporting a loss of connection
    _messagebox_connection_loss: typing.Optional["kvui.MessageBox"] = None
    minecraft_process: subprocess.Popen[str] = None
    confirm_up: bool = False

    def __init__(self):
        self.ui_Task = None
        self.input_queue = asyncio.Queue()
        self.input_requests = 0
        self.server_address = None
        self.minecraft_process = None

        # server state
        self.exit_event = asyncio.Event()
        self.watcher_event = asyncio.Event()

    def run_gui(self):
        self.ui = MinecraftUI(self)
        self.ui.title += f" {forge_version}"
        self.ui_task = asyncio.create_task(self.ui.async_run(), name="UI")

    async def shutdown(self):
        while self.input_requests > 0:
            self.input_queue.put_nowait(None)
            self.input_requests -= 1
        if self.ui_task:
            await self.ui_task
        if self.input_task:
            self.input_task.cancel()
        if self.minecraft_process:
            self.minecraft_process.kill()

    @property
    def suggested_address(self) -> str:
        if self.server_address:
            return self.server_address
        return Utils.persistent_load().get("MinecraftClient", {}).get("last_server_address", "")

    def gui_error(self, title: str, text: typing.Union[Exception, str]) -> typing.Optional["kvui.MessageBox"]:
        """Displays an error messagebox"""
        if not self.ui:
            return None
        title = title or "Error"
        from kvui import MessageBox
        if self._messagebox:
            self._messagebox.dismiss()
        # make "Multiple exceptions" look nice
        text = str(text).replace('[Errno', '\n[Errno').strip()
        # split long messages into title and text
        parts = title.split('. ', 1)
        if len(parts) == 1:
            parts = title.split(', ', 1)
        if len(parts) > 1:
            text = parts[1] + '\n\n' + text
            title = parts[0]
        # display error
        self._messagebox = MessageBox(title, text, error=True)
        self._messagebox.open()
        return self._messagebox

    def gui_confirm(self, text: str, action, title: str = "Prompt") -> kvui.Popup: #typing.Optional["kvui.Popup"]:
        """Displays an error messagebox"""
        if not self.ui:
            return None
        from kvui import Popup
        if self._messagebox:
            self._messagebox.dismiss()

        content = ConfirmPopup(text=text)
        content.bind(on_answer=action)
        # display error
        self._messagebox = Popup(title=title,
                                 content=content,
                                 size_hint=(None, None),
                                 size=(480, 400),
                                 auto_dismiss=False)
        self._messagebox.open()
        self.confirm_up = True
        def dismiss_confirm(something):
            self.confirm_up = False

        self._messagebox.bind(on_dismiss=dismiss_confirm)

        return self._messagebox

    def send_command(self, raw):
        self.minecraft_process.stdin.write(f'{raw}\n')
        self.minecraft_process.stdin.flush()

    def connect(self, address):
        self.send_command(f"connect \"{address}\"")


def stream_minecraft_output(pipe, queue, process):
    pipe.reconfigure(errors="replace")

    def queuer():
        while process.poll() is None:
            text = pipe.readline().strip()
            if text:
                queue.put_nowait(text)

    thread = Thread(target=queuer, name="Minecraft Output Queue", daemon=True)
    thread.start()
    return thread


async def minecraft_launch_server(ctx):
    global jdk_executable
    try:
        heap_arg = re.compile(r"^\d+[mMgG][bB]?$").match(max_heap).group()

        if heap_arg[-1] in ['b', 'B']:
            heap_arg = heap_arg[:-1]
        heap_arg = "-Xmx" + heap_arg

        os_args = "win_args.txt" if Utils.is_windows else "unix_args.txt"
        args_file = os.path.join(forge_dir, "libraries", "net", "minecraftforge", "forge", forge_version, os_args)
        forge_args = []
        with open(args_file) as argfile:
            for line in argfile:
                forge_args.extend(line.strip().split(" "))

        minecraft_process = subprocess.Popen(
            (jdk_executable, heap_arg, *forge_args, "-nogui"),
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stdin=subprocess.PIPE,
            encoding="utf-8",
            text=True,
            cwd=forge_dir
        )
        ctx.minecraft_process = minecraft_process
        minecraft_client_logger.info("Starting Minecraft Forge Server.")
        minecraft_queue = Queue()
        stream_minecraft_output(minecraft_process.stdout, minecraft_queue, minecraft_process)
        stream_minecraft_output(minecraft_process.stderr, minecraft_queue, minecraft_process)

        while not ctx.exit_event.is_set():
            if minecraft_process.poll() is not None:
                minecraft_client_logger.info("Minecraft server has exited.")
                ctx.exit_event.set()

            while not minecraft_queue.empty():
                raw_message: str = minecraft_queue.get()
                minecraft_queue.task_done()

                match = re.match("^\[[0-9:]+\] \[.+\/(WARN|INFO|ERROR)\] \[.+\]: (.*)", raw_message)
                if match:
                    level = match.group(1)
                    msg = escape_markup(match.group(2))

                    if level == "INFO" and msg.startswith("Successfully connected to "):
                        addr = msg.strip("Successfully connected to ")
                        ctx.ui.update_address_bar(addr)

                    if level == "WARN":
                        minecraft_server_logger.warning(f"[color=FFFF00][b][WARN][/b][/color] {msg}")
                    elif level == "ERROR":
                        minecraft_server_logger.error(f"[color=FFFF00][b][ERROR][/b][/color] {msg}")
                    elif level == "INFO":
                        minecraft_server_logger.info(f"[b][INFO][/b] {msg}")
                else:
                    minecraft_server_logger.info(f"[b][INFO][/b] {escape_markup(raw_message)}")
            await asyncio.sleep(0.01)

    except Exception as e:
        logger.exception(e, extra={"compact_gui": True})
        msg = "Minecraft Server Spinup Error"
        logger.error(msg)
        ctx.gui_error(msg, e)
        ctx.exit_event.set()


def get_jdk_download_url():
    if Utils.is_windows:
        return f"https://corretto.aws/downloads/latest/amazon-corretto-{java_version}-x64-windows-jdk.zip"
    elif Utils.is_macos:
        return f"https://corretto.aws/downloads/latest/amazon-corretto-{java_version}-x64-macos-jdk.tar.gz"
    elif Utils.is_linux:
        if platform.machine() == "aarch64":
            return f"https://corretto.aws/downloads/latest/amazon-corretto-{java_version}-aarch64-linux-jdk.tar.gz"
        else:
            return f"https://corretto.aws/downloads/latest/amazon-corretto-{java_version}-x64-linux-jdk.tar.gz"
    else:
        return None


def is_jdk_up_to_date(jdk_exe) -> bool:
    """
    checks if given jdk executable is up-to-date. if amazon can not be reached returns true.
    """
    jdk_process = subprocess.run(
        (jdk_exe, "--version"),
        encoding="utf-8",
        text=True,
        capture_output=True
    )
    minecraft_client_logger.info(jdk_process.stdout)
    jdk_url = get_jdk_download_url()
    resp = requests.get(jdk_url, allow_redirects=False)
    if resp.status_code == 302:
        if match := re.search("amazon-(corretto-[0-9\.]+)-", resp.headers['Location']):
            return match.group(1).casefold() in jdk_process.stdout.casefold()
        else:
            return True
    return True


def get_jdk():
    """get the java exe location"""

    if options["minecraft_options"].get("java"):
        jdk_exe = shutil.which(options["minecraft_options"].get("java"))
        if not os.path.exists(os.path.dirname(jdk_exe)):
            raise Exception(f"Java `{jdk_exe}` from user options not found.")
        return jdk_exe

    if Utils.is_windows:
        jdk_exe = os.path.join(jdk_dir, "bin", "java.exe")
    elif Utils.is_macos or Utils.is_linux:
        jdk_exe = os.path.join(jdk_dir, "bin", "java")
    else:
        jdk_exe = shutil.which("java")

    jdk_exe = os.path.abspath(jdk_exe)
    if not os.path.exists(os.path.dirname(jdk_exe)):
        minecraft_client_logger.info(f"Java {jdk_exe} not found.")
        download_jdk()
    else:
        if not is_jdk_up_to_date(jdk_exe):
            minecraft_client_logger.info(f"Updating java.")
            download_jdk()

    if os.path.isfile(jdk_exe):
        global jdk_executable
        jdk_executable = jdk_exe
        return

    raise Exception(
        f"Could not determine system type, and java not found on path. Please install java {java_version} and use host.yaml to point to it.")


def download_jdk():
    """Download Corretto (Amazon JDK)"""

    if os.path.isdir(jdk_dir):
        minecraft_client_logger.info(f"Removing old JDK...")
        from shutil import rmtree
        rmtree(jdk_dir)

    minecraft_client_logger.info("Downloading Java...")

    resp = requests.get(get_jdk_download_url())
    if resp.status_code == 200:  # OK
        minecraft_client_logger.info(f"Extracting...")
        import zipfile
        from io import BytesIO
        with zipfile.ZipFile(BytesIO(resp.content)) as archive:
            for full_name in archive.namelist():
                if archive.getinfo(full_name).is_dir():
                    continue

                file_path = list(filter(bool, full_name.split("/")))
                del file_path[0]
                if Utils.is_macos:
                    if file_path[1] == "Home":
                        del file_path[0]
                        del file_path[0]
                    else:
                        continue

                target_path = os.path.join(f"jdk{java_version}", *file_path)
                target_dir = os.path.dirname(target_path)
                os.makedirs(target_dir, exist_ok=True)

                with archive.open(full_name) as source:
                    with open(target_path, 'wb') as dest:
                        shutil.copyfileobj(source, dest)
    else:
        raise Exception(f"Error downloading Java.")
    minecraft_client_logger.info("Done fetching Java")


async def check_forge(ctx):
    if not os.path.isdir(os.path.join(forge_dir, "libraries", "net", "minecraftforge", "forge", forge_version)):
        forge_task = asyncio.create_task(download_forge(ctx), name="ForgeInstall")
        if forge_task:
            await forge_task


async def download_forge(ctx):
    global jdk_executable
    try:
        minecraft_client_logger.info(f"Downloading Forge {forge_version}...")
        forge_url = f"https://maven.minecraftforge.net/net/minecraftforge/forge/{forge_version}/forge-{forge_version}-installer.jar"
        resp = requests.get(forge_url)
        if resp.status_code == 200:  # OK
            forge_install_jar = os.path.join(forge_dir, "forge_install.jar")
            if not os.path.exists(forge_dir):
                os.mkdir(forge_dir)
            with open(forge_install_jar, 'wb') as f:
                f.write(resp.content)
            minecraft_client_logger.info(f"Installing Forge...")

            forge_install_process = subprocess.Popen(
                (jdk_executable, "-jar", forge_install_jar, "--installServer", forge_dir),
                stderr=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stdin=subprocess.PIPE,
                encoding="utf-8",
                text=True,
                cwd=forge_dir
            )

            minecraft_queue = Queue()
            stream_minecraft_output(forge_install_process.stdout, minecraft_queue, forge_install_process)
            stream_minecraft_output(forge_install_process.stderr, minecraft_queue, forge_install_process)

            while True:
                if forge_install_process.poll() is not None:
                    minecraft_client_logger.info("Done with install")
                    break

                while not minecraft_queue.empty():
                    raw_message: str = minecraft_queue.get()
                    minecraft_queue.task_done()
                    minecraft_client_logger.info(f"[b][INFO][/b] {escape_markup(raw_message)}")

                await asyncio.sleep(0.01)

            os.remove(forge_install_jar)
    except Exception as e:
        logger.exception(e, extra={"compact_gui": True})
        msg = "Minecraft Server Spinup Error"
        logger.error(msg)
        ctx.gui_error(msg, e)


async def check_eula(ctx):
    """Check if the EULA is agreed to, and prompt the user to read and agree if necessary."""
    eula_path = os.path.join(forge_dir, "eula.txt")
    if not os.path.isfile(eula_path):
        # Create eula.txt
        with open(eula_path, 'w') as f:
            f.write(
                "#By changing the setting below to TRUE you are indicating your agreement to our EULA (https://account.mojang.com/documents/minecraft_eula).\n")
            f.write(f"#{strftime('%a %b %d %X %Z %Y')}\n")
            f.write("eula=false\n")

    def confirm(answer):
        with open(eula_path, 'r+') as f:
            text = f.read()
            if answer:
                f.seek(0)
                f.write(text.replace('false', 'true'))
                f.truncate()
                minecraft_client_logger.info(f"Set {eula_path} to true")
        ctx._messagebox.dismiss()

    with open(eula_path, 'r') as f:
        text = f.read()
        if 'false' in text:

            # Prompt user to agree to the EULA
            ctx.gui_confirm("You need to agree to the Minecraft EULA in order to\nrun the server. \n\nThe EULA can be "
                            "found at\nhttps://account.mojang.com/documents/minecraft_eula\n\nDo you agree to the "
                            "EULA?", title="Do you agree to the Minecraft EULA?", action=confirm)
    while ctx.confirm_up:
        await asyncio.sleep(0.01)


def find_ap_randomizer_jar():
    """Create mods folder if needed; find AP randomizer jar; return None if not found."""
    mods_dir = os.path.join(forge_dir, 'mods')
    if os.path.isdir(mods_dir):
        for entry in os.scandir(mods_dir):
            if entry.name.startswith("aprandomizer") and entry.name.endswith(".jar"):
                minecraft_client_logger.info(f"Found AP randomizer mod: {entry.name}")
                return entry.name
        return None
    else:
        os.mkdir(mods_dir)
        logging.info(f"Created mods folder in {forge_dir}")
        return None


def read_apmc_file(apmc_file):
    from base64 import b64decode

    with open(apmc_file, 'r') as f:
        return json.loads(b64decode(f.read()))


def replace_apmc_files():
    """Create APData folder if needed; clean .apmc files from APData; copy given .apmc into directory."""
    if apmc_file is None:
        return
    apdata_dir = os.path.join(forge_dir, 'APData')
    copy_apmc = True
    if not os.path.isdir(apdata_dir):
        os.mkdir(apdata_dir)
        logging.info(f"Created APData folder in {forge_dir}")
    for entry in os.scandir(apdata_dir):
        if entry.name.endswith(".apmc") and entry.is_file():
            if not os.path.samefile(apmc_file, entry.path):
                os.remove(entry.path)
                logging.info(f"Removed {entry.name} in {apdata_dir}")
            else: # apmc already in apdata
                copy_apmc = False
    if copy_apmc:
        shutil.copyfile(apmc_file, os.path.join(apdata_dir, os.path.basename(apmc_file)))
        logging.info(f"Copied {os.path.basename(apmc_file)} to {apdata_dir}")


async def update_mod(ctx, url: str):
    """Check mod version, download new mod from GitHub releases page if needed. """
    ap_randomizer = find_ap_randomizer_jar()
    os.path.basename(url)
    if ap_randomizer is not None:
        minecraft_client_logger.info(f"Your current mod is {ap_randomizer}.")
    else:
        minecraft_client_logger.info(f"You do not have the AP randomizer mod installed.")

    if ap_randomizer != os.path.basename(url):
        def confirm(self, answer):
            if answer:
                old_ap_mod = os.path.join(forge_dir, 'mods', ap_randomizer) if ap_randomizer is not None else None
                new_ap_mod = os.path.join(forge_dir, 'mods', os.path.basename(url))
                minecraft_client_logger.info("Downloading AP randomizer mod. This may take a moment...")
                apmod_resp = requests.get(url)
                if apmod_resp.status_code == 200:
                    with open(new_ap_mod, 'wb') as f:
                        f.write(apmod_resp.content)
                        minecraft_client_logger.info(f"Wrote new mod file to {new_ap_mod}")
                    if old_ap_mod is not None:
                        os.remove(old_ap_mod)
                        minecraft_client_logger.info(f"Removed old mod file from {old_ap_mod}")
                else:
                    minecraft_client_logger.error(
                        f"Error retrieving the randomizer mod (status code {apmod_resp.status_code}).")
                    minecraft_client_logger.error(f"Please report this issue on the Archipelago Discord server.")
            ctx._messagebox.dismiss()

        ctx.gui_confirm("A new release of the Minecraft AP randomizer mod was found.\nWould you like to update?", action=confirm)

    while ctx.confirm_up:
        await asyncio.sleep(0.01)


async def prepare_server(ctx):
    try:
        global apmc_file
        if apmc_file is None and not args.install:
            apmc_file = Utils.open_filename('Select APMC file', (('APMC File', ('.apmc',)),))

        get_jdk_thread = Thread(target=get_jdk)
        get_jdk_thread.start()

        while get_jdk_thread.is_alive():
            await asyncio.sleep(0.01)

        forge_check_task = asyncio.create_task(check_forge(ctx), name="Forge Install")
        if forge_check_task:
            await forge_check_task

        update_mod_task = asyncio.create_task(update_mod(ctx, mod_url), name="Forge Install")
        if update_mod_task:
            await update_mod_task

        check_eula_task = asyncio.create_task(check_eula(ctx), name="Eula Check")
        if check_eula_task:
            await check_eula_task

    except Exception as e:
        logger.exception(e, extra={"compact_gui": True})
        msg = "Error Preparing Minecraft Server"
        logger.error(msg)
        ctx.gui_error(msg, e)
        ctx.exit_event.set()


async def main():
    global jdk_executable
    ctx = MinecraftContext()

    ctx.run_gui()

    prepare_server_task = asyncio.create_task(prepare_server(ctx), name="Download Java")
    if prepare_server_task:
        await prepare_server_task

    minecraft_server_task = asyncio.create_task(minecraft_launch_server(ctx),
                                                name="MinecraftServer")

    await ctx.exit_event.wait()
    ctx.server_address = None

    if minecraft_server_task:
        await minecraft_server_task

    await ctx.shutdown()
