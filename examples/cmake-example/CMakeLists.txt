cmake_minimum_required(VERSION 3.20)
project(content_builder)
set(CMAKE_CXX_STANDARD 23)

find_package(Python3 REQUIRED COMPONENTS Interpreter)

set(GENERATED_DIR "${CMAKE_BINARY_DIR}/generated")
set(CODEGEN_PY "${PROJECT_SOURCE_DIR}/../../metacodegen.py")
set(PASS_BUILD_DIR "${CMAKE_BINARY_DIR}/passes")
set(STAMP_DIR "${CMAKE_BINARY_DIR}/stamps")
set(CONTENT_STAMP "${STAMP_DIR}/content.stamp")
set(SYNTAX_HINTS_HEADER "${GENERATED_DIR}/syntax_hints.h")

set(CMAKE_RUNTIME_OUTPUT_DIRECTORY "${PROJECT_SOURCE_DIR}/out")
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY_RELEASE "${PROJECT_SOURCE_DIR}/out")
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY_DEBUG "${PROJECT_SOURCE_DIR}/out")

file(GLOB_RECURSE SHARED_INPUTS CONFIGURE_DEPENDS
    "${CMAKE_SOURCE_DIR}/src/shared/*.h"
    "${CMAKE_SOURCE_DIR}/src/shared/*.cpp"
)
list(SORT SHARED_INPUTS)

file(GLOB_RECURSE SERVER_SOURCES CONFIGURE_DEPENDS
    "${CMAKE_SOURCE_DIR}/src/server/*.cpp"
)
file(GLOB_RECURSE CLIENT_SOURCES CONFIGURE_DEPENDS
    "${CMAKE_SOURCE_DIR}/src/client/*.cpp"
)

function(sanitize_codegen_token OUT_VAR INPUT_VALUE)
    string(REGEX REPLACE "[^A-Za-z0-9]+" "_" _token "${INPUT_VALUE}")
    string(REGEX REPLACE "_+" "_" _token "${_token}")
    string(REGEX REPLACE "^_" "" _token "${_token}")
    string(REGEX REPLACE "_$" "" _token "${_token}")
    string(TOLOWER "${_token}" _token)
    if(_token STREQUAL "")
        set(_token "file")
    endif()
    set(${OUT_VAR} "${_token}" PARENT_SCOPE)
endfunction()

file(MAKE_DIRECTORY "${GENERATED_DIR}")
file(MAKE_DIRECTORY "${PASS_BUILD_DIR}")
file(MAKE_DIRECTORY "${STAMP_DIR}")
file(WRITE "${SYNTAX_HINTS_HEADER}" "#pragma once\n#define $pass struct\n")

execute_process(
    COMMAND
        "${CMAKE_COMMAND}" -E env
        "PYTHONPYCACHEPREFIX=${CMAKE_BINARY_DIR}/pycache"
        "${Python3_EXECUTABLE}"
        "${CODEGEN_PY}"
        compile-passes
        --passes-dir "${CMAKE_SOURCE_DIR}/src/shared"
        --build-root "${PASS_BUILD_DIR}"
        --shared-dir "${CMAKE_SOURCE_DIR}/src/shared"
        --output-root "${GENERATED_DIR}"
        --generated-header-root g
    WORKING_DIRECTORY "${PROJECT_SOURCE_DIR}"
    COMMAND_ERROR_IS_FATAL ANY
)

file(GLOB PASS_DESCRIPTORS CONFIGURE_DEPENDS "${PASS_BUILD_DIR}/pass_*.json")
list(SORT PASS_DESCRIPTORS)
list(LENGTH PASS_DESCRIPTORS PASS_COUNT)
set(COMPILE_PASSES_STAMP "${STAMP_DIR}/compile_passes.stamp")

