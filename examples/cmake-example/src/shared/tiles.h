#pragma once

#include "syntax_hints.h"

$tile_air {
  texture = none;
  durability = 0;
  material = empty;
};

$tile_dirt {
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

$pass {
  prefix = "tile_"

  schema {
    prefix name" {"
    "texture = "texture";"
    "durability = "durability";"
    ["material = "material";"|"surface = "material";"|"kind = "material";"]
    ["onhit = "onhit";"|]
    "};"
  }

  instance {
    out.tiles += "inline constexpr tile "prefix name" = {"index"};"
    out.textures += texture","
    out.materials += material","
    out.hits += "case "prefix name".index: {"
    out.hits += { return durability == "0" ? "return false;" : onhit"return power >= "durability";" }
    out.hits += "} break;"
  }
};
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

inline void init_tiles(tile value = tile_air) {
    map.fill(value);
}
