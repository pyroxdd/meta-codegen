#pragma once

struct tile {
    int index;
    bool hit() const;
#include "tile_decls.h"
};

#include "tiles.h"

inline bool tile::hit() const {
    switch(index) {
#include "hits.h"
    default: return false;
    }
}
