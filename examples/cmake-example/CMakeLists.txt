cmake_minimum_required(VERSION 3.20)
project(content_builder)
set(CMAKE_CXX_STANDARD 23)

find_package(Python3 REQUIRED COMPONENTS Interpreter)

set(GENERATED_DIR "${CMAKE_BINARY_DIR}/generated")
set(CONTENT_STAMP "${GENERATED_DIR}/content.stamp")
set(SYNTAX_HINTS_HEADER "${GENERATED_DIR}/syntax_hints.h")

set(CMAKE_RUNTIME_OUTPUT_DIRECTORY "${PROJECT_SOURCE_DIR}/out")
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY_RELEASE "${PROJECT_SOURCE_DIR}/out")
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY_DEBUG "${PROJECT_SOURCE_DIR}/out")

file(GLOB_RECURSE SHARED_INPUTS CONFIGURE_DEPENDS
    "${CMAKE_SOURCE_DIR}/src/shared/*.h"
    "${CMAKE_SOURCE_DIR}/src/shared/*.cpp"
)

file(GLOB_RECURSE SERVER_SOURCES CONFIGURE_DEPENDS
    "${CMAKE_SOURCE_DIR}/src/server/*.cpp"
)
file(GLOB_RECURSE CLIENT_SOURCES CONFIGURE_DEPENDS
    "${CMAKE_SOURCE_DIR}/src/client/*.cpp"
)

add_custom_command(
    OUTPUT
        "${CONTENT_STAMP}"
        "${SYNTAX_HINTS_HEADER}"
    COMMAND
        "${CMAKE_COMMAND}" -E env
        "PYTHONPYCACHEPREFIX=${CMAKE_BINARY_DIR}/pycache"
        "${Python3_EXECUTABLE}"
        "${PROJECT_SOURCE_DIR}/../../metacodegen.py"
        --shared-dir "${PROJECT_SOURCE_DIR}/src/shared"
        --output-root "${GENERATED_DIR}"
        --generated-header-root g
        --stamp "${CONTENT_STAMP}"
    DEPENDS
        "${PROJECT_SOURCE_DIR}/../../metacodegen.py"
        ${SHARED_INPUTS}
    WORKING_DIRECTORY "${PROJECT_SOURCE_DIR}"
    COMMENT "Generating content sources"
    VERBATIM
)

add_custom_target(generate_content DEPENDS
    "${CONTENT_STAMP}"
    "${SYNTAX_HINTS_HEADER}"
)

add_executable(server ${SERVER_SOURCES})
add_dependencies(server generate_content)
target_include_directories(server PRIVATE
    "${GENERATED_DIR}"
    "${GENERATED_DIR}/src"
    "${PROJECT_SOURCE_DIR}/src/server"
    "${PROJECT_SOURCE_DIR}/src"
)

add_executable(client ${CLIENT_SOURCES})
add_dependencies(client generate_content)
target_include_directories(client PRIVATE
    "${GENERATED_DIR}"
    "${GENERATED_DIR}/src"
    "${PROJECT_SOURCE_DIR}/src/client"
    "${PROJECT_SOURCE_DIR}/src"
)
