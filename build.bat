@echo off
setlocal
SET ToolRoot=C:\dev\external
SET NINJA_PATH=%ToolRoot%\msys64\mingw64\bin\ninja.exe
SET PATH=%PATH%;%ToolRoot%\msys64\usr\bin
:: ----------------------
%ToolRoot%\msys64\mingw64\bin\cmake.exe -B build -G "Ninja" -DCMAKE_BUILD_TYPE=Release -DCMAKE_MAKE_PROGRAM="%NINJA_PATH%"
%ToolRoot%\msys64\mingw64\bin\cmake.exe --build build
endlocal
