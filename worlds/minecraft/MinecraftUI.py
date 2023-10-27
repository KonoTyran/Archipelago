import logging
import os
import sys

from kivy.lang import Builder
from kivy.properties import StringProperty
from kivy.uix.label import Label
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.gridlayout import GridLayout
from kivy.uix.layout import Layout
from kivy.app import App
from kivy.uix.stacklayout import StackLayout
from kivy.uix.tabbedpanel import TabbedPanel, TabbedPanelItem
from kivy.uix.textinput import TextInput

import Utils
import kvui
from kvui import ConnectBarTextInput, MainLayout, ContainerLayout, UILog

if sys.platform == "win32":
    import ctypes

    # kivy 2.2.0 introduced DPI awareness on Windows, but it makes the UI enter an infinitely recursive re-layout
    # by setting the application to not DPI Aware, Windows handles scaling the entire window on its own, ignoring kivy's
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(0)
    except FileNotFoundError:  # shcore may not be found on <= Windows 7
        pass  # TODO: remove silent except when Python 3.8 is phased out.

os.environ["KIVY_NO_CONSOLELOG"] = "1"
os.environ["KIVY_NO_FILELOG"] = "1"
os.environ["KIVY_NO_ARGS"] = "1"
os.environ["KIVY_LOG_ENABLE"] = "0"

from kivy.config import Config

Config.set("input", "mouse", "mouse,disable_multitouch")
Config.set('kivy', 'exit_on_escape', '0')
Config.set('graphics', 'multisamples', '0')  # multisamples crash old intel drivers


class OptionsLayout(StackLayout):
    pass


class ConfirmPopup(GridLayout):
    text = StringProperty()

    def __init__(self, **kwargs):
        self.register_event_type('on_answer')
        super(ConfirmPopup, self).__init__(**kwargs)

    def on_answer(self, *args):
        pass


