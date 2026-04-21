#include <iostream>

enum tile_texture {
    none,
    tex_dirt_01,
};

#include "shared/tiles.h"

int main() {
    tiles::init_tiles();
    tiles::set_tile(3, 0, tile::dirt);

    const tile selected = tiles::get_tile(3, 0);
    std::cout << "client: selected.index = " << selected.index << "\n";
    return 0;
}
