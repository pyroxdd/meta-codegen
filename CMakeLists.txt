cmake_minimum_required(VERSION 3.20)
project(content_builder)
set(CMAKE_CXX_STANDARD 23)

find_package(Python3 REQUIRED COMPONENTS Interpreter)

set(GEN_PY_DIR "${CMAKE_BINARY_DIR}/gen_py")
set(SERVER_BUILD_DIR "${CMAKE_BINARY_DIR}/server")
set(CLIENT_BUILD_DIR "${CMAKE_BINARY_DIR}/client")
set(SERVER_CONTENT_STAMP "${SERVER_BUILD_DIR}/content.stamp")
set(CLIENT_CONTENT_STAMP "${CLIENT_BUILD_DIR}/content.stamp")
set(TILE_GENERATED_PY "${GEN_PY_DIR}/content.py")
set(HAPPY_SYNTAX_HEADER "${CMAKE_BINARY_DIR}/happy_syntax.h")
set(CURSOR_HEADERS
    tile_decls.h
    tiles.h
    hits.h
    textures.h
    item_decls.h
    items.h
    item_weights.h
)

file(GLOB TILE_INPUTS CONFIGURE_DEPENDS "${CMAKE_SOURCE_DIR}/src/shared/*.h")
set(TILE_GENERATED_SHARED_HEADERS)
foreach(TILE_INPUT IN LISTS TILE_INPUTS)
    file(RELATIVE_PATH TILE_INPUT_REL "${CMAKE_SOURCE_DIR}/src" "${TILE_INPUT}")
    list(APPEND TILE_GENERATED_SHARED_HEADERS
        "${SERVER_BUILD_DIR}/${TILE_INPUT_REL}"
        "${CLIENT_BUILD_DIR}/${TILE_INPUT_REL}"
    )
endforeach()
set(TILE_GENERATED_CURSOR_HEADERS)
foreach(CURSOR_HEADER IN LISTS CURSOR_HEADERS)
    list(APPEND TILE_GENERATED_CURSOR_HEADERS
        "${SERVER_BUILD_DIR}/${CURSOR_HEADER}"
        "${CLIENT_BUILD_DIR}/${CURSOR_HEADER}"
    )
endforeach()

add_custom_command(
    OUTPUT
        "${TILE_GENERATED_PY}"
        "${SERVER_CONTENT_STAMP}"
        "${CLIENT_CONTENT_STAMP}"
        "${HAPPY_SYNTAX_HEADER}"
        ${TILE_GENERATED_SHARED_HEADERS}
        ${TILE_GENERATED_CURSOR_HEADERS}
    COMMAND
        "${CMAKE_COMMAND}" -E env
        "PYTHONPYCACHEPREFIX=${CMAKE_BINARY_DIR}/pycache"
        "${Python3_EXECUTABLE}"
        "${CMAKE_SOURCE_DIR}/aot_transpiler.py"
        "${CMAKE_SOURCE_DIR}/src/shared"
        "${GEN_PY_DIR}"
        "${CMAKE_BINARY_DIR}"
        server=server/content.stamp
        client=client/content.stamp
    DEPENDS
        "${CMAKE_SOURCE_DIR}/aot_transpiler.py"
        ${TILE_INPUTS}
    WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
    COMMENT "Generating tile content"
    VERBATIM
)

add_custom_target(generate_content DEPENDS
    "${TILE_GENERATED_PY}"
    "${SERVER_CONTENT_STAMP}"
    "${CLIENT_CONTENT_STAMP}"
    "${HAPPY_SYNTAX_HEADER}"
    ${TILE_GENERATED_SHARED_HEADERS}
    ${TILE_GENERATED_CURSOR_HEADERS}
)

add_executable(server "src/server/main.cpp")
add_dependencies(server generate_content)
set_target_properties(server PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${SERVER_BUILD_DIR}"
)
target_include_directories(server PRIVATE
    "${SERVER_BUILD_DIR}"
    "${CMAKE_SOURCE_DIR}/src/server"
    "${CMAKE_SOURCE_DIR}/src"
    "${CMAKE_CURRENT_BINARY_DIR}"
)

add_executable(client "src/client/main.cpp")
add_dependencies(client generate_content)
set_target_properties(client PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CLIENT_BUILD_DIR}"
)
target_include_directories(client PRIVATE
    "${CLIENT_BUILD_DIR}"
    "${CMAKE_SOURCE_DIR}/src/client"
    "${CMAKE_SOURCE_DIR}/src"
    "${CMAKE_CURRENT_BINARY_DIR}"
)
