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
    init_tiles();
    set_tile(3, 0, tile_dirt);

    const tile selected = get_tile(3, 0);
    std::cout << "client: selected.index = " << selected.index << "\n";
    std::cout << "client: starter item weight = " << item_weight(starter()) << "\n";
    return 0;
}
