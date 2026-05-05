#pragma once

struct item {
    int index;
#include "g/item_decls.h"
};

#include "g/items.h"

inline int item_weight(item value) {
    static constexpr int weights[] = {
#include "g/item_weights.h"
    };
    return weights[value.index];
}
