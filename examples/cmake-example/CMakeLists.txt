cmake_minimum_required(VERSION 3.20)
project(content_builder)
set(CMAKE_CXX_STANDARD 23)

find_package(Python3 REQUIRED COMPONENTS Interpreter)

set(SERVER_BUILD_DIR "${CMAKE_BINARY_DIR}/server")
set(CLIENT_BUILD_DIR "${CMAKE_BINARY_DIR}/client")
set(SERVER_CONTENT_STAMP "${SERVER_BUILD_DIR}/content.stamp")
set(CLIENT_CONTENT_STAMP "${CLIENT_BUILD_DIR}/content.stamp")
set(SYNTAX_HINTS_HEADER "${CMAKE_BINARY_DIR}/syntax_hints.h")

set(CMAKE_RUNTIME_OUTPUT_DIRECTORY "${PROJECT_SOURCE_DIR}/out")
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY_RELEASE "${PROJECT_SOURCE_DIR}/out")
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY_DEBUG "${PROJECT_SOURCE_DIR}/out")

file(GLOB_RECURSE PROJECT_INPUTS CONFIGURE_DEPENDS
    "${CMAKE_SOURCE_DIR}/src/*.h"
    "${CMAKE_SOURCE_DIR}/src/*.cpp"
)

set(SERVER_GENERATED_FILES)
set(CLIENT_GENERATED_FILES)
set(SERVER_SOURCES)
set(CLIENT_SOURCES)
foreach(PROJECT_INPUT IN LISTS PROJECT_INPUTS)
    file(RELATIVE_PATH PROJECT_INPUT_REL "${CMAKE_SOURCE_DIR}" "${PROJECT_INPUT}")
    set(SERVER_GENERATED_PATH "${SERVER_BUILD_DIR}/${PROJECT_INPUT_REL}")
    set(CLIENT_GENERATED_PATH "${CLIENT_BUILD_DIR}/${PROJECT_INPUT_REL}")

    list(APPEND SERVER_GENERATED_FILES "${SERVER_GENERATED_PATH}")
    list(APPEND CLIENT_GENERATED_FILES "${CLIENT_GENERATED_PATH}")

    if(PROJECT_INPUT_REL MATCHES "^src/(server|shared)/.*\\.cpp$")
        list(APPEND SERVER_SOURCES "${SERVER_GENERATED_PATH}")
    endif()
    if(PROJECT_INPUT_REL MATCHES "^src/(client|shared)/.*\\.cpp$")
        list(APPEND CLIENT_SOURCES "${CLIENT_GENERATED_PATH}")
    endif()
endforeach()

set_source_files_properties(
    ${SERVER_GENERATED_FILES}
    ${CLIENT_GENERATED_FILES}
    PROPERTIES GENERATED TRUE
)

add_custom_command(
    OUTPUT
        "${SERVER_CONTENT_STAMP}"
        "${CLIENT_CONTENT_STAMP}"
        "${SYNTAX_HINTS_HEADER}"
        ${SERVER_GENERATED_FILES}
        ${CLIENT_GENERATED_FILES}
    COMMAND
        "${CMAKE_COMMAND}" -E env
        "PYTHONPYCACHEPREFIX=${CMAKE_BINARY_DIR}/pycache"
        "${Python3_EXECUTABLE}"
        "${PROJECT_SOURCE_DIR}/../../metacodegen.py"
        --shared-dir "${PROJECT_SOURCE_DIR}/src"
        --output-root "${PROJECT_BINARY_DIR}"
        --target server=server/content.stamp
        --target client=client/content.stamp
    DEPENDS
        "${PROJECT_SOURCE_DIR}/../../metacodegen.py"
        ${PROJECT_INPUTS}
    WORKING_DIRECTORY "${PROJECT_SOURCE_DIR}"
    COMMENT "Generating content sources"
    VERBATIM
)

add_custom_target(generate_content DEPENDS
    "${SERVER_CONTENT_STAMP}"
    "${CLIENT_CONTENT_STAMP}"
    "${SYNTAX_HINTS_HEADER}"
    ${SERVER_GENERATED_FILES}
    ${CLIENT_GENERATED_FILES}
)

add_executable(server ${SERVER_SOURCES})
add_dependencies(server generate_content)
target_include_directories(server PRIVATE
    "${SERVER_BUILD_DIR}"
    "${SERVER_BUILD_DIR}/src"
    "${SERVER_BUILD_DIR}/src/server"
    "${CMAKE_CURRENT_BINARY_DIR}"
)

add_executable(client ${CLIENT_SOURCES})
add_dependencies(client generate_content)
target_include_directories(client PRIVATE
    "${CLIENT_BUILD_DIR}"
    "${CLIENT_BUILD_DIR}/src"
    "${CLIENT_BUILD_DIR}/src/client"
    "${CMAKE_CURRENT_BINARY_DIR}"
)
