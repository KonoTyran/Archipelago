import io
import json
import os.path
import pkgutil
from tkinter import filedialog
from typing import List, Optional

from kivy import Config
from kivy.app import App
from kivy.clock import Clock
from kivy.core.image import Image as CoreImage
from kivy.core.window import Window
from kivy.lang import Builder
from kivy.network.urlrequest import UrlRequest, UrlRequestUrllib
from kivy.properties import StringProperty, ObjectProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.popup import Popup
from kivy.uix.stacklayout import StackLayout
from kivy.uix.textinput import TextInput
from kivy.uix.widget import Widget

import Utils

version_file_endpoint = "https://raw.githubusercontent.com/KonoTyran/Minecraft_AP_Randomizer/master/versions/minecraft_versions.json"

options = Utils.get_options()["minecraft_options"]

Config.set('input', 'mouse', 'mouse,disable_multitouch')


def load_text(*path: str):
    return pkgutil.get_data(__name__, "/".join(path)).decode()


def load_image(*path: str):
    data = io.BytesIO(pkgutil.get_data(__name__, "/".join(path)))
    texture = CoreImage(data, ext="png")
    return texture


class MinecraftClient(App):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.welcome: Optional[WelcomeLayout] = None

    def build(self):
        Builder.load_string(load_text("layouts", "minecraft.kv"))
        self.welcome = WelcomeLayout(self)
        Clock.schedule_interval(self.update, 1 / 60)
        Clock.schedule_once(self.init, 1)
        return self.welcome

    def get_application_icon(self):
        return load_image("assets", "icon.png")

    def update(self, dt):
        pass

    def init(self, dt=None):
        layout: Widget = self.welcome.ids.saves
        layout.clear_widgets()
        for name, path in self.get_recent_items():
            layout.add_widget(RecentItem(name=name, path=path, client=self))

        ids: StackLayout = self.welcome.ids
        ids.path.value = options.server_directory
        ids.memory.value = options.max_heap_size

        # send our request out to fetch the versions file
        UrlRequest(version_file_endpoint, self.process_versions, on_failure=self.process_local_versions)

    def process_versions(self, response: UrlRequestUrllib, result):
        data = json.loads(result)
        with open(Utils.user_path("minecraft_versions.json"), 'w') as f:
            json.dump(data, f)

    def process_local_versions(self, response: UrlRequestUrllib, result: str):
        print(f"[Warning] unable to fetch remote versions due to {result}. falling back to local cache.")
        with open(Utils.user_path("minecraft_versions.json"), 'r') as f:
            data = json.load(f)

    def get_recent_items(self) -> List:
        directory = os.path.abspath(options.server_directory)
        saves = []
        for directory in os.listdir(directory):
            if directory.startswith("Archipelago-"):
                saves.append(("test", directory))
        return saves


class TextOption(GridLayout):
    value = StringProperty()
    label = StringProperty()
    button_label = StringProperty()


class FolderOption(TextOption):

    def button_press(self):
        print("open folder dialog")
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

    def delete(self):
        self.client.welcome.confirm_delete(target=self.path, title="Confirm Delete",
                                           content=f"Delete {self.name}?\nThis Action is permanent.")

    def rename(self):
        self.client.welcome.edit(title="Confirm Edit", content=f"Rename {self.name}", edit=self)


class ConfirmDialog(Popup):
    text = StringProperty()
    confirm_text = StringProperty()
    cancel_text = StringProperty()
    pass


def confirmPrompt(confirm, title="Prompt", content="Are you sure?", cancel=None):
    popup = ConfirmDialog(title=title, text=content, confirm_text="Yes", cancel_text="No")
    popup.open()

    if cancel is not None:
        popup.ids.cancel.bind(on_press=cancel)

    popup.ids.confirm.bind(on_press=confirm)


def EditPrompt(confirm, title="Prompt", content="Are you sure?", cancel=None, edit: RecentItem = None):
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

    popup.ids.confirm.bind(on_press=lambda _:confirm(edit, textinput.text))


class WelcomeLayout(BoxLayout):
    version = StringProperty()

    def __init__(self, client: MinecraftClient, **kwargs):
        super().__init__(**kwargs)
        self.client = client
        Window.minimum_width, Window.minimum_height = (400, 300)

    def do_delete(self, target):
        print(f"should delete {target}")

    def confirm_delete(self, target, title="Confirm Delete", content="This Action is permanent."):
        confirmPrompt(title=title, content=content, confirm=lambda _: self.do_delete(target))

    def edit(self, title, content, edit):
        EditPrompt(title=title, content=content, edit=edit, confirm=self.do_rename)

    def do_rename(self, target, rename):
        print(f"should rename {target}, to {rename}")
        target.name = rename

    def save_options(self):
        options.server_directory = self.ids.path.value
        options.max_heap_size = self.ids.memory.value
        Utils.get_options().save()
        self.client.init()


def launch():
    MinecraftClient().run()
