#pragma once

struct tile {
    int index;
#include "tile_decls.h"
};

#include "tiles.h"

tile_texture tile_textures[] = {
#include "textures.h"
};