class MinecraftUI(App):
    base_title: str = "Minecraft Forge Server"

    main_area_container: GridLayout
    """ subclasses can add more columns beside the tabs """

    def __init__(self, ctx):
        self.title = self.base_title
        self.ctx = ctx
        self.icon = r"data/mcicon.png"
        self.log_panels = {}
        self.commandprocessor = ctx.command_processor(ctx)
        self.log: UILog = None
        self.logging_pairs = [
            ("Client", "Minecraft Client"),
            ("MinecraftServer", "Minecraft Server Log"),
        ]

        super(MinecraftUI, self).__init__()

    @property
    def tab_count(self):
        if hasattr(self, "tabs"):
            return max(1, len(self.tabs.tab_list))
        return 1

    def connect_button_action(self, button):
        self.ctx.connect(self.server_connect_bar.text)

    def command_button_action(self, button):
        pass

    def on_stop(self):
        # "kill" input tasks
        for x in range(self.ctx.input_requests):
            self.ctx.input_queue.put_nowait("")
        self.ctx.input_requests = 0

        self.ctx.exit_event.set()

    def focus_textinput(self):
        if hasattr(self, "textinput"):
            self.textinput.focus = True

    def update_address_bar(self, text: str):
        if hasattr(self, "server_connect_bar"):
            self.server_connect_bar.text = text
        else:
            logging.getLogger("Client").info("Could not update address bar as the GUI is not yet initialized.")

    def on_message(self, textinput: TextInput):
        try:
            input_text = textinput.text.strip()
            textinput.text = ""

            if self.ctx.input_requests > 0:
                self.ctx.input_requests -= 1
                self.ctx.input_queue.put_nowait(input_text)
            elif input_text:
                self.commandprocessor(input_text)

        except Exception as e:
            logging.getLogger("Client").exception(e)

    def build(self) -> Layout:
        self.container = ContainerLayout()
        self.grid = MainLayout()
        self.grid.cols = 1
        self.connect_layout = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(30))
        # top part
        server_label = Label(text="Server: ", size_hint=(None, 1), width=dp(100))
        self.connect_layout.add_widget(server_label)

        self.server_connect_bar = ConnectBarTextInput(text=self.ctx.suggested_address or "archipelago.gg:",
                                                      size_hint_y=None,
                                                      height=dp(30), multiline=False, write_tab=False)

        def connect_bar_validate(sender):
            self.connect_button_action(sender)

        self.server_connect_bar.bind(on_text_validate=connect_bar_validate)
        self.connect_layout.add_widget(self.server_connect_bar)
        self.server_connect_button = Button(text="Connect", size=(dp(100), dp(30)), size_hint=(None, None))
        self.server_connect_button.bind(on_press=self.connect_button_action)
        self.connect_layout.add_widget(self.server_connect_button)
        self.grid.add_widget(self.connect_layout)

        # middle part
        self.tabs = TabbedPanel(size_hint_y=1)
        self.tabs.default_tab_text = "All"
        self.log_panels["All"] = self.tabs.default_tab_content = MCLog(*(logging.getLogger(logger_name)
                                                                         for logger_name, name in
                                                                         self.logging_pairs), strip_markup=False)

        for logger_name, display_name in self.logging_pairs:
            bridge_logger = logging.getLogger(logger_name)
            panel = TabbedPanelItem(text=display_name)
            self.log_panels[display_name] = panel.content = MCLog(bridge_logger, strip_markup=False)
            self.tabs.add_widget(panel)

        # MC options
        options_tab = TabbedPanelItem(text="Options")
        options_panel = OptionsLayout()
        # install_button = Button(text="Force Reinstall", height=dp(30), size_hint=(None, None))
        # install_button.bind(on_release=self.ctx.force_install_all)
        # options_panel.add_widget(install_label)
        # options_panel.add_widget(install_button)
        options_tab.content = options_panel
        self.tabs.add_widget(options_tab)

        self.main_area_container = GridLayout(size_hint_y=1, rows=1)
        self.main_area_container.add_widget(self.tabs)

        self.grid.add_widget(self.main_area_container)

        # bottom part
        bottom_layout = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(30))
        info_button = Button(size=(dp(100), dp(30)), text="Command:", size_hint_x=None)
        info_button.bind(on_release=self.command_button_action)
        bottom_layout.add_widget(info_button)
        self.textinput = TextInput(size_hint_y=None, height=dp(30), multiline=False, write_tab=False)
        self.textinput.bind(on_text_validate=self.on_message)
        self.textinput.text_validate_unfocus = False
        bottom_layout.add_widget(self.textinput)
        self.grid.add_widget(bottom_layout)
        # self.commandprocessor("/help")
        # Clock.schedule_interval(self.update_texts, 1 / 30)
        self.container.add_widget(self.grid)

        # If the address contains a port, select it; otherwise, select the host.
        s = self.server_connect_bar.text
        host_start = s.find("@") + 1
        ipv6_end = s.find("]", host_start) + 1
        port_start = s.find(":", ipv6_end if ipv6_end > 0 else host_start) + 1
        self.server_connect_bar.focus = True
        self.server_connect_bar.select_text(port_start if port_start > 0 else host_start, len(s))

        return self.container


class MCLog(UILog):

    def __init__(self, *loggers_to_handle, strip_markup=True, **kwargs):
        super(UILog, self).__init__(**kwargs)
        self.data = []
        for logger in loggers_to_handle:
            if strip_markup:
                logger.addHandler(kvui.LogtoUI(self.on_log))
            else:
                logger.addHandler(kvui.LogtoUI(self.on_message_markup))


dir_path = os.path.dirname(os.path.realpath(__file__))
Builder.load_file(os.path.join(dir_path, "mcclient.kv"))
user_file = Utils.user_path("data", "user.kv")
if os.path.exists(user_file):
    logging.info("Loading user.kv into builder.")
    Builder.load_file(user_file)
