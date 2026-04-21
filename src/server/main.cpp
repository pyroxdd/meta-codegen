#include <iostream>

#include "shared/items.h"
#include "shared/tiles.h"

int main() {
    tiles::init_tiles();
    tiles::set_tile(1, 2, tile::dirt);

    const tile selected = tiles::get_tile(1, 2);
    std::cout << "server: selected.hit(4) = " << selected.hit(4) << "\n";
    std::cout << "server: selected.hit(5) = " << selected.hit(5) << "\n";

    const item starter = items::starter();
    std::cout << "server: starter item weight = " << item_weight(starter) << "\n";
    return 0;
}
