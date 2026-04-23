#pragma once

struct tile {
    int index;
    bool hit(int power) const;
#include "tile_decls.h"
};

#include "tiles.h"

inline bool tile::hit(int power) const {
    switch(index) {
#include "hits.h"
    default: return false;
    }
    return false;
}
