import os
import json
from base64 import b64encode, b64decode
from typing import Dict, Any

from BaseClasses import Region, Entrance, Item, ItemClassification, Location
from worlds.AutoWorld import World

from . import Constants
from .ItemPool import build_item_pool, get_junk_item_names
from .Rules import set_rules

client_version = -1


class MinecraftWorld(World):
    """
    Minecraft Dig - dig a hole.
    """
    game: str = "Minecraft Dig"
    topology_present = False

    item_name_to_id = Constants.item_name_to_id
    location_name_to_id = Constants.location_name_to_id

    data_version = 0

    def _get_mc_data(self) -> Dict[str, Any]:
        return {
            'world_seed': self.multiworld.per_slot_randoms[self.player].getrandbits(32),
            'seed_name': self.multiworld.seed_name,
            'player_name': self.multiworld.get_player_name(self.player),
            'player_id': self.player,
            'client_version': client_version,
            'race': self.multiworld.is_race,
        }

    def create_item(self, name: str) -> Item:
        item_class = ItemClassification.filler
        if name in Constants.item_info["progression_items"]:
            item_class = ItemClassification.progression
        elif name in Constants.item_info["useful_items"]:
            item_class = ItemClassification.useful
        elif name in Constants.item_info["trap_items"]:
            item_class = ItemClassification.trap

        return MinecraftItem(name, item_class, self.item_name_to_id.get(name, None), self.player)

    def create_event(self, region_name: str, event_name: str) -> None:
        region = self.multiworld.get_region(region_name, self.player)
        loc = MinecraftLocation(self.player, event_name, None, region)
        loc.place_locked_item(self.create_event_item(event_name))
        region.locations.append(loc)

    def create_event_item(self, name: str) -> Item:
        item = self.create_item(name)
        item.classification = ItemClassification.progression
        return item

    def create_regions(self) -> None:
        # Create regions and generate location names
        for region_name, exits, layer_range in Constants.region_info["regions"]:
            r = Region(region_name, self.player, self.multiworld)

            # create exits for region
            for exit_name in exits:
                r.exits.append(Entrance(self.player, exit_name, r))

            # generate Location's from range
            if layer_range is not None:
                for layerID in range(layer_range["top"], layer_range["bottom"]-1, -1):
                    loc_name = f"Layer {layerID}"
                    loc = MinecraftLocation(self.player, loc_name,
                                            self.location_name_to_id.get(loc_name, None), r)
                    r.locations.append(loc)

            self.multiworld.regions.append(r)

        # Bind mandatory connections
        for entr_name, region_name in Constants.region_info["mandatory_connections"]:
            e = self.multiworld.get_entrance(entr_name, self.player)
            r = self.multiworld.get_region(region_name, self.player)
            e.connect(r)

    def create_items(self) -> None:
        self.multiworld.itempool += build_item_pool(self)

    set_rules = set_rules

    def generate_output(self, output_directory: str) -> None:
        data = self._get_mc_data()
        filename = f"AP_{self.multiworld.get_out_file_name_base(self.player)}.apmc"
        with open(os.path.join(output_directory, filename), 'wb') as f:
            f.write(b64encode(bytes(json.dumps(data), 'utf-8')))

    def fill_slot_data(self) -> dict:
        slot_data = self._get_mc_data()
        return slot_data

    def get_filler_item_name(self) -> str:
        return get_junk_item_names(self.multiworld.random, 1)[0]


class MinecraftLocation(Location):
    game = "Minecraft dig"


class MinecraftItem(Item):
    game = "Minecraft dig"


def mc_update_output(raw_data, server, port):
    data = json.loads(b64decode(raw_data))
    data['server'] = server
    data['port'] = port
    return b64encode(bytes(json.dumps(data), 'utf-8'))
