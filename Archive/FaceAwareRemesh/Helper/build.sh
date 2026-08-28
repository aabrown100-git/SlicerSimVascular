#!/usr/bin/env bash
#
# Build the g3remesh helper that the Face Aware Remesh module calls.
#
# geometry3Sharp is fetched rather than vendored, pinned to one commit on the
# dotnet8 branch. That branch is the maintained one and carries breaking changes
# against the older .NET 4.5 / .NET Standard line, so the pin is deliberate: it is
# the version the module's behaviour was measured against.
#
# Needs the .NET 8 SDK. If `dotnet` is not on PATH, set DOTNET_ROOT to an SDK
# directory and this script will use it.
#
#   ./build.sh                 build in place, for a development checkout
#   ./build.sh --self-contained  publish a standalone binary into Helper/bin,
#                                for a machine with no .NET runtime installed
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GEOMETRY3SHARP_URL="https://github.com/gradientspace/geometry3Sharp.git"
GEOMETRY3SHARP_COMMIT="4664ac3c8880a1d256f3562ac6468a7b3c4ba21a"   # dotnet8, 2026-01-05

if command -v dotnet >/dev/null 2>&1; then
  DOTNET="$(command -v dotnet)"
elif [[ -n "${DOTNET_ROOT:-}" && -x "${DOTNET_ROOT}/dotnet" ]]; then
  DOTNET="${DOTNET_ROOT}/dotnet"
else
  echo "error: no dotnet on PATH and DOTNET_ROOT is unset or has no dotnet." >&2
  echo "Install the .NET 8 SDK from https://dotnet.microsoft.com/download, or:" >&2
  echo "  curl -sSL https://dot.net/v1/dotnet-install.sh | bash -s -- --channel 8.0" >&2
  exit 1
fi

if [[ ! -d "${HERE}/geometry3Sharp/.git" ]]; then
  echo "fetching geometry3Sharp at ${GEOMETRY3SHARP_COMMIT:0:8}"
  rm -rf "${HERE}/geometry3Sharp"
  git clone --quiet "${GEOMETRY3SHARP_URL}" "${HERE}/geometry3Sharp"
fi
git -C "${HERE}/geometry3Sharp" fetch --quiet origin
git -C "${HERE}/geometry3Sharp" checkout --quiet "${GEOMETRY3SHARP_COMMIT}"

if [[ "${1:-}" == "--self-contained" ]]; then
  case "$(uname -s)/$(uname -m)" in
    Darwin/arm64) RUNTIME="osx-arm64" ;;
    Darwin/x86_64) RUNTIME="osx-x64" ;;
    Linux/aarch64) RUNTIME="linux-arm64" ;;
    Linux/x86_64) RUNTIME="linux-x64" ;;
    *) echo "error: unsupported platform $(uname -s)/$(uname -m)" >&2; exit 1 ;;
  esac
  # Note that Slicer's own architecture is what matters here only if the helper is
  # ever loaded in-process. It is not -- it is a subprocess -- so the host
  # architecture is the right choice even when Slicer itself runs translated.
  echo "publishing self-contained for ${RUNTIME}"
  "${DOTNET}" publish "${HERE}/g3remesh/g3remesh.csproj" \
    --configuration Release --runtime "${RUNTIME}" --self-contained true \
    -p:PublishSingleFile=true -p:DebugType=none \
    --output "${HERE}/bin"
  echo "built ${HERE}/bin/g3remesh"
else
  "${DOTNET}" build "${HERE}/g3remesh/g3remesh.csproj" --configuration Release
  echo "built ${HERE}/g3remesh/bin/Release/net8.0/g3remesh.dll"
  echo "the module will run it with ${DOTNET}"
fi
