cmake_minimum_required(VERSION 3.24)
project(BilingualPanel VERSION 0.7.0 LANGUAGES C CXX)

if(NOT WIN32 OR NOT MSVC OR NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
    message(FATAL_ERROR "BilingualPanel requires a 64-bit Windows MSVC toolchain.")
endif()

# Match the DLL to the full-add-on ReShade 6.8.0 runtime (API 20).
# Dear ImGui functions are supplied by ReShade's function table, not imgui.cpp.
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")

add_library(bilingual_sqlite STATIC vendor/sqlite/sqlite3.c)
target_include_directories(bilingual_sqlite PUBLIC vendor/sqlite)
target_compile_definitions(bilingual_sqlite PRIVATE
    SQLITE_THREADSAFE=1 SQLITE_OMIT_LOAD_EXTENSION
)
target_compile_options(bilingual_sqlite PRIVATE /utf-8)

add_library(BilingualPanel SHARED Overlay.cpp WindowInput.hpp RuntimeStatus.hpp shared/Corpus.cpp shared/NoteWriter.cpp)
target_include_directories(BilingualPanel PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}"
    vendor/reshade/include
    vendor/imgui
    shared
)
target_compile_features(BilingualPanel PRIVATE cxx_std_20)
target_compile_options(BilingualPanel PRIVATE /utf-8 /W4 /permissive- /EHsc)
target_compile_definitions(BilingualPanel PRIVATE UNICODE _UNICODE)
target_link_libraries(BilingualPanel PRIVATE bilingual_sqlite user32 imm32)
add_executable(NoteWriterCheck EXCLUDE_FROM_ALL ../tests/native_note_writer.cpp shared/NoteWriter.cpp)
target_include_directories(NoteWriterCheck PRIVATE shared)
target_compile_features(NoteWriterCheck PRIVATE cxx_std_20)
target_compile_options(NoteWriterCheck PRIVATE /utf-8 /W4 /EHsc)
target_link_libraries(NoteWriterCheck PRIVATE bilingual_sqlite)
set_target_properties(BilingualPanel PROPERTIES
    PREFIX ""
    SUFFIX ".addon64"
    CXX_EXTENSIONS OFF
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin/$<CONFIG>"
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin/$<CONFIG>"
    ARCHIVE_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/lib/$<CONFIG>"
)
