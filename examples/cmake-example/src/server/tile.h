#pragma once

struct tile {
    int index;
    bool hit(int power) const;
#include "g/tile_decls.h"
};

#include "g/tiles.h"

tile_material tile_materials[] = {
#include "g/materials.h"
};

inline bool tile::hit(int power) const {
    switch(index) {
#include "g/hits.h"
    default: return false;
    }
    return false;
}
