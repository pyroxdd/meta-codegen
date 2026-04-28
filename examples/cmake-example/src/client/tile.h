#pragma once

struct tile {
    int index;
#include "g_tile_decls.h"
};

#include "g_tiles.h"

tile_texture tile_textures[] = {
#include "g_textures.h"
};

tile_material tile_materials[] = {
#include "g_materials.h"
};
