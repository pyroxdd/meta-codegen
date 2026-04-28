#pragma once

struct tile {
    int index;
    bool hit(int power) const;
#include "g/tile/tile_decls.h"
};

#include "g/tile/tiles.h"

tile_material tile_materials[] = {
#include "g/tile/materials.h"
};

inline bool tile::hit(int power) const {
    switch(index) {
#include "g/tile/hits.h"
    default: return false;
    }
    return false;
}
