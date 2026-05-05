#pragma once

struct tile {
    int index;
#include "g/tile_decls.h"
};

#include "g/tiles.h"

tile_texture tile_textures[] = {
#include "g/textures.h"
};

tile_material tile_materials[] = {
#include "g/materials.h"
};
