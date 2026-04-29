#pragma once

#include "syntax_hints.h"

$item coin {
  weight = 1;
};

$item stone {
  weight = 4;
};

$pass {
  "item "name" {"
  "weight = "weight";"
  "};"
} {
  out.item_decls += "static const item "name";"
  out.items += "inline constexpr item item::"name" = {"index"};"
  out.item_weights += weight","
};

namespace items {
inline item starter() {
    return item::coin;
}
}
