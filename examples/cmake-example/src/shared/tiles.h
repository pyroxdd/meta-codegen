#pragma once

#include "../../build/syntax_hints.h"

$tile air {
  texture = none;
  durability = 0;
  material = empty;
};

$tile dirt {
  texture = tex_dirt_01;
  durability = 5;
  surface = soil;
  onhit = {
    cout << "dirt hit\n";
  };
};

#include <array>
#include <cstddef>
#include <iostream>

$pass tile {
  count = 0
  tile_decls = []
  tiles = []
  hits = []
  textures = []
  materials = []

  schema() {
    "tile "name" {"
    "texture = "texture";"
    "durability = "durability";"
    ["material = "material";"|"surface = "material";"|"kind = "material";"]
    ["onhit = "onhit";"|]
    "};"
  }

  instance() {
    tile_decls += "static const tile "name";"
    tiles += "inline constexpr tile tile::"name" = {"count++"};"
    textures += texture","
    materials += material","
    hits += "case tile::"name".index: {"
    hits += { return durability == "0" ? "return false;" : onhit"return power >= "durability";" }
    hits += "} break;"
  }
};



namespace tiles {
constexpr std::size_t width = 4;
constexpr std::size_t height = 4;

inline std::array<tile, width * height> map{};

inline std::size_t tile_offset(std::size_t x, std::size_t y) {
    return y * width + x;
}

inline void set_tile(std::size_t x, std::size_t y, tile value) {
    map[tile_offset(x, y)] = value;
}

inline tile get_tile(std::size_t x, std::size_t y) {
    return map[tile_offset(x, y)];
}

inline void init_tiles(tile value = tile::air) {
    map.fill(value);
}
}
