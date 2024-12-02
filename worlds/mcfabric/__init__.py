import Utils
import settings
import typing

from worlds.AutoWorld import World
from worlds.LauncherComponents import Component, components, Type, launch_subprocess, SuffixIdentifier



def launch_client():
    from .client.MinecraftClient import launch
    launch_subprocess(launch, name="MinecraftClient")


# Append Minecraft Client to launcher components
components.append(Component('Minecraft Fabric Client', 'MinecraftClient', icon='mcicon', func=launch_client,
                            file_identifier=SuffixIdentifier('.apmc'), component_type=Type.CLIENT))


class MinecraftSettings(settings.Group):
    class ForgeDirectory(settings.OptionalUserFolderPath):
        pass
    class ServerDirectory(settings.OptionalUserFolderPath):
        pass

    class ReleaseChannel(str):
        """
        release channel, currently "release", or "beta"
        any games played on the "beta" channel have a high likelihood of no longer working on the "release" channel.
        """

    forge_directory: ForgeDirectory = ForgeDirectory("Minecraft Forge server")
    server_directory: ServerDirectory = ServerDirectory(Utils.user_path('minecraft_server'))
    max_heap_size: str = ""
    min_heap_size: str = ""
    release_channel: ReleaseChannel = ReleaseChannel("release")

class MinecraftWorld(World):
    """
    A Fabric Based minecraft apmc launccher.
    """
    game = "Minecraft Fabric Client"
    settings: typing.ClassVar[MinecraftSettings]

    item_name_to_id = {}
    location_name_to_id = {}