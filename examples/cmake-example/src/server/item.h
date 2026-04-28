#pragma once

struct item {
    int index;
#include "g_item_decls.h"
};

#include "g_items.h"

inline int item_weight(item value) {
    static constexpr int weights[] = {
#include "g_item_weights.h"
    };
    return weights[value.index];
}
