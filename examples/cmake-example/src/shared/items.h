#pragma once

#include "syntax_hints.h"

$item coin {
  weight = 1;
};

$item stone {
  weight = 4;
};

$pass {
  schema() {
    "item "name" {"
    "weight = "weight";"
    "};"
  }

  instance() {
    item_decls += "static const item "name";"
    items += "inline constexpr item item::"name" = {"index"};"
    item_weights += weight","
  }
};

namespace items {
inline item starter() {
    return item::coin;
}
}
