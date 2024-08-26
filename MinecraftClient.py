import ModuleUpdate

ModuleUpdate.update()

from worlds.minecraft.client.MinecraftClient import launch
import Utils

if __name__ == "__main__":
    Utils.init_logging("MinecraftServer", exception_logger="Client")
    launch()
