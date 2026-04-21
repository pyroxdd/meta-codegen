cmake_minimum_required(VERSION 3.20)
project(content_builder)
set(CMAKE_CXX_STANDARD 23)

find_package(Python3 REQUIRED COMPONENTS Interpreter)

set(GEN_PY_DIR "${CMAKE_BINARY_DIR}/gen_py")
set(SERVER_BUILD_DIR "${CMAKE_BINARY_DIR}/server")
set(CLIENT_BUILD_DIR "${CMAKE_BINARY_DIR}/client")
set(TILE_PASS "${CMAKE_SOURCE_DIR}/tile.pass")
set(TILE_SERVER_HEADER "${SERVER_BUILD_DIR}/tile.h")
set(TILE_CLIENT_HEADER "${CLIENT_BUILD_DIR}/tile.h")
set(TILE_GENERATED_PY "${GEN_PY_DIR}/tile.py")

file(GLOB TILE_INPUTS CONFIGURE_DEPENDS "${CMAKE_SOURCE_DIR}/src/shared/*.h")

add_custom_command(
    OUTPUT
        "${TILE_GENERATED_PY}"
        "${TILE_SERVER_HEADER}"
        "${TILE_CLIENT_HEADER}"
    COMMAND
        "${Python3_EXECUTABLE}"
        "${CMAKE_SOURCE_DIR}/aot_transpiler.py"
        "${TILE_PASS}"
        "${GEN_PY_DIR}"
        "${CMAKE_BINARY_DIR}"
        server=server/tile.h
        client=client/tile.h
    DEPENDS
        "${CMAKE_SOURCE_DIR}/aot_transpiler.py"
        "${TILE_PASS}"
        ${TILE_INPUTS}
    WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
    COMMENT "Generating tile content"
    VERBATIM
)

add_custom_target(generate_content DEPENDS
    "${TILE_GENERATED_PY}"
    "${TILE_SERVER_HEADER}"
    "${TILE_CLIENT_HEADER}"
)

add_executable(server "src/server/main.cpp")
add_dependencies(server generate_content)
set_target_properties(server PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${SERVER_BUILD_DIR}"
)
target_include_directories(server PRIVATE
    "${CMAKE_SOURCE_DIR}/src"
    "${SERVER_BUILD_DIR}"
    "${CMAKE_CURRENT_BINARY_DIR}"
)

add_executable(client "src/client/main.cpp")
add_dependencies(client generate_content)
set_target_properties(client PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CLIENT_BUILD_DIR}"
)
target_include_directories(client PRIVATE
    "${CMAKE_SOURCE_DIR}/src"
    "${CLIENT_BUILD_DIR}"
    "${CMAKE_CURRENT_BINARY_DIR}"
)
