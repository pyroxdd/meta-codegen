#include <iostream>

enum tile_texture {
    none,
    tex_dirt_01,
};

#include "tile.h"

int main() {
    std::cout << "client: tile::dirt.index = " << tile::dirt.index << "\n";
    return 0;
}
