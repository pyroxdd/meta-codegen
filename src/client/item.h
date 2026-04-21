#pragma once

struct item {
    int index;
#include "item_decls.h"
};

#include "items.h"

inline int item_weight(item value) {
    static constexpr int weights[] = {
#include "item_weights.h"
    };
    return weights[value.index];
}
