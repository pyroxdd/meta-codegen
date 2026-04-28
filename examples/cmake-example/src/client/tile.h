#pragma once

struct tile {
    int index;
#include "g/tile/tile_decls.h"
};

#include "g/tile/tiles.h"

tile_texture tile_textures[] = {
#include "g/tile/textures.h"
};

tile_material tile_materials[] = {
#include "g/tile/materials.h"
};
