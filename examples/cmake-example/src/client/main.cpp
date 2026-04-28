#include <iostream>
using std::cout;

enum tile_texture {
    none,
    tex_dirt_01,
};

enum tile_material {
    empty,
    soil,
};

#include "item.h"
#include "tile.h"
#include "shared/items.h"
#include "shared/tiles.h"

int main() {
    tiles::init_tiles();
    tiles::set_tile(3, 0, tile::dirt);

    const tile selected = tiles::get_tile(3, 0);
    std::cout << "client: selected.index = " << selected.index << "\n";
    std::cout << "client: starter item weight = " << item_weight(items::starter()) << "\n";
    return 0;
}
