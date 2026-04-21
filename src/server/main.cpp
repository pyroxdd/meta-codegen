#include <iostream>

#include "shared/tiles.h"

int main() {
    tiles::init_tiles();
    tiles::set_tile(1, 2, tile::dirt);

    const tile selected = tiles::get_tile(1, 2);
    std::cout << "server: selected.hit() = " << selected.hit() << "\n";
    return 0;
}