set(PASS_IDS "")
set(PUBLIC_OUTPUTS "")
set(PASS_HEADER_OUTPUTS "")
if(PASS_COUNT GREATER 0)
    foreach(PASS_DESCRIPTOR IN LISTS PASS_DESCRIPTORS)
        file(READ "${PASS_DESCRIPTOR}" PASS_JSON)
        string(JSON PASS_ID GET "${PASS_JSON}" id)
        string(JSON PASS_OUTPUT_COUNT LENGTH "${PASS_JSON}" outputs)
        list(APPEND PASS_IDS "${PASS_ID}")

        set(PASS_MAIN_OUTPUTS "")
        set(PASS_OUTPUT_LIST "")
        if(PASS_OUTPUT_COUNT GREATER 0)
            math(EXPR PASS_OUTPUT_LAST_INDEX "${PASS_OUTPUT_COUNT} - 1")
            foreach(PASS_OUTPUT_INDEX RANGE ${PASS_OUTPUT_LAST_INDEX})
                string(JSON PASS_OUTPUT_REL GET "${PASS_JSON}" outputs ${PASS_OUTPUT_INDEX})
                list(APPEND PASS_MAIN_OUTPUTS "${GENERATED_DIR}/${PASS_OUTPUT_REL}")
                list(APPEND PUBLIC_OUTPUTS "${GENERATED_DIR}/${PASS_OUTPUT_REL}")
                cmake_path(GET PASS_OUTPUT_REL PARENT_PATH PASS_OUTPUT_PARENT_REL)
                cmake_path(GET PASS_OUTPUT_REL STEM PASS_OUTPUT_STEM)
                list(APPEND PASS_OUTPUT_LIST "${GENERATED_DIR}/${PASS_OUTPUT_PARENT_REL}/${PASS_OUTPUT_STEM}/${PASS_ID}.h")
                list(APPEND PASS_HEADER_OUTPUTS "${GENERATED_DIR}/${PASS_OUTPUT_PARENT_REL}/${PASS_OUTPUT_STEM}/${PASS_ID}.h")
            endforeach()
        endif()
        set("PASS_${PASS_ID}_MAIN_OUTPUTS" "${PASS_MAIN_OUTPUTS}")
        set("PASS_${PASS_ID}_OUTPUTS" "${PASS_OUTPUT_LIST}")
    endforeach()
endif()

set(STRIPPED_SHARED_OUTPUTS "")
set(PROCESS_FILE_STAMPS "")
set(ASSEMBLE_PASS_STAMPS "")
set(PREV_PROCESS_DEP "")
foreach(SHARED_INPUT IN LISTS SHARED_INPUTS)
    file(RELATIVE_PATH SHARED_REL_FROM_SRC "${CMAKE_SOURCE_DIR}/src" "${SHARED_INPUT}")
    file(RELATIVE_PATH SHARED_REL_FROM_SHARED "${CMAKE_SOURCE_DIR}/src/shared" "${SHARED_INPUT}")
    sanitize_codegen_token(SHARED_CACHE_TOKEN "${SHARED_REL_FROM_SHARED}")

    set(STRIPPED_SHARED_OUTPUT "${GENERATED_DIR}/${SHARED_REL_FROM_SRC}")
    list(APPEND STRIPPED_SHARED_OUTPUTS "${STRIPPED_SHARED_OUTPUT}")
    set(PROCESS_FILE_STAMP "${STAMP_DIR}/process_${SHARED_CACHE_TOKEN}.stamp")
    list(APPEND PROCESS_FILE_STAMPS "${PROCESS_FILE_STAMP}")

    set(FILE_SHARD_OUTPUTS "")
    foreach(PASS_ID IN LISTS PASS_IDS)
        set(PASS_MAIN_OUTPUTS "${PASS_${PASS_ID}_MAIN_OUTPUTS}")
        if(PASS_MAIN_OUTPUTS)
            foreach(PASS_OUTPUT IN LISTS PASS_MAIN_OUTPUTS)
                cmake_path(GET PASS_OUTPUT PARENT_PATH PASS_OUTPUT_PARENT)
                cmake_path(GET PASS_OUTPUT STEM PASS_OUTPUT_STEM)
                list(APPEND FILE_SHARD_OUTPUTS "${PASS_OUTPUT_PARENT}/${PASS_OUTPUT_STEM}/${PASS_ID}/${SHARED_CACHE_TOKEN}.h")
            endforeach()
        endif()
    endforeach()

    add_custom_command(
        OUTPUT "${PROCESS_FILE_STAMP}"
        BYPRODUCTS
            "${STRIPPED_SHARED_OUTPUT}"
            ${FILE_SHARD_OUTPUTS}
        COMMAND
            "${CMAKE_COMMAND}" -E env
            "PYTHONPYCACHEPREFIX=${CMAKE_BINARY_DIR}/pycache"
            "${Python3_EXECUTABLE}"
            "${CODEGEN_PY}"
            process-file
            --build-root "${PASS_BUILD_DIR}"
            --input "${SHARED_INPUT}"
            --shared-root "${CMAKE_SOURCE_DIR}/src/shared"
            --shared-output-root "${GENERATED_DIR}"
        COMMAND "${CMAKE_COMMAND}" -E touch "${PROCESS_FILE_STAMP}"
        DEPENDS
            "${COMPILE_PASSES_STAMP}"
            "${CODEGEN_PY}"
            "${SHARED_INPUT}"
            ${PREV_PROCESS_DEP}
        WORKING_DIRECTORY "${PROJECT_SOURCE_DIR}"
        COMMENT "Processing ${SHARED_REL_FROM_SHARED}"
        VERBATIM
    )
    set(PREV_PROCESS_DEP "${PROCESS_FILE_STAMP}")
