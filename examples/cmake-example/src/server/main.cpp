#include <iostream>
using std::cout;

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
    set_tile(1, 2, tile_dirt);

    const tile selected = get_tile(1, 2);
    std::cout << "server: selected.hit(4) = " << selected.hit(4) << "\n";
    std::cout << "server: selected.hit(5) = " << selected.hit(5) << "\n";

    const item starter_item = starter();
    std::cout << "server: starter item weight = " << item_weight(starter_item) << "\n";
    return 0;
}
