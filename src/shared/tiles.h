#pragma once

#include "../../build/happy_syntax.h"

$tile air {
  texture = none;
  durability = 0;
};

$tile dirt {
  texture = tex_dirt_01;
  durability = 5;
};

#include <array>
#include <cstddef>

$pass tile {
  schema() {
    tile [name] {
      texture = [texture];
      durability = [durability];
    }
  }

  init() {
    count = 0
    tile_decls = []
    tiles = []
    hits = []
    textures = []
  }

  instance() {
    tile_decls += static const tile [name];
    tiles += inline constexpr tile tile::[name] = {[count++]};
    textures += [texture],
    hits += 
      case tile::[name].index: {
        [ "return false;" if durability == "0" else "return true;" ]
      } break;
  }

  target(server) {
    struct tile {
      int index;
      bool hit() const;
      [tile_decls]
    };

    [tiles]

    inline bool tile::hit() const {
      switch(index) {
        [hits]
        default: return false;
      }
    }
  }

  target(client) {
    struct tile {
      int index;
      [tile_decls]
    };

    [tiles]

    tile_texture tile_textures[] = {
      [textures]
    };
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