endforeach()

foreach(PASS_ID IN LISTS PASS_IDS)
    sanitize_codegen_token(PASS_TOKEN "${PASS_ID}")
    set(ASSEMBLE_PASS_STAMP "${STAMP_DIR}/assemble_${PASS_TOKEN}.stamp")
    list(APPEND ASSEMBLE_PASS_STAMPS "${ASSEMBLE_PASS_STAMP}")
    set(PASS_OUTPUT_LIST "${PASS_${PASS_ID}_OUTPUTS}")

    add_custom_command(
        OUTPUT "${ASSEMBLE_PASS_STAMP}"
        BYPRODUCTS ${PASS_OUTPUT_LIST}
        COMMAND
            "${CMAKE_COMMAND}" -E env
            "PYTHONPYCACHEPREFIX=${CMAKE_BINARY_DIR}/pycache"
            "${Python3_EXECUTABLE}"
            "${CODEGEN_PY}"
            assemble-pass
            --build-root "${PASS_BUILD_DIR}"
            --pass-id "${PASS_ID}"
            --shared-root "${CMAKE_SOURCE_DIR}/src/shared"
            --output-root "${GENERATED_DIR}"
        COMMAND "${CMAKE_COMMAND}" -E touch "${ASSEMBLE_PASS_STAMP}"
        DEPENDS
            "${COMPILE_PASSES_STAMP}"
            "${CODEGEN_PY}"
            ${PROCESS_FILE_STAMPS}
        WORKING_DIRECTORY "${PROJECT_SOURCE_DIR}"
        COMMENT "Assembling pass ${PASS_ID}"
        VERBATIM
    )
endforeach()

add_custom_command(
    OUTPUT "${COMPILE_PASSES_STAMP}"
    BYPRODUCTS
        ${PASS_DESCRIPTORS}
        ${PUBLIC_OUTPUTS}
    COMMAND
        "${CMAKE_COMMAND}" -E env
        "PYTHONPYCACHEPREFIX=${CMAKE_BINARY_DIR}/pycache"
        "${Python3_EXECUTABLE}"
        "${CODEGEN_PY}"
        compile-passes
        --passes-dir "${CMAKE_SOURCE_DIR}/src/shared"
        --build-root "${PASS_BUILD_DIR}"
        --shared-dir "${CMAKE_SOURCE_DIR}/src/shared"
        --output-root "${GENERATED_DIR}"
        --generated-header-root g
    COMMAND "${CMAKE_COMMAND}" -E touch "${COMPILE_PASSES_STAMP}"
    DEPENDS
        "${CODEGEN_PY}"
        ${SHARED_INPUTS}
    WORKING_DIRECTORY "${PROJECT_SOURCE_DIR}"
    COMMENT "Compiling passes"
    VERBATIM
)

add_custom_command(
    OUTPUT "${CONTENT_STAMP}"
    COMMAND "${CMAKE_COMMAND}" -E touch "${CONTENT_STAMP}"
    DEPENDS
        "${SYNTAX_HINTS_HEADER}"
        "${COMPILE_PASSES_STAMP}"
        ${PROCESS_FILE_STAMPS}
        ${ASSEMBLE_PASS_STAMPS}
    VERBATIM
)

add_custom_target(generate_content DEPENDS
    "${CONTENT_STAMP}"
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
