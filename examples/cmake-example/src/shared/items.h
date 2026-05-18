#pragma once

#include "syntax_hints.h"

$item_coin {
  weight = 1;
};

$item_stone {
  weight = 4;
};

$pass {
  prefix = "item_"

  schema {
    prefix name" {"
    "weight = "weight";"
    "};"
  }

  instance {
    out.items += "inline constexpr item "prefix name" = {"index"};"
    out.item_weights += weight","
  }
};

inline item starter() {
    return item_coin;
}
