#!/bin/sh
set -eu

apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends clang lld
rust_lld=$(find "${RUSTUP_HOME:?}" -path '*/bin/rust-lld' -print -quit)
test -x "$rust_lld"
clang --version | head -n 1
ld.lld --version
"$rust_lld" -flavor darwin --version

printf '%s\r\n' 'int parcel_fee(int x) { if (x < 0) return 0; return x * 3 + 17; }' > /out/original.c
printf '%s\r\n' 'int parcel_fee(int x) { if (x < 0) return 0; return x * 3 + 19; }' > /out/candidate.c

for pair in 'x86_64 x64 x86_64' 'aarch64 arm64 arm64'; do
    set -- $pair
    for variant in original candidate; do
        clang --target="$1-windows-msvc" -O2 -ffreestanding -c "/out/$variant.c" -o /tmp/probe.obj
        lld-link -dll -noentry -machine:"$2" -export:parcel_fee -out:"/out/$variant-$2.dll" /tmp/probe.obj
        clang --target="$3-apple-macos11" -O2 -ffreestanding -c "/out/$variant.c" -o /tmp/probe.o
        "$rust_lld" -flavor darwin -dylib -arch "$3" -platform_version macos 11.0 11.0 \
            -o "/out/$variant-$2.dylib" /tmp/probe.o
    done
done

for variant in original candidate; do
    clang --target=aarch64-linux-gnu -O2 -ffreestanding -fPIC -c "/out/$variant.c" -o /tmp/probe.o
    ld.lld -shared -o "/out/$variant-arm64.so" /tmp/probe.o
done
